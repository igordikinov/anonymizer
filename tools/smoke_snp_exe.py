"""Exercise the separate SNP v1 profile in the portable EXE, offline."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile


def fixture_module():
    # This is a developer-side fixture, never bundled into the application.
    path = Path(__file__).resolve().parents[1] / "tests/snp_connectivity_fixture.py"
    spec = importlib.util.spec_from_file_location("snp_smoke_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(exe, *, report_path=None):
    from openpyxl import load_workbook

    exe = Path(exe).resolve()
    fixture = fixture_module()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    results = []
    with tempfile.TemporaryDirectory(prefix="snp-profile-exe-") as temporary:
        root = Path(temporary)
        for kind in ("csv", "xlsx"):
            source = fixture.write_dataset(root / f"source-{kind}", kind=kind, optional=True,
                                           date_style=("res", "res") if kind == "xlsx" else None)
            dumps = []
            for index in range(2):
                output = root / f"output-{kind}-{index}"
                private_map = root / f"private-{kind}-{index}.xlsx"
                subprocess.run([str(exe), "--input", str(source), "--output", str(output),
                                "--project", str(root / f"project-{kind}.snpanon"), "--bypass-model",
                                "--connectivity-profile", "builtin:snp-v1", "--require-integrity",
                                "--delimiter", ";", "--mapping-xlsx", str(private_map)],
                               cwd=root, env=env, timeout=90, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                assert report["connectivity_profile_id"] == "snp-v1" and not report["model_applied"]
                assert report["integrity_status"] == "PASSED" and report["dataset_published"]
                assert report["publication_mode"] == "VERIFIED_INTEGRITY"
                assert report["relationships_checked"] and report["keys_checked"]
                assert report["tables"] == 16 and report["rows"] == sum(map(len, fixture.dataset().values()))
                assert len(report["coverage"]["required"]) == 66
                assert report["coverage"]["checked"] == report["coverage"]["declared"] == 72
                assert all(row["status"] == "PASS" for row in report["checks"])
                fixture.assert_business_links(output)
                for name in ("dump.sql", "inserts.sql"):
                    text = (output / name).read_text(encoding="utf-8")
                    assert fixture.EXACT in text and fixture.ITEM_A not in text
                    assert "SYN_PRIVATE" not in text and "SYN_IGNORED_ID" not in text and "SYN_AUTHOR" not in text
                assert not list(output.rglob("*.snpanon")) and not list(output.rglob("*.xlsx"))
                book = load_workbook(private_map, read_only=True, data_only=False)
                try:
                    rows = list(book.active.values)
                    assert any(old == fixture.ITEM_A for _, old, _ in rows[1:])
                    assert all(not str(row[0]).startswith("connectivity:") for row in rows[1:])
                finally:
                    book.close()
                applied = (output / "connectivity-profile.json").read_bytes()
                assert hashlib.sha256(applied).hexdigest() == report["connectivity_profile_hash"]
                dumps.append((output / "dump.sql").read_bytes())
            assert dumps[0] == dumps[1]
            results.append({"kind": kind, "tables": 16, "rows": report["rows"], "required_checks": 66,
                            "declared_checks": 72, "status": "PASS", "repeat_identical": True})
    result = {"verdict": "PASS", "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "python_on_child_path": False, "checks": results}
    if report_path is not None:
        Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("SNP EXE smoke: all16 CSV/XLSX, symbolic/numeric codes,66required/72checked, exact measures/dates, private map and repeat PASS; no Python on child PATH")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    check(args.exe, report_path=args.report)

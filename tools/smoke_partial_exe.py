"""Check partial SNP coverage in the portable executable using synthetic inputs."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile


MISSING_TABLES = {"item", "loc", "mot", "demand_stream", "sp_demand_profile"}
MISSING_COLUMNS = {("prod_cap_req_sp", "date"), ("sp_demand", "demandtype")}


def fixture_module():
    path = Path(__file__).resolve().parents[1] / "tests/snp_connectivity_fixture.py"
    spec = importlib.util.spec_from_file_location("partial_smoke_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def partial_dataset(fixture):
    rows = fixture.dataset(optional=True)
    for name in MISSING_TABLES:
        del rows[name]
    for row in rows["prod_cap_req_sp"]:
        row["datefr"] = date.fromisoformat(row.pop("date"))
    for row in rows["sp_demand"]:
        del row["demandtype"]
    return rows


def check(exe, *, report_path=None):
    from openpyxl import load_workbook

    exe = Path(exe).resolve()
    fixture = fixture_module()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    results = []
    with tempfile.TemporaryDirectory(prefix="partial-snp-exe-") as temporary:
        root = Path(temporary)
        for kind in ("csv", "xlsx"):
            inputs = partial_dataset(fixture)
            source = fixture.write_dataset(root / f"source-{kind}", kind=kind, rows=inputs)
            dumps = []
            for index in range(2):
                output = root / f"output-{kind}-{index}"
                private_map = root / f"private-{kind}-{index}.xlsx"
                log_path = root / f"run-{kind}-{index}.csv"
                subprocess.run([str(exe), "--input", str(source), "--output", str(output),
                                "--project", str(root / f"project-{kind}.snpanon"), "--bypass-model",
                                "--connectivity-profile", "builtin:snp-v1", "--require-integrity",
                                "--delimiter", ";", "--mapping-xlsx", str(private_map), "--log-csv", str(log_path)],
                               cwd=root, env=env, timeout=90, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                assert report["connectivity_profile_id"] == "snp-v1" and not report["model_applied"]
                assert report["integrity_status"] == "NOT_CHECKED" and report["dataset_published"]
                assert report["publication_mode"] == "UNVERIFIED_INTEGRITY" and report["integrity_message"]
                assert report["tables"] == 11 and report["rows"] == sum(map(len, inputs.values()))
                assert len(report["coverage"]["required"]) == 66
                assert all(row["status"] != "FAIL" for row in report["checks"])
                assert {row["id"] for row in report["checks"] if row["kind"] == "export" and row["status"] == "PASS"} == {
                    "export:csv", "export:dump", "export:inserts"}
                missing = [obj for row in report["checks"] if row.get("required") for obj in row.get("missing", [])]
                assert {obj["table"] for obj in missing if obj["reason"] == "TABLE_MISSING"} == MISSING_TABLES
                assert {(obj["table"], col) for obj in missing if obj["reason"] == "COLUMN_MISSING" for col in obj["columns"]} == MISSING_COLUMNS
                tables, _ = fixture.output_rows(output)
                rows = {name: values for (_, name), values in tables.items()}
                assert rows["comp_req_sp"][0]["item"] == rows["sku_attributes"][0]["item"] == rows["sp_demand"][0]["item"]
                assert rows["prodsource_sp"][0]["prodsource"] == rows["comp_req_sp"][0]["prodsource"] == rows["prod_cap_req_sp"][0]["prodsource"]
                assert rows["res"][0]["res"] == rows["res_attributes"][0]["res"] == rows["prod_cap_req_sp"][0]["res"]
                assert rows["tlane_mot"][0]["mot"] == rows["tlane_prod_mot"][0]["mot"]
                assert rows["sp_demand"][0]["quantity"] == fixture.EXACT
                assert all(row["datefr"] == fixture.DATE_A and "date" not in row for row in rows["prod_cap_req_sp"])
                for name in ("dump.sql", "inserts.sql"):
                    text = (output / name).read_text(encoding="utf-8")
                    assert fixture.EXACT in text and fixture.ITEM_A not in text
                    assert "SYN_PRIVATE" not in text and "SYN_IGNORED_ID" not in text and "SYN_AUTHOR" not in text
                assert not list(output.rglob("*.snpanon")) and not list(output.rglob("*.xlsx"))
                with log_path.open(encoding="utf-8-sig", newline="") as stream:
                    logs = list(csv.reader(stream, delimiter=";"))
                assert all(len(row) == 6 for row in logs)
                assert any(row[3] == "integrity_check" and "prod_cap_req_sp" in row[4] and "date" in row[5] for row in logs[1:])
                public_report = json.dumps(report, ensure_ascii=False)
                assert fixture.ITEM_A not in public_report and "SYN_PRIVATE" not in public_report and str(root) not in public_report
                book = load_workbook(private_map, read_only=True, data_only=False)
                try:
                    assert any(old == fixture.ITEM_A for _, old, _ in list(book.active.values)[1:])
                finally:
                    book.close()
                applied = (output / "connectivity-profile.json").read_bytes()
                assert hashlib.sha256(applied).hexdigest() == report["connectivity_profile_hash"]
                dumps.append((output / "dump.sql").read_bytes())
            assert dumps[0] == dumps[1]
            results.append({"kind": kind, "tables": 11, "rows": report["rows"], "required_checks": 66,
                            "status": "PASS", "integrity_status": "NOT_CHECKED", "repeat_identical": True})
    result = {"verdict": "PASS", "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "python_on_child_path": False, "checks": results}
    if report_path is not None:
        Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Partial SNP EXE smoke: 11-table CSV/XLSX, shared mappings, exact numbers/dates, honest missing coverage, private map and repeat PASS; no Python on child PATH")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    check(args.exe, report_path=args.report)

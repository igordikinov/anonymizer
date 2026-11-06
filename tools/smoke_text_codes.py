"""Portable EXE regression for symbolic comp/item/loc mixed with digit codes."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from openpyxl import Workbook, load_workbook


def check(exe):
    exe = exe.resolve()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    with tempfile.TemporaryDirectory(prefix="snp-text-codes-exe-") as temporary:
        root = Path(temporary)
        subprocess.run([str(exe), "--smoke-gui", "--smoke-gui-report", str(root / "gui.json")],
                       cwd=root, env=env, check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        gui = json.loads((root / "gui.json").read_text(encoding="utf-8"))
        assert gui["fonts"]["resolved"]["bundled"]
        assert gui["processing_progress"]["queue_drained"] and gui["processing_progress"]["poll_scheduled"]
        for kind in ("csv", "xlsx"):
            source = root / kind
            source.mkdir()
            rows = [["comp", "item", "loc", "quantity"],
                    ["SYN_CODE_2.9KG"] * 3 + ["-2476,700000000000000001"],
                    [7300019] * 3 + ["12,50"]]
            if kind == "xlsx":
                book = Workbook()
                for row in rows:
                    book.active.append(row)
                book.save(source / "custom.xlsx")
                book.close()
            else:
                with (source / "custom.csv").open("w", encoding="utf-8", newline="") as stream:
                    csv.writer(stream, delimiter=";").writerows(rows)
            dumps = []
            for repeat in range(2):
                output = root / f"out-{kind}-{repeat}"
                private_map = root / f"map-{kind}-{repeat}.xlsx"
                subprocess.run([str(exe), "--input", str(source), "--output", str(output),
                                "--project", str(root / f"{kind}.snpanon"), "--bypass-model",
                                "--mapping-xlsx", str(private_map)], cwd=root, env=env, check=True,
                               timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
                manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
                entry, = manifest["tables"]
                assert [f["sql_type"] for f in entry["fields"]] == ["text", "text", "text", "numeric"]
                with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
                    actual = list(csv.DictReader(stream))
                assert [r["quantity"] for r in actual] == ["-2476.700000000000000001", "12.50"]
                for column in ("comp", "item", "loc"):
                    assert [r[column] for r in actual] == [f"{column}_000001", f"{column}_000002"]
                for filename in ("dump.sql", "inserts.sql"):
                    text = (output / filename).read_text(encoding="utf-8")
                    assert "7300019" not in text and "SYN_CODE" not in text
                    assert "-2476.700000000000000001" in text
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                assert report["dataset_published"] and report["validation"]["preserved_fields_equal"]
                book = load_workbook(private_map, read_only=True, data_only=False)
                try:
                    mappings = list(book.active.values)[1:]
                    for column in ("comp", "item", "loc"):
                        assert any(old == "7300019" and new == f"{column}_000002"
                                   for _, old, new in mappings)
                finally:
                    book.close()
                dumps.append(hashlib.sha256((output / "dump.sql").read_bytes()).hexdigest())
            assert dumps[0] == dumps[1]
    print("EXE text codes: CSV/XLSX mixed symbolic/native numeric comp/item/loc, precise decimal comma, private map and repeat PASS; no Python on child PATH")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    check(parser.parse_args().exe)

"""Run the portable EXE from an empty directory without Python on its PATH."""
import argparse
import json
import os
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def check(exe):
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    with tempfile.TemporaryDirectory(prefix="snp-anon-exe-smoke-") as name:
        root = Path(name)
        kwargs = {"cwd": root, "env": env, "timeout": 60, "creationflags": subprocess.CREATE_NO_WINDOW}
        subprocess.run([str(exe), "--smoke-gui", "--smoke-gui-report", str(root / "gui.json")], check=True, **kwargs)
        gui = json.loads((root / "gui.json").read_text(encoding="utf-8"))
        assert gui["fonts"]["resolved"]["bundled"], "EXE used a system fallback instead of its bundled fonts"
        assert gui["fonts"]["actual"]["body"]["family"].casefold() == "open sans"
        assert gui["fonts"]["actual"]["heading"]["family"].casefold() == "open sans semibold"
        assert gui["fonts"]["actual"]["button"]["family"].casefold() == "open sans semibold"
        assert all(font["family"].casefold() == "open sans" for font in gui["fonts"]["widgets"].values())
        expected_height = round(40 * gui["scale"])
        assert all(height == expected_height for height in gui["heights"].values()), gui["heights"]
        assert all(height == expected_height for height in gui["actual_heights"].values()), gui["actual_heights"]
        assert gui["radii"] == {"button": 4, "input": 5, "card": 8}
        assert gui["states"]["Primary.TButton"]["disabled active pressed"] == "#f5f6f8"
        assert gui["states"]["Primary.TButton"]["pressed active"] == "#4f0096"
        source = root / "source.sql"
        source.write_text("CREATE TABLE public.item(sys_id bigint, item text, proddescr text, volume numeric); INSERT INTO public.item VALUES (789,'PRIVATE_CODE','PRIVATE_DESCRIPTION',12345.678901);", encoding="utf-8")
        subprocess.run([str(exe), "--input", str(source), "--output", str(root / "out"), "--project", str(root / "client.snpanon"), "--industry", "chem"], check=True, **kwargs)
        report = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
        assert report["rows"] == 1 and report["validation"]["preserved_fields_equal"]
        assert "PRIVATE_" not in (root / "out/dump.sql").read_text(encoding="utf-8")
        assert "12345.678901" in (root / "out/dump.sql").read_text(encoding="utf-8")
        review_source = root / "review.sql"
        review_source.write_text("""
            CREATE TABLE public.alpha(qty numeric);
            CREATE TABLE public.beta(qty numeric, note text, UNIQUE NULLS NOT DISTINCT (qty));
            INSERT INTO public.alpha VALUES (11);
            INSERT INTO public.beta VALUES (22, '=1+1');
            INSERT INTO public.alpha VALUES (11, 99);
            CREATE TABLE public.gamma(qty numeric);
            INSERT INTO public.gamma VALUES (33);
        """, encoding="utf-8")
        subprocess.run([str(exe), "--input", str(review_source), "--output", str(root / "review-out"),
                        "--project", str(root / "review.snpanon"), "--skip-error-tables",
                        "--mapping-xlsx", str(root / "private-map.xlsx")], check=True, **kwargs)
        review = json.loads((root / "review-out/report.json").read_text(encoding="utf-8"))
        assert review["rows"] == 2 and review["tables"] == 2
        assert [t["table"] for t in review["skipped_tables"]] == ["public.alpha"]
        manifest = json.loads((root / "review-out/csv/manifest.json").read_text(encoding="utf-8"))
        import csv
        for entry in manifest["tables"]:
            with (root / "review-out/csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            assert [r["qty"] for r in rows] == [{"beta": "22", "gamma": "33"}[entry["table"]]]
        with zipfile.ZipFile(root / "private-map.xlsx") as book:
            worksheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
            assert not list(worksheet.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f"))
            assert "=1+1" in "".join(worksheet.itertext())
        csv_source = root / "csv-source"
        csv_source.mkdir()
        (csv_source / "sku.csv").write_text(
            "prod_prof;item;fixhor;loc;prof_descr;fixlotsize\n"
            'SYN_UNKNOWN_A;SYN_I1;;SYN_L;SYN_UNKNOWN_B;""\n'
            "SYN_UNKNOWN_A;SYN_I2;0;SYN_L;SYN_UNKNOWN_B;1234567890.123456\n", encoding="utf-8")
        subprocess.run([str(exe), "--input", str(csv_source), "--output", str(root / "csv-out"),
                        "--project", str(root / "csv.snpanon")], check=True, **kwargs)
        csv_report = json.loads((root / "csv-out/report.json").read_text(encoding="utf-8"))
        assert csv_report["rows"] == 2 and csv_report["tables"] == 1 and not csv_report["skipped_tables"]
        assert csv_report["validation"]["preserved_fields_equal"]
        csv_manifest = json.loads((root / "csv-out/csv/manifest.json").read_text(encoding="utf-8"))
        entry, = csv_manifest["tables"]
        with (root / "csv-out/csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        assert [(r["fixhor"], r["fixlotsize"]) for r in csv_rows] == [(r"\N", r"\N"), ("0", "1234567890.123456")]
        assert "prod_prof" not in csv_rows[0] and "prof_descr" not in csv_rows[0]
        for filename in ("dump.sql", "inserts.sql"):
            sql_text = (root / "csv-out" / filename).read_text(encoding="utf-8")
            assert "1234567890.123456" in sql_text and "SYN_UNKNOWN_" not in sql_text
        print("EXE smoke: bundled Open Sans, DS, pipeline, skip isolation, UNIQUE NULLS, text-only XLSX and nullable CSV numbers passed; no Python on child PATH")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    args = parser.parse_args()
    check(args.exe.resolve())

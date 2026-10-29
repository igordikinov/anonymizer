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
        faces = gui["fonts"]["selected_faces"]
        assert {role: row["weight"] for role, row in faces.items()} == {"body": 400, "semibold": 600, "bold": 700}
        assert all(row["matches_bundled"] for row in faces.values()), "GDI selected a system font instead of a bundled static face"
        assert gui["fonts"]["actual"]["body"]["family"].casefold() == "inplan desktop manrope"
        assert gui["fonts"]["actual"]["heading"]["family"].casefold() == "inplan desktop manrope semibold"
        assert gui["fonts"]["actual"]["button"]["family"].casefold() == "inplan desktop manrope semibold"
        assert all(font == gui["fonts"]["actual"]["field"] for font in gui["fonts"]["widgets"].values())
        assert gui["fonts"]["popdowns"] and all(
            font == gui["fonts"]["actual"]["field"] for font in gui["fonts"]["popdowns"].values())
        assert gui["fonts"]["actual"]["field"]["family"].casefold() == "inplan desktop manrope"
        for role, pixels in [("field", 12), ("body", 14), ("button", 14), ("heading", 16), ("caption", 11)]:
            assert gui["fonts"]["configured_pixels"][role] == round(pixels * gui["scale"])
        expected_height = round(40 * gui["scale"])
        assert all(height == expected_height for height in gui["heights"].values()), gui["heights"]
        assert all(height == expected_height for height in gui["actual_heights"].values()), gui["actual_heights"]
        assert gui["radii"] == {"button": 4, "input": 5, "card": 8}
        assert gui["states"]["Primary.TButton"]["disabled active pressed"] == "#f4f6fa"
        assert gui["states"]["Primary.TButton"]["pressed active"] == "#3350c4"
        source = root / "source.sql"
        source.write_text("CREATE TABLE public.item(sys_id bigint, item text, proddescr text, volume numeric, update_date_time timestamp, change_author uuid); INSERT INTO public.item VALUES ('SYN_BAD_ID','PRIVATE_CODE','PRIVATE_DESCRIPTION',12345.678901,'SYN_RAW_TIME','SYN_BAD_AUTHOR');", encoding="utf-8")
        subprocess.run([str(exe), "--input", str(source), "--output", str(root / "out"), "--project", str(root / "client.snpanon"), "--industry", "chem"], check=True, **kwargs)
        report = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
        assert report["rows"] == 1 and report["validation"]["preserved_fields_equal"]
        assert "PRIVATE_" not in (root / "out/dump.sql").read_text(encoding="utf-8")
        assert "12345.678901" in (root / "out/dump.sql").read_text(encoding="utf-8")
        manifest = json.loads((root / "out/csv/manifest.json").read_text(encoding="utf-8"))
        import csv
        with (root / "out/csv" / manifest["tables"][0]["file"]).open(encoding="utf-8", newline="") as stream:
            row, = list(csv.DictReader(stream))
        assert row["sys_id"] == r"\N" and row["change_author"] == "snp" and row["update_date_time"] == "SYN_RAW_TIME"
        assert "SYN_BAD_ID" not in (root / "out/dump.sql").read_text(encoding="utf-8")
        assert "SYN_BAD_AUTHOR" not in (root / "out/dump.sql").read_text(encoding="utf-8")
        assert '"sys_id"' not in (root / "out/inserts.sql").read_text(encoding="utf-8")
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
        # Fixtures are generated by the host development Python; every import
        # command below runs the frozen executable with Python removed from PATH.
        from openpyxl import Workbook
        def write_book(path, rows):
            book = Workbook()
            for row in rows:
                book.active.append(row)
            book.save(path)
            book.close()

        xlsx_source = root / "xlsx-source"
        xlsx_source.mkdir()
        item_book = xlsx_source / "item.xlsx"
        write_book(item_book, [["item", "proddescr", "volume"], ["SYN_XI", "SYN_XPRIVATE", 1.25]])
        with zipfile.ZipFile(item_book) as book:
            parts = {entry.filename: book.read(entry) for entry in book.infolist()}
        parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
            b"<v>1.25</v>", b"<v>1234567890.123456789</v>")
        with zipfile.ZipFile(item_book, "w", zipfile.ZIP_DEFLATED) as book:
            for part, content in parts.items():
                book.writestr(part, content)
        (xlsx_source / "loc.csv").write_text("loc,locdescr\nSYN_XL,SYN_XPRIVATE_LOCATION\n", encoding="utf-8")
        write_book(xlsx_source / "sku.xlsx", [["item", "loc", "fixhor"], ["SYN_XI", "SYN_XL", None]])
        zipped = root / "xlsx-dataset.zip"
        with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in xlsx_source.iterdir():
                archive.write(file, "data/" + file.name)
        for index, source in enumerate((item_book, xlsx_source, zipped)):
            output = root / f"xlsx-out-{index}"
            subprocess.run([str(exe), "--input", str(source), "--output", str(output),
                            "--project", str(root / "xlsx.snpanon")], check=True, **kwargs)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            assert report["rows"] == (1 if index == 0 else 3)
            assert report["validation"]["preserved_fields_equal"]
            assert not list(output.rglob("*.xlsx"))
            for filename in ("dump.sql", "inserts.sql"):
                text = (output / filename).read_text(encoding="utf-8")
                assert "1234567890.123456789" in text and "SYN_XPRIVATE" not in text
        # A late formula must remove all earlier rows from this workbook.
        write_book(item_book, [["item", "proddescr"], ["SYN_XI", "SYN_XPRIVATE"], ["SYN_XI2", "=1+1"]])
        formula_out = root / "xlsx-formula-out"
        failed = subprocess.run([str(exe), "--input", str(item_book), "--output", str(formula_out),
                                 "--project", str(root / "formula.snpanon")], **kwargs)
        assert failed.returncode == 1 and not formula_out.exists()
        subprocess.run([str(exe), "--input", str(xlsx_source), "--output", str(root / "xlsx-skip-out"),
                        "--project", str(root / "xlsx-skip.snpanon"), "--skip-error-tables"], check=True, **kwargs)
        skipped = json.loads((root / "xlsx-skip-out/report.json").read_text(encoding="utf-8"))
        assert skipped["rows"] == 2 and skipped["tables"] == 2
        assert [table["table"] for table in skipped["skipped_tables"]] == ["public.item"]
        bypass_source = root / "bypass-source"
        bypass_source.mkdir()
        (bypass_source / "sku.csv").write_text(
            "note,order_id,qty,day,uom,abc,activeflag,sys_id,update_date_time,change_author\n"
            "SYN_GENERIC_PRIVATE,999,1234567890.123456789,2026-10-29,kg,A,Y,SYN_BAD_ID,SYN_RAW_TIME,SYN_BAD_AUTHOR\n", encoding="utf-8")
        (bypass_source / "optimizer_production.csv").write_text(
            "product,resource,location,bomnum,qty,day\n"
            "SYN_SHARED,SYN_SHARED,SYN_SHARED,SYN_SHARED,1234567890.123456789,2026-10-29\n", encoding="utf-8")
        from datetime import date
        book = Workbook(iso_dates=True)
        book.active.append(["code", "qty", "day"])
        book.active.append(["123", 42.125, date(2026, 10, 29)])
        book.save(bypass_source / "custom.xlsx")
        book.close()
        for index in (1, 2):
            output = root / f"bypass-out-{index}"
            subprocess.run([str(exe), "--input", str(bypass_source), "--output", str(output),
                            "--project", str(root / "bypass.snpanon"), "--bypass-model"], check=True, **kwargs)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            assert report["tables"] == report["rows"] == 3
            assert not report["model_applied"] and not report["relationships_checked"] and not report["keys_checked"]
            assert report["validation"]["preserved_fields_equal"]
            manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["tables"]:
                with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
                    row, = list(csv.DictReader(stream))
                assert row["day"] == "2026-10-29"
                if "sys_id" in row:
                    assert row["sys_id"] == r"\N"
                if entry["table"] == "sku":
                    assert row["change_author"] == "snp" and row["update_date_time"] == "SYN_RAW_TIME"
                    assert row["order_id"] == "999" and row["qty"] == "1234567890.123456789"
                    assert all(row[column] == f"{column}_000001" for column in ("note", "uom", "abc", "activeflag"))
                elif entry["table"] == "custom":
                    assert row["code"] == "code_000001" and row["qty"] == "42.125"
                else:
                    assert entry["table"] == "optimizer_production"
                    assert all(row[column] == f"{column}_000001" for column in ("product", "resource", "location", "bomnum"))
                    assert row["qty"] == "1234567890.123456789"
        assert (root / "bypass-out-1/dump.sql").read_bytes() == (root / "bypass-out-2/dump.sql").read_bytes()
        print("EXE smoke: bundled Manrope with 12px fields, brand DS, SQL/CSV/XLSX file/folder/ZIP, exact numbers, formula/skip and full model bypass passed; no Python on child PATH")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    args = parser.parse_args()
    check(args.exe.resolve())

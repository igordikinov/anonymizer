"""Run the portable EXE from an empty directory without Python on its PATH."""
import argparse
import hashlib
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
        progress = gui["processing_progress"]
        assert set(progress["checked_stages"]) == {"prevalidate", "connectivity", "verify_exports"}
        assert all(progress[key] for key in ("unknown_stage_keeps_value", "queue_drained", "poll_scheduled"))
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
        source.write_text("CREATE TABLE public.item(sys_id bigint, item text, proddescr text, volume numeric, update_date_time timestamp, change_author uuid, cost numeric); INSERT INTO public.item VALUES ('SYN_BAD_ID','PRIVATE_CODE','PRIVATE_DESCRIPTION',12345.678901,'SYN_RAW_TIME','SYN_BAD_AUTHOR',NULL);", encoding="utf-8")
        subprocess.run([str(exe), "--input", str(source), "--output", str(root / "out"), "--project", str(root / "client.snpanon"), "--industry", "chem"], check=True, **kwargs)
        report = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
        assert report["rows"] == 1 and report["validation"]["preserved_fields_equal"]
        assert "PRIVATE_" not in (root / "out/dump.sql").read_text(encoding="utf-8")
        assert "12345.678901" in (root / "out/dump.sql").read_text(encoding="utf-8")
        manifest = json.loads((root / "out/csv/manifest.json").read_text(encoding="utf-8"))
        import csv
        with (root / "out/csv" / manifest["tables"][0]["file"]).open(encoding="utf-8", newline="") as stream:
            row, = list(csv.DictReader(stream))
        assert row["sys_id"] == "" and row["change_author"] == "snp" and row["update_date_time"] == "SYN_RAW_TIME"
        assert row["cost"] == "" and {"cost", "sys_id"}.issubset(manifest["tables"][0]["empty_null_columns"])
        assert row["proddescr"] == "Interior paint - series 0001" and row["item"].isascii()
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
        assert [t["table"] for t in review["skipped_tables"]] == [review["fields"][0]["schema"] + ".alpha"]
        manifest = json.loads((root / "review-out/csv/manifest.json").read_text(encoding="utf-8"))
        import csv
        for entry in manifest["tables"]:
            with (root / "review-out/csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            assert [r["qty"] for r in rows] == [{"beta": "22", "gamma": "33"}[entry["table"]]]
            if entry["table"] == "beta":
                assert rows[0]["note"] == "Attribute 0001"
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
        assert [(r["fixhor"], r["fixlotsize"]) for r in csv_rows] == [("", ""), ("0", "1234567890.123456")]
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
        assert [table["table"] for table in skipped["skipped_tables"]] == [skipped["fields"][0]["schema"] + ".item"]
        bypass_source = root / "bypass-source"
        bypass_source.mkdir()
        (bypass_source / "sku.csv").write_text(
            "note,order_id,qty,day,uom,abc,activeflag,sys_id,update_date_time,change_author\n"
            "SYN_GENERIC_PRIVATE,999,1234567890.123456789,2026-10-29,kg,A,Y,SYN_BAD_ID,SYN_RAW_TIME,SYN_BAD_AUTHOR\n", encoding="utf-8")
        (bypass_source / "optimizer_production.csv").write_text(
            "product,resource,location,bomnum,qty,day,cost\n"
            "SYN_SHARED,SYN_SHARED,SYN_SHARED,SYN_SHARED,1234567890.123456789,2026-10-29,\\N\n", encoding="utf-8")
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
                    assert row["sys_id"] == ""
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
                    assert row["cost"] == "" and "cost" in entry["empty_null_columns"]
        assert (root / "bypass-out-1/dump.sql").read_bytes() == (root / "bypass-out-2/dump.sql").read_bytes()
        subprocess.run([str(exe), "--input", str(root / "bypass-out-1/csv"), "--output", str(root / "csv-roundtrip"),
                        "--project", str(root / "roundtrip.snpanon"), "--bypass-model"], check=True, **kwargs)
        roundtrip = json.loads((root / "csv-roundtrip/csv/manifest.json").read_text(encoding="utf-8"))
        optimizer, = [entry for entry in roundtrip["tables"] if entry["table"] == "optimizer_production"]
        with (root / "csv-roundtrip/csv" / optimizer["file"]).open(encoding="utf-8", newline="") as stream:
            row, = list(csv.DictReader(stream))
        assert row["cost"] == "" and row["qty"] == "1234567890.123456789"
        assert "cost" in optimizer["empty_null_columns"]
        connected = root / "connected-source"
        connected.mkdir()
        fixture = {
            "periods": [["datasetid", "period"], [1, 0]],
            "demand": [["datasetid", "period", "location", "product", "client", "quantity"],
                       [1, 0, "SYN_B", "SYN_P", "SYN_A", "10.000001"]],
            "procurement": [["datasetid", "period", "location", "product", "supplier"],
                            [1, 0, "SYN_B", "SYN_P", "SYN_A"]],
            "production": [["datasetid", "period", "location", "product", "bomnum", "resource", "extra"],
                           [1, 0, "SYN_A", "SYN_P", "SYN_BOM", "SYN_R", "SYN_PRIVATE_EXTRA"]],
            "storage": [["datasetid", "period", "location", "product", "resource"],
                        [1, 0, "SYN_A", "SYN_P", "SYN_R"]],
            "bom": [["datasetid", "period", "location", "product", "bomnum", "input_output"],
                    [1, 0, "SYN_A", "SYN_P", "SYN_BOM", -1]],
            "transportation": [["datasetid", "period", "loc_from", "loc_to", "product", "transport_type", "resource"],
                               [1, 0, "SYN_A", "SYN_T", "SYN_P", "SYN_A", "SYN_R"],
                               [1, 0, "SYN_T", "SYN_B", "SYN_P", "SYN_A", "SYN_R"],
                               [1, 0, "SYN_A", "SYN_A", "SYN_P", "SYN_A", "SYN_R"],
                               [1, 0, "SYN_B", "SYN_A", "SYN_P", "SYN_A", "SYN_R"]],
        }
        for table, rows in fixture.items():
            with (connected / f"optimizer_{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream).writerows(rows)
        connected_outputs = []
        for index in (1, 2):
            output = root / f"connected-out-{index}"
            subprocess.run([str(exe), "--input", str(connected), "--output", str(output),
                            "--project", str(root / "connected.snpanon"), "--bypass-model",
                            "--connectivity-profile", "builtin:optimizer-v1", "--require-integrity"], check=True, **kwargs)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            assert report["format"] == "snp-anonymizer-report-v2" and not report["model_applied"]
            assert report["integrity_status"] == "PASSED" and report["publication_mode"] == "VERIFIED_INTEGRITY"
            assert report["relationships_checked"] and report["keys_checked"] and report["dataset_published"]
            profile_bytes = (output / "connectivity-profile.json").read_bytes()
            assert hashlib.sha256(profile_bytes).hexdigest() == report["connectivity_profile_hash"]
            manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
            rows_by_table = {}
            for entry in manifest["tables"]:
                with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
                    rows_by_table[entry["table"].removeprefix("optimizer_")] = list(csv.DictReader(stream))
            routes = rows_by_table["transportation"]
            location_a = rows_by_table["production"][0]["location"]
            assert location_a == rows_by_table["procurement"][0]["supplier"] == routes[0]["loc_from"]
            assert routes[0]["loc_to"] == routes[1]["loc_from"]  # transit has no demand row
            assert routes[1]["loc_to"] == rows_by_table["demand"][0]["location"]
            assert routes[2]["loc_from"] == routes[2]["loc_to"] == location_a
            assert rows_by_table["demand"][0]["client"] != location_a
            assert routes[0]["transport_type"] not in {location_a, rows_by_table["demand"][0]["client"]}
            assert rows_by_table["production"][0]["bomnum"] == rows_by_table["bom"][0]["bomnum"]
            assert rows_by_table["storage"][0]["resource"] == routes[0]["resource"]
            assert rows_by_table["demand"][0]["quantity"] == "10.000001"
            assert rows_by_table["production"][0]["extra"] != "SYN_PRIVATE_EXTRA"
            connected_outputs.append(output)
        assert (connected_outputs[0] / "dump.sql").read_bytes() == (connected_outputs[1] / "dump.sql").read_bytes()
        refusal = root / "required-without-profile"
        failed = subprocess.run([str(exe), "--input", str(connected), "--output", str(refusal),
                                 "--project", str(root / "unchecked.snpanon"), "--bypass-model", "--require-integrity"], **kwargs)
        assert failed.returncode == 1 and [p.name for p in refusal.iterdir()] == ["report.json"]
        diagnostic = json.loads((refusal / "report.json").read_text(encoding="utf-8"))
        assert diagnostic["integrity_status"] == "NOT_CHECKED" and diagnostic["dataset_published"] is False
        assert diagnostic["publication_mode"] is None
        print("EXE smoke: bundled Manrope, brand DS, SQL/CSV/XLSX, exact numbers/NULLs, legacy bypass, connectivity profile/common domains/transit/repeat, diagnostic-only refusal passed; no Python on child PATH")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    args = parser.parse_args()
    check(args.exe.resolve())

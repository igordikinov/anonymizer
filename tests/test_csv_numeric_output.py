"""Readable numeric CSV NULLs with lossless manifest and legacy decoding."""
import csv
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store


def physical(output):
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "snp-anonymizer-csv-v2"
    entry, = manifest["tables"]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return entry, rows


def logical(source, db_path, bypass, *, is_csv=False, fields=None):
    with Store(db_path, Model(), bypass_model=bypass) as store:
        if fields:
            store.define(("snp_a000001", "custom"), fields)
        (parse_csv if is_csv else parse_sql)(source, store)
        table, = store.tables.values()
        return [dict(zip((f["name"] for f in table["fields"]), row)) for row in store.rows(table["id"])]


@pytest.mark.parametrize("bypass", [False, True])
def test_numeric_csv_null_is_empty_and_three_exports_still_agree(tmp_path, bypass):
    source = tmp_path / "source.sql"
    source.write_text(r"""
        CREATE TABLE public.custom(sys_id bigint, cost numeric, quantity numeric NOT NULL,
            counter bigint, note text, update_date_time numeric);
        INSERT INTO public.custom VALUES
            (99,NULL,12345678901234567890.123456789,NULL,NULL,NULL),
            (99,0,-0,0,'',''),
            (99,-0,1e-19,42,'SYN_NOTE',E'\\N');
        """, encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=bypass))
    entry, rows = physical(output)
    assert entry["empty_null_columns"] == ["sys_id", "cost", "quantity", "counter"]
    assert [row["cost"] for row in rows] == ["", "0", "-0"]
    assert [row["quantity"] for row in rows] == ["12345678901234567890.123456789", "-0", "1e-19"]
    assert [row["counter"] for row in rows] == ["", "0", "42"]
    assert all(row["sys_id"] == "" for row in rows)
    assert [row["update_date_time"] for row in rows] == [r"\N", "", r"\\N"]
    assert rows[0]["note"] == r"\N" and rows[1]["note"] == ""
    assert report["validation"]["preserved_fields_equal"]
    dump = logical(output / "dump.sql", tmp_path / "dump.sqlite", bypass)
    inserts = logical(output / "inserts.sql", tmp_path / "inserts.sqlite", bypass, fields=entry["fields"])
    csv_rows = logical(output / "csv", tmp_path / "csv.sqlite", bypass, is_csv=True)
    assert dump == inserts == csv_rows
    assert [row["cost"] for row in dump] == [None, "0", "-0"]
    assert [row["update_date_time"] for row in dump] == [None, "", r"\N"]


def test_optimizer_unknown_null_only_cost_is_empty_without_guessing_type(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "optimizer_production.csv").write_text(
        "datasetid,bomnum,location,product,period,cost,sys_id,date,input_output,offset,empty_text,mixed\n"
        "11,SYN_B,SYN_L,SYN_P,0,\\N,99,\\N,41.7,0,,\\N\n"
        "11,SYN_B,SYN_L,SYN_P,1,\\N,99,\\N,1234567890.123456789,-0,,\n",
        encoding="utf-8")
    output = tmp_path / "out"
    project = tmp_path / "private.snpanon"
    run(Options(source, output, project, bypass_model=True))
    entry, rows = physical(output)
    assert {f["name"]: f["sql_type"] for f in entry["fields"]}["cost"] == "text"
    assert {"cost", "sys_id", "date"}.issubset(entry["empty_null_columns"])
    assert "empty_text" not in entry["empty_null_columns"] and "mixed" not in entry["empty_null_columns"]
    assert all(row["cost"] == row["sys_id"] == row["date"] == row["empty_text"] == "" for row in rows)
    assert [row["mixed"] for row in rows] == [r"\N", ""]
    assert [row["input_output"] for row in rows] == ["41.7", "1234567890.123456789"]
    restored = logical(output / "csv", tmp_path / "read.sqlite", True, is_csv=True)
    assert all(row["cost"] is None and row["date"] is None and row["empty_text"] == "" for row in restored)
    # The all-NULL TEXT declaration survives importing a manifest, too.
    second = tmp_path / "second"
    run(Options(output / "csv", second, project, bypass_model=True))
    second_entry, second_rows = physical(second)
    assert "cost" in second_entry["empty_null_columns"]
    assert all(row["cost"] == "" for row in second_rows)


@pytest.mark.parametrize("marker", ["qty", None, ["qty", "qty"], ["missing"], [1], [{}]])
def test_invalid_empty_null_declaration_is_safe(tmp_path, marker):
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("qty,note\n,SYN_PRIVATE\n", encoding="utf-8")
    entry = {"schema": "public", "table": "custom", "file": "custom.csv",
             "fields": [{"name": "qty", "sql_type": "numeric"}, {"name": "note", "sql_type": "text"}],
             "empty_null_columns": marker}
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v2", "tables": [entry]}), encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError, match="empty_null_columns") as error:
            parse_csv(source, store)
        assert "SYN_PRIVATE" not in str(error.value)


@pytest.mark.parametrize("nullable", [False, True])
def test_manifest_marker_uses_header_names_not_field_positions_and_checks_nullable(tmp_path, nullable):
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("note,qty\n,\n", encoding="utf-8")
    entry = {"schema": "public", "table": "custom", "file": "custom.csv",
             "fields": [{"name": "qty", "sql_type": "numeric", "nullable": nullable},
                        {"name": "note", "sql_type": "text"}], "empty_null_columns": ["qty"]}
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v2", "tables": [entry]}), encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        if nullable:
            parse_csv(source, store)
            assert list(store.rows(1)) == [[None, ""]]
        else:
            with pytest.raises(AnonymizerError, match="qty"):
                parse_csv(source, store)


def test_xlsx_missing_numeric_and_untyped_null_cells_export_empty(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["qty", "cost", "note"])
    book.active.append([12.5, None, "SYN_NOTE"])
    book.active.append([None, None, "SYN_NOTE"])
    source = tmp_path / "custom.xlsx"
    book.save(source)
    book.close()
    output = tmp_path / "out"
    run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True))
    entry, rows = physical(output)
    assert entry["empty_null_columns"] == ["qty", "cost"]
    assert [row["qty"] for row in rows] == ["12.5", ""]
    assert all(row["cost"] == "" for row in rows)
    restored = logical(output / "csv", tmp_path / "read.sqlite", True, is_csv=True)
    assert [row["qty"] for row in restored] == ["12.5", None]
    assert all(row["cost"] is None for row in restored)

"""Synthetic regressions for typed blanks in external CSV, not manifest CSV."""
import csv
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.runlog import RunLog
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.validation import validate_value


def contract_model():
    model = Model()
    model.tables["csv_contract"] = {"fields": [
        {"name": name, "type": kind, "nullable": nullable, "size": None,
         "keys": [], "fk": None}
        for name, kind, nullable in [
            ("qty", "NUMBER", True), ("counter", "INTEGER", True),
            ("required_qty", "NUMBER", False), ("note", "TEXT", True),
            ("day", "DATE", True), ("flag", "BOOLEAN", True), ("payload", "JSONB", True),
        ]
    ]}
    return model


@pytest.mark.parametrize("cell", ["", '""'])
def test_plain_csv_typed_blanks_respect_type_and_nullable(tmp_path, cell):
    source = tmp_path / "source"
    source.mkdir()
    # Reordered fields and an unknown column guard original header alignment.
    (source / "csv_contract.csv").write_text(
        "note;extra;counter;qty;required_qty;day;flag;payload\n"
        + f";SYN_IGNORED;{cell};{cell};{cell};{cell};{cell};{cell}\n", encoding="utf-8")
    with Store(tmp_path / "read.sqlite", contract_model()) as store:
        parse_csv(source, store)
        table = store.tables[("public", "csv_contract")]
        row, = store.rows(table["id"])
        values = dict(zip(store.columns(("public", "csv_contract")), row))
        assert values == {"qty": None, "counter": None, "required_qty": "", "note": "",
                          "day": "", "flag": "", "payload": ""}
        for field, value in zip(table["fields"], row):
            if field["name"] in {"required_qty", "day", "flag", "payload"}:
                with pytest.raises(AnonymizerError):
                    validate_value("csv_contract", field, value)
            else:
                validate_value("csv_contract", field, value)


@pytest.mark.parametrize("value", ["0", "-0.000001", "12345678901234567890.123456789", "1e-6", " ", "\t", "30,0", "NULL"])
def test_nonempty_numeric_input_is_not_rewritten(tmp_path, value):
    source = tmp_path / "source"
    source.mkdir()
    with (source / "csv_contract.csv").open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, delimiter=";").writerows([
            ["qty", "counter", "required_qty"], [value, r"\N", "1"]])
    with Store(tmp_path / "read.sqlite", contract_model()) as store:
        parse_csv(source, store, delimiter=";")
        row, = store.rows(1)
        assert row[:3] == [value, None, "1"]
        if value in {" ", "\t", "30,0", "NULL"}:
            with pytest.raises(AnonymizerError):
                validate_value("csv_contract", store.tables[("public", "csv_contract")]["fields"][0], row[0])


@pytest.mark.parametrize("with_fields", [False, True])
def test_manifest_does_not_apply_external_csv_blank_policy(tmp_path, with_fields):
    source = tmp_path / "source"
    source.mkdir()
    entry = {"schema": "public", "table": "sku", "file": "sku.csv"}
    if with_fields:
        entry["fields"] = [
            {"name": "item", "sql_type": "text", "nullable": False},
            {"name": "loc", "sql_type": "text", "nullable": False},
            {"name": "fixhor", "sql_type": "numeric", "nullable": True},
            {"name": "planner", "sql_type": "text", "nullable": True},
        ]
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v1", "tables": [entry]}))
    with (source / "sku.csv").open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows([
            ["item", "loc", "fixhor", "planner"],
            ["SYN_I1", "SYN_L", "", ""], ["SYN_I2", "SYN_L", r"\N", r"\\N"],
        ])
    with Store(tmp_path / "read.sqlite", Model()) as store:
        parse_csv(source, store)
        table = store.tables[("public", "sku")]
        rows = [dict(zip(store.columns(("public", "sku")), row)) for row in store.rows(table["id"])]
        assert (rows[0]["fixhor"], rows[0]["planner"]) == ("", "")
        assert (rows[1]["fixhor"], rows[1]["planner"]) == (None, r"\N")
        field = next(f for f in table["fields"] if f["name"] == "fixhor")
        with pytest.raises(AnonymizerError):
            validate_value("sku", field, rows[0]["fixhor"])


@pytest.mark.parametrize("data", ["INSERT INTO public.t VALUES ('');", "COPY public.t(qty) FROM stdin;\n\n\\.\n"])
def test_sql_numeric_empty_string_still_rejected(tmp_path, data):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.t(qty numeric);\n" + data, encoding="utf-8")
    with pytest.raises(AnonymizerError, match="qty"):
        run(Options(source, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def read_export(source, db_path, *, csv_input=False):
    with Store(db_path, Model()) as store:
        (parse_csv if csv_input else parse_sql)(source, store)
        return {name[1]: [{f["name"]: value for f, value in zip(table["fields"], row)
                          if f["name"] != "sys_id"} for row in store.rows(table["id"])]
                for name, table in store.tables.items()}


@pytest.mark.parametrize("skip", [False, True])
def test_nullable_sku_numbers_match_all_exports_and_manifest_roundtrip(tmp_path, skip):
    source = tmp_path / "source"
    source.mkdir()
    (source / "item.csv").write_text("item,proddescr\nSYN_I1,Synthetic A\nSYN_I2,Synthetic B\n", encoding="utf-8")
    (source / "loc.csv").write_text("loc,locdescr\nSYN_L,Synthetic site\n", encoding="utf-8")
    (source / "sku.csv").write_text(
        "prod_prof;item;fixhor;loc;prof_descr;fixlotsize;grtime;scrapfactor\n"
        'SYN_UNKNOWN_A;SYN_I1;;SYN_L;SYN_UNKNOWN_B;"";;\\N\n'
        "SYN_UNKNOWN_A;SYN_I2;0;SYN_L;SYN_UNKNOWN_B;1234567890.123456;0.000001;\\N\n", encoding="utf-8")
    log = RunLog()
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "synthetic.snpanon", log=log, skip_error_tables=skip))
    assert (report["tables"], report["rows"], report["skipped_tables"]) == (3, 5, [])
    assert report["validation"]["preserved_fields_equal"]
    dump = read_export(output / "dump.sql", tmp_path / "dump.sqlite")
    inserts = read_export(output / "inserts.sql", tmp_path / "inserts.sqlite")
    csv_rows = read_export(output / "csv", tmp_path / "csv.sqlite", csv_input=True)
    assert dump == inserts == csv_rows
    assert [(r["fixhor"], r["fixlotsize"], r["grtime"], r["scrapfactor"]) for r in dump["sku"]] == [
        (None, None, None, None), ("0", "1234567890.123456", "0.000001", None)]
    warnings = {e[4] for e in log.entries if e[3] == "unknown_column"}
    assert warnings == {"public.sku: prod_prof", "public.sku: prof_descr"}
    for file in output.rglob("*"):
        if file.is_file():
            assert "SYN_UNKNOWN_" not in file.read_text(encoding="utf-8")


def test_missing_business_key_still_drops_table_before_normalization(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sku.csv").write_text("item,fixhor\nSYN_I,\n", encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model()) as store:
        parse_csv(source, store)
        assert not store.tables and not store.count
        assert store.dropped_tables[0][0] == "public.sku"

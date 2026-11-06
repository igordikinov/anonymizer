"""Exact locale decimals at the external tabular boundary, not in SQL/TEXT."""
import csv
import json

import pytest
from openpyxl import Workbook

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.generic_input import inferred_type
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.xlsx_reader import parse_xlsx


@pytest.mark.parametrize("raw", ["-2476,7", "+2476,70", "0,0", "-0,000", ",5", "-,5",
                                 "1,", "-1,2345678901234567890123456789", "1,25e-7"])
def test_comma_decimals_are_numbers(raw):
    assert inferred_type(raw) == "NUMBER"


@pytest.mark.parametrize("raw", ["001,5", "01", " 1,5", "1,5 ", "1,2,3", "1.234,5",
                                 "1,234.5", "1 234,5", "1\u00a0234,5", "-,", "NaN,1"])
def test_codes_and_ambiguous_or_malformed_text_are_not_reinterpreted(raw):
    assert inferred_type(raw) == "TEXT"


def write_source(tmp_path, kind, rows, table="custom", delimiter=";"):
    directory = tmp_path / "source"
    directory.mkdir(exist_ok=True)
    if kind == "xlsx":
        path = directory / f"{table}.xlsx"
        book = Workbook()
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()
        return path
    with (directory / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, delimiter=delimiter).writerows(rows)
    return directory


@pytest.mark.parametrize("kind,delimiter", [("csv", ";"), ("csv", ","), ("csv", "\t"), ("xlsx", ";")])
@pytest.mark.parametrize("profile", [False, True])
def test_actual_exports_preserve_comma_decimal_values_and_repeat(tmp_path, kind, delimiter, profile):
    exact = "-2476.700000000000000001"
    source = write_source(tmp_path, kind, [
        ["code", "quantity", "update_date_time", "sys_id", "change_author"],
        ["SYN_A", exact.replace(".", ","), "-2476,7", "-2476,7", "-2476,7"],
        ["SYN_B", "0,000", "RAW_TIME", "RAW_ID", "RAW_AUTHOR"],
        ["SYN_C", None if kind == "xlsx" else r"\N", "", "", ""],
    ], delimiter=delimiter)
    contract = {
        "format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-comma", "version": "1",
        "domains": [{"id": "CODE", "members": [{"table": "custom", "column": "code"}]}],
        "required_checks": ["domain:CODE"],
    } if profile else None
    outputs = []
    for index in range(2):
        output = tmp_path / f"out-{index}"
        report = run(Options(source, output, tmp_path / "project.snpanon", bypass_model=True,
                             delimiter=delimiter, connectivity_profile=contract, require_integrity=profile))
        assert report["validation"]["preserved_fields_equal"]
        if profile:
            assert report["integrity_status"] == "PASSED"
        manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
        entry, = manifest["tables"]
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            actual = list(csv.DictReader(stream))
        assert [r["quantity"] for r in actual] == [exact, "0.000", ""]
        assert actual[0]["update_date_time"] == "-2476,7"
        assert actual[0]["sys_id"] == "" and actual[0]["change_author"] == "snp"
        assert all(r["code"] != f"SYN_{letter}" for r, letter in zip(actual, "ABC"))
        assert entry["fields"][1]["sql_type"] == "numeric"
        for filename in ("dump.sql", "inserts.sql"):
            with Store(tmp_path / f"decoded-{index}-{filename}.sqlite", Model(), bypass_model=True) as store:
                if filename == "inserts.sql":
                    store.define((entry["schema"], entry["table"]), entry["fields"])
                parse_sql(output / filename, store)
                table, = store.tables.values()
                values = [dict(zip(store.columns(name), row))
                          for name in store.tables for row in store.rows(table["id"])]
                assert [r["quantity"] for r in values] == [exact, "0.000", None]
        outputs.append(output)
    assert (outputs[0] / "dump.sql").read_bytes() == (outputs[1] / "dump.sql").read_bytes()
    # Manifest roundtrip keeps the declared numeric type and exact values.
    with Store(tmp_path / "roundtrip.sqlite", Model(), bypass_model=True) as store:
        parse_csv(outputs[0] / "csv", store)
        table, = store.tables.values()
        assert [row[1] for row in store.rows(table["id"])] == [exact, "0.000", None]


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_model_numeric_columns_accept_comma_but_text_fields_remain_text(tmp_path, kind):
    source = write_source(tmp_path, kind, [["item", "volume", "proddescr"],
                        ["SYN_ITEM", "-2476,7", "-2476,7"], ["SYN_OTHER", "12.500", "001,50"]], "item")
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "project.snpanon"))
    assert report["rows"] == 2 and report["validation"]["preserved_fields_equal"]
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    entry, = manifest["tables"]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["volume"] for r in rows] == ["-2476.7", "12.500"]
    assert rows[0]["proddescr"] != "-2476,7"  # declared TEXT still anonymized


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_xlsx_numeric_cells_and_text_comma_cells_share_a_number_column(tmp_path, kind):
    source = write_source(tmp_path, kind, [["quantity", "code"], ["-2476,7", "001,50"],
                        [-12.25 if kind == "xlsx" else "-12.25", "002,50"], ["", "003,50"]])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        (parse_xlsx if kind == "xlsx" else parse_csv)(source, store)
        assert [f["type"] for f in store.tables[("public", "custom")]["fields"]] == ["NUMBER", "TEXT"]
        assert list(store.rows(1)) == [["-2476.7", "001,50"], ["-12.25", "002,50"], [None, "003,50"]]


def test_manifest_respects_declared_text_and_numeric_precision(tmp_path):
    source = write_source(tmp_path, "csv", [["quantity", "code"], ["-12,30", "-2476,7"]], delimiter=",")
    manifest = {"format": "snp-anonymizer-csv-v2", "tables": [{"schema": "public", "table": "custom",
        "file": "custom.csv", "fields": [{"name": "quantity", "sql_type": "numeric(6,2)", "nullable": True},
                                         {"name": "code", "sql_type": "text", "nullable": True}]}]}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with Store(tmp_path / "typed.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert list(store.rows(1)) == [["-12.30", "-2476,7"]]
    (source / "custom.csv").write_text('quantity,code\n"-12,345",SYN\n', encoding="utf-8")
    with pytest.raises(AnonymizerError, match="типу или размеру"):
        run(Options(source, tmp_path / "bad-out", tmp_path / "bad.snpanon", bypass_model=True))
    assert not (tmp_path / "bad-out").exists()


def test_sql_text_is_not_normalized_and_sql_numeric_comma_is_rejected(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.custom(code text, quantity numeric);"
                      "INSERT INTO public.custom VALUES ('-2476,7',-2476.7);", encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_sql(source, store)
        assert list(store.rows(1)) == [["-2476,7", "-2476.7"]]
    source.write_text("CREATE TABLE public.custom(quantity numeric);"
                      "INSERT INTO public.custom VALUES ('-2476,7');", encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(source, tmp_path / "out", tmp_path / "project.snpanon", bypass_model=True))


@pytest.mark.parametrize("sql_type,raw,expected", [
    ("smallint", "-32768,0", "-32768"), ("integer", "2147483647,000", "2147483647"),
    ("bigint", "-9223372036854775808,0", "-9223372036854775808"),
    ("integer", "-2,0e1", "-20"), ("bigint", "0,0e999999999", "0"),
])
def test_declared_integer_comma_outputs_loadable_integer_lexemes(tmp_path, sql_type, raw, expected):
    source = write_source(tmp_path, "csv", [["quantity"], [raw]], delimiter=",")
    manifest = {"format": "snp-anonymizer-csv-v2", "tables": [{"schema": "public", "table": "custom",
        "file": "custom.csv", "fields": [{"name": "quantity", "sql_type": sql_type, "nullable": True}]}]}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "out"
    run(Options(source, output, tmp_path / "project.snpanon", bypass_model=True))
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    entry, = manifest["tables"]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        row, = list(csv.DictReader(stream))
    assert row["quantity"] == expected
    assert f"\n{expected}\n" in (output / "dump.sql").read_text(encoding="utf-8")
    assert f"E'{expected}'" in (output / "inserts.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("raw", ["-32768,01", "-32769,0", "32768,0", "1,0e999999999", "1,0e-999999999"])
def test_declared_integer_fraction_or_range_is_rejected_without_expansion(tmp_path, raw):
    source = write_source(tmp_path, "csv", [["quantity"], [raw]], delimiter=",")
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v2", "tables": [
        {"schema": "public", "table": "custom", "file": "custom.csv",
         "fields": [{"name": "quantity", "sql_type": "smallint", "nullable": True}]}]}), encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(source, tmp_path / "out", tmp_path / "project.snpanon", bypass_model=True))
    assert not (tmp_path / "out").exists()


def test_model_integer_xlsx_comma_uses_integer_lexeme(tmp_path, monkeypatch):
    model = Model()
    model.tables["custom"] = {"fields": [{"name": "quantity", "type": "INTEGER", "nullable": True,
                                         "size": None, "keys": [], "fk": None}]}
    monkeypatch.setattr("anonymizer.pipeline.Model", lambda: model)
    source = write_source(tmp_path, "xlsx", [["quantity"], ["-2,0"]])
    output = tmp_path / "out"
    run(Options(source, output, tmp_path / "project.snpanon"))
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    entry, = manifest["tables"]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        row, = list(csv.DictReader(stream))
    assert row["quantity"] == "-2"
    assert "\n-2\n" in (output / "dump.sql").read_text(encoding="utf-8")

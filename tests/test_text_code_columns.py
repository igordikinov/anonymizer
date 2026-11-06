"""Symbolic code columns are text even when Excel stores a digit-only code."""
import csv
import json

import pytest
from openpyxl import Workbook

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.generic_input import InputInference
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.xlsx_reader import parse_xlsx


def make_source(tmp_path, kind, rows, table="custom", date_style=False):
    directory = tmp_path / "source"
    directory.mkdir(exist_ok=True)
    if kind == "xlsx":
        path = directory / f"{table}.xlsx"
        book = Workbook()
        for row in rows:
            book.active.append(row)
        if date_style:
            book.active["A2"].number_format = "yyyy-mm-dd"
        book.save(path)
        book.close()
        return path
    with (directory / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, delimiter=";").writerows(rows)
    return directory


@pytest.mark.parametrize("column", ["comp", "item", "loc", "COMP", "Item", "Loc"])
@pytest.mark.parametrize("reverse", [False, True])
def test_code_type_is_text_for_numeric_and_symbolic_values_in_any_order(column, reverse):
    state = InputInference("custom", [column])
    values = ["SYN_CODE_2.9KG", "7300019"]
    for value in reversed(values) if reverse else values:
        state.observe([value])
    assert state.fields()[0]["type"] == "TEXT"


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize("reverse", [False, True])
def test_actual_pipeline_anonymizes_both_code_forms_and_keeps_measures(tmp_path, kind, reverse):
    rows = [["SYN_CODE_2.9KG"] * 3 + ["-2476,700000000000000001"],
            [7300019] * 3 + [12.5 if kind == "xlsx" else "12.500"]]
    if reverse:
        rows.reverse()
    source = make_source(tmp_path, kind, [["comp", "item", "loc", "quantity"], *rows])
    outputs = []
    for index in range(2):
        output = tmp_path / f"out-{index}"
        report = run(Options(source, output, tmp_path / "project.snpanon", bypass_model=True))
        assert report["rows"] == 2 and report["validation"]["preserved_fields_equal"]
        manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
        entry, = manifest["tables"]
        assert [f["sql_type"] for f in entry["fields"]] == ["text", "text", "text", "numeric"]
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            actual = list(csv.DictReader(stream))
        for column in ("comp", "item", "loc"):
            assert [r[column] for r in actual] == [f"{column}_000001", f"{column}_000002"]
        expected_numbers = [str(row[3]).replace(",", ".") for row in rows]
        assert [r["quantity"] for r in actual] == expected_numbers
        for filename in ("dump.sql", "inserts.sql"):
            with Store(tmp_path / f"decoded-{index}-{filename}.sqlite", Model(), bypass_model=True) as store:
                if filename == "inserts.sql":
                    store.define((entry["schema"], entry["table"]), entry["fields"])
                parse_sql(output / filename, store)
                table, = store.tables.values()
                assert list(store.rows(table["id"])) == [list(row.values()) for row in actual]
        outputs.append(output)
    assert (outputs[0] / "dump.sql").read_bytes() == (outputs[1] / "dump.sql").read_bytes()


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_codes_keep_spelling_null_empty_and_commas_before_anonymizing(tmp_path, kind):
    raw = ["0019", "1,20", "1.20", "1e2", "2026-11-06", "true", "", None]
    rows = [[value if value is not None or kind == "xlsx" else r"\N"] for value in raw]
    source = make_source(tmp_path, kind, [["comp"], *rows])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        if kind == "xlsx":
            parse_xlsx(source, store)
        else:
            parse_csv(source, store, delimiter=";")
        assert store.tables[("public", "custom")]["fields"][0]["type"] == "TEXT"
        # A trailing empty XLSX row has no physical data cells and is skipped.
        assert list(store.rows(1)) == [[value] for value in raw[:7 if kind == "xlsx" else 8]]


def test_xlsx_all_numeric_codes_ignore_display_date_format_and_are_anonymized(tmp_path):
    source = make_source(tmp_path, "xlsx", [["item"], [7300019], [7300020]], date_style=True)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_xlsx(source, store)
        assert list(store.rows(1)) == [["7300019"], ["7300020"]]
        assert store.tables[("public", "custom")]["fields"][0]["type"] == "TEXT"
    report = run(Options(source, tmp_path / "out", tmp_path / "project.snpanon", bypass_model=True))
    assert report["rows"] == 2
    assert "7300019" not in (tmp_path / "out/dump.sql").read_text(encoding="utf-8")


def test_explicit_manifest_numeric_type_remains_authoritative(tmp_path):
    source = make_source(tmp_path, "csv", [["comp"], ["7300019"]])
    # Manifest CSV has comma delimiters irrespective of external defaults.
    (source / "custom.csv").write_text("comp\n7300019\n", encoding="utf-8")
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v2", "tables": [
        {"schema": "public", "table": "custom", "file": "custom.csv",
         "fields": [{"name": "comp", "sql_type": "numeric", "nullable": True}]}]}), encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert store.tables[("public", "custom")]["fields"][0]["type"] == "NUMBER"
        assert list(store.rows(1)) == [["7300019"]]


@pytest.mark.parametrize("column", ["quantity", "cost", "item_qty", "loc_capacity", "comp_cost"])
def test_numeric_measure_type_errors_are_not_hidden(column):
    state = InputInference("custom", [column])
    state.observe(["-2476,7"])
    with pytest.raises(AnonymizerError, match="неоднородные типы"):
        state.observe(["SYN_NOT_NUMBER"])


def test_text_profile_matches_digit_code_across_csv_comp_and_native_numeric_xlsx_item(tmp_path):
    source = make_source(tmp_path, "csv", [["comp", "quantity"], [7300019, "-2476,7"]], table="components")
    make_source(tmp_path, "xlsx", [["item"], [7300019]], table="items")
    profile = {"format": "snp-anonymizer-connectivity-v1", "profile_id": "symbolic-code", "version": "1",
        "domains": [{"id": "ITEM", "kind": "text", "prefix": "item", "members": [
            {"table": "components", "column": "comp"}, {"table": "items", "column": "item"}]}],
        "required_checks": ["domain:ITEM"]}
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "project.snpanon", bypass_model=True,
                         connectivity_profile=profile, require_integrity=True))
    assert report["integrity_status"] == "PASSED" and report["rows"] == 2
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    codes = []
    for entry in manifest["tables"]:
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            row, = list(csv.DictReader(stream))
        codes.append(row["comp" if entry["table"] == "components" else "item"])
    assert codes[0] == codes[1] != "7300019"

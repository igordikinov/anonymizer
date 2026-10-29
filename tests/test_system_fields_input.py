"""Synthetic readers ignore audit semantics while retaining file safeguards."""
import csv

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.generic_input import InputInference
from anonymizer.metadata import Model
from anonymizer.store import Store
from anonymizer.xlsx_reader import parse_xlsx
from test_generic_xlsx import styled_book
from test_xlsx_reader import book, cell, row, sheet


def audit_model(stamp_type="DATE_TIME"):
    model = Model()
    model.tables["audit_input"] = {"fields": [
        {"name": name, "type": kind, "nullable": nullable, "size": None, "keys": [], "fk": None}
        for name, kind, nullable in [
            ("note", "TEXT", False), ("sys_id", "INTEGER", False),
            ("update_date_time", stamp_type, False), ("change_author", "UUID", False),
            ("qty", "NUMBER", True),
        ]
    ]}
    return model


def records(store):
    name = "public", "audit_input"
    table = store.tables[name]
    return [dict(zip(store.columns(name), values)) for values in store.rows(table["id"])]


def test_inference_does_not_inspect_audit_values_or_native_types():
    state = InputInference("audit_input", ["sys_id", "update_date_time", "change_author", "qty"])
    state.observe(["not-an-id", "not-a-date", "not-a-uuid", "1.2500"],
                  ["NUMBER", "DATE_TIME", "UUID", "NUMBER"])
    state.observe([None, "2026-10-29T12:00:00+03:00", "", "2"],
                  [None, "DATE_TIME", "TEXT", "NUMBER"])
    state.observe(["", "2026-10-29T12:00:00", "42", "3"],
                  ["TEXT", "DATE_TIME", "NUMBER", "NUMBER"])
    assert state.types == [None, None, None, "NUMBER"]
    assert state.aware == [None] * 4 and state.empty == [False] * 4


@pytest.mark.parametrize("bypass", [False, True])
def test_csv_audit_strings_empty_and_null_are_read_without_type_checks(tmp_path, bypass):
    source = tmp_path / "source"
    source.mkdir()
    header = ["note", "sys_id", "update_date_time", "change_author", "qty"]
    values = [
        ["SYN_A", "not-an-id", "  not ISO  ", "not-a-uuid", "1.2500"],
        ["SYN_B", "", "", "", ""],
        ["SYN_C", r"\N", r"\N", r"\N", "2"],
    ]
    with (source / "audit_input.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(values)
    with Store(tmp_path / "spool.sqlite", audit_model(), bypass_model=bypass) as store:
        parse_csv(source, store, delimiter=",")
        result = records(store)
        assert result[0] == dict(zip(header, values[0]))
        assert result[1] == dict(zip(header, ["SYN_B", "", "", "", None]))
        assert result[2] == dict(zip(header, ["SYN_C", None, None, None, "2"]))


def test_csv_does_not_normalize_empty_audit_value_as_nullable_number(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "audit_input.csv").write_text(
        "note,sys_id,update_date_time,change_author,qty\nSYN_A,7,,SYN_AUTHOR,\n",
        encoding="utf-8",
    )
    with Store(tmp_path / "spool.sqlite", audit_model("NUMBER")) as store:
        parse_csv(source, store, delimiter=",")
        record, = records(store)
        assert record["update_date_time"] == "" and record["qty"] is None


@pytest.mark.parametrize("bypass", [False, True])
def test_xlsx_audit_cells_keep_raw_serial_boolean_iso_text_and_empty(tmp_path, bypass):
    header = row(1, *[cell(f"{letter}1", name) for letter, name in zip(
        "ABCDE", ["note", "sys_id", "update_date_time", "change_author", "qty"])])
    data = header + row(2, cell("A2", "SYN_A"), cell("B2", "not-an-id"),
                        cell("C2", "60", "n").replace('r="C2"', 'r="C2" s="1"'),
                        cell("D2", "not-a-uuid"), cell("E2", "1.2500", "n"))
    data += row(3, cell("A3", "SYN_B"), cell("B3", ""), cell("C3", "not-iso", "d"), cell("D3", "1", "b"))
    data += row(4, cell("A4", "SYN_C"), cell("B4"), cell("C4", ""), cell("D4", ""))
    data += row(5, cell("A5", "SYN_D"), cell("B5", r"\N"), cell("C5", r"\N"), cell("D5", r"\N"))
    source = styled_book(tmp_path / "audit_input.xlsx", data, ["yyyy-mm-dd"])
    with Store(tmp_path / "spool.sqlite", audit_model(), bypass_model=bypass) as store:
        parse_xlsx(source, store)
        result = records(store)
        assert [record["update_date_time"] for record in result] == ["60", "not-iso", "", None]
        assert [record["change_author"] for record in result] == ["not-a-uuid", "1", "", None]
        assert [record["sys_id"] for record in result] == ["not-an-id", "", None, None]
        assert result[0]["qty"] == "1.2500"


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("unsafe", [
    '<c r="C2"><f>1+1</f><v>2</v></c>',
    '<c r="C2" t="e"><v>#VALUE!</v></c>',
    '<c r="C2" t="n"><v>SYN_NOT_NUMBER</v></c>',
])
def test_xlsx_audit_cells_still_obey_formula_error_and_xml_number_guards(tmp_path, bypass, unsafe):
    header = row(1, cell("A1", "note"), cell("B1", "sys_id"), cell("C1", "update_date_time"),
                 cell("D1", "change_author"))
    source = book(tmp_path / "audit_input.xlsx", sheet(
        header + row(2, cell("A2", "SYN_A"), cell("B2", "7", "n"), unsafe, cell("D2", "SYN_AUTHOR"))))
    with Store(tmp_path / "spool.sqlite", audit_model(), bypass_model=bypass) as store:
        with pytest.raises(AnonymizerError):
            parse_xlsx(source, store)

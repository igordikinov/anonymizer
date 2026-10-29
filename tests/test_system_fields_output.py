"""Synthetic system-field policy and streaming PostgreSQL projection checks."""
import csv
import io
import json

import pytest

from anonymizer.errors import AnonymizerError, Cancelled
from anonymizer.metadata import Model
from anonymizer.pg_loader import _SectionReader, load
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.validation import Validator, validate_value
from test_pg_loader import FakePsycopg2, install, pg_options


@pytest.mark.parametrize("name", ["sys_id", "update_date_time", "change_author"])
@pytest.mark.parametrize("value", [None, "", "SYN_BAD_TYPE", "\0"])
def test_system_fields_have_no_semantic_value_validation(name, value):
    validate_value("custom", {"name": name, "type": "INTEGER", "sql_type": "smallint", "nullable": False}, value)


def test_business_values_still_validate_type_and_nullability():
    field = {"name": "quantity", "type": "NUMBER", "sql_type": "numeric(2,1)", "nullable": False}
    for value in (None, "SYN_BAD_TYPE", "123.45", "\0"):
        with pytest.raises(AnonymizerError):
            validate_value("custom", field, value)


def read_exported_sql(path, sqlite_path, tables=()):
    with Store(sqlite_path, Model()) as store:
        for table in tables:
            store.define((table["schema"], table["table"]), table["fields"])
        parse_sql(path, store)
        return {name[1]: (store.columns(name), list(store.rows(table["id"]))) for name, table in store.tables.items()}


@pytest.mark.parametrize("bypass", [False, True])
def test_system_output_policy_matches_all_exports_and_never_maps_audit_values(tmp_path, bypass):
    source = tmp_path / "source.sql"
    source.write_text(
        "CREATE TABLE public.custom(note text, sys_id bigint PRIMARY KEY, "
        "update_date_time timestamp NOT NULL, change_author uuid NOT NULL, quantity numeric);"
        "INSERT INTO public.custom VALUES "
        "('SYN_NOTE','SYN_BAD_ID','SYN_BAD_TIMESTAMP','SYN_PRIVATE_AUTHOR',1234567890.123456789),"
        "('SYN_NOTE',NULL,'',NULL,42),"
        "('SYN_NOTE','SYN_BAD_ID',NULL,'',0.000001);",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    project = tmp_path / "private.snpanon"
    report = run(Options(source, output, project, bypass_model=bypass))
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    entry, = manifest["tables"]
    with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["sys_id"] for row in rows] == [r"\N"] * 3
    assert [row["update_date_time"] for row in rows] == ["SYN_BAD_TIMESTAMP", "", r"\N"]
    assert [row["change_author"] for row in rows] == ["snp"] * 3
    assert [row["quantity"] for row in rows] == ["1234567890.123456789", "42", "0.000001"]
    assert report["validation"]["preserved_fields_equal"]
    assert report["validation"]["business_key_duplicates"] == []
    assert "key_groups" not in entry
    assert {field["name"]: field["sql_type"] for field in entry["fields"]}["change_author"] == "text"
    dump = (output / "dump.sql").read_text(encoding="utf-8")
    assert '"sys_id" bigint' in dump and "IDENTITY" not in dump
    assert "SYN_PRIVATE_AUTHOR" not in dump and "SYN_BAD_ID" not in dump
    inserts = (output / "inserts.sql").read_text(encoding="utf-8")
    assert '"sys_id"' not in inserts
    from pglast import parse_sql as pg_parse
    insert_statements = [item.stmt for item in pg_parse(inserts) if item.stmt.__class__.__name__ == "InsertStmt"]
    assert len(insert_statements) == 1
    statement, = insert_statements
    assert [column.name for column in statement.cols] == ["note", "update_date_time", "change_author", "quantity"]
    assert all(len(row) == 4 for row in statement.selectStmt.valuesLists)
    parsed_dump = read_exported_sql(output / "dump.sql", tmp_path / "dump-read.sqlite")
    parsed_insert = read_exported_sql(output / "inserts.sql", tmp_path / "insert-read.sqlite", manifest["tables"])
    assert parsed_dump == parsed_insert
    columns, values = parsed_dump["custom"]
    assert [dict(zip(columns, row))["sys_id"] for row in values] == [None] * 3
    import sqlite3
    with sqlite3.connect(project) as db:
        assert not db.execute("SELECT domain FROM mapping WHERE old IN ('SYN_BAD_ID','SYN_PRIVATE_AUTHOR','SYN_BAD_TIMESTAMP')").fetchall()


def test_validator_does_not_extract_partial_reference_from_audit_group(tmp_path):
    with Store(tmp_path / "read.sqlite", Model()) as store:
        store.define(("public", "parent"), [{"name": "sys_id", "sql_type": "bigint"}, {"name": "code", "sql_type": "text"}])
        store.define(("public", "child"), [{"name": "sys_id", "sql_type": "bigint"},
                     {"name": "code", "sql_type": "text"}],
                     [{"columns": ["sys_id", "code"], "target_schema": "public", "target_table": "parent",
                       "target_columns": ["sys_id", "code"], "match": "simple"}])
        # Synthetic parser-like component hints must not become partial links.
        store.tables[("public", "child")]["fields"][1]["fk"] = {"schema": "public", "table": "parent", "field": "code"}
        validator = Validator(store, None)
        assert validator.links == [] and validator.key_links == []


def pg_tables():
    return {("public", "custom"): {"fields": [{"name": name} for name in ("note", "sys_id", "change_author", "update_date_time")]}}


def pg_dump(tmp_path, body=r"SYN\tNOTE" + "\t" + r"\N" + "\tsnp\t2026-10-29\n"):
    path = tmp_path / "dump.sql"
    path.write_text('COPY "snp_a000001"."custom" ("note", "sys_id", "change_author", "update_date_time") FROM stdin;\n'
                    + body + "\\.\n", encoding="utf-8", newline="")
    return path


def test_pg_omits_only_cleared_sys_id_and_preserves_escaped_data(tmp_path, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"custom"}), monkeypatch)
    summary, skipped = load(pg_tables(), pg_dump(tmp_path), pg_options())
    assert summary["rows"] == 1 and skipped == []
    assert fake.copied == [('COPY "public_5"."custom" ("note", "change_author", "update_date_time") FROM STDIN',
                            r"SYN\tNOTE" + "\tsnp\t2026-10-29\n")]
    assert fake.commits == 1 and fake.rollbacks == 0


@pytest.mark.parametrize("body", ["SYN_NOTE\t123\tsnp\tstamp\n", "SYN_NOTE\t" + r"\N" + "\tsnp\n"])
def test_pg_projection_rejects_noncleared_id_or_wrong_width_and_rolls_back(tmp_path, monkeypatch, body):
    fake = install(FakePsycopg2(existing_tables={"custom"}), monkeypatch)
    with pytest.raises(AnonymizerError):
        load(pg_tables(), pg_dump(tmp_path, body), pg_options())
    assert fake.commits == 0 and fake.rollbacks == 1
    assert fake.rows == {"custom": ["OLD_custom"]}


def test_pg_projection_rejects_metadata_header_mismatch(tmp_path, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"custom"}), monkeypatch)
    path = pg_dump(tmp_path)
    path.write_text(path.read_text().replace('"note", "sys_id"', '"sys_id", "note"'), encoding="utf-8")
    with pytest.raises(AnonymizerError, match="Колонки COPY"):
        load(pg_tables(), path, pg_options())
    assert fake.commits == 0 and fake.rollbacks == 1


def test_pg_projected_section_retains_cancellation_and_terminator():
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Cancelled("synthetic")
    reader = _SectionReader(io.StringIO(r"\N" + "\tfirst\n" + r"\N" + "\tsecond\n\\.\n"), check, 0, 2)
    with pytest.raises(Cancelled):
        reader.read()
    assert reader.count == 1
    reader = _SectionReader(io.StringIO(r"\N" + "\tfirst\n\\.\nTAIL\n"), lambda: None, 0, 2)
    assert reader.read() == b"first\n" and reader.read() == b"" and reader.count == 1


def test_pg_projected_section_has_byte_row_limit(monkeypatch):
    from anonymizer import pg_loader
    monkeypatch.setattr(pg_loader, "MAX_COPY_ROW_BYTES", 12)
    reader = _SectionReader(io.StringIO(r"\N" + "\t" + "Ж" * 7 + "\n\\.\n"), lambda: None, 0, 2)
    with pytest.raises(AnonymizerError, match="лимит строки"):
        reader.read()

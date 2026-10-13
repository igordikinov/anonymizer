import io
import json

import pytest

from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.sql_reader import Lexer, Reader, decode_escape, parse_sql
from anonymizer.store import Store


def test_lexer_quoting_comments_and_dollar():
    lex = Lexer(Reader(io.StringIO("/* outer /* inner */ end */ 'O''Brien' E'one\\ntwo' $tag$a; b$tag$ \"Quoted\" -- comment\n123.450")))
    assert [lex.pop().value for _ in range(5)] == ["O'Brien", "one\ntwo", "a; b", "Quoted", "123.450"]


def test_copy_escapes():
    assert decode_escape(r"a\tb\nc\\d\141\x42") == "a\tb\nc\\daB"
    assert decode_escape(r"\303\251\xD0\xAF") == "éЯ"
    assert decode_escape(r"\u0410\U0001F600") == "А😀"


def test_exported_csv_distinguishes_null_empty_and_literal_backslash(tmp_path):
    from anonymizer.csv_reader import parse_csv
    import csv
    src = tmp_path / "csv"
    src.mkdir()
    manifest = {"format": "snp-anonymizer-csv-v1", "tables": [{"schema": "public", "table": "item", "file": "item.csv",
                "fields": [{"name": "item", "sql_type": "text", "nullable": True}, {"name": "proddescr", "sql_type": "text", "nullable": True}]}]}
    (src / "manifest.json").write_text(json.dumps(manifest))
    with (src / "item.csv").open("w", newline="") as stream:
        csv.writer(stream).writerows([["item", "proddescr"], ["i1", r"\N"], ["i2", ""], ["i3", r"\\N"]])
    store = Store(tmp_path / "read.sqlite", Model())
    parse_csv(src, store)
    assert list(store.rows(1)) == [["i1", None], ["i2", ""], ["i3", r"\N"]]
    store.close()


def test_manifest_traversal_rejected(tmp_path):
    from anonymizer.csv_reader import parse_csv
    src = tmp_path / "csv"
    src.mkdir()
    (src / "in.csv").write_text("note\nsecret\n")
    (src / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v1", "tables": [{"schema": "public", "table": "../../../victim", "file": "in.csv", "fields": [{"name": "note", "sql_type": "text"}]}]}))
    store = Store(tmp_path / "read.sqlite", Model())
    with pytest.raises(AnonymizerError):
        parse_csv(src, store)
    store.close()


def test_streaming_single_insert(tmp_path):
    src = tmp_path / "big.sql"
    with src.open("w") as stream:
        stream.write("CREATE TABLE public.item(item text);INSERT INTO public.item(item) VALUES ")
        for i in range(10000):
            stream.write(("," if i else "") + f"('key{i:05d}')")
        stream.write(";")
    store = Store(tmp_path / "read.sqlite", Model())
    parse_sql(src, store)
    assert store.count == 10000
    store.close()


def test_all_model_roots_are_resolvable():
    model = Model()
    for table, entry in model.tables.items():
        for field in entry["fields"]:
            assert model.root(table, field["name"])


def test_spool_batch_has_byte_budget(tmp_path):
    store = Store(tmp_path / "read.sqlite", Model())
    store.define(("public", "large"), [{"name": "note", "sql_type": "text"}])
    for _ in range(10):
        store.add_row(("public", "large"), ["note"], ["x" * 1024 * 1024])
    assert store.batch_bytes < 4 * 1024 * 1024
    assert store.db.execute("SELECT count(*) FROM rows").fetchone()[0] >= 9
    store.close()

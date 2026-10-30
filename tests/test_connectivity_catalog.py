"""Physical presence and raw declarations are distinct from model output fields."""
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.metadata import Model
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store


@pytest.mark.parametrize("bypass", [False, True])
def test_audit_and_composite_declarations_survive_normalization_without_fake_scalar_fks(tmp_path, bypass):
    source = tmp_path / "source.sql"
    source.write_text("""
        CREATE TABLE public.parent(sys_id bigint PRIMARY KEY, code text, part text);
        CREATE TABLE public.child(sys_id bigint, code text, part text,
            UNIQUE(code,part), FOREIGN KEY(code,part) REFERENCES public.parent(code,part));
        ALTER TABLE public.child ADD FOREIGN KEY(sys_id,code) REFERENCES public.parent(sys_id,code);
        INSERT INTO public.child VALUES (1,'SYN_CODE','SYN_PART');
        """, encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=bypass) as store:
        parse_sql(source, store)
        store.finish_import()
        raw = store.declarations[("public", "child")]
        assert sorted(group["columns"] for group in raw["fk_groups"]) == [["code", "part"], ["sys_id", "code"]]
        assert raw["key_groups"] == [{"columns": ["code", "part"], "kind": "unique", "nulls": "distinct"}]
        assert store.declarations[("public", "parent")]["key_groups"][0]["columns"] == ["sys_id"]
        assert store.tables[("public", "child")]["physical_columns"] == {"sys_id", "code", "part"}
        assert all("sys_id" not in group["columns"] for group in store.tables[("public", "child")]["fk_groups"])
        if bypass:
            assert not store.tables[("public", "child")]["fk_groups"]


def test_model_added_nullable_field_is_not_a_physical_header_column(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "item.csv").write_text("item,proddescr\nSYN_ITEM,SYN_DESCRIPTION\n", encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model()) as store:
        parse_csv(source, store, delimiter=",")
        table = store.tables[("public", "item")]
        assert "volume" in {field["name"] for field in table["fields"]}
        assert table["physical_columns"] == {"item", "proddescr"}
        assert table["nonnull_columns"] == {"item", "proddescr"}


def test_manifest_null_codec_is_not_used_as_a_null_only_fact(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("note,cost\nSYN_NOTE,\n,2\n", encoding="utf-8")
    manifest = {"format": "snp-anonymizer-csv-v2", "tables": [{"schema": "public", "table": "custom",
        "file": "custom.csv", "fields": [{"name": "note", "sql_type": "text"},
                                             {"name": "cost", "sql_type": "numeric"}],
        "empty_null_columns": ["note", "cost"]}]}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        table = store.tables[("public", "custom")]
        assert table["nonnull_columns"] == {"note", "cost"}
        assert "inferred_null_only_columns" not in table


def test_row_identity_and_private_registry_never_use_input_sys_id_or_defaults(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define(name, [{"name": "sys_id", "sql_type": "bigint", "default": "SYN_PRIVATE_DEFAULT"},
                            {"name": "note", "sql_type": "text"}])
        for value in ("SYN_PRIVATE_ID", "SYN_PRIVATE_ID"):
            store.add_row(name, ["sys_id", "note"], [value, "SYN_NOTE"])
        rows = list(store.rows_with_ids(store.tables[name]["id"]))
        assert [row_id for row_id, _ in rows] == [1, 2]
        assert "SYN_PRIVATE" not in json.dumps(store.declarations[name])


def test_skipped_table_retains_constraints_for_coverage_but_releases_field_catalog(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define(name, [{"name": "code", "sql_type": "text"}], key_groups=[
            {"columns": ["code"], "kind": "unique", "nulls": "distinct"}])
        store.drop_table(name, "synthetic")
        assert name not in store.tables
        assert store.declarations[name]["fields"] == []
        assert store.declarations[name]["key_groups"][0]["columns"] == ["code"]


def test_standard_skip_stream_has_a_global_metadata_budget(tmp_path, monkeypatch):
    from anonymizer import store as module
    from anonymizer.generic_input import InputLimitError

    monkeypatch.setattr(module, "MAX_INPUT_METADATA", 3000)
    with Store(tmp_path / "read.sqlite", Model()) as store:
        store.drop_table(("public", "first"), "synthetic")
        store.drop_table(("public", "second"), "synthetic")
        with pytest.raises(InputLimitError):
            store.drop_table(("public", "third"), "synthetic")
        assert len(store.dropped_tables) == 2
        assert store._input_metadata_used <= 3000


@pytest.mark.parametrize("bypass", [False, True])
def test_collation_marker_cannot_retain_arbitrary_nested_metadata(tmp_path, bypass):
    from anonymizer.errors import AnonymizerError

    with Store(tmp_path / "read.sqlite", Model(), bypass_model=bypass) as store:
        with pytest.raises(AnonymizerError) as caught:
            store.define(("public", "custom"), [{"name": "code", "sql_type": "text",
                         "collation_declared": {"SYN_PRIVATE_METADATA": [1, 2]}}])
        assert "SYN_PRIVATE" not in str(caught.value)

"""Synthetic coverage for explicit full model bypass and isolated domains."""
import csv
import json
from pathlib import Path
from uuid import UUID

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError, Cancelled
from anonymizer.generic_input import InputInference, inferred_type
from anonymizer.mapping import Project, Transformer
from anonymizer.metadata import Model
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store


def csv_source(tmp_path, table, rows):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    with (source / (table + ".csv")).open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)
    return source


@pytest.mark.parametrize("raw,kind", [
    ("0", "NUMBER"), ("-1.234567890123456789", "NUMBER"), ("1e-6", "NUMBER"),
    (".5", "NUMBER"), ("1.", "NUMBER"), ("-.5e+2", "NUMBER"),
    ("001", "TEXT"), (" 1", "TEXT"), ("1 ", "TEXT"), ("1,5", "TEXT"),
    ("2026-10-29", "DATE"), ("2026-10-29T01:02:03.123456", "DATE_TIME"),
    ("12:34:56", "TIME"), ("2026-02-30", "TEXT"), ("true", "BOOLEAN"),
    ("2026-10-29T01:02:03+03:00", "DATE_TIME"), ("12:34:56+03:00", "TIME"),
    ("90b5637e-f19e-4e8d-937d-24fe873f98ed", "UUID"), ("SECRET", "TEXT"),
])
def test_inference_has_no_float_or_name_heuristics(raw, kind):
    assert inferred_type(raw) == kind


def test_bypass_known_table_keeps_extra_columns_without_model_keys(tmp_path):
    source = csv_source(tmp_path, "sku", [["order_id", "extra", "day", "qty"],
                                        ["12345678901234567890", "PRIVATE", "2026-10-29", "1.230000000000000001"],
                                        ["0", "OTHER", "2026-10-30", ""]])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        table = store.tables[("public", "sku")]
        assert store.columns(("public", "sku")) == ["order_id", "extra", "day", "qty"]
        assert [f["type"] for f in table["fields"]] == ["NUMBER", "TEXT", "DATE", "NUMBER"]
        assert all(not f["keys"] and f["fk"] is None for f in table["fields"])
        assert table["fk_groups"] == table["key_groups"] == []
        assert list(store.rows(table["id"])) == [
            ["12345678901234567890", "PRIVATE", "2026-10-29", "1.230000000000000001"],
            ["0", "OTHER", "2026-10-30", None]]
        assert store.root("public", "sku", "item") == ("public", "sku", "item")
        assert store.field("public", "sku", "item") is None
        assert not store._input_inference and not store.dropped_columns


def test_unknown_csv_blank_and_null_semantics(tmp_path):
    source = csv_source(tmp_path, "private_extension", [["code", "note", "all_null", "all_empty"],
                                                       ["001", "", r"\N", ""],
                                                       ["002", "PRIVATE", r"\N", ""]])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert [f["type"] for f in store.tables[("public", "private_extension")]["fields"]] == ["TEXT"] * 4
        assert list(store.rows(1)) == [["001", "", None, ""], ["002", "PRIVATE", None, ""]]


def test_inferred_nontext_blanks_are_null_without_affecting_empty_text(tmp_path):
    source = csv_source(tmp_path, "custom", [["number", "day", "flag", "uuid", "text"],
        [".5", "2026-10-29", "true", "90b5637e-f19e-4e8d-937d-24fe873f98ed", "PRIVATE"],
        ["", "", "", "", ""]])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert list(store.rows(1))[1] == [None, None, None, None, ""]


def test_timezone_inference_preserves_raw_offsets(tmp_path):
    source = csv_source(tmp_path, "custom", [["stamp", "clock"],
        ["2026-10-29T01:02:03.123456+03:00", "12:34:56+03:00"],
        ["2026-10-30T00:02:03Z", "11:34:56Z"]])
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert [f["sql_type"] for f in store.tables[("public", "custom")]["fields"]] == ["timestamp with time zone", "time with time zone"]
        assert list(store.rows(1))[0] == ["2026-10-29T01:02:03.123456+03:00", "12:34:56+03:00"]


def test_timezone_aware_and_naive_mix_is_rejected():
    state = InputInference("custom", ["day"])
    state.observe(["2026-10-29T00:00:00+03:00"])
    with pytest.raises(AnonymizerError, match="часовые пояса"):
        state.observe(["2026-10-29T00:00:00"])


def test_mixed_types_fail_safely_and_skip_removes_whole_table(tmp_path):
    source = csv_source(tmp_path, "custom", [["amount"], ["123"], ["PRIVATE_NOT_NUMBER"]])
    with Store(tmp_path / "first.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError, match="неоднородные типы") as error:
            parse_csv(source, store, delimiter=",")
        assert "PRIVATE_NOT_NUMBER" not in str(error.value)
    with Store(tmp_path / "second.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store, delimiter=",", skip_error_tables=True)
        assert not store.tables and store.count == 0 and not store._input_inference
        assert store.db.execute("SELECT count(*) FROM rows").fetchone()[0] == 0


def test_native_text_numeric_looking_codes_are_not_inferred_as_numbers(tmp_path):
    with Store(tmp_path / "native.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, ["code", "amount"])
        store.observe_input(name, ["123", "456.789"], ["TEXT", "NUMBER"])
        store.add_row(name, ["code", "amount"], ["123", "456.789"])
        store.finish_input(name)
        assert [f["type"] for f in store.tables[name]["fields"]] == ["TEXT", "NUMBER"]


def test_normalization_pass_is_cancellable(tmp_path):
    with Store(tmp_path / "cancel.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, ["qty"])
        for value in ["1", ""]:
            store.observe_input(name, [value])
            store.add_row(name, ["qty"], [value])
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise Cancelled("synthetic cancellation")
        with pytest.raises(Cancelled):
            store.finish_input(name, check)


def test_generic_transform_changes_all_text_but_preserves_numbers_and_dates(tmp_path):
    with Store(tmp_path / "native.sqlite", Model(), bypass_model=True) as store:
        name = "public", "sku"
        header = ["item", "loc", "uom", "abc", "activeflag", "order_id", "day", "sys_id"]
        values = ["SYN_ITEM", "SYN_LOC", "kg", "A", "Y", "12345", "2026-10-29", "789"]
        store.define_input(name, header)
        store.observe_input(name, values, ["TEXT"] * 5 + ["NUMBER", "DATE", "NUMBER"])
        store.add_row(name, header, values)
        store.finish_input(name)
        table = store.tables[name]
        project = Project(tmp_path / "private.snpanon", "food")
        try:
            transform = Transformer(store.model, project, store)
            result = transform.transform(*name, table["fields"], values)
            assert len(result) == len(values)
            assert all(a != b for a, b in zip(result[:5], values[:5]))
            assert result[5:7] == values[5:7]
            assert result[7] is None
            assert {domain for domain, _, _ in project.iter_mapping()} == {"generic:public:sku." + col for col in header[:5]}
        finally:
            project.finish(True)


def test_generic_domains_do_not_reuse_standard_project_mappings(tmp_path):
    project_path = tmp_path / "private.snpanon"
    name, values = ("public", "custom"), ["SYN_PRIVATE"]
    results = []
    for index in range(2):
        with Store(tmp_path / f"repeat-{index}.sqlite", Model(), bypass_model=True) as store:
            store.define_input(name, ["note"])
            store.observe_input(name, values)
            store.add_row(name, ["note"], values)
            store.finish_input(name)
            project = Project(project_path, "food")
            try:
                project.pseudonym("public:custom.note", "SYN_PRIVATE", label="supplier")
                results.append(Transformer(store.model, project, store).transform(*name, store.tables[name]["fields"], values))
            finally:
                project.finish(True)
    assert results[0] == results[1]
    assert results[0][0] == "note_000001"


def test_sql_ddl_bypass_has_no_model_enrichment_and_discards_alter_fks(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.item(note text, order_id numeric); "
                      "CREATE TABLE public.custom(note text); "
                      "ALTER TABLE public.item ADD FOREIGN KEY(note) REFERENCES public.custom(note); "
                      "INSERT INTO public.item VALUES ('PRIVATE',12345);", encoding="utf-8")
    with Store(tmp_path / "ddl.sqlite", Model(), bypass_model=True) as store:
        parse_sql(source, store)
        store.finish_import()
        fields = store.tables[("public", "item")]["fields"]
        assert [f["name"] for f in fields] == ["note", "order_id"]
        assert all(f["fk"] is None and not f["keys"] for f in fields)
        assert all(not t["fk_groups"] and not t["key_groups"] for t in store.tables.values())
        project = Project(tmp_path / "private.snpanon", "food")
        try:
            assert Transformer(store.model, project, store).rule("item", fields[1], "public") == "preserve"
        finally:
            project.finish(False)


def test_data_only_sql_bypass_requires_structure(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError, match="DDL"):
            store.ensure(("public", "item"), ["item"])


def test_generic_json_and_uuid_ignore_model_aliases(tmp_path):
    from anonymizer import json_data
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.item(key uuid, payload jsonb);", encoding="utf-8")
    old_uuid = "90b5637e-f19e-4e8d-937d-24fe873f98ed"
    old_json = '{"item":"PRIVATE_ITEM","qty":12345678901234567890.123456789,"date":"2026-10-29","other":"kg"}'
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_sql(source, store)
        store.finish_import()
        fields = store.tables[("public", "item")]["fields"]
        project = Project(tmp_path / "private.snpanon", "food")
        try:
            result = Transformer(store.model, project, store).transform("public", "item", fields, [old_uuid, old_json])
            assert result[0] != old_uuid and str(UUID(result[0])) == result[0]
            assert "PRIVATE_ITEM" not in result[1] and '"kg"' not in result[1]
            assert "12345678901234567890.123456789" in result[1] and "2026-10-29" in result[1]
            assert json_data.signature(json_data.load(old_json)) == json_data.signature(json_data.load(result[1]))
            assert all(domain.startswith("generic:") for domain, _, _ in project.iter_mapping())
        finally:
            project.finish(False)


def test_numeric_blank_normalization_spans_multiple_store_batches(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, ["qty", "note"])
        for index in range(2200):
            row = ["" if index % 2 else "1.00001", ""]
            store.observe_input(name, row)
            store.add_row(name, ["qty", "note"], row)
        store.finish_input(name)
        count = 0
        for index, row in enumerate(store.rows(1)):
            assert row == [None if index % 2 else "1.00001", ""]
            count += 1
        assert count == 2200


def test_manifest_physical_fields_not_enriched(tmp_path):
    source = csv_source(tmp_path, "item", [["extra", "qty"], ["SECRET", "1.234567890123456789"]])
    (source / "manifest.json").write_text(json.dumps({"format": "snp-anonymizer-csv-v1", "tables": [
        {"schema": "public", "table": "item", "file": "item.csv", "fields": [
            {"name": "extra", "sql_type": "text"}, {"name": "qty", "sql_type": "numeric"}]}]}), encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store)
        assert store.columns(("public", "item")) == ["extra", "qty"]
        assert list(store.rows(1)) == [["SECRET", "1.234567890123456789"]]


def test_inference_state_has_no_samples_and_bounded_columns():
    state = InputInference("custom", ["note"])
    for _ in range(1000):
        state.observe(["PRIVATE_STRING"])
    assert state.types == ["TEXT"]
    assert "PRIVATE_STRING" not in repr(state.__dict__)
    with pytest.raises(AnonymizerError, match="лимит"):
        InputInference("custom", ["field"] * 16385)


@pytest.mark.parametrize("sql_type,original", [("character(1)", "1"), ("text", "note_000001")])
def test_generic_text_retries_equal_candidate_and_repeats_stably(tmp_path, sql_type, original):
    results = []
    for index in range(2):
        with Store(tmp_path / f"read-{index}.sqlite", Model(), bypass_model=True) as store:
            name = "public", "custom"
            store.define(name, [{"name": "note", "sql_type": sql_type}])
            project = Project(tmp_path / "private.snpanon", "food")
            try:
                transformer = Transformer(store.model, project, store)
                first = transformer.transform(*name, store.tables[name]["fields"], [original])
                second = transformer.transform(*name, store.tables[name]["fields"], ["2" if sql_type != "text" else "OTHER"])
                assert first[0] != original and first != second
                if sql_type != "text":
                    assert len(first[0]) == len(second[0]) == 1
                results.append((first, second))
                rows = list(project.iter_mapping())
                assert len(rows) == 2 and all(old != new for _, old, new in rows)
            finally:
                project.finish(True)
    assert results[0] == results[1]


def test_generic_text_code_space_exhaustion_is_bounded_and_not_cached(tmp_path):
    project = Project(tmp_path / "private.snpanon", "food")
    try:
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        project.db.executemany("INSERT INTO mapping VALUES (?,?,?,?)", [
            ("generic:tiny", f"SYN_OLD_{index}", value, index) for index, value in enumerate(alphabet)])
        with pytest.raises(AnonymizerError, match="Недостаточно уникальных"):
            project.pseudonym("generic:tiny", "SYN_NEW", 1, label="text", avoid_original=True)
        assert ("generic:tiny", "SYN_NEW") not in project.cache
        assert project.db.execute("SELECT count(*) FROM mapping").fetchone()[0] == 36
    finally:
        project.finish(False)


def test_generic_json_avoids_equal_key_and_value_candidates(tmp_path):
    from anonymizer import json_data
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define(name, [{"name": "payload", "sql_type": "jsonb"}])
        project = Project(tmp_path / "private.snpanon", "food")
        try:
            value, = Transformer(store.model, project, store).transform(*name, store.tables[name]["fields"],
                                                                       ['{"A000001":"Атрибут 0001"}'])
            actual = json_data.load(value)
            assert "A000001" not in actual
            assert "Атрибут 0001" not in actual.values()
        finally:
            project.finish(False)


@pytest.mark.parametrize("value", ["PRIVATE_" + "x" * 100, '"' + "PRIVATE\n" * 12 + '"'])
def test_generic_csv_record_budget_precedes_parser_accumulation(tmp_path, monkeypatch, value):
    import anonymizer.csv_reader as reader
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("note\n" + value + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "MAX_FIELD", 48)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError, match="запись превышает") as error:
            parse_csv(source, store, delimiter=",")
        assert "PRIVATE" not in str(error.value)


def test_generic_csv_record_budget_resets_and_multiline_kept(tmp_path, monkeypatch):
    import anonymizer.csv_reader as reader
    source = csv_source(tmp_path, "custom", [["note"], ["line1\nline2"], ["line3\nline4"]])
    monkeypatch.setattr(reader, "MAX_FIELD", 18)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store, delimiter=",")
        assert list(store.rows(1)) == [["line1\nline2"], ["line3\nline4"]]


def test_generic_csv_cancellation_checked_for_each_physical_line(tmp_path):
    import io
    from anonymizer.csv_reader import _BoundedRecordLines
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise Cancelled("synthetic")
    reader = csv.reader(_BoundedRecordLines(io.StringIO('"line1\nline2\nline3"\n'), check, "custom"))
    with pytest.raises(Cancelled):
        next(reader)
    assert calls == 3


def test_generic_catalog_budget_is_global_and_drop_releases_it(tmp_path, monkeypatch):
    import anonymizer.store as module
    from anonymizer.generic_input import InputLimitError
    monkeypatch.setattr(module, "MAX_INPUT_METADATA", 3000)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        store.define_input(("public", "first"), ["note"])
        store.observe_input(("public", "first"), ["PRIVATE"])
        store.add_row(("public", "first"), ["note"], ["PRIVATE"])
        store.finish_input(("public", "first"))
        retained = store._input_metadata_used
        assert 0 < retained < 3000
        with pytest.raises(InputLimitError, match="общий лимит"):
            store.define_input(("public", "second"), ["note"])
        assert store._input_metadata_used == retained
        assert ("public", "second") not in store.tables
        assert ("public", "second") not in store._input_inference
        store.drop_table(("public", "first"), "synthetic")
        assert 0 < store._input_metadata_used < retained
        assert store._input_metadata_costs == {("public", "first"): store._input_metadata_used}
        store.define_input(("public", "third"), ["note"])
        assert 0 < store._input_metadata_used < 3000


def test_generic_catalog_finish_replacement_reserves_before_allocation(tmp_path, monkeypatch):
    import anonymizer.store as module
    from anonymizer.generic_input import InputLimitError
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, ["stamp"])
        original_budget = store._input_metadata_used
        monkeypatch.setattr(module, "MAX_INPUT_METADATA", original_budget)
        store.observe_input(name, ["2026-10-29T12:34:56+03:00"])
        store.add_row(name, ["stamp"], ["2026-10-29T12:34:56+03:00"])
        with pytest.raises(InputLimitError):
            store.finish_input(name)
        assert store._input_metadata_used == original_budget
        assert store.tables[name]["fields"][0]["type"] == "TEXT"
        store.drop_table(name, "synthetic")
        assert 0 < store._input_metadata_used < original_budget


def test_generic_invalid_ddl_type_releases_catalog_reservation(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError):
            store.define(("public", "custom"), [{"name": "note", "sql_type": "unknown_type"}])
        assert store._input_metadata_used == 0 and not store._input_metadata_costs


def test_csv_generic_global_catalog_limit_is_not_skipped(tmp_path, monkeypatch):
    import anonymizer.store as module
    from anonymizer.generic_input import InputLimitError
    source = csv_source(tmp_path, "first", [["note"], ["PRIVATE"]])
    csv_source(tmp_path, "second", [["note"], ["OTHER"]])
    monkeypatch.setattr(module, "MAX_INPUT_METADATA", 3000)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(InputLimitError):
            parse_csv(source, store, delimiter=",", skip_error_tables=True)
        assert not store.dropped_tables


def test_generic_catalog_discards_unaccounted_manifest_attributes(tmp_path, monkeypatch):
    import anonymizer.store as module
    monkeypatch.setattr(module, "MAX_INPUT_METADATA", 3000)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        store.define(("public", "custom"), [{"name": "note", "sql_type": "text", "nullable": True,
                     "description": "PRIVATE" * 100000, "unknown": {"payload": ["PRIVATE"] * 10000}}])
        field = store.tables[("public", "custom")]["fields"][0]
        assert "description" not in field and "unknown" not in field
        assert set(field) <= {"name", "type", "sql_type", "size", "nullable", "keys", "fk", "length"}
        assert 0 < store._input_metadata_used < 3000


@pytest.mark.parametrize("field", [
    {"name": "note", "type": {"payload": "PRIVATE"}},
    {"name": "note", "type": "TEXT", "size": [100]},
    {"name": "note", "sql_type": "text", "nullable": {"payload": "PRIVATE"}},
])
def test_generic_catalog_rejects_nonscalar_schema_attributes(tmp_path, field):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(AnonymizerError):
            store.define(("public", "custom"), [field])
        assert store._input_metadata_used == 0 and not store.tables


def test_generic_sql_skip_tombstones_share_catalog_byte_budget(tmp_path, monkeypatch):
    import anonymizer.store as module
    from anonymizer.generic_input import InputLimitError
    source = tmp_path / "source.sql"
    source.write_text("\n".join(
        f"CREATE TABLE public.custom{index}(note text); INSERT INTO public.custom{index} VALUES ('PRIVATE','EXTRA');"
        for index in range(500)), encoding="utf-8")
    monkeypatch.setattr(module, "MAX_INPUT_METADATA", 3000)
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        with pytest.raises(InputLimitError, match="общий лимит"):
            parse_sql(source, store, skip_error_tables=True)
        assert len(store.dropped_tables) < 500
        assert store._input_metadata_used <= 3000
        assert store._dropped_names
        assert all(name in store._input_metadata_costs for name in store._dropped_names)


def test_generic_repeated_drop_does_not_retain_another_reason(tmp_path):
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, [f"note{index}" for index in range(10)])
        active_cost = store._input_metadata_used
        store.drop_table(name, "synthetic first reason")
        dropped_cost = store._input_metadata_used
        assert 0 < dropped_cost < active_cost
        for _ in range(100):
            store.drop_table(name, "SYNTHETIC_OTHER" * 100)
            store.define_input(name, ["note"])
        assert store._input_metadata_used == dropped_cost
        assert store.dropped_tables == [("public.custom", "synthetic first reason")]
        assert store._dropped_names == {name}
        assert not store.tables and not store._input_inference


def test_generic_failed_tombstone_reservation_is_atomic(tmp_path, monkeypatch):
    import anonymizer.store as module
    from anonymizer.generic_input import InputLimitError
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        name = "public", "custom"
        store.define_input(name, ["note"])
        original_cost = store._input_metadata_used
        monkeypatch.setattr(module, "MAX_INPUT_METADATA", original_cost)
        with pytest.raises(InputLimitError):
            store.drop_table(name, "SYNTHETIC_REASON" * 100)
        assert store._input_metadata_used == original_cost
        assert name in store.tables and name in store._input_inference
        assert not store._dropped_names and not store.dropped_tables

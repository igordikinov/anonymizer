"""GLM-002: independent PK/UNIQUE key groups and per-group duplicate checks. Synthetic data only."""
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.validation import Validator


def groups_by_columns(report):
    return {tuple(entry["columns"]): entry for entry in report["validation"]["business_key_duplicates"]}


def test_independent_unique_groups_are_checked_separately(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.things(a text, b numeric, UNIQUE (a), UNIQUE (b));
      INSERT INTO public.things VALUES ('SYN_X',1),('SYN_X',2);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    groups = groups_by_columns(report)
    assert groups[("a",)]["extra_rows_before"] == groups[("a",)]["extra_rows_after"] == 1
    assert groups[("b",)]["extra_rows_before"] == groups[("b",)]["extra_rows_after"] == 0
    assert groups[("b",)]["unit"] == "rows"


def test_composite_unique_is_not_implied_by_single_ones(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.things(a text, b numeric, UNIQUE (a), UNIQUE (b), UNIQUE (a,b));
      INSERT INTO public.things VALUES ('SYN_X',1),('SYN_X',2),('SYN_Y',1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    groups = groups_by_columns(report)
    assert groups[("a",)]["extra_rows_before"] == 1
    assert groups[("b",)]["extra_rows_before"] == 1
    assert groups[("a", "b")]["extra_rows_before"] == 0  # the pairs themselves are unique


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE public.t(a numeric PRIMARY KEY, b text)",
    "CREATE TABLE public.t(a numeric, b text); ALTER TABLE ONLY public.t ADD CONSTRAINT t_pk PRIMARY KEY(a);",
], ids=["create", "alter"])
def test_pk_marks_column_not_null_so_null_rows_are_rejected(tmp_path, ddl):
    src = tmp_path / "source.sql"
    src.write_text(ddl + "; INSERT INTO public.t VALUES (NULL,'SYN_A'),(1,'SYN_C');", encoding="utf-8")
    with pytest.raises(AnonymizerError, match="обязательном поле"):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE public.t(a text UNIQUE NULLS NOT DISTINCT)",
    "CREATE TABLE public.t(a text, UNIQUE NULLS NOT DISTINCT (a))",
    "CREATE TABLE public.t(a text); ALTER TABLE public.t ADD CONSTRAINT uq UNIQUE NULLS NOT DISTINCT (a)",
], ids=["column-level", "table-level", "alter"])
def test_nulls_not_distinct_counts_null_duplicates(tmp_path, ddl):
    src = tmp_path / "source.sql"
    src.write_text(ddl + "; INSERT INTO public.t VALUES (NULL),(NULL),('SYN_A');", encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (group,) = report["validation"]["business_key_duplicates"]
    assert group["nulls"] == "not distinct"
    assert group["extra_rows_before"] == group["extra_rows_after"] == 1


def test_nulls_distinct_explicit_and_empty_string_is_a_value(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a text, UNIQUE NULLS DISTINCT (a));
      INSERT INTO public.t VALUES (NULL),(NULL),(''),(''),('SYN_A');
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (group,) = report["validation"]["business_key_duplicates"]
    assert group["nulls"] == "distinct"
    assert group["extra_rows_before"] == group["extra_rows_after"] == 1  # the '' pair; NULLs do not count


@pytest.mark.parametrize("nulls", ["DISTINCT", "NOT DISTINCT"])
@pytest.mark.parametrize("form", ["column", "create", "alter"])
def test_unique_nulls_position_preserves_semantics(tmp_path, nulls, form):
    if form == "column":
        ddl = f"CREATE TABLE public.t(a text UNIQUE NULLS {nulls});"
    elif form == "create":
        ddl = f"CREATE TABLE public.t(a text, CONSTRAINT uq UNIQUE NULLS {nulls} (a));"
    else:
        ddl = f"CREATE TABLE public.t(a text); ALTER TABLE ONLY public.t ADD CONSTRAINT uq UNIQUE NULLS {nulls} (a);"
    src = tmp_path / "source.sql"
    src.write_text(ddl, encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model()) as store:
        parse_sql(src, store)
        assert store.tables[("public", "t")]["key_groups"] == [
            {"columns": ["a"], "kind": "unique", "nulls": nulls.lower()}
        ]


@pytest.mark.parametrize("nulls", ["DISTINCT", "NOT DISTINCT"])
@pytest.mark.parametrize("form", ["create", "alter"])
def test_unique_nulls_postfix_is_rejected(tmp_path, nulls, form):
    constraint = f"UNIQUE (a) NULLS {nulls}"
    ddl = (f"CREATE TABLE public.t(a text, {constraint});" if form == "create" else
           f"CREATE TABLE public.t(a text); ALTER TABLE public.t ADD CONSTRAINT uq {constraint};")
    src = tmp_path / "source.sql"
    src.write_text(ddl, encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model()) as store:
        with pytest.raises(AnonymizerError, match="NULLS"):
            parse_sql(src, store)


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE public.t(a text UNIQUE NULLS NOT DISTINCT INCLUDE (b), b text);",
    "CREATE TABLE public.t(a text, b text, UNIQUE NULLS NOT DISTINCT (a) INCLUDE (b));",
    "CREATE TABLE public.t(a text, b text); ALTER TABLE public.t ADD CONSTRAINT uq UNIQUE NULLS DISTINCT (a) INCLUDE (b);",
    "CREATE TABLE public.t(a text UNIQUE NOT NULL NULLS NOT DISTINCT);",
    "CREATE TABLE public.t(a text UNIQUE NULLS DISTINCT NULLS NOT DISTINCT);",
    "CREATE TABLE public.t(a text, UNIQUE NULLS NOT (a));",
    "CREATE TABLE public.t(a text PRIMARY KEY NULLS DISTINCT);",
])
def test_unique_nulls_unsupported_or_malformed_forms_are_rejected(tmp_path, ddl):
    src = tmp_path / "source.sql"
    src.write_text(ddl, encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model()) as store:
        with pytest.raises(AnonymizerError):
            parse_sql(src, store)


def test_include_is_rejected_without_output(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a text, b numeric);
      INSERT INTO public.t VALUES ('SYN_A',1);
      ALTER TABLE ONLY public.t ADD CONSTRAINT t_uq UNIQUE (a) INCLUDE (b);
    """, encoding="utf-8")
    with pytest.raises(AnonymizerError) as error:
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert "SYN_" not in str(error.value)
    assert not (tmp_path / "out").exists()


def test_numeric_and_uuid_components_compare_canonically(tmp_path):
    upper = "11111111-1111-4111-8111-111111111111"
    lower = "11111111-1111-4111-8111-111111111111".lower()
    src = tmp_path / "source.sql"
    src.write_text(f"""
      CREATE TABLE public.t(n numeric UNIQUE, u uuid UNIQUE);
      INSERT INTO public.t VALUES (1.0,'{upper}'),(1.00,'{lower}');
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    groups = groups_by_columns(report)
    assert groups[("n",)]["extra_rows_before"] == groups[("n",)]["extra_rows_after"] == 1
    assert groups[("u",)]["extra_rows_before"] == groups[("u",)]["extra_rows_after"] == 1


def test_new_uniqueness_loss_blocks_publication(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a numeric UNIQUE, note text);
      INSERT INTO public.t VALUES (1,'SYN_A'),(2,'SYN_B'),(3,'SYN_C');
    """, encoding="utf-8")
    store = Store(tmp_path / "spool.sqlite", Model())
    parse_sql(src, store)
    store.flush()

    class Collide:
        def rule(self, table, field, schema=None):
            return "numeric-id" if field["name"] == "a" else "preserve"

    validator = Validator(store, Collide())
    seen = 0
    for name, table in store.tables.items():
        for old in store.rows(table["id"]):
            new = list(old)
            if seen >= 1:  # every row after the first collides on the unique value
                new[0] = "1"
            seen += 1
            validator.row(name, table, old, new)
    with pytest.raises(AnonymizerError, match="уникальность"):
        validator.finish()
    store.close()


def test_key_tuples_spool_to_sqlite_within_byte_budget(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a text UNIQUE, b text UNIQUE);
      INSERT INTO public.t VALUES """ + ",".join(f"('SYN{i:05d}','ALT{i:05d}')" for i in range(4000)) + """;
    """, encoding="utf-8")
    store = Store(tmp_path / "spool.sqlite", Model())
    parse_sql(src, store)
    store.flush()

    class PreserveAll:
        def rule(self, *args):
            return "preserve"

    validator = Validator(store, PreserveAll())
    for name, table in store.tables.items():
        for old in store.rows(table["id"]):
            validator.row(name, table, old, old)
            assert len(validator.pending_keys) < 5000 and validator.pending_bytes < 4 * 1024 * 1024
    validator.finish()
    stored = validator.db.execute("SELECT count(*) FROM keyvals").fetchone()[0]
    assert stored >= 16000  # 2 groups x 2 phases x 4000 rows live in SQLite
    store.close()


def test_manifest_roundtrip_preserves_key_groups_and_legacy_stays_valid(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a text UNIQUE, b text, UNIQUE NULLS NOT DISTINCT (a,b));
      INSERT INTO public.t VALUES ('SYN_A','SYN_B');
    """, encoding="utf-8")
    output = tmp_path / "out"
    run(Options(src, output, tmp_path / "synthetic.snpanon"))
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    (entry,) = manifest["tables"]
    assert entry["key_groups"] == [{"columns": ["a"], "kind": "unique", "nulls": "distinct"},
                                   {"columns": ["a", "b"], "kind": "unique", "nulls": "not distinct"}]
    store = Store(tmp_path / "roundtrip.sqlite", Model())
    parse_csv(output / "csv", store)
    store.flush()
    # model grain is rebuilt from the snapshot, DDL groups come from the manifest
    assert {g["kind"] for g in store.tables[(entry["schema"], "t")]["key_groups"]} >= {"unique"}
    store.close()
    legacy = {**manifest, "tables": [{k: v for k, v in entry.items() if k != "key_groups"}]}
    legacy_dir = tmp_path / "legacy"
    (legacy_dir / entry["file"]).parent.mkdir(parents=True)
    (legacy_dir / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    (legacy_dir / entry["file"]).write_bytes((output / "csv" / entry["file"]).read_bytes())
    store = Store(tmp_path / "legacy.sqlite", Model())
    parse_csv(legacy_dir, store)
    assert all(g["kind"] != "unique" for g in store.tables[(entry["schema"], "t")]["key_groups"])
    store.close()


@pytest.mark.parametrize("patch", [
    {"columns": "a"}, {"columns": []}, {"columns": ["a", "a"]},
    {"columns": ["nope"]}, {"kind": "primary"}, {"kind": 7},
    {"columns": ["a"], "kind": "unique", "nulls": "maybe"},
])
def test_hostile_manifest_key_groups_are_rejected(tmp_path, patch):
    fields = [{"name": "a", "sql_type": "text", "nullable": True}]
    group = {"columns": ["a"], "kind": "unique", **patch}
    store = Store(tmp_path / "read.sqlite", Model())
    with pytest.raises(AnonymizerError):
        store.define(("public", "t"), fields, (), [group])
    store.close()


@pytest.mark.parametrize("foreign_keys,key_groups", [
    (None, ()), ([{"columns": ["a"], "target_schema": "public", "target_table": "m", "target_columns": ["x"]}], None),
    ([{"columns": [["a"]], "target_columns": ["x"]}], ()), (({"columns": None, "target_columns": ["x"]},), ()),
])
def test_hostile_manifest_fk_groups_reject_none_and_unhashable(tmp_path, foreign_keys, key_groups):
    fields = [{"name": "a", "sql_type": "text", "nullable": True}]
    store = Store(tmp_path / "read.sqlite", Model())
    with pytest.raises(AnonymizerError):
        store.define(("public", "t"), fields, foreign_keys, key_groups)
    store.close()


def test_column_level_unique_and_reference_coexist(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.master(x numeric PRIMARY KEY);
      CREATE TABLE public.child(code numeric UNIQUE REFERENCES public.master(x));
      INSERT INTO public.master VALUES (1),(2);
      INSERT INTO public.child VALUES (2.0);
    """, encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(src, output, tmp_path / "synthetic.snpanon"))
    (fk,) = report["validation"]["foreign_keys"]
    assert fk["missing_before"] == fk["missing_after"] == 0  # the reference survived
    (group,) = [entry for entry in report["validation"]["business_key_duplicates"] if entry["columns"] == ["code"]]
    assert group["kind"] == "unique"


def test_primary_key_rejects_nulls_tail(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a numeric PRIMARY KEY NULLS NOT DISTINCT);
      INSERT INTO public.t VALUES (1);
    """, encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_model_grain_reported_with_explicit_columns(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    with (src / "item_202610130001.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        import csv
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["sys_id", "item", "proddescr", "baseuom"])
        writer.writerow(["1", "000001", "SYN_PRODUCT", "KG"])
        writer.writerow(["2", "000001", "SYN_PRODUCT2", "KG"])
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (group,) = report["validation"]["business_key_duplicates"]
    assert group["kind"] == "model_bk" and group["columns"] == ["item"]
    assert group["extra_rows_before"] == group["extra_rows_after"] == 1


def test_model_grain_excludes_ddl_only_flags_and_pk_groups_use_ddl(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.orders_h(ord_num numeric);
      INSERT INTO public.orders_h VALUES (900001);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    groups = report["validation"]["business_key_duplicates"]
    assert [entry["kind"] for entry in groups] == ["model_bk"]  # model BK only; no DDL keys in this input
    assert groups[0]["columns"] == ["ord_num"]


def test_sys_id_pk_group_is_skipped_and_does_not_crash(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(sys_id bigint PRIMARY KEY, note text);
      INSERT INTO public.t VALUES (1,'SYN_A'),(2,'SYN_B');
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert all("sys_id" not in entry["columns"] for entry in report["validation"]["business_key_duplicates"])


def test_alter_restates_create_group_only_once(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.t(a text, b numeric, UNIQUE (a));
      INSERT INTO public.t VALUES ('SYN_X',1),('SYN_X',2);
      ALTER TABLE ONLY public.t ADD CONSTRAINT t_uq UNIQUE (a);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    groups = groups_by_columns(report)
    assert len(groups) == 1 and groups[("a",)]["extra_rows_before"] == 1

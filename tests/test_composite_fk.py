"""GLM-001: composite FOREIGN KEY groups and full-tuple checks. Synthetic data only."""
import json

import pytest

from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store
from anonymizer.validation import Validator


def composite_sql(alter=False):
    ddl = "ALTER TABLE ONLY public.child ADD CONSTRAINT child_fk FOREIGN KEY (x,y) REFERENCES public.parent(a,b) NOT VALID;" if alter else ""
    return f"""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric{"" if alter else ", FOREIGN KEY (x,y) REFERENCES public.parent(a,b)"});
      INSERT INTO public.parent VALUES ('SYN_A',1),('SYN_B',2);
      INSERT INTO public.child VALUES ('SYN_A',2);
      {ddl}
    """


@pytest.mark.parametrize("alter", [False, True], ids=["create", "alter"])
def test_catalog_keeps_ordered_group_for_create_and_alter(tmp_path, alter):
    src = tmp_path / "source.sql"
    src.write_text(composite_sql(alter), encoding="utf-8")
    store = Store(tmp_path / "read.sqlite", Model())
    parse_sql(src, store)
    store.flush()
    assert store.tables[("public", "child")]["fk_groups"] == [
        {"columns": ["x", "y"], "target_schema": "public", "target_table": "parent",
         "target_columns": ["a", "b"], "match": "simple"}]
    store.close()


@pytest.mark.parametrize("alter", [False, True], ids=["create", "alter"])
def test_broken_tuple_is_counted_before_and_after(tmp_path, alter):
    src = tmp_path / "source.sql"
    src.write_text(composite_sql(alter), encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["columns"] == ["x", "y"] and "column" not in entry
    assert entry["missing_before"] == 1 and entry["missing_after"] == 1
    assert entry["unit"] == "distinct tuples"


def test_consistent_tuple_reports_zero(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES ('SYN_A',1),('SYN_B',2);
      INSERT INTO public.child VALUES ('SYN_B',2),('SYN_A',1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["missing_before"] == entry["missing_after"] == 0


def test_null_and_empty_components_are_excluded(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES ('SYN_A',1);
      INSERT INTO public.child VALUES (NULL,1),('SYN_Z',NULL),('',1),('SYN_A',NULL);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["missing_before"] == entry["missing_after"] == 0


def test_missing_master_table_is_unverified_not_clean(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["status"] == "target_not_in_input"
    assert "missing_before" not in entry


def test_filtered_out_master_schema_is_not_leaked(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE client2.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES client2.parent(a,b));
      INSERT INTO client2.parent VALUES ('SYN_A',1);
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(src, output, tmp_path / "synthetic.snpanon", schemas=("public",)))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["status"] == "target_not_in_input"
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    assert all("foreign_keys" not in table for table in manifest["tables"])
    for path in output.rglob("*"):
        if path.is_file():
            assert "client2" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("match", ["FULL", "PARTIAL"])
def test_match_full_partial_is_rejected_before_output(tmp_path, match):
    src = tmp_path / "source.sql"
    src.write_text(f"""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric,
        FOREIGN KEY (x,y) REFERENCES public.parent(a,b) MATCH {match});
      INSERT INTO public.parent VALUES ('SYN_A',1);
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    with pytest.raises(AnonymizerError) as error:
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert "SYN_" not in str(error.value)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("side", ["target", "source"], ids=["target-sys_id", "source-sys_id"])
def test_composite_reference_through_sys_id_is_not_checked(tmp_path, side):
    columns, targets = (("x", "y"), ("a", "sys_id")) if side == "target" else (("sys_id", "x"), ("sys_id", "a"))
    src = tmp_path / "source.sql"
    src.write_text(f"""
      CREATE TABLE public.parent(a text, sys_id bigint, b numeric);
      CREATE TABLE public.child(sys_id bigint, x text, y numeric,
        FOREIGN KEY ({",".join(columns)}) REFERENCES public.parent({",".join(targets)}));
      INSERT INTO public.parent VALUES ('SYN_A', 1, 1);
      INSERT INTO public.child VALUES (1, 'SYN_A', 1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert report["validation"]["foreign_keys"] == []


def test_numeric_and_uuid_components_compare_canonically(tmp_path):
    upper = "11111111-1111-4111-8111-111111111111"
    lower = "11111111-1111-4111-8111-111111111111".lower()
    src = tmp_path / "source.sql"
    src.write_text(f"""
      CREATE TABLE public.parent(a uuid, b numeric);
      CREATE TABLE public.child(x uuid, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES ('{upper}',123);
      INSERT INTO public.child VALUES ('{lower}',123.0);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["missing_before"] == entry["missing_after"] == 0


def test_cross_schema_and_self_reference_groups(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE client2.parent(a text, b numeric);
      CREATE TABLE public.tree(id numeric, pid numeric, kind text,
        FOREIGN KEY (pid,kind) REFERENCES public.tree(id,kind));
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES client2.parent(a,b));
      INSERT INTO client2.parent VALUES ('SYN_A',1);
      INSERT INTO public.tree VALUES (1,NULL,'K'),(2,1,'K'),(3,2,'X');
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    by_columns = {tuple(entry.get("columns", ())): entry for entry in report["validation"]["foreign_keys"]}
    assert by_columns[("x", "y")]["missing_before"] == by_columns[("x", "y")]["missing_after"] == 0
    assert by_columns[("pid", "kind")]["missing_before"] == 1  # (2,'X') has no master tuple
    assert by_columns[("pid", "kind")]["missing_after"] == 1


def test_new_tuple_loss_blocks_publication(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES ('SYN_A',1);
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    store = Store(tmp_path / "spool.sqlite", Model())
    parse_sql(src, store)
    store.flush()

    class RenumberIds:
        def rule(self, table, field, schema=None):
            return "numeric-id" if field["type"] in {"NUMBER", "INTEGER"} else "preserve"

    validator = Validator(store, RenumberIds())
    for name, table in store.tables.items():
        for old in store.rows(table["id"]):
            broken = list(old)
            if name[1] == "child":
                broken[-1] = "9999"  # tuple no longer present in master
            validator.row(name, table, old, broken)
    with pytest.raises(AnonymizerError, match="ссылочная целостность"):
        validator.finish()
    store.close()


def test_tuple_keys_spool_to_sqlite_within_byte_budget(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES """ + ",".join(f"('SYN{i:05d}',{i})" for i in range(3000)) + """;
      INSERT INTO public.child VALUES """ + ",".join(f"('SYN{i:05d}',{i})" for i in range(3000)) + """;
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
            assert len(validator.pending_fk) < 5000 and validator.pending_bytes < 4 * 1024 * 1024
    validator.finish()
    stored = validator.db.execute("SELECT count(*) FROM fkvals").fetchone()[0]
    assert stored >= 6000  # child+master sides live in SQLite, not in a Python set
    store.close()


def test_manifest_roundtrip_preserves_groups_and_old_manifest_stays_valid(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text, b numeric);
      CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES ('SYN_A',1);
      INSERT INTO public.child VALUES ('SYN_A',1);
    """, encoding="utf-8")
    output = tmp_path / "out"
    run(Options(src, output, tmp_path / "synthetic.snpanon"))
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    tables = {table["table"]: table for table in manifest["tables"]}
    (group,) = tables["child"]["foreign_keys"]
    assert group["columns"] == ["x", "y"] and group["target_table"] == "parent"
    assert group["target_columns"] == ["a", "b"]
    assert group["target_schema"] == tables["parent"]["schema"] == tables["child"]["schema"]
    assert "foreign_keys" not in tables["parent"]
    store = Store(tmp_path / "roundtrip.sqlite", Model())
    parse_csv(output / "csv", store)
    store.flush()
    assert store.tables[(tables["child"]["schema"], "child")]["fk_groups"][0]["columns"] == ["x", "y"]
    store.close()
    # Old manifest v1 without the optional key keeps working (no group guessing).
    legacy = {**manifest, "tables": [{k: v for k, v in table.items() if k != "foreign_keys"} for table in manifest["tables"]]}
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    for table in legacy["tables"]:
        (legacy_dir / table["file"]).parent.mkdir(exist_ok=True)
        (legacy_dir / table["file"]).write_bytes((output / "csv" / table["file"]).read_bytes())
    store = Store(tmp_path / "legacy.sqlite", Model())
    parse_csv(legacy_dir, store)
    assert all(not data["fk_groups"] for data in store.tables.values())
    store.close()


def test_foreign_key_without_references_is_rejected_safely(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("CREATE TABLE public.t(a int, b int, FOREIGN KEY (a,b));", encoding="utf-8")
    with pytest.raises(AnonymizerError, match="REFERENCES"):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_dump_does_not_add_source_constraints_and_single_fk_shape_kept(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text PRIMARY KEY);
      CREATE TABLE public.child(x text REFERENCES public.parent(a),
                                y numeric, FOREIGN KEY (y) REFERENCES public.qty(n));
      CREATE TABLE public.qty(n numeric);
      INSERT INTO public.parent VALUES ('SYN_A');
      INSERT INTO public.child VALUES ('SYN_A',1);
      INSERT INTO public.qty VALUES (1);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    dump = (tmp_path / "out/dump.sql").read_text(encoding="utf-8")
    assert "FOREIGN KEY" not in dump and "CONSTRAINT" not in dump
    singles = [entry for entry in report["validation"]["foreign_keys"] if "column" in entry]
    assert {entry["column"] for entry in singles} == {"x", "y"}
    assert all(entry["unit"] == "distinct values" for entry in singles)


def test_grouped_column_ignores_its_own_sys_id_reference_but_checks_business_group(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(sys_id bigint, a numeric, b numeric);
      CREATE TABLE public.child(x bigint REFERENCES public.parent(sys_id), y numeric,
        FOREIGN KEY (x,y) REFERENCES public.parent(a,b));
      INSERT INTO public.parent VALUES (1, 10, 100);
      INSERT INTO public.child VALUES (1, 10);
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    entry, = report["validation"]["foreign_keys"]
    assert entry["columns"] == ["x", "y"]
    assert entry["missing_before"] == entry["missing_after"] == 1


def test_repeated_identical_group_is_not_duplicated(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text(composite_sql(alter=True).replace(
        "CREATE TABLE public.child(x text, y numeric);",
        "CREATE TABLE public.child(x text, y numeric, FOREIGN KEY (x,y) REFERENCES public.parent(a,b));"), encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    (entry,) = report["validation"]["foreign_keys"]
    assert entry["columns"] == ["x", "y"] and entry["missing_before"] == 1


@pytest.mark.parametrize("patch", [
    {"columns": "x"}, {"columns": ["x"], "target_columns": ["a", "b"]},
    {"columns": ["nope"], "target_columns": ["a"]},
    {"columns": ["x", "x"], "target_columns": ["a", "b"]},
    {"target_schema": "bad schema"}, {"target_table": 7},
    {"columns": ["x"], "target_columns": ["a"], "match": "full"},
])
def test_hostile_manifest_groups_are_rejected(tmp_path, patch):
    fields = [{"name": "x", "sql_type": "text", "nullable": True}, {"name": "y", "sql_type": "numeric", "nullable": True}]
    group = {"columns": ["x"], "target_schema": "public", "target_table": "parent", "target_columns": ["a"], **patch}
    store = Store(tmp_path / "read.sqlite", Model())
    with pytest.raises(AnonymizerError):
        store.define(("public", "child"), fields, [group])
    store.close()

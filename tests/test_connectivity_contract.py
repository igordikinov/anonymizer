"""Connectivity profile schema, shared identity allocation and durable bindings."""
import copy
import json
import sqlite3
from decimal import Decimal

import pytest

from anonymizer import connectivity
from anonymizer.connectivity import ContractError, compile_contract, load_profile
from anonymizer.mapping import Project, Transformer
from anonymizer.metadata import Model
from anonymizer.metadata import physical_type
from anonymizer.store import Store
from anonymizer.csv_reader import parse_csv
from anonymizer import canonical
from anonymizer.errors import IntegrityError
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql


def profile(members=None, kind="auto"):
    return {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-v1", "version": "1",
            "namespace_rule": "source_schema", "domains": [{"id": "LOCATION", "kind": kind,
                "members": members or [{"table": "a", "column": "code"}, {"table": "b", "column": "code"}],
                "prefix": "LOC"}], "relations": [], "keys": [], "required_checks": ["domain:LOCATION"]}


def define(store, table="a", values=("SYN_A", "SYN_B"), sql="text", schema="public"):
    name = schema, table
    store.define(name, [{"name": "code", "sql_type": sql}, {"name": "qty", "sql_type": "numeric"}])
    for value in values:
        store.add_row(name, ["code", "qty"], [value, "1234567890.123456789"])
    return name


def transform_all(store, contract, path):
    result = {}
    with_project = Project(path, "food", contract=contract)
    try:
        with_project.prepare_domains(store)
        transformer = Transformer(store.model, with_project, store, contract=contract)
        for name, table in store.tables.items():
            result[name] = [transformer.transform(*name, table["fields"], row) for row in store.rows(table["id"])]
        with_project.finish(True)
    except Exception:
        with_project.finish(False)
        raise
    return result


def test_builtin_has_agreed_supplier_client_transport_and_period_contracts():
    p = load_profile("builtin:optimizer-v2")
    domains = {d["id"]: d for d in p["domains"]}
    assert len(domains) == 8 and len(p["keys"]) == 7
    assert "RESOURCE" not in domains
    for identifier, table, prefix in (("RESOURCE_PROD", "optimizer_production", "resource_prod"),
                                      ("RESOURCE_STOCK", "optimizer_storage", "resource_stock"),
                                      ("RESOURCE_TRANSP", "optimizer_transportation", "resource_transp")):
        assert domains[identifier]["members"] == [{"table": table, "column": "resource"}]
        assert domains[identifier]["prefix"] == prefix
    assert len(p["relations"]) == 15
    assert {"table": "optimizer_procurement", "column": "supplier"} in domains["LOCATION"]["members"]
    assert domains["CLIENT"]["members"] == [{"table": "optimizer_demand", "column": "client"}]
    assert domains["TRANSPORT_TYPE"]["members"] == [{"table": "optimizer_transportation", "column": "transport_type"}]
    assert sum(r["kind"] == "fk" for r in p["relations"]) == 6
    assert all(r["target"]["members"][0]["table"] == "optimizer_periods" for r in p["relations"] if r["kind"] == "fk")


def test_profile_shared_locations_preserve_numbers_and_saved_project(tmp_path):
    project = tmp_path / "private.snpanon"
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a")
        define(store, "b", values=("SYN_B", "SYN_A"))
        contract = compile_contract(store, profile())
        result = transform_all(store, contract, project)
        assert result[("public", "a")][0][0] == result[("public", "b")][1][0]
        assert result[("public", "a")][1][0] == result[("public", "b")][0][0]
        assert all(row[1] == "1234567890.123456789" for rows in result.values() for row in rows)
        assert result == transform_all(store, compile_contract(store, profile()), project)
    with sqlite3.connect(project) as db:
        assert db.execute("SELECT value FROM settings WHERE name='format'").fetchone()[0] == "snp-anonymizer-project-v2"


def test_numeric_equivalence_uses_common_integer_representation(tmp_path):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("1", "1.0", "2"), sql="numeric(5,2)")
        define(store, "b", values=("1.00", "2"), sql="smallint")
        contract = compile_contract(store, profile())
        rows = transform_all(store, contract, tmp_path / "private.snpanon")
        assert rows[("public", "a")][0][0] == rows[("public", "a")][1][0] == rows[("public", "b")][0][0]
        assert rows[("public", "a")][2][0] == rows[("public", "b")][1][0]
        assert all(Decimal(row[0]) == Decimal(row[0]).to_integral_value() for rs in rows.values() for row in rs)


def test_fractional_numeric_encoding_respects_precision_scale_without_float(tmp_path):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("0.001", "0.002", "0.003"), sql="numeric(2,3)")
        define(store, "b", values=("0.003",), sql="numeric(4,4)")
        contract = compile_contract(store, profile())
        assert contract.domains[0]["quantum"] == "0.001"
        rows = transform_all(store, contract, tmp_path / "private.snpanon")
        assert len({row[0] for row in rows[("public", "a")]}) == 3
        assert all(Decimal(row[0]) < Decimal("0.1") for rs in rows.values() for row in rs)


def test_declared_null_only_numeric_member_still_limits_code_capacity(tmp_path):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=tuple(str(i) for i in range(20)), sql="numeric(8,0)")
        define(store, "b", values=(None,), sql="numeric(1,0)")
        c = compile_contract(store, profile())
        assert c.domains[0]["capacity"] == 19
        with pytest.raises(ContractError, match="DOMAIN_CAPACITY_EXHAUSTED"):
            transform_all(store, c, tmp_path / "private.snpanon")


def test_signed_numeric_space_and_asymmetric_integer_minimum_are_available(tmp_path):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=tuple(str(i) for i in range(-9, 10)), sql="numeric(1,0)")
        c = compile_contract(store, profile([{"table": "a", "column": "code"}]))
        result = transform_all(store, c, tmp_path / "private.snpanon")
        assert {Decimal(row[0]) for row in result[("public", "a")]} == {Decimal(i) for i in range(-9, 10)}
    with Store(tmp_path / "smallint.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("1",), sql="smallint")
        c = compile_contract(store, profile([{"table": "a", "column": "code"}]))
        domain = c.domains[0]
        assert domain["capacity"] == 65536
        project = Project(tmp_path / "smallint.snpanon", "food", c)
        try:
            assert project._domain_candidate(domain, "1", 65536) == "-32768"
        finally:
            project.finish(False)


@pytest.mark.parametrize("physical", ["real", "float", "float4", "double precision", "float8"])
def test_profile_float_identifiers_fail_closed_and_float_relations_are_unsupported(tmp_path, physical):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("1.00000001",), sql=physical)
        with pytest.raises(ContractError, match="CHECK_UNSUPPORTED"):
            compile_contract(store, profile([{"table": "a", "column": "code"}]))
        p = profile([])
        p["domains"] = []
        p["required_checks"] = ["floating-join"]
        side = {"members": [{"table": "a", "columns": ["code"]}]}
        p["relations"] = [{"id": "floating-join", "source": side, "target": side}]
        c = compile_contract(store, p)
        assert c.relations[0]["status"] == "UNSUPPORTED"


@pytest.mark.parametrize("physical", ["char", "character"])
def test_bare_char_has_length_one_and_saved_oversize_code_is_rejected(tmp_path, physical):
    assert physical_type(physical) == (physical, "TEXT", 1)
    project_path = tmp_path / "private.snpanon"
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("A", "B"), sql=physical)
        c = compile_contract(store, profile([{"table": "a", "column": "code"}]))
        assert c.domains[0]["length"] == 1 and c.domains[0]["equality"] == "pg_char"
        rows = transform_all(store, c, project_path)
        assert all(len(row[0]) == 1 for row in rows[("public", "a")])
        db = sqlite3.connect(project_path)
        try:
            db.execute("UPDATE mapping SET new='SYN_OVERSIZED' WHERE domain=? AND old='A'", (c.domains[0]["mapping_domain"],))
            db.commit()
        finally:
            db.close()
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            Project(project_path, "food", c)
    # Constructor exceptions release the SQLite handle on Windows.
    moved = project_path.with_suffix(".moved")
    project_path.rename(moved)
    assert moved.exists()


@pytest.mark.parametrize("physical", ["char", "character"])
def test_bare_char_canonical_equality_trims_only_ascii_spaces(physical):
    field = {"name": "code", "sql_type": physical, "type": "TEXT"}
    assert canonical.value(field, "A ") == canonical.value(field, "A")
    assert canonical.value(field, "A\t") == "A\t"


@pytest.mark.parametrize("sql_type", ["json", "jsonb", "time", "timestamp without time zone", "timestamp with time zone"])
def test_unsupported_physical_relation_semantics_never_publish_required_dataset(tmp_path, sql_type):
    source = tmp_path / "source.sql"
    if sql_type in {"json", "jsonb"}:
        left, right = "'{\"a\":1,\"b\":2}'", "'{\"b\":2,\"a\":1}'"
    elif sql_type == "time":
        left, right = "'12:00:00'", "'12:00:00.000'"
    else:
        left, right = "'2026-10-30T12:00:00+00:00'", "'2026-10-30T13:00:00+01:00'"
    source.write_text(f"CREATE TABLE public.a(code {sql_type}); CREATE TABLE public.b(code {sql_type});"
                      f"INSERT INTO public.a VALUES({left}); INSERT INTO public.b VALUES({right});", encoding="utf-8")
    p = profile()
    p["domains"] = []
    p["relations"] = [{"id": "typed-relation", "kind": "fk",
                       "source": {"members": [{"table": "a", "columns": ["code"]}]},
                       "target": {"members": [{"table": "b", "columns": ["code"]}]}}]
    p["required_checks"] = ["typed-relation"]
    output = tmp_path / "out"
    with pytest.raises(IntegrityError):
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True,
                    connectivity_profile=p, require_integrity=True))
    assert {file.name for file in output.iterdir()} == {"report.json"}
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert any(c["status"] == "UNSUPPORTED" for c in report["checks"])
    assert not report["dataset_published"]


def test_collation_is_preserved_as_unsupported_contract_metadata(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text('CREATE TABLE public.a(code text COLLATE "C"); INSERT INTO public.a VALUES(\'SYN_VALUE\');', encoding="utf-8")
    with Store(tmp_path / "read.sqlite", Model(), bypass_model=True) as store:
        parse_sql(source, store)
        assert store.tables[("public", "a")]["fields"][0]["collation_declared"]
        with pytest.raises(ContractError, match="CHECK_UNSUPPORTED"):
            compile_contract(store, profile([{"table": "a", "column": "code"}]))
        p = profile()
        p["domains"] = []
        p["keys"] = [{"id": "collated-key", "table": "a", "columns": ["code"], "kind": "unique"}]
        p["required_checks"] = ["collated-key"]
        c = compile_contract(store, p)
        assert c.keys[0]["status"] == "UNSUPPORTED"


def test_date_equality_is_canonical_but_original_date_lexemes_are_preserved(tmp_path):
    field = {"name": "day", "type": "DATE", "sql_type": "date"}
    assert canonical.value(field, "20261030") == canonical.value(field, "2026-10-30")
    assert canonical.value(field, "infinity") == "infinity"
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.a(code date); CREATE TABLE public.b(code date);"
                      "INSERT INTO public.a VALUES('20261030'); INSERT INTO public.b VALUES('2026-10-30');", encoding="utf-8")
    p = profile()
    p["domains"] = []
    p["relations"] = [{"id": "dates", "kind": "fk", "source": {"members": [{"table": "a", "columns": ["code"]}]},
                       "target": {"members": [{"table": "b", "columns": ["code"]}]}}]
    p["required_checks"] = ["dates"]
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True,
                         connectivity_profile=p, require_integrity=True))
    assert report["integrity_status"] == "PASSED"
    assert "20261030" in (output / "dump.sql").read_text(encoding="utf-8")


def test_only_inference_null_only_text_can_be_ignored_for_auto_kind(tmp_path):
    with Store(tmp_path / "declared.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("1",), sql="numeric")
        define(store, "b", values=(None,), sql="text")
        store.tables[("public", "b")]["csv_empty_null_columns"] = {"code"}
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            compile_contract(store, profile())
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.csv").write_text("code,qty\n1,42\n", encoding="utf-8")
    (source / "b.csv").write_text("code,qty\n\\N,42\n", encoding="utf-8")
    with Store(tmp_path / "inferred.sqlite", Model(), bypass_model=True) as store:
        parse_csv(source, store, delimiter=",")
        c = compile_contract(store, profile())
        assert c.domains[0]["kind"] == "NUMBER"
        result = transform_all(store, c, tmp_path / "private.snpanon")
        assert result[("public", "b")][0][0] is None


def test_empty_text_is_an_identifier_but_null_is_not(tmp_path):
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=("", None, "001", "1"))
        define(store, "b", values=("", "001"))
        contract = compile_contract(store, profile())
        result = transform_all(store, contract, tmp_path / "private.snpanon")
        a, b = result[("public", "a")], result[("public", "b")]
        assert a[0][0] != "" and a[0][0] == b[0][0]
        assert a[1][0] is None and a[2][0] != a[3][0]


@pytest.mark.parametrize("count", [36, 37])
def test_one_character_code_space_uses_all_slots_or_fails_atomically(tmp_path, count):
    values = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" + ("a" if count == 37 else ""))
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a", values=values, sql="varchar(1)")
        contract = compile_contract(store, profile([{"table": "a", "column": "code"}]))
        if count == 37:
            with pytest.raises(ContractError, match="DOMAIN_CAPACITY_EXHAUSTED"):
                transform_all(store, contract, tmp_path / "private.snpanon")
        else:
            result = transform_all(store, contract, tmp_path / "private.snpanon")
            assert len({row[0] for row in result[("public", "a")]}) == 36
            assert all(row[0] != original for row, original in zip(result[("public", "a")], values))


def test_namespace_extension_does_not_change_existing_codes(tmp_path):
    project = tmp_path / "private.snpanon"
    p = profile([{"table": "a", "column": "code"}])
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, schema="schema_b")
        first = transform_all(store, compile_contract(store, p), project)
        define(store, schema="schema_a")
        second = transform_all(store, compile_contract(store, p), project)
        assert first[("schema_b", "a")] == second[("schema_b", "a")]
        domains = compile_contract(store, p).domains
        assert domains[0]["mapping_domain"] != domains[1]["mapping_domain"]


def test_shared_namespace_is_explicit_and_shares_maps(tmp_path):
    p = profile([{"table": "a", "column": "code"}])
    p["namespace_rule"] = "shared"
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, schema="schema_a")
        define(store, schema="schema_b")
        c = compile_contract(store, p)
        assert len(c.domains) == 1
        result = transform_all(store, c, tmp_path / "private.snpanon")
        assert result[("schema_a", "a")] == result[("schema_b", "a")]


def test_unselected_schema_does_not_expand_required_profile_instances(tmp_path):
    p = profile([{"table": "a", "column": "code"}])
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, schema="selected")
        define(store, schema="excluded")
        store.tables.pop(("excluded", "a"))  # --schema filtering keeps declarations for coverage
        c = compile_contract(store, p)
        assert len(c.domains) == 1 and c.domains[0]["namespace"] == "selected"
        assert len(c.required_checks) == 1


def test_absent_fk_source_does_not_merge_two_present_domains(tmp_path):
    p = profile([{"table": "a", "column": "code"}])
    p["domains"].append({"id": "PRODUCT", "members": [{"table": "b", "column": "code"}]})
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "a")
        define(store, "b")
        store.define(("excluded", "ghost"), [{"name": "code", "sql_type": "text"}], fk_groups=[
            {"columns": ["code"], "target_schema": "public", "target_table": table, "target_columns": ["code"]}
            for table in ("a", "b")])
        store.tables.pop(("excluded", "ghost"))
        c = compile_contract(store, p)
        assert len(c.domains) == 2
        assert all(r["status"] == "NOT_CHECKED" and not r["required"] for r in c.relations)


def test_nonempty_legacy_and_changed_profile_require_new_project(tmp_path):
    path = tmp_path / "private.snpanon"
    old = Project(path, "food")
    old.pseudonym("old", "SYN_VALUE")
    old.finish(True)
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store)
        c = compile_contract(store, profile([{"table": "a", "column": "code"}]))
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            Project(path, "food", c)
        fresh = tmp_path / "fresh.snpanon"
        transform_all(store, c, fresh)
        modified = copy.deepcopy(profile([{"table": "a", "column": "code"}]))
        modified["domains"][0]["prefix"] = "SITE"
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            Project(fresh, "food", compile_contract(store, modified))
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            Project(fresh, "food")


def test_ddl_cannot_unify_two_explicit_domains(tmp_path):
    p = profile([{"table": "a", "column": "code"}])
    p["domains"].append({"id": "PRODUCT", "members": [{"table": "b", "column": "code"}]})
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store, "b")
        store.define(("public", "a"), [{"name": "code", "sql_type": "text"}], fk_groups=[
            {"columns": ["code"], "target_schema": "public", "target_table": "b", "target_columns": ["code"]}])
        store.add_row(("public", "a"), ["code"], ["SYN_A"])
        with pytest.raises(ContractError, match="CONTRACT_CONFLICT"):
            compile_contract(store, p)


@pytest.mark.parametrize("bad", [[], {"format": "bad"}, {"format": "snp-anonymizer-connectivity-v1", "secret": "SYN_PRIVATE"}])
def test_bad_profile_is_safe(bad):
    with pytest.raises(ContractError) as error:
        load_profile(bad)
    assert "SYN_PRIVATE" not in str(error.value)


def test_profile_and_compiled_metadata_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(connectivity, "MAX_PROFILE_BYTES", 8)
    with pytest.raises(ContractError, match="1 МиБ"):
        load_profile(profile())
    monkeypatch.setattr(connectivity, "MAX_PROFILE_BYTES", 1024 * 1024)
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store)
        monkeypatch.setattr(connectivity, "MAX_CONTRACT_BYTES", 8)
        with pytest.raises(ContractError, match="CHECK_INCOMPLETE"):
            compile_contract(store, profile())


def test_missing_physical_member_and_unsupported_predicate_have_no_fake_pass(tmp_path):
    p = profile()
    p["relations"] = [{"id": "date-rule", "source": {"members": [{"table": "a", "columns": ["code"]}]},
                       "target": {"members": [{"table": "a", "columns": ["code"]}]}, "predicate": "arrival_offset"}]
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        define(store)
        c = compile_contract(store, p)
        assert c.domains[0]["status"] == "NOT_CHECKED"
        assert next(r for r in c.relations if r["id"].startswith("date-rule"))["status"] == "UNSUPPORTED"

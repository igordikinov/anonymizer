"""Optimizer resource kinds never share identities or text-code namespaces."""
from contextlib import contextmanager
import copy
import json
import re
import sqlite3
from uuid import uuid4

import pytest

from anonymizer.connectivity import Contract, ContractError, compile_contract, load_profile
from anonymizer.errors import IntegrityError
from anonymizer.mapping import Project, Transformer
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.store import Store


ROLES = {
    "optimizer_production": ("RESOURCE_PROD", "resource_prod"),
    "optimizer_storage": ("RESOURCE_STOCK", "resource_stock"),
    "optimizer_transportation": ("RESOURCE_TRANSP", "resource_transp"),
}
OLD_HASH = "1d82586f2ce01a65c5f1649152fdd407f30e162dccb8ec6012bf4f32eb070867"
OLD_KEY = "connectivity:" + OLD_HASH + ":04630553c1f17f8e606bc0ce4eb827cf1759308d65a392c5502d4ed7eca8c71f"


@contextmanager
def resource_store(tmp_path, *, schemas=("public",), types=None, additions=None, foreign_keys=None):
    types, additions, foreign_keys = types or {}, additions or {}, foreign_keys or {}
    with Store(tmp_path / f"input-{uuid4().hex}.sqlite", Model(), bypass_model=True) as store:
        for schema in schemas:
            for table in ROLES:
                name = schema, table
                store.define(name, [
                    {"name": "resource", "sql_type": types.get(table, "text")},
                    {"name": "datasetid", "sql_type": "numeric"},
                    {"name": "period", "sql_type": "numeric"},
                    {"name": "quantity", "sql_type": "numeric"},
                    {"name": "startdate", "sql_type": "date"},
                ], fk_groups=foreign_keys.get(table, ()))
                physical = types.get(table, "text")
                value = "1" if physical == "numeric" else "R" if physical == "varchar(3)" else "SYN_R"
                values = [value, value, None, *additions.get(table, ())]
                for index, value in enumerate(values):
                    store.add_row(name, ["resource", "datasetid", "period", "quantity", "startdate"],
                                  [value, str(11 + index), "0", "1234567890.123456789", "2026-10-01"])
        yield store


def transformed(store, project_path, profile="builtin:optimizer-v2"):
    contract = compile_contract(store, profile)
    project = Project(project_path, "food", contract=contract)
    try:
        project.prepare_domains(store)
        transformer = Transformer(store.model, project, store, contract=contract)
        result = {}
        for name, table in store.tables.items():
            names = [f["name"] for f in table["fields"]]
            result[name] = [dict(zip(names, transformer.transform(*name, table["fields"], row)))
                            for row in store.rows(table["id"])]
        raw = list(project.iter_mapping())
        project.finish(True)
        return contract, result, raw
    except BaseException:
        project.finish(False)
        raise


def joined_profile(*, domains=None, relations=None):
    return {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-resources", "version": "1",
            "namespace_rule": "source_schema", "domains": domains or [], "relations": relations or [],
            "keys": [], "required_checks": [], "missing_target_policy": "not_checked"}


def member(table, column="resource"):
    return {"table": table, "column": column}


def side(table, column="resource"):
    return {"members": [{"table": table, "columns": [column]}], "projection": "bag"}


def equality(identifier, source, target, source_column="resource", target_column="resource"):
    return {"id": identifier, "kind": "join", "source": side(source, source_column),
            "target": side(target, target_column), "predicate": "equality", "nulls": "sql"}


def logical_project(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                for table in ("settings", "mapping", "connectivity_bindings")}


def test_identical_codes_get_distinct_prefixes_but_are_stable_within_each_kind(tmp_path):
    with resource_store(tmp_path) as store:
        contract, result, raw = transformed(store, tmp_path / "private.snpanon")
        codes = []
        for table, (identifier, prefix) in ROLES.items():
            rows = result[("public", table)]
            first = rows[0]["resource"]
            assert re.fullmatch(re.escape(prefix) + r"_[0-9]{6,}", first)
            assert first == rows[1]["resource"] and rows[2]["resource"] is None
            assert [r["datasetid"] for r in rows] == ["11", "12", "13"]
            assert all(r["quantity"] == "1234567890.123456789" and r["startdate"] == "2026-10-01" for r in rows)
            assert contract.assignments[("public", table, "resource")]["domain_id"] == identifier
            codes.append(first)
        assert len(set(codes)) == 3 and len({domain for domain, _, _ in raw}) == 3
        assert len(raw) == 3  # NULLs never get map entries


@pytest.mark.parametrize("changed_table", list(ROLES))
def test_adding_object_to_one_resource_kind_does_not_change_other_kinds(tmp_path, changed_table):
    project = tmp_path / "private.snpanon"
    with resource_store(tmp_path) as store:
        _, before, raw_before = transformed(store, project)
    with resource_store(tmp_path, additions={changed_table: ["SYN_NEW"]}) as store:
        _, after, raw_after = transformed(store, project)
    assert set(raw_before) <= set(raw_after) and len(raw_after) == len(raw_before) + 1
    for table in ROLES:
        assert after[("public", table)][:3] == before[("public", table)]


@pytest.mark.parametrize("namespace", ["source_schema", "shared"])
def test_schema_namespace_does_not_merge_different_resource_kinds(tmp_path, namespace):
    profile = load_profile("builtin:optimizer-v2")
    profile["namespace_rule"] = namespace
    with resource_store(tmp_path, schemas=("alpha", "beta")) as store:
        contract, result, raw = transformed(store, tmp_path / "private.snpanon", profile)
        for schema in ("alpha", "beta"):
            assert len({result[(schema, table)][0]["resource"] for table in ROLES}) == 3
        for table in ROLES:
            a = contract.assignments[("alpha", table, "resource")]["mapping_domain"]
            b = contract.assignments[("beta", table, "resource")]["mapping_domain"]
            assert (a == b) == (namespace == "shared")
        assert len(raw) == (3 if namespace == "shared" else 6)


def test_profile_has_only_type_local_resource_relations_and_preserves_business_keys():
    profile = load_profile("builtin:optimizer-v2")
    assert profile["profile_id"] == "optimizer-v2" and profile["version"] == "2"
    assert len(profile["domains"]) == 8 and len(profile["relations"]) == 15
    assert "RESOURCE" not in {d["id"] for d in profile["domains"]}
    relations = {r["id"]: r for r in profile["relations"]}
    assert "roles:RESOURCE" not in relations
    for table, (identifier, prefix) in ROLES.items():
        relation = relations["roles:" + identifier]
        for part in ("source", "target"):
            assert relation[part]["members"] == [{"table": table, "columns": ["datasetid", "resource"]}]
        key, = [key for key in profile["keys"] if key["table"] == table]
        assert "resource" in key["columns"] and key["kind"] == "bk" and key["nulls"] == "not_distinct"
        domain, = [domain for domain in profile["domains"] if domain["id"] == identifier]
        assert domain["prefix"] == prefix and domain["kind"] == "TEXT"


def test_retired_builtin_name_fails_with_explicit_v2_guidance():
    with pytest.raises(ContractError, match="optimizer-v2"):
        load_profile("builtin:optimizer-v1")


def test_old_applied_profile_json_cannot_restore_the_shared_resource_domain(tmp_path):
    old = joined_profile(domains=[{"id": "RESOURCE", "kind": "auto", "prefix": "RES",
                                   "members": [member(table) for table in ROLES]}])
    old.update(profile_id="optimizer-v1", version="1")
    path = tmp_path / "old-applied-profile.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    with resource_store(tmp_path) as store, pytest.raises(ContractError):
        compile_contract(store, path)


def test_old_project_is_rejected_without_changing_its_bytes_or_logical_rows(tmp_path):
    path = tmp_path / "old.snpanon"
    old = Contract(profile={"profile_id": "optimizer-v1"}, profile_hash=OLD_HASH,
                   profile_id="optimizer-v1", version="1", fingerprint="historical-fixture")
    project = Project(path, "food", contract=old)
    project.pseudonym(OLD_KEY, "SYN_R")
    semantic = json.dumps({"mapping_domain": OLD_KEY, "kind": "TEXT", "equality": "exact", "namespace": "public", "prefix": "RES"})
    project.db.executemany("INSERT INTO connectivity_bindings VALUES (?,?,?)", [
        (json.dumps(["public", table, "resource"]), OLD_KEY, semantic) for table in ROLES])
    project.finish(True)
    before, body = logical_project(path), path.read_bytes()
    with resource_store(tmp_path) as store:
        current = compile_contract(store, "builtin:optimizer-v2")
        with pytest.raises(ContractError):
            Project(path, "food", contract=current)
    assert logical_project(path) == before and path.read_bytes() == body


@pytest.mark.parametrize("shape", ["direct_domain", "direct_equality", "transitive_equality"])
def test_custom_profiles_cannot_merge_the_three_exact_optimizer_resource_roles(tmp_path, shape):
    if shape == "direct_domain":
        p = joined_profile(domains=[{"id": "OBJECT", "kind": "text", "members": [member(table) for table in ROLES]}])
    elif shape == "direct_equality":
        p = joined_profile(relations=[equality("bad", "optimizer_production", "optimizer_storage")])
    else:
        p = joined_profile(relations=[equality("left", "optimizer_production", "bridge", target_column="code"),
                                      equality("right", "bridge", "optimizer_storage", source_column="code")])
    with resource_store(tmp_path) as store:
        store.define(("public", "bridge"), [{"name": "code", "sql_type": "text"}])
        store.add_row(("public", "bridge"), ["code"], ["SYN_R"])
        with pytest.raises(ContractError):
            compile_contract(store, p)


@pytest.mark.parametrize("shape", ["domain", "transitive_equality"])
def test_forbidden_resource_declaration_is_rejected_even_when_target_is_absent(tmp_path, shape):
    if shape == "domain":
        profile = joined_profile(domains=[{"id": "OBJECT", "kind": "text",
                                           "members": [member("optimizer_production"), member("optimizer_storage")]}])
    else:
        profile = joined_profile(relations=[equality("left", "optimizer_production", "bridge", target_column="code"),
                                            equality("right", "bridge", "optimizer_storage", source_column="code")])
    with Store(tmp_path / "input.sqlite", Model(), bypass_model=True) as store:
        store.define(("public", "optimizer_production"), [{"name": "resource", "sql_type": "text"}])
        store.add_row(("public", "optimizer_production"), ["resource"], ["SYN_R"])
        with pytest.raises(ContractError):
            compile_contract(store, profile)


@pytest.mark.parametrize("transitive", [False, True])
def test_declared_fk_cannot_merge_production_and_storage_resource_types(tmp_path, transitive):
    fk = {"columns": ["resource"], "target_schema": "public",
          "target_table": "bridge" if transitive else "optimizer_storage",
          "target_columns": ["code" if transitive else "resource"]}
    groups = {"optimizer_production": [fk]}
    if transitive:
        groups["optimizer_storage"] = [copy.deepcopy(fk)]
    with resource_store(tmp_path, foreign_keys=groups) as store:
        if transitive:
            store.define(("public", "bridge"), [{"name": "code", "sql_type": "text"}])
        with pytest.raises(ContractError):
            compile_contract(store, "builtin:optimizer-v2")


def test_custom_resources_of_arbitrary_tables_may_still_use_one_explicit_domain(tmp_path):
    p = joined_profile(domains=[{"id": "RESOURCE", "kind": "text", "prefix": "CUSTOM",
                                 "members": [member("machines"), member("tasks")]}])
    with Store(tmp_path / "input.sqlite", Model(), bypass_model=True) as store:
        for table in ("machines", "tasks"):
            store.define(("public", table), [{"name": "resource", "sql_type": "text"}])
            store.add_row(("public", table), ["resource"], ["SYN_R"])
        _, result, raw = transformed(store, tmp_path / "private.snpanon", p)
        assert result[("public", "machines")] == result[("public", "tasks")]
        assert len(raw) == 1


@pytest.mark.parametrize("table", list(ROLES))
@pytest.mark.parametrize("kind", ["numeric", "varchar(3)"])
def test_builtin_resource_refuses_types_that_cannot_keep_the_requested_prefix(tmp_path, table, kind):
    with resource_store(tmp_path, types={table: kind}) as store, pytest.raises(ContractError):
        compile_contract(store, "builtin:optimizer-v2")


@pytest.mark.parametrize("kind,value", [("numeric", "1"), ("varchar(3)", "'R'")])
def test_impossible_prefix_contract_publishes_only_diagnostic_before_project_creation(tmp_path, kind, value):
    source = tmp_path / "source.sql"
    source.write_text(f"CREATE TABLE public.optimizer_production(resource {kind});\n"
                      f"INSERT INTO public.optimizer_production VALUES ({value});", encoding="utf-8")
    output, project = tmp_path / "out", tmp_path / "private.snpanon"
    with pytest.raises(IntegrityError):
        run(Options(source, output, project, bypass_model=True,
                    connectivity_profile="builtin:optimizer-v2", require_integrity=True))
    assert not project.exists()
    assert {path.name for path in output.iterdir()} == {"report.json"}
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["dataset_published"] is False and report["publication_mode"] is None


@pytest.mark.parametrize("table", list(ROLES))
def test_shortest_allowed_field_keeps_prefix_until_capacity_exhaustion(tmp_path, table):
    identifier, prefix = ROLES[table]
    width = len(prefix) + 7
    with resource_store(tmp_path, types={table: f"varchar({width})"}) as store:
        contract = compile_contract(store, "builtin:optimizer-v2")
        domain = contract.assignments[("public", table, "resource")]
        project = Project(tmp_path / "private.snpanon", "food", contract=contract)
        try:
            assert project._domain_candidate(domain, "SYN_R", 999999) == prefix + "_999999"
            with pytest.raises(ContractError) as error:
                project._domain_candidate(domain, "SYN_R", 1000000)
            assert error.value.code == "DOMAIN_CAPACITY_EXHAUSTED"
        finally:
            project.finish(False)


def test_saved_code_without_required_prefix_is_rejected_before_reuse(tmp_path):
    path = tmp_path / "private.snpanon"
    with resource_store(tmp_path) as store:
        contract, _, _ = transformed(store, path)
    with sqlite3.connect(path) as db:
        target = db.execute("SELECT domain,old FROM mapping ORDER BY domain LIMIT 1").fetchone()
        db.execute("UPDATE mapping SET new='WRONG_000001' WHERE domain=? AND old=?", target)
    before = logical_project(path)
    with pytest.raises(ContractError):
        Project(path, "food", contract=contract)
    assert logical_project(path) == before

"""Synthetic end-to-end acceptance of connectivity profiles and publication gates."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import csv
from decimal import Decimal
import hashlib
import json
import sqlite3
from threading import Event
from uuid import UUID

import pytest

from anonymizer import pipeline
from anonymizer.connectivity import load_profile
from anonymizer.errors import AnonymizerError, IntegrityError
from anonymizer.exporters import Exporter
from anonymizer.integrity import IntegrityVerifier
from anonymizer.mapping import Project, Transformer
from anonymizer.pipeline import Options, PgOptions, run


def side(table, columns, projection="bag"):
    return {"members": [{"table": table, "columns": list(columns)}], "projection": projection}


def profile(*, domains=None, relations=None, keys=None, required=None, namespace="source_schema"):
    return {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-acceptance", "version": "1",
            "namespace_rule": namespace, "domains": domains or [], "relations": relations or [], "keys": keys or [],
            "required_checks": required or [], "missing_target_policy": "not_checked"}


def domain(table_columns, *, kind="text", identifier="OBJECT", prefix="OBJ"):
    return {"id": identifier, "kind": kind, "prefix": prefix,
            "members": [{"table": table, "column": column} for table, column in table_columns]}


def csv_dataset(root, tables):
    root.mkdir()
    for table, rows in tables.items():
        with (root / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow({key: r"\N" if value is None else value for key, value in row.items()})
    return root


def exported(output):
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    result = {}
    for entry in manifest["tables"]:
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            result[(entry["schema"], entry["table"])] = list(csv.DictReader(stream))
    return result


def table_rows(output, table):
    groups = [rows for (schema, name), rows in exported(output).items() if name == table]
    assert len(groups) == 1
    return groups[0]


def check(report, base):
    matches = [item for item in report["checks"] if item["id"] == base or item["id"].startswith(base + ":ns")]
    assert len(matches) == 1, base
    return matches[0]


def assert_diagnostic_only(output, error):
    assert isinstance(error, IntegrityError)
    assert error.report_path == output / "report.json"
    assert {path.name for path in output.iterdir()} == {"report.json"}
    report = json.loads(error.report_path.read_text(encoding="utf-8"))
    assert not report["dataset_published"] and report["publication_mode"] is None
    assert report["integrity_status"] != "PASSED"
    return report


def optimizer_tables(datasets=(11,), *, missing_period=False):
    tables = {name: [] for name in ["optimizer_periods", "optimizer_demand", "optimizer_storage",
                                  "optimizer_production", "optimizer_procurement", "optimizer_bom", "optimizer_transportation"]}
    for dataset in datasets:
        common = {"datasetid": dataset, "period": 0}
        tables["optimizer_periods"].append({**common, "start_date": "2026-10-01"})
        tables["optimizer_demand"].append({**common, "location": "SYN_B", "product": "SYN_A", "client": "SYN_A",
                                           "quantity": "10.000000000000000001", "extra_note": "SYN_EXTRA"})
        for location in ("SYN_A", "SYN_B"):
            tables["optimizer_storage"].append({**common, "location": location, "product": "SYN_A", "resource": "SYN_A", "quantity": 0})
        tables["optimizer_production"].append({**common, "location": "SYN_A", "product": "SYN_A", "bomnum": "SYN_A",
                                               "resource": "SYN_A", "quantity": "1234567890.123456789"})
        tables["optimizer_procurement"].append({**common, "location": "SYN_A", "product": "SYN_A", "supplier": "SYN_B", "quantity": "1.25"})
        tables["optimizer_bom"].append({**common, "location": "SYN_A", "product": "SYN_A", "bomnum": "SYN_A", "input_output": 1})
        # Transit T has no demand, production, procurement or storage row.
        for origin, destination in [("SYN_A", "SYN_T"), ("SYN_T", "SYN_B"), ("SYN_A", "SYN_A"),
                                    ("SYN_B", "SYN_A"), ("SYN_A", "SYN_T")]:
            tables["optimizer_transportation"].append({**common, "loc_from": origin, "loc_to": destination,
                                                       "product": "SYN_A", "transport_type": "SYN_A", "resource": "SYN_A", "duration": "2.5"})
    if missing_period:
        tables["optimizer_demand"][0]["period"] = 99
    return tables


def test_builtin_optimizer_preserves_network_domains_measures_and_extra_columns(tmp_path):
    """AC01/02/04/05/13/18/20/21/22; explicit supplier/client/type decisions."""
    source_tables = optimizer_tables(datasets=(11, 12))
    source = csv_dataset(tmp_path / "input", source_tables)
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",",
                         connectivity_profile="builtin:optimizer-v1", require_integrity=True))
    assert report["integrity_status"] == "PASSED" and report["publication_mode"] == "VERIFIED_INTEGRITY"
    assert not report["model_applied"] and report["relationships_checked"] and report["keys_checked"]
    assert report["source_integrity_status"] == "WARNINGS"  # parallel route BK duplicates are preserved
    assert all(item.get("lost", 0) == item.get("added", 0) == 0 for item in report["checks"])
    demand = table_rows(output, "optimizer_demand")
    storage = table_rows(output, "optimizer_storage")
    production = table_rows(output, "optimizer_production")
    procurement = table_rows(output, "optimizer_procurement")
    bom = table_rows(output, "optimizer_bom")
    routes = table_rows(output, "optimizer_transportation")
    loc_a, loc_b = storage[0]["location"], demand[0]["location"]
    loc_t = routes[0]["loc_to"]
    assert routes[0]["loc_from"] == loc_a and routes[1]["loc_from"] == loc_t and routes[1]["loc_to"] == loc_b
    assert routes[2]["loc_from"] == routes[2]["loc_to"] == loc_a
    assert routes[3]["loc_from"] == loc_b and routes[3]["loc_to"] == loc_a
    assert routes[0] == routes[4]  # multiplicity remains, even when rows are identical
    assert procurement[0]["supplier"] == loc_b
    assert production[0]["bomnum"] == bom[0]["bomnum"]
    assert production[0]["resource"] == routes[0]["resource"] == storage[0]["resource"]
    assert len({loc_a, demand[0]["product"], production[0]["resource"], bom[0]["bomnum"],
                demand[0]["client"], routes[0]["transport_type"]}) == 6
    assert demand[0]["quantity"] == "10.000000000000000001"
    assert production[0]["quantity"] == "1234567890.123456789" and routes[0]["duration"] == "2.5"
    assert "extra_note" in demand[0] and demand[0]["extra_note"] != "SYN_EXTRA"
    # Product joins are scoped to dataset: two sets of 11 occurrences, not 22² pairs.
    occurrences = Counter((str(row["datasetid"]), row["product"]) for table, rows in source_tables.items()
                          if table != "optimizer_periods" for row in rows)
    assert check(report, "roles:PRODUCT")["before_count"] == sum(n * n for n in occurrences.values())
    assert (output / "connectivity-profile.json").is_file()
    for path in output.rglob("*"):
        if path.is_file():
            assert "SYN_" not in path.read_text(encoding="utf-8")
            assert path.suffix not in {".snpanon", ".sqlite"}
    assert source_hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}


def test_builtin_source_missing_period_warns_without_changing_bad_reference(tmp_path):
    source = csv_dataset(tmp_path / "input", optimizer_tables(missing_period=True))
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",",
                         connectivity_profile="builtin:optimizer-v1", require_integrity=True))
    relation = check(report, "period:optimizer_demand")
    assert relation["unmatched_before"] == relation["unmatched_after"] == 1
    assert relation["lost"] == relation["added"] == 0 and relation["status"] == "PASS"
    assert report["integrity_status"] == "PASSED" and report["source_integrity_status"] == "WARNINGS"
    assert table_rows(output, "optimizer_demand")[0]["period"] == "99"


@pytest.mark.parametrize("namespace,expected_pairs,expected_domains", [("source_schema", 2, 2), ("shared", 4, 1)])
def test_namespace_isolated_by_default_and_shared_only_explicitly(tmp_path, namespace, expected_pairs, expected_domains):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE alpha.nodes(code text); INSERT INTO alpha.nodes VALUES ('SYN_A');"
                      "CREATE TABLE beta.nodes(code text); INSERT INTO beta.nodes VALUES ('SYN_A');", encoding="utf-8")
    p = profile(domains=[domain([("nodes", "code")])], relations=[{
        "id": "node_roles", "kind": "join", "source": side("nodes", ["code"]), "target": side("nodes", ["code"])}],
        required=["domain:OBJECT", "node_roles"], namespace=namespace)
    output, project = tmp_path / "out", tmp_path / "private.snpanon"
    report = run(Options(source, output, project, bypass_model=True, connectivity_profile=p, require_integrity=True))
    relations = [item for item in report["checks"] if item["id"].startswith("node_roles:")]
    assert sum(item["before_count"] for item in relations) == expected_pairs
    assert sum(item["after_count"] for item in relations) == expected_pairs
    assert len([item for item in report["checks"] if item["kind"] == "domain"]) == expected_domains
    public = json.dumps(report) + (output / "connectivity-profile.json").read_text(encoding="utf-8")
    assert "alpha" not in public and "beta" not in public


def test_numeric_ids_share_exact_identity_and_preserve_measures(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE public.master(id numeric(6,2)); CREATE TABLE public.child(parent_id numeric(6,2),quantity numeric);"
                      "INSERT INTO public.master VALUES(1),(2); INSERT INTO public.child VALUES(1.0,1234567890.123456789),(2,0);", encoding="utf-8")
    p = profile(domains=[domain([("master", "id"), ("child", "parent_id")], kind="numeric")], relations=[{
        "id": "parent_link", "kind": "fk", "source": side("child", ["parent_id"]), "target": side("master", ["id"]), "nulls": "match_simple"}],
        required=["domain:OBJECT", "parent_link"])
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True))
    master, child = table_rows(output, "master"), table_rows(output, "child")
    assert [Decimal(row["id"]) for row in master] == [Decimal(row["parent_id"]) for row in child]
    assert all(not any(c.isalpha() for c in row["id"]) for row in master)
    assert [row["quantity"] for row in child] == ["1234567890.123456789", "0"]
    assert report["integrity_status"] == "PASSED"


def test_uuid_identity_and_external_json_file_use_project_v2(tmp_path):
    source = tmp_path / "input.sql"
    original = "ABCDEFAB-1234-4234-8234-ABCDEFABCDEF"
    source.write_text("CREATE TABLE public.master(code uuid); CREATE TABLE public.child(reference uuid);"
                      f"INSERT INTO public.master VALUES('{original}');"
                      f"INSERT INTO public.child VALUES('{original.lower()}');", encoding="utf-8")
    p = profile(domains=[domain([("master", "code"), ("child", "reference")], kind="uuid")], relations=[{
        "id": "uuid_link", "kind": "fk", "source": side("child", ["reference"]), "target": side("master", ["code"])}],
        required=["domain:OBJECT", "uuid_link"])
    profile_path = tmp_path / "portable-profile.json"
    profile_path.write_text(json.dumps(p), encoding="utf-8")
    output, project = tmp_path / "out", tmp_path / "private.snpanon"
    report = run(Options(source, output, project, bypass_model=True, connectivity_profile=profile_path, require_integrity=True))
    value = table_rows(output, "master")[0]["code"]
    assert value == table_rows(output, "child")[0]["reference"] and UUID(value) != UUID(original)
    with closing(sqlite3.connect(project)) as db:
        assert dict(db.execute("SELECT name,value FROM settings"))["format"] == "snp-anonymizer-project-v2"
    assert json.loads((output / "connectivity-profile.json").read_text(encoding="utf-8")) == load_profile(profile_path)
    assert report["integrity_status"] == "PASSED"


def test_text_leading_zero_empty_null_and_literal_backslash_remain_distinct(tmp_path):
    # An explicit physical TEXT type keeps '1' textual; a plain external CSV
    # intentionally infers numeric-looking cells and cannot supply that fact.
    source = tmp_path / "input.sql"
    source.write_text(r"CREATE TABLE public.nodes(code text,note text); INSERT INTO public.nodes VALUES "
                      r"('1',''),('01',NULL),('',E'\\N'),(NULL,'');", encoding="utf-8")
    p = profile(domains=[domain([("nodes", "code")])], required=["domain:OBJECT"])
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",",
                         connectivity_profile=p, require_integrity=True))
    rows = table_rows(output, "nodes")
    assert len({row["code"] for row in rows[:3]}) == 3
    assert rows[3]["code"] == r"\N" and rows[0]["note"] == rows[3]["note"] == ""
    assert rows[1]["note"] == r"\N" and rows[2]["note"] not in {"", r"\N"}
    assert report["integrity_status"] == "PASSED"


def test_legacy_default_is_unverified_and_require_without_profile_is_refused(tmp_path):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    first = tmp_path / "out"
    report = run(Options(source, first, tmp_path / "legacy.snpanon", bypass_model=True, delimiter=","))
    assert report["integrity_status"] == "NOT_CHECKED" and report["publication_mode"] == "UNVERIFIED_INTEGRITY"
    assert not report["relationships_checked"] and not report["keys_checked"]
    output = tmp_path / "refused"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "required.snpanon", bypass_model=True, delimiter=",", require_integrity=True))
    assert_diagnostic_only(output, error.value)


@pytest.mark.parametrize("require", [False, True])
def test_missing_profile_field_has_honest_partial_coverage(tmp_path, require):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    p = profile(domains=[domain([("nodes", "code"), ("missing", "code")])], required=["domain:OBJECT"])
    output = tmp_path / "out"
    options = Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=require)
    if require:
        with pytest.raises(IntegrityError) as error:
            run(options)
        report = assert_diagnostic_only(output, error.value)
    else:
        report = run(options)
        assert report["publication_mode"] == "UNVERIFIED_INTEGRITY"
    assert report["integrity_status"] == "NOT_CHECKED"
    assert check(report, "domain:OBJECT")["status"] == "NOT_CHECKED"


@pytest.mark.parametrize("require", [False, True])
def test_unsupported_predicate_is_never_success(tmp_path, require):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    p = profile(relations=[{"id": "future_predicate", "kind": "join", "source": side("nodes", ["code"]),
                            "target": side("nodes", ["code"]), "predicate": "arrival_offset"}], required=["future_predicate"])
    output = tmp_path / "out"
    options = Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=require)
    if require:
        with pytest.raises(IntegrityError) as error:
            run(options)
        report = assert_diagnostic_only(output, error.value)
    else:
        report = run(options)
    assert check(report, "future_predicate")["status"] == "UNSUPPORTED"
    assert report["integrity_status"] == "NOT_CHECKED" and not report["relationships_checked"]


def test_saved_project_repeats_across_file_order_and_chunk_sizes(tmp_path, monkeypatch):
    tables = {"nodes": [{"code": "SYN_A", "qty": 1}, {"code": "SYN_B", "qty": 2}],
              "links": [{"reference": "SYN_A", "qty": 3}, {"reference": "SYN_B", "qty": 4}]}
    source = csv_dataset(tmp_path / "input", tables)
    reordered = csv_dataset(tmp_path / "reordered", dict(reversed(list(tables.items()))))
    p = profile(domains=[domain([("nodes", "code"), ("links", "reference")])], required=["domain:OBJECT"])
    project = tmp_path / "private.snpanon"
    first, second = tmp_path / "first", tmp_path / "second"
    run(Options(source, first, project, bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=True))
    from anonymizer.store import Store
    original_add = Store.add_row
    def each_row(self, *args, **kwargs):
        result = original_add(self, *args, **kwargs)
        self.flush()
        return result
    monkeypatch.setattr(Store, "add_row", each_row)
    run(Options(reordered, second, project, bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=True))
    assert (first / "dump.sql").read_bytes() == (second / "dump.sql").read_bytes()
    assert exported(first) == exported(second)


def test_old_nonempty_project_and_changed_profile_are_not_silently_migrated(tmp_path):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    legacy = tmp_path / "legacy.snpanon"
    project = Project(legacy, "food")
    project.pseudonym("synthetic:old", "SYN_A", text_prefix="code", avoid_original=True)
    project.finish(True)
    original = legacy.read_bytes()
    p = profile(domains=[domain([("nodes", "code")])], required=["domain:OBJECT"])
    output = tmp_path / "refused"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, legacy, bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=True))
    assert_diagnostic_only(output, error.value)
    assert legacy.read_bytes() == original
    current = tmp_path / "current.snpanon"
    run(Options(source, tmp_path / "first", current, bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=True))
    original = current.read_bytes()
    changed = json.loads(json.dumps(p))
    changed["domains"][0]["prefix"] = "NEW"
    output = tmp_path / "changed"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, current, bypass_model=True, delimiter=",", connectivity_profile=changed, require_integrity=True))
    assert_diagnostic_only(output, error.value)
    assert current.read_bytes() == original


@pytest.mark.parametrize("artifact", ["csv", "dump", "inserts"])
def test_corrupted_actual_export_blocks_dataset_and_pg(tmp_path, monkeypatch, artifact):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "quantity": "1234567890.123456789"}]})
    p = profile(domains=[domain([("nodes", "code")])], required=["domain:OBJECT"])
    original = Exporter.close
    def corrupt(self, success):
        result = original(self, success)
        if success:
            if artifact == "csv":
                manifest = json.loads((self.directory / "csv/manifest.json").read_text(encoding="utf-8"))
                target = self.directory / "csv" / manifest["tables"][0]["file"]
            else:
                target = self.directory / ("dump.sql" if artifact == "dump" else "inserts.sql")
            text = target.read_text(encoding="utf-8")
            assert "1234567890.123456789" in text
            target.write_text(text.replace("1234567890.123456789", "1234567890.123456788", 1), encoding="utf-8")
        return result
    monkeypatch.setattr(Exporter, "close", corrupt)
    monkeypatch.setattr(pipeline, "pg_load", lambda *a, **kw: pytest.fail("PG reached before failed integrity gate"))
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p,
                    require_integrity=False, pg=PgOptions("synthetic.invalid", 5432, "synthetic", "synthetic", "", "public")))
    report = assert_diagnostic_only(output, error.value)
    assert report["integrity_status"] == "FAILED"
    assert any(item["kind"] == "export" and item["status"] == "FAIL" for item in report["checks"])


def test_equal_join_counts_do_not_hide_swapped_pairs_in_unverified_mode(tmp_path, monkeypatch):
    source = csv_dataset(tmp_path / "input", {"child": [{"code": "SYN_A"}, {"code": "SYN_B"}],
                                              "master": [{"code": "SYN_A"}, {"code": "SYN_B"}]})
    p = profile(relations=[{"id": "link", "kind": "join", "source": side("child", ["code"]), "target": side("master", ["code"])}])
    original = Transformer.transform
    def swap(self, schema, table, fields, row):
        result = original(self, schema, table, fields, row)
        code = row[0][-1]
        result[0] = "CODE_" + ({"A": "B", "B": "A"}[code] if table == "master" else code)
        return result
    monkeypatch.setattr(Transformer, "transform", swap)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p))
    report = assert_diagnostic_only(output, error.value)
    relation = check(report, "link")
    assert relation["before_count"] == relation["after_count"] == 2
    assert relation["lost"] == relation["added"] == 2 and relation["status"] == "FAIL"
    assert report["integrity_status"] == "FAILED" and report["relationships_checked"]


def test_same_duplicate_counts_do_not_hide_changed_group_membership(tmp_path, monkeypatch):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A"}, {"code": "SYN_A"}, {"code": "SYN_B"}, {"code": "SYN_B"}]})
    p = profile(keys=[{"id": "groups", "table": "nodes", "columns": ["code"], "kind": "bk", "nulls": "not_distinct"}])
    original, ordinals = Transformer.transform, iter(["CODE_X", "CODE_Y", "CODE_X", "CODE_Y"])
    def repartition(self, schema, table, fields, row):
        result = original(self, schema, table, fields, row)
        result[0] = next(ordinals)
        return result
    monkeypatch.setattr(Transformer, "transform", repartition)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p))
    report = assert_diagnostic_only(output, error.value)
    groups = check(report, "groups")
    assert groups["before_count"] == groups["after_count"] == 2
    assert groups["membership_changes"] > 0 and groups["status"] == "FAIL"
    assert report["keys_checked"]


def test_composite_fk_preserves_unmatched_tuple_instead_of_columnwise_success(tmp_path):
    source = csv_dataset(tmp_path / "input", {"child": [{"code": "SYN_A", "variant": 2}],
                                              "master": [{"code": "SYN_A", "variant": 1}, {"code": "SYN_B", "variant": 2}]})
    p = profile(domains=[domain([("child", "code"), ("master", "code")])], relations=[{
        "id": "composite", "kind": "fk", "source": side("child", ["code", "variant"]),
        "target": side("master", ["code", "variant"]), "nulls": "match_simple"}], required=["domain:OBJECT", "composite"])
    report = run(Options(source, tmp_path / "out", tmp_path / "private.snpanon", bypass_model=True,
                         delimiter=",", connectivity_profile=p, require_integrity=True))
    relation = check(report, "composite")
    assert relation["unmatched_before"] == relation["unmatched_after"] == 1 and relation["lost"] == relation["added"] == 0
    assert report["source_integrity_status"] == "WARNINGS" and report["integrity_status"] == "PASSED"


def test_standard_mode_independent_unique_groups_keep_their_members(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE public.custom(a text,b text,UNIQUE(a),UNIQUE(b));"
                      "INSERT INTO public.custom VALUES('SYN_A','SYN_X'),('SYN_A','SYN_Y'),('SYN_B','SYN_Y');", encoding="utf-8")
    p = profile(domains=[domain([("custom", "a")], identifier="A", prefix="A"),
                         domain([("custom", "b")], identifier="B", prefix="B")], keys=[
        {"id": "key_a", "table": "custom", "columns": ["a"], "kind": "unique"},
        {"id": "key_b", "table": "custom", "columns": ["b"], "kind": "unique"}],
        required=["domain:A", "domain:B", "key_a", "key_b"])
    report = run(Options(source, tmp_path / "out", tmp_path / "private.snpanon", connectivity_profile=p, require_integrity=True))
    assert report["model_applied"] and report["keys_checked"] and report["integrity_status"] == "PASSED"
    for name in ("key_a", "key_b"):
        group = check(report, name)
        assert group["before_count"] == group["after_count"] == 1 and group["membership_changes"] == 0
    assert report["source_integrity_status"] == "WARNINGS"


@pytest.mark.parametrize("target_exists", [False, True])
def test_missing_target_differs_from_present_empty_target(tmp_path, target_exists):
    source = tmp_path / "input.sql"
    source.write_text(("CREATE TABLE public.master(code text);" if target_exists else "")
                      + "CREATE TABLE public.child(code text);INSERT INTO public.child VALUES('SYN_A');", encoding="utf-8")
    p = profile(domains=[domain([("child", "code"), ("master", "code")])], relations=[{
        "id": "parent", "kind": "fk", "source": side("child", ["code"]), "target": side("master", ["code"]), "nulls": "match_simple"}],
        required=["domain:OBJECT", "parent"])
    output = tmp_path / "out"
    options = Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True)
    if target_exists:
        report = run(options)
        relation = check(report, "parent")
        assert report["integrity_status"] == "PASSED" and report["source_integrity_status"] == "WARNINGS"
        assert relation["status"] == "PASS" and relation["unmatched_before"] == relation["unmatched_after"] == 1
    else:
        with pytest.raises(IntegrityError) as error:
            run(options)
        report = assert_diagnostic_only(output, error.value)
        assert check(report, "parent")["status"] == "NOT_CHECKED"


def test_new_false_match_of_originally_unmatched_row_is_a_failure(tmp_path, monkeypatch):
    source = csv_dataset(tmp_path / "input", {"child": [{"code": "SYN_A"}, {"code": "SYN_MISSING"}],
                                              "master": [{"code": "SYN_A"}, {"code": "SYN_B"}]})
    p = profile(relations=[{"id": "link", "kind": "fk", "source": side("child", ["code"]),
                            "target": side("master", ["code"]), "nulls": "match_simple"}], required=["link"])
    original = Transformer.transform
    def introduce_false_match(self, schema, table, fields, row):
        result = original(self, schema, table, fields, row)
        result[0] = "CODE_X" if row[0] == "SYN_A" else "CODE_Y"
        return result
    monkeypatch.setattr(Transformer, "transform", introduce_false_match)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p))
    relation = check(assert_diagnostic_only(output, error.value), "link")
    assert relation["status"] == "FAIL" and relation["added"] == 1
    assert relation["unmatched_before"] == 1 and relation["unmatched_after"] == 0
    assert relation["unmatched_identity_changes"] == 1


@pytest.mark.parametrize("require", [False, True])
def test_zero_declared_profile_checks_cannot_be_passed(tmp_path, require):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    output = tmp_path / "out"
    options = Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",",
                      connectivity_profile=profile(), require_integrity=require)
    if require:
        with pytest.raises(IntegrityError) as error:
            run(options)
        report = assert_diagnostic_only(output, error.value)
    else:
        report = run(options)
    assert report["integrity_status"] == "NOT_CHECKED" and not report["relationships_checked"] and not report["keys_checked"]


def test_fractional_numeric_identifier_range_uses_exact_common_quantum(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE public.master(code numeric(2,3)); CREATE TABLE public.child(code numeric(3,4));"
                      "INSERT INTO public.master VALUES(0.001),(0.002); INSERT INTO public.child VALUES(1e-3),(0.0020);", encoding="utf-8")
    p = profile(domains=[domain([("child", "code"), ("master", "code")], kind="numeric")], relations=[{
        "id": "link", "kind": "fk", "source": side("child", ["code"]), "target": side("master", ["code"])}],
        required=["domain:OBJECT", "link"])
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True))
    master = [Decimal(row["code"]) for row in table_rows(output, "master")]
    child = [Decimal(row["code"]) for row in table_rows(output, "child")]
    assert master == child and len(set(master)) == 2
    assert all(abs(value) < Decimal("0.1") and value.quantize(Decimal("0.001")) == value for value in master)
    assert report["integrity_status"] == "PASSED"


def test_missing_system_key_is_coverage_not_pass_and_blocks_when_required(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE public.nodes(sys_id bigint PRIMARY KEY,code text); INSERT INTO public.nodes VALUES(9,'SYN_A');", encoding="utf-8")
    p = profile(domains=[domain([("nodes", "code")])], keys=[{"id": "system_key", "table": "nodes", "columns": ["sys_id"], "kind": "pk"}],
                required=["domain:OBJECT", "system_key"])
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True))
    report = assert_diagnostic_only(output, error.value)
    assert check(report, "system_key")["status"] == "NOT_CHECKED" and not report["keys_checked"]


def test_capacity_and_contract_conflict_never_publish_dataset(tmp_path):
    source = tmp_path / "input.sql"
    values = ",".join("('" + code + "')" for code in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    source.write_text("CREATE TABLE public.nodes(code varchar(1)); INSERT INTO public.nodes VALUES" + values + ";", encoding="utf-8")
    p = profile(domains=[domain([("nodes", "code")])], required=["domain:OBJECT"])
    output = tmp_path / "capacity"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True))
    report = assert_diagnostic_only(output, error.value)
    assert report["error_code"] == "DOMAIN_CAPACITY_EXHAUSTED"
    conflicting = profile(domains=[domain([("nodes", "code")], identifier="FIRST"), domain([("nodes", "code")], identifier="SECOND")])
    output = tmp_path / "conflict"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "other.snpanon", bypass_model=True, connectivity_profile=conflicting))
    assert assert_diagnostic_only(output, error.value)["error_code"] == "CONTRACT_CONFLICT"


def test_declared_ddl_reference_cannot_be_split_by_conflicting_profile_domains(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("CREATE TABLE public.master(code text PRIMARY KEY);"
                      "CREATE TABLE public.child(code text REFERENCES public.master(code));"
                      "INSERT INTO public.master VALUES('SYN_A');INSERT INTO public.child VALUES('SYN_A');", encoding="utf-8")
    p = profile(domains=[domain([("master", "code")], identifier="MASTER"), domain([("child", "code")], identifier="CHILD")],
                required=["domain:MASTER", "domain:CHILD"])
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, connectivity_profile=p, require_integrity=True))
    assert assert_diagnostic_only(output, error.value)["error_code"] == "CONTRACT_CONFLICT"


def test_existing_output_is_not_replaced_with_diagnostics(tmp_path):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A"}]})
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_bytes(b"SYN_EXISTING_DO_NOT_REPLACE")
    with pytest.raises(AnonymizerError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True,
                    connectivity_profile={"format": "invalid"}, require_integrity=True))
    assert not isinstance(error.value, IntegrityError)
    assert list(output.iterdir()) == [sentinel] and sentinel.read_bytes() == b"SYN_EXISTING_DO_NOT_REPLACE"


def test_cancel_during_last_check_publishes_diagnostic_only(tmp_path, monkeypatch):
    source = csv_dataset(tmp_path / "input", {"nodes": [{"code": "SYN_A", "qty": 1}]})
    p = profile(domains=[domain([("nodes", "code")])], keys=[{"id": "last_key", "table": "nodes", "columns": ["code"], "kind": "bk"}],
                required=["domain:OBJECT", "last_key"])
    cancel = Event()
    original = IntegrityVerifier._key
    def last_check(self, descriptor):
        result = original(self, descriptor)
        if descriptor["id"].startswith("last_key:"):
            cancel.set()
            self.check()
        return result
    monkeypatch.setattr(IntegrityVerifier, "_key", last_check)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",", connectivity_profile=p, require_integrity=True), cancel=cancel)
    assert assert_diagnostic_only(output, error.value)["error_code"] == "CHECK_INCOMPLETE"

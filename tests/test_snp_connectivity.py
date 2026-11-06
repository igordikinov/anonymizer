"""Synthetic acceptance for the separate, explicit SNP connectivity contract."""
import copy
import hashlib
import json
from threading import Event

import pytest

from anonymizer.connectivity import compile_contract, input_text_hints, load_profile
from anonymizer.csv_reader import parse_csv
from anonymizer.errors import AnonymizerError, IntegrityError
from anonymizer.generic_input import InputLimitError
from anonymizer.exporters import Exporter
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store

from snp_connectivity_fixture import (
    CORE, EXACT, ITEM_A, TABLES, assert_business_links, dataset, output_rows, snapshot,
    table_rows, write_dataset, write_sql,
)


def options(tmp_path, source, *, output="out", project="private.snpanon", **kw):
    defaults = dict(bypass_model=True, delimiter=";", connectivity_profile="builtin:snp-v1", require_integrity=True)
    defaults.update(kw)
    return Options(source, tmp_path / output, tmp_path / project, **defaults)


def check(report, identifier):
    return next(row for row in report["checks"] if row["id"].startswith(identifier + ":"))


def diagnostic(output, error):
    assert {p.name for p in output.iterdir()} == {"report.json"}
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert not report["dataset_published"] and report["publication_mode"] is None
    assert error.report_path == output / "report.json"
    return report


def decoded_artifacts(output, tmp_path):
    _, manifest = output_rows(output)
    decoded = []
    for name in ("csv", "dump.sql", "inserts.sql"):
        with Store(tmp_path / f"decode-{name.replace('.', '-')}.sqlite", Model(), bypass_model=True) as store:
            if name == "inserts.sql":
                for entry in manifest["tables"]:
                    store.define((entry["schema"], entry["table"]), entry["fields"])
            (parse_csv if name == "csv" else parse_sql)(output / name, store)
            decoded.append({key: [dict(zip(store.columns(key), row)) for row in store.rows(table["id"])]
                            for key, table in store.tables.items()})
    assert decoded[0] == decoded[1] == decoded[2]
    return decoded[0]


def test_snp_profile_uses_snapshot_fk_and_bk_with_explicit_context_equalities():
    profile = load_profile("builtin:snp-v1")
    assert profile["profile_id"] == "snp-v1" and profile["version"] == "1"
    assert profile["namespace_rule"] == "source_schema"
    assert len(profile["domains"]) == 10 and len(profile["relations"]) == 46 and len(profile["keys"]) == 16
    assert len(profile["required_checks"]) == 66
    assert {k["table"] for k in profile["keys"]} == set(TABLES)
    assert all(domain["kind"] == "TEXT" for domain in profile["domains"])
    metadata = snapshot()
    for key in profile["keys"]:
        assert key["columns"] == [f["name"] for f in metadata[key["table"]]["fields"] if "BK" in (f.get("keys") or [])]
        assert key["kind"] == "bk" and key["nulls"] == "not_distinct"
    expected = {(table, field["name"], field["fk"]["table"], field["fk"]["field"])
                for table in TABLES for field in metadata[table]["fields"]
                if field.get("fk") and field["fk"]["table"] in TABLES}
    actual = set()
    for rel in profile["relations"]:
        if rel["kind"] != "fk":
            continue
        source, = rel["source"]["members"]
        target, = rel["target"]["members"]
        assert len(source["columns"]) == len(target["columns"]) == 1
        actual.add((source["table"], source["columns"][0], target["table"], target["columns"][0]))
    assert actual == expected and len(actual) == 31
    for name in ("SKU", "RESOURCE_LOC", "PRODSOURCE", "LANE", "DEMAND"):
        rel, = [r for r in profile["relations"] if r["id"] == "context:" + name]
        assert rel["kind"] == "join" and rel["source"] == rel["target"]
        assert rel["source"]["projection"] == "bag"
        assert rel["id"] in profile["required_checks"]
    assert all(not member["table"].startswith("optimizer_") for domain in profile["domains"] for member in domain["members"])
    assert all("domain:" + name in profile["required_checks"] for name in CORE)
    assert all("domain:" + name not in profile["required_checks"] for name in ("UOM", "CALENDAR", "RESOURCE_GROUP"))


def test_text_hints_are_opt_in_for_the_canonical_profile_and_equivalent_json(tmp_path):
    profile = load_profile("builtin:snp-v1")
    expected = frozenset((m["table"], m["column"]) for d in profile["domains"] for m in d["members"])
    assert input_text_hints(profile) == expected
    path = tmp_path / "applied-profile.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=4), encoding="utf-8")
    assert input_text_hints(load_profile(path)) == expected
    modified = copy.deepcopy(profile)
    modified["version"] = "2"
    assert modified["profile_id"] == "snp-v1" and not input_text_hints(modified)
    modified = copy.deepcopy(profile)
    modified["domains"][0]["prefix"] = "custom"
    assert not input_text_hints(modified)
    assert not input_text_hints(load_profile("builtin:optimizer-v2"))
    assert not input_text_hints(None)


def test_hint_metadata_has_a_global_budget_and_is_not_released_by_dropped_tables(tmp_path, monkeypatch):
    import anonymizer.store as module

    hints = input_text_hints(load_profile("builtin:snp-v1"))
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True, input_text_hints=hints) as store:
        reserved = store._input_metadata_used
        assert reserved == sum(128 + sum(len(part.encode("utf-8")) * 4 for part in hint) for hint in hints)
        assert reserved > 0
        store.define_input(("public", "mot"), ["mot"])
        store.observe_input(("public", "mot"), ["7300019"])
        store.add_row(("public", "mot"), ["mot"], ["7300019"])
        store.finish_input(("public", "mot"))
        assert store._input_metadata_used > reserved
        store.drop_table(("public", "mot"), "Synthetic dropped table")
        assert store._input_metadata_used >= reserved
        store.define_input(("public", "mot"), ["mot"])
        assert store._input_metadata_used >= reserved
    monkeypatch.setattr(module, "MAX_INPUT_METADATA", reserved - 1)
    failed_path = tmp_path / "not-created.sqlite"
    with pytest.raises(InputLimitError):
        Store(failed_path, Model(), bypass_model=True, input_text_hints=hints)
    assert not failed_path.exists()


@pytest.mark.parametrize("kind", ["csv", "xlsx", "mixed"])
def test_all_sixteen_tables_preserve_roles_contexts_values_and_repeat(tmp_path, kind):
    source = write_dataset(tmp_path / "source", kind=kind)
    dumps = []
    for index in range(2):
        output = tmp_path / f"out-{index}"
        report = run(options(tmp_path, source, output=output.name))
        assert report["integrity_status"] == "PASSED" and report["publication_mode"] == "VERIFIED_INTEGRITY"
        assert report["tables"] == 16 and report["rows"] == sum(map(len, dataset().values()))
        assert report["validation"]["preserved_fields_equal"] and not report["model_applied"]
        assert report["keys_checked"]
        assert all(row["status"] == "PASS" for row in report["checks"] if row.get("required"))
        assert len(report["coverage"]["required"]) == 66
        assert_business_links(output)
        decode_dir = tmp_path / f"decoded-{index}"
        decode_dir.mkdir()
        decoded_artifacts(output, decode_dir)
        dumps.append((output / "dump.sql").read_bytes())
        assert ITEM_A not in dumps[-1].decode("utf-8") and "SYN_PRIVATE" not in dumps[-1].decode("utf-8")
    assert dumps[0] == dumps[1]
    assert any(row["status"] == "NOT_CHECKED" and not row.get("required") for row in report["checks"])


def test_all_optional_roles_and_short_uom_fields_are_checked_when_present(tmp_path):
    source = write_sql(tmp_path / "source.sql", rows=dataset(optional=True))
    output = tmp_path / "out"
    report = run(options(tmp_path, source))
    assert report["integrity_status"] == "PASSED" and report["relationships_checked"] and report["keys_checked"]
    assert report["coverage"]["checked"] == report["coverage"]["declared"]
    assert all(row["status"] == "PASS" for row in report["checks"])
    optional_values = {"UOM": set(), "CALENDAR": set(), "RESOURCE_GROUP": set()}
    profile = load_profile("builtin:snp-v1")
    all_rows, _ = output_rows(output)
    by_table = {table: rows for (_, table), rows in all_rows.items()}
    for domain in profile["domains"]:
        if domain["id"] not in optional_values:
            continue
        for member in domain["members"]:
            optional_values[domain["id"]].update(row[member["column"]] for row in by_table[member["table"]])
    assert all(len(values) == 1 for values in optional_values.values())
    assert len(set.union(*optional_values.values())) == 3
    assert all(len(value) <= 5 for value in optional_values["UOM"])
    assert_business_links(output)


@pytest.mark.parametrize("missing", ["table", "column"])
def test_missing_required_input_publishes_with_honest_partial_coverage(tmp_path, missing):
    rows = dataset()
    if missing == "table":
        del rows["mot"]
    else:
        for row in rows["comp_req_sp"]:
            del row["prodsource"]
    source = write_dataset(tmp_path / "source", rows=rows)
    output = tmp_path / "out"
    report = run(options(tmp_path, source))
    assert report["integrity_status"] == "NOT_CHECKED"
    assert report["dataset_published"] and report["publication_mode"] == "UNVERIFIED_INTEGRITY"
    assert (output / "dump.sql").is_file() and (output / "inserts.sql").is_file()
    assert any(row["status"] == "NOT_CHECKED" and row.get("required") for row in report["checks"])


def test_present_null_required_role_column_is_not_reported_as_missing(tmp_path):
    rows = dataset()
    for row in rows["comp_req_sp"]:
        row["prodsource"] = None
    source = write_dataset(tmp_path / "source", rows=rows)
    report = run(options(tmp_path, source))
    assert report["integrity_status"] == "PASSED"
    assert check(report, "domain:PRODSOURCE")["status"] == "PASS"
    assert all(row["prodsource"] == r"\N" for row in table_rows(tmp_path / "out", "comp_req_sp"))


def test_original_duplicate_group_and_orphan_link_are_preserved_with_source_warnings(tmp_path):
    rows = dataset()
    rows["sp_demand"].append(dict(rows["sp_demand"][0]))
    orphan = dict(rows["sp_demand"][1], item="SYN_ORPHAN_ITEM")
    rows["sp_demand"].append(orphan)
    source = write_dataset(tmp_path / "source", rows=rows)
    report = run(options(tmp_path, source))
    assert report["integrity_status"] == "PASSED" and report["source_integrity_status"] == "WARNINGS"
    key = check(report, "bk:sp_demand")
    assert key["before_count"] == key["after_count"] == 1 and key["membership_changes"] == 0
    link = check(report, "fk:sp_demand:item")
    assert link["unmatched_before"] == link["unmatched_after"] == 1
    assert link["lost"] == link["added"] == link["unmatched_identity_changes"] == 0


def test_empty_master_is_not_the_same_as_a_missing_table(tmp_path):
    rows = dataset()
    rows["item"] = []
    source = write_dataset(tmp_path / "source", rows=rows)
    report = run(options(tmp_path, source))
    assert report["integrity_status"] == "PASSED" and report["source_integrity_status"] == "WARNINGS"
    assert check(report, "fk:sp_demand:item")["unmatched_before"] == 2


def test_namespaces_use_independent_mapping_domains_for_the_same_original_codes(tmp_path):
    source = write_sql(tmp_path / "source.sql", schemas=("alpha", "beta"))
    with Store(tmp_path / "store.sqlite", Model(), bypass_model=True) as store:
        parse_sql(source, store)
        contract = compile_contract(store, "builtin:snp-v1")
        a = contract.assignments[("alpha", "res", "res")]
        b = contract.assignments[("beta", "res", "res")]
        assert a["namespace"] != b["namespace"] and a["mapping_domain"] != b["mapping_domain"]
        assert a["mapping_domain"] != contract.assignments[("alpha", "item", "item")]["mapping_domain"]
    report = run(options(tmp_path, source))
    assert report["tables"] == 32 and report["integrity_status"] == "PASSED"
    assert len(report["coverage"]["required"]) == 132


def test_declared_numeric_resource_is_not_silently_cast_to_profile_text(tmp_path):
    source = write_sql(tmp_path / "source.sql", overrides={("res", "res"): "numeric"},
                       rows={name: ([dict(row, res=7300019) for row in values] if name == "res" else values)
                             for name, values in dataset().items()})
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source))
    report = diagnostic(output, error.value)
    assert report["error_code"] == "CONTRACT_CONFLICT"


def test_modified_profile_with_same_id_does_not_get_hidden_text_inference(tmp_path):
    source = write_dataset(tmp_path / "source", kind="mixed")
    profile = load_profile("builtin:snp-v1")
    profile["version"] = "2"
    output = tmp_path / "out"
    with pytest.raises(AnonymizerError):
        run(options(tmp_path, source, connectivity_profile=profile))
    assert not output.exists() and not (tmp_path / "private.snpanon").exists()


def test_project_from_optimizer_profile_is_rejected_without_modification(tmp_path):
    import sqlite3
    from anonymizer.connectivity import _hash

    source = write_dataset(tmp_path / "source")
    project = tmp_path / "private.snpanon"
    with sqlite3.connect(project) as db:
        db.executescript("CREATE TABLE settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);"
                         "CREATE TABLE mapping(domain TEXT,old TEXT,new TEXT NOT NULL,ordinal INTEGER NOT NULL,PRIMARY KEY(domain,old),UNIQUE(domain,new));"
                         "CREATE INDEX map_ordinal ON mapping(domain,ordinal);")
        db.executemany("INSERT INTO settings VALUES (?,?)", [
            ("format", "snp-anonymizer-project-v2"), ("industry", "food"), ("secret", "00" * 32),
            ("connectivity_compiler", "1"), ("connectivity_profile_hash", _hash(load_profile("builtin:optimizer-v2")))])
    before = hashlib.sha256(project.read_bytes()).hexdigest()
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source))
    diagnostic(tmp_path / "out", error.value)
    assert hashlib.sha256(project.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("artifact", ["csv", "dump.sql", "inserts.sql"])
def test_corruption_of_each_actual_snp_export_blocks_publication(tmp_path, monkeypatch, artifact):
    source = write_dataset(tmp_path / "source")
    original = Exporter.close

    def corrupt(self, success):
        original(self, success)
        if not success:
            return
        if artifact == "csv":
            manifest = json.loads((self.directory / "csv/manifest.json").read_text(encoding="utf-8"))
            entry = next(e for e in manifest["tables"] if e["table"] == "sku_attributes")
            path = self.directory / "csv" / entry["file"]
        else:
            path = self.directory / artifact
        text = path.read_text(encoding="utf-8")
        assert "item_000001" in text
        path.write_text(text.replace("item_000001", "item_999999", 1), encoding="utf-8")

    monkeypatch.setattr(Exporter, "close", corrupt)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, require_integrity=False))
    report = diagnostic(output, error.value)
    assert report["integrity_status"] == "FAILED"
    assert any(row["status"] == "FAIL" for row in report["checks"] if row["kind"] == "export")


def test_cancellation_after_last_snp_export_check_is_diagnostic_only(tmp_path):
    source = write_dataset(tmp_path / "source")
    cancel = Event()

    def progress(stage, amount, message):
        if stage == "verify_exports" and amount == 1:
            cancel.set()

    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source), progress=progress, cancel=cancel)
    assert diagnostic(tmp_path / "out", error.value)["error_code"] == "CHECK_INCOMPLETE"

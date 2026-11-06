"""Partial profile coverage must preserve data without pretending full PASS."""
import copy
import csv
from datetime import date
import hashlib
import json
from threading import Event

import pytest

from anonymizer import pipeline
from anonymizer.connectivity import load_profile
from anonymizer.csv_reader import parse_csv
from anonymizer.errors import IntegrityError
from anonymizer.exporters import Exporter
from anonymizer.integrity import IntegrityVerifier
from anonymizer.metadata import Model
from anonymizer.pipeline import Options, run
from anonymizer.runlog import RunLog
from anonymizer.sql_reader import parse_sql
from anonymizer.store import Store

from snp_connectivity_fixture import DATE_A, DATE_B, EXACT, SHARED_CODE, dataset, output_rows, write_dataset, write_sql


MISSING_TABLES = {"item", "loc", "mot", "demand_stream", "sp_demand_profile"}
MISSING_COLUMNS = {("prod_cap_req_sp", "date"), ("sp_demand", "demandtype")}


def partial_dataset():
    rows = dataset(optional=True)
    for name in MISSING_TABLES:
        del rows[name]
    for row in rows["prod_cap_req_sp"]:
        row["datefr"] = date.fromisoformat(row.pop("date"))
    for row in rows["sp_demand"]:
        del row["demandtype"]
    return rows


def options(tmp_path, source, **changes):
    values = dict(source=source, output=tmp_path / "out", project=tmp_path / "private.snpanon",
                  bypass_model=True, delimiter=";", connectivity_profile="builtin:snp-v1", require_integrity=True)
    values.update(changes)
    return Options(**values)


def by_id(report, prefix):
    return next(item for item in report["checks"] if item["id"] == prefix or item["id"].startswith(prefix + ":"))


def diagnostic(output, error):
    assert {path.name for path in output.iterdir()} == {"report.json"}
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["dataset_published"] is False and report["publication_mode"] is None
    assert error.report_path == output / "report.json"
    return report


def decode_three(output, directory):
    directory.mkdir()
    _, manifest = output_rows(output)
    decoded = []
    for name in ("csv", "dump.sql", "inserts.sql"):
        with Store(directory / (name.replace(".", "-") + ".sqlite"), Model(), bypass_model=True) as store:
            if name == "inserts.sql":
                for entry in manifest["tables"]:
                    store.define((entry["schema"], entry["table"]), entry["fields"])
            (parse_csv if name == "csv" else parse_sql)(output / name, store)
            decoded.append({key: [dict(zip(store.columns(key), row)) for row in store.rows(table["id"])]
                            for key, table in store.tables.items()})
    assert decoded[0] == decoded[1] == decoded[2]


def assert_available_links(output):
    tables, _ = output_rows(output)
    rows = {name: values for (_, name), values in tables.items()}
    assert len(rows) == 11 and not MISSING_TABLES.intersection(rows)
    comp, prod, caps = rows["comp_req_sp"], rows["prodsource_sp"], rows["prod_cap_req_sp"]
    sku, demand, stock = rows["sku_attributes"], rows["sp_demand"], rows["stock_levels"]
    lanes, product_lanes = rows["tlane_mot"], rows["tlane_prod_mot"]
    assert comp[0]["item"] == prod[0]["item"] == caps[0]["item"] == sku[0]["item"] == demand[0]["item"] == stock[0]["item"]
    assert comp[0]["comp"] == comp[1]["item"] == sku[1]["item"]
    assert comp[0]["loc"] == prod[0]["loc"] == caps[0]["loc"] == lanes[0]["locfr"] == product_lanes[0]["locfr"]
    assert lanes[0]["locto"] == lanes[1]["locfr"] == product_lanes[0]["locto"]
    assert lanes[2]["locfr"] == lanes[2]["locto"]
    assert lanes[0]["mot"] == product_lanes[0]["mot"]
    resource = rows["res"][0]["res"]
    assert resource == rows["res"][1]["res"] == rows["res_attributes"][0]["res"] == rows["rescapacity"][1]["res"] == caps[0]["res"]
    assert rows["res"][0]["loc"] != rows["res"][1]["loc"]
    assert prod[0]["prodsource"] == comp[0]["prodsource"] == caps[0]["prodsource"]
    assert demand[0]["dmdstream"] != SHARED_CODE
    assert len({comp[0]["item"], resource, lanes[0]["mot"], prod[0]["prodsource"], demand[0]["dmdstream"]}) == 5
    assert [row["quantity"] for row in demand] == [EXACT, "12.50"]
    assert all(row["datefr"] == DATE_A and row["periodid"] == "1" and "date" not in row for row in caps)
    assert all(row["validfr"] == DATE_A and row["validto"] == DATE_B for row in product_lanes)
    assert demand[0]["sys_id"] == "" and demand[0]["change_author"] == "snp" and demand[0]["update_date_time"] == "SYN_RAW_TIME"
    return rows


@pytest.mark.parametrize("kind", ["csv", "xlsx", "mixed"])
def test_partial_eleven_tables_publish_with_required_checks_enabled_and_repeat(tmp_path, kind):
    source = write_dataset(tmp_path / "source", kind=kind, rows=partial_dataset())
    dumps = []
    for index in range(2):
        output = tmp_path / f"out-{index}"
        log = RunLog()
        report = run(options(tmp_path, source, output=output, log=log))
        assert report["dataset_published"] and report["publication_mode"] == "UNVERIFIED_INTEGRITY"
        assert report["integrity_status"] == "NOT_CHECKED" and not report["relationships_checked"] and not report["keys_checked"]
        assert report["tables"] == 11 and report["rows"] == sum(map(len, partial_dataset().values()))
        assert report["validation"]["preserved_fields_equal"] and report["coverage"]["skipped"] > 0
        assert len(report["coverage"]["required"]) == 66
        assert all(by_id(report, "export:" + artifact)["status"] == "PASS" for artifact in ("csv", "dump", "inserts"))
        assert all(item["status"] != "FAIL" for item in report["checks"])
        assert_available_links(output)
        decode_three(output, tmp_path / f"decoded-{index}")
        dumps.append((output / "dump.sql").read_bytes())
    assert dumps[0] == dumps[1]


def test_missing_report_and_six_column_log_explain_exact_tables_and_columns(tmp_path):
    source = write_dataset(tmp_path / "source", rows=partial_dataset())
    log = RunLog()
    report = run(options(tmp_path, source, log=log))
    assert isinstance(report["integrity_message"], str) and report["integrity_message"]
    missing = [obj for check in report["checks"] if check.get("required") for obj in check.get("missing", [])]
    absent = {obj["table"] for obj in missing if obj["reason"] == "TABLE_MISSING"}
    columns = {(obj["table"], col) for obj in missing if obj["reason"] == "COLUMN_MISSING" for col in obj["columns"]}
    assert absent == MISSING_TABLES and columns == MISSING_COLUMNS
    assert all(obj["namespace"].startswith("ns") for obj in missing)
    key = by_id(report, "bk:prod_cap_req_sp")
    assert key["status"] == "NOT_CHECKED" and "date" in key["message"] and "prod_cap_req_sp" in key["message"]
    assert "datefr" not in [col for obj in key["missing"] for col in obj["columns"]]
    assert by_id(report, "fk:rescapacity:res")["status"] == "PASS"
    assert all(check.get("title") and check.get("message") for check in report["checks"])
    path = tmp_path / "run-log.csv"
    log.write_csv(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream, delimiter=";"))
    assert rows and all(len(row) == 6 for row in rows)
    checks = [row for row in rows[1:] if row[3] == "integrity_check"]
    assert any("prod_cap_req_sp" in row[4] and "date" in row[5] for row in checks)
    assert any("item" in row[5] and "отсутств" in row[5].lower() for row in checks)
    assert any("demandtype" in row[5] and "sp_demand" in row[5] for row in checks)
    assert not any(row[5].startswith("NOT_CHECKED; lost=0") for row in checks)


def test_null_is_not_missing_and_source_duplicates_and_orphans_are_nonfatal(tmp_path):
    rows = partial_dataset()
    rows["comp_req_sp"].append(dict(rows["comp_req_sp"][0]))
    rows["rescapacity"].append(dict(rows["rescapacity"][0], res="SYN_ORPHAN_RESOURCE"))
    for row in rows["res_attributes"]:
        row["resgroup"] = None
    source = write_dataset(tmp_path / "source", rows=rows)
    log = RunLog()
    report = run(options(tmp_path, source, log=log))
    assert report["dataset_published"] and report["source_integrity_status"] == "WARNINGS"
    key = by_id(report, "bk:comp_req_sp")
    assert key["status"] == "PASS" and key["before_count"] == key["after_count"] == 1
    reference = by_id(report, "fk:rescapacity:res")
    assert reference["status"] == "PASS" and reference["unmatched_before"] == reference["unmatched_after"] == 1
    assert not any(obj["table"] == "res_attributes" and "resgroup" in obj["columns"]
                   for check in report["checks"] for obj in check.get("missing", []))
    source_warnings = [entry for entry in log.entries if entry[1] == "warning" and "исход" in entry[5].lower()]
    assert source_warnings and not any(entry[1] == "error" for entry in log.entries)


def temporal_profile(*, optional=False):
    side = {"members": [{"table": "events", "columns": ["stamp"]}, {"table": "missing", "columns": ["stamp"]}], "projection": "bag"}
    return {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-partial", "version": "1",
            "domains": [{"id": "OBJECT", "members": [{"table": "events", "column": "code"}, {"table": "missing", "column": "code"}]}] if optional else [],
            "relations": [{"id": "temporal", "source": side, "target": copy.deepcopy(side), "kind": "join", "nulls": "sql"}],
            "required_checks": ["domain:OBJECT"] if optional else ["temporal"]}


@pytest.mark.parametrize("physical,value", [("timestamp", "2026-01-01 12:00:00"), ("double precision", "1.25")])
def test_missing_member_does_not_hide_unsupported_available_comparison(tmp_path, physical, value):
    source = tmp_path / "source.sql"
    source.write_text(f"CREATE TABLE public.events(code text,stamp {physical}); INSERT INTO public.events VALUES ('SYN_CODE','{value}');", encoding="utf-8")
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, connectivity_profile=temporal_profile()))
    report = diagnostic(output, error.value)
    check = by_id(report, "temporal")
    assert check["status"] == "UNSUPPORTED" and check["reason"] != "RELATION_TARGET_MISSING"
    assert check["message"] and report["error_code"] == "CHECK_UNSUPPORTED"


def test_optional_unsupported_does_not_create_a_new_partial_publication_block(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.events(code text,stamp timestamp); INSERT INTO public.events VALUES ('SYN_CODE','2026-01-01 12:00:00');", encoding="utf-8")
    report = run(options(tmp_path, source, connectivity_profile=temporal_profile(optional=True)))
    assert report["dataset_published"] and report["integrity_status"] == "NOT_CHECKED"
    assert by_id(report, "temporal")["status"] == "UNSUPPORTED" and not by_id(report, "temporal")["required"]


@pytest.mark.parametrize("missing_kind", ["table", "column"])
@pytest.mark.parametrize("system_side", ["source", "target"])
def test_missing_before_system_member_cannot_relax_required_system_policy(tmp_path, missing_kind, system_side):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.events(code text,sys_id text);"
                      "INSERT INTO public.events VALUES ('SYN_CODE','SYN_SYS');", encoding="utf-8")
    absent = {"table": "missing" if missing_kind == "table" else "events",
              "columns": ["code" if missing_kind == "table" else "absent_column"]}
    regular = {"members": [{"table": "events", "columns": ["code"]}], "projection": "bag"}
    mixed = {"members": [absent, {"table": "events", "columns": ["sys_id"]},
                         {"table": "events", "columns": ["code"]}], "projection": "bag"}
    relation = {"id": "system_relation", "source": mixed if system_side == "source" else regular,
                "target": mixed if system_side == "target" else regular, "kind": "join", "nulls": "sql"}
    profile = {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-system-partial", "version": "1",
               "domains": [], "relations": [relation], "required_checks": ["system_relation"]}
    log = RunLog()
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, connectivity_profile=profile, log=log))
    report = diagnostic(tmp_path / "out", error.value)
    check = by_id(report, "system_relation")
    assert check["status"] == "NOT_CHECKED" and check["reason"] == "SYSTEM_FIELD_POLICY"
    assert not check.get("partial") and "Системные поля" in check["message"]
    assert any(item["reason"] == ("TABLE_MISSING" if missing_kind == "table" else "COLUMN_MISSING")
               for item in check["missing"])
    assert any(entry[3] == "integrity_check" and entry[4] == check["title"] and "Системные поля" in entry[5]
               for entry in log.entries)
    assert "SYN_CODE" not in json.dumps(report) and "SYN_SYS" not in json.dumps(report)


def test_explicit_block_policy_stays_strict(tmp_path):
    source = write_sql(tmp_path / "source.sql", rows=partial_dataset())
    profile = load_profile("builtin:snp-v1")
    profile["missing_target_policy"] = "block"
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, connectivity_profile=profile))
    report = diagnostic(output, error.value)
    assert report["integrity_status"] == "NOT_CHECKED" and report["error_code"] == "RELATION_TARGET_MISSING"


def test_profile_without_required_set_is_still_refused(tmp_path):
    profile = load_profile("builtin:snp-v1")
    profile["required_checks"] = []
    # Preserve declared TEXT types without relying on canonical-SNP hints.
    sql = write_sql(tmp_path / "source.sql", rows=partial_dataset())
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, sql, connectivity_profile=profile))
    report = diagnostic(tmp_path / "out", error.value)
    assert report["integrity_status"] == "NOT_CHECKED"
    assert "NO_REQUIRED_CHECKS" in report["limitations"]
    assert not any(item.get("required") for item in report["checks"]
                   if item["kind"] != "export" and item["id"] != "completion_gate")


def test_cross_schema_missing_target_uses_only_pseudonyms(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE source_private_xyz.child(code text,parent_code text REFERENCES absent_private_xyz.parent(code));"
                      "INSERT INTO source_private_xyz.child VALUES ('SYN_CODE','SYN_PARENT');", encoding="utf-8")
    profile = {"format": "snp-anonymizer-connectivity-v1", "profile_id": "missing-namespace", "version": "1",
               "domains": [{"id": "CODE", "members": [{"table": "child", "column": "code"}]}],
               "required_checks": ["domain:CODE"]}
    log = RunLog()
    report = run(options(tmp_path, source, connectivity_profile=profile, log=log))
    missing = [obj for check in report["checks"] for obj in check.get("missing", [])]
    parent, = [obj for obj in missing if obj["table"] == "parent"]
    assert parent["reason"] == "TABLE_MISSING" and parent["namespace"] == "ns2"
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "source_private_xyz" not in text and "absent_private_xyz" not in text
    assert all("source_private_xyz" not in entry[5] and "absent_private_xyz" not in entry[5] for entry in log.entries)


def test_multi_schema_titles_and_missing_messages_distinguish_private_namespaces(tmp_path):
    source = tmp_path / "source.sql"
    private_schemas = ("synthetic_private_a", "synthetic_private_b")
    source.write_text("".join(f"CREATE TABLE {schema}.events(code text);"
                              f"INSERT INTO {schema}.events VALUES ('SYN_SHARED');" for schema in private_schemas),
                      encoding="utf-8")
    profile = {"format": "snp-anonymizer-connectivity-v1", "profile_id": "synthetic-two-namespaces", "version": "1",
               "domains": [{"id": "OBJECT", "members": [{"table": "events", "column": "code"},
                                                          {"table": "missing", "column": "code"}]}],
               "relations": [{"id": "parent", "source": {"members": [{"table": "events", "columns": ["code"]}]},
                              "target": {"members": [{"table": "missing", "columns": ["code"]}]}, "kind": "join"}],
               "keys": [{"id": "key", "table": "events", "columns": ["code"], "kind": "bk"}],
               "required_checks": ["domain:OBJECT", "parent", "key"]}
    log = RunLog()
    report = run(options(tmp_path, source, connectivity_profile=profile, log=log))
    assert report["dataset_published"] and report["integrity_status"] == "NOT_CHECKED"
    for prefix in ("domain:OBJECT", "parent", "key"):
        checks = [item for item in report["checks"] if item["id"].startswith(prefix + ":")]
        assert len(checks) == 2 and len({item["title"] for item in checks}) == 2
        assert {item["title"].rsplit(" ", 1)[1] for item in checks} == {"[ns1]", "[ns2]"}
        for check in checks:
            assert any(entry[3] == "integrity_check" and entry[4:] == (check["title"], check["message"])
                       for entry in log.entries)
    missing = [obj for check in report["checks"] for obj in check.get("missing", [])]
    assert {obj["namespace"] for obj in missing} == {"ns1", "ns2"}
    assert all(obj["qualified"] and obj["table"] == "missing" and obj["reason"] == "TABLE_MISSING" for obj in missing)
    for namespace in ("ns1", "ns2"):
        assert namespace + ".missing" in report["integrity_message"]
        checks = [item for item in report["checks"] if item["id"].startswith("parent:") and item["id"].endswith(namespace)]
        assert len(checks) == 1 and namespace + ".missing" in checks[0]["message"]
    tables, _ = output_rows(tmp_path / "out")
    assert len(tables) == 2 and all(rows[0]["code"] != "SYN_SHARED" for rows in tables.values())
    public = json.dumps(report, ensure_ascii=False) + json.dumps(log.entries, ensure_ascii=False)
    assert all(schema not in public for schema in private_schemas)
    assert "SYN_SHARED" not in public


def test_saved_project_conflict_keeps_safe_full_cause_in_report_log_and_exception(tmp_path):
    # Explicit physical TEXT avoids unrelated CSV inference for a different profile.
    source = write_sql(tmp_path / "source.sql", rows=partial_dataset())
    run(options(tmp_path, source))
    project = tmp_path / "private.snpanon"
    before = hashlib.sha256(project.read_bytes()).hexdigest()
    log = RunLog()
    output = tmp_path / "refused"
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, output=output, connectivity_profile="builtin:optimizer-v2", log=log))
    report = diagnostic(output, error.value)
    assert report["error_code"] == "CONTRACT_CONFLICT"
    message = report["error_message"]
    assert "проект" in message.lower() and "создайте" in message.lower()
    assert message in str(error.value) and any(message in item.get("message", "") for item in report["checks"])
    assert any(message in entry[5] for entry in log.entries)
    assert hashlib.sha256(project.read_bytes()).hexdigest() == before


def test_skipped_table_is_distinct_from_missing_table_and_null_column(tmp_path):
    from openpyxl import load_workbook

    rows = dataset(optional=True)
    source = write_dataset(tmp_path / "source", kind="xlsx", rows=rows)
    path = source / "item.xlsx"
    book = load_workbook(path)
    book.active.cell(2, 1, "=1+1")
    book.save(path)
    book.close()
    report = run(options(tmp_path, source, skip_error_tables=True))
    assert report["dataset_published"] and report["integrity_status"] == "NOT_CHECKED"
    missing = [obj for check in report["checks"] for obj in check.get("missing", [])]
    item = [obj for obj in missing if obj["table"] == "item"]
    assert item and all(obj["reason"] == "TABLE_SKIPPED" for obj in item)
    assert all(obj["table"] != "item" or obj["reason"] != "TABLE_MISSING" for obj in missing)
    assert "=1+1" not in json.dumps(report)


@pytest.mark.parametrize("artifact", ["csv", "dump.sql", "inserts.sql"])
def test_partial_coverage_never_allows_a_corrupted_export(tmp_path, monkeypatch, artifact):
    source = write_dataset(tmp_path / "source", rows=partial_dataset())
    close = Exporter.close
    def corrupt(self, success):
        result = close(self, success)
        if success:
            if artifact == "csv":
                manifest = json.loads((self.directory / "csv/manifest.json").read_text(encoding="utf-8"))
                path = next(self.directory / "csv" / entry["file"] for entry in manifest["tables"] if entry["table"] == "sp_demand")
            else:
                path = self.directory / artifact
            text = path.read_text(encoding="utf-8")
            assert EXACT in text
            path.write_text(text.replace(EXACT, "-2476.700000000000000002", 1), encoding="utf-8")
        return result
    monkeypatch.setattr(Exporter, "close", corrupt)
    monkeypatch.setattr(pipeline, "pg_load", lambda *args, **kw: pytest.fail("PG reached after corrupted export"))
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source))
    report = diagnostic(tmp_path / "out", error.value)
    assert report["integrity_status"] == "FAILED" and any(item["status"] == "FAIL" for item in report["checks"])


def test_partial_coverage_cancellation_after_final_check_never_publishes(tmp_path):
    source = write_dataset(tmp_path / "source", rows=partial_dataset())
    cancel = Event()
    def progress(stage, amount, message):
        if stage == "verify_exports" and amount == 1:
            cancel.set()
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source), progress=progress, cancel=cancel)
    report = diagnostic(tmp_path / "out", error.value)
    assert report["error_code"] == "CHECK_INCOMPLETE"


def test_unknown_required_skip_reason_is_not_treated_as_missing(tmp_path, monkeypatch):
    source = write_dataset(tmp_path / "source", rows=partial_dataset())
    summarize = IntegrityVerifier._summary
    def unknown(self):
        result = summarize(self)
        target = next(item for item in result["checks"] if item.get("required") and item["status"] == "NOT_CHECKED")
        target["reason"] = "SYN_UNKNOWN_REASON"
        return result
    monkeypatch.setattr(IntegrityVerifier, "_summary", unknown)
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source))
    diagnostic(tmp_path / "out", error.value)


def test_missing_details_are_complete_and_metadata_limit_remains_fatal(tmp_path, monkeypatch):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.events(code text); INSERT INTO public.events VALUES ('SYN_CODE');", encoding="utf-8")
    profile = {"format": "snp-anonymizer-connectivity-v1", "profile_id": "many-missing", "version": "1",
               "domains": [{"id": "CODE", "members": [{"table": "events", "column": "code"}] +
                           [{"table": f"missing{index}", "column": "code"} for index in range(100)]}],
               "required_checks": ["domain:CODE"]}
    report = run(options(tmp_path, source, connectivity_profile=profile))
    check = by_id(report, "domain:CODE")
    assert len(check["missing"]) == 100 and {obj["table"] for obj in check["missing"]} == {f"missing{index}" for index in range(100)}
    import anonymizer.connectivity as module
    monkeypatch.setattr(module, "MAX_CONTRACT_BYTES", 1024)
    with pytest.raises(IntegrityError) as error:
        run(options(tmp_path, source, output=tmp_path / "limited", project=tmp_path / "limited.snpanon", connectivity_profile=profile))
    result = diagnostic(tmp_path / "limited", error.value)
    assert result["error_code"] == "CHECK_INCOMPLETE"

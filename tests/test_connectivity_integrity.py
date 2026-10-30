"""Synthetic read-back/identity acceptance independent of code generation."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from anonymizer.connectivity import Contract
from anonymizer.errors import Cancelled
from anonymizer.exporters import Exporter
from anonymizer.integrity import IntegrityVerifier


def field(name, kind="TEXT", **kw):
    return {"name": name, "type": kind, "sql_type": {"TEXT": "text", "NUMBER": "numeric", "UUID": "uuid"}[kind], "nullable": True, **kw}


def side(table, columns, projection="bag"):
    return {"members": [{"table": ("private_source", table), "columns": columns}], "projection": projection}


def relation(columns=("code",), *, nulls="sql", kind="fk", projection="bag"):
    return {"id": "relation:test:ns1", "kind": kind, "source": side("child", list(columns)),
            "target": side("master", list(columns), projection), "nulls": nulls,
            "status": "PENDING", "required": True}


def prepare(tmp_path, specs, contract, *, mutate=None):
    tables, rows = {}, {}
    for tid, (name, fields, old, new) in enumerate(specs, 1):
        identity = ("private_source", name)
        tables[identity] = {"id": tid, "fields": fields, "rows": len(old), "physical_columns": {f["name"] for f in fields}}
        rows[identity] = (old, new)
    db = sqlite3.connect(tmp_path / "private.sqlite")
    db.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY,tid INTEGER NOT NULL,body TEXT NOT NULL)")
    store = SimpleNamespace(db=db, tables=tables, bypass_model=True)
    directory = tmp_path / "out"
    directory.mkdir()
    schemas = {"private_source": "snp_a000001"}
    verifier = IntegrityVerifier(store, contract)
    exporter = Exporter(directory, tables, schemas)
    rid = 0
    for name, table in tables.items():
        exporter.start(name, table)
        old, new = rows[name]
        for before, after in zip(old, new, strict=True):
            rid += 1
            db.execute("INSERT INTO rows VALUES (?,?,?)", (rid, table["id"], json.dumps(before)))
            verifier.expected_row(name, rid, before, after)
            exporter.row(after)
        exporter.end()
    exporter.close(True)
    if mutate:
        mutate(directory)
    return db, verifier, directory, schemas


def run_case(tmp_path, specs, contract, **kw):
    db, verifier, directory, schemas = prepare(tmp_path, specs, contract, **kw)
    try:
        return verifier.verify(directory, schemas)
    finally:
        db.close()


def result_for(report, identifier):
    return next(item for item in report["checks"] if item["id"] == identifier)


def test_exact_join_pairs_detect_swap_even_when_counts_equal(tmp_path):
    rel = relation(kind="join")
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    report = run_case(tmp_path, [
        ("child", [field("code")], [["A"], ["B"]], [["X"], ["Y"]]),
        ("master", [field("code")], [["A"], ["B"]], [["Y"], ["X"]]),
    ], contract)
    check = result_for(report, rel["id"])
    assert check["before_count"] == check["after_count"] == 2
    assert check["lost"] == check["added"] == 2
    assert check["status"] == "FAIL" and report["integrity_status"] == "FAILED"
    assert report["relationships_checked"] and not report["dataset_published"]
    assert "private_source" not in json.dumps(report)


def test_composite_fk_preserves_original_unmatched_reference_and_warns(tmp_path):
    rel = relation(("code", "n"), nulls="match_simple")
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    fields = [field("code"), field("n", "NUMBER")]
    report = run_case(tmp_path, [
        ("child", fields, [["A", "2"]], [["X", "2"]]),
        ("master", fields, [["A", "1"], ["B", "2"]], [["X", "1"], ["Y", "2"]]),
    ], contract)
    check = result_for(report, rel["id"])
    assert check["status"] == "PASS" and check["lost"] == check["added"] == 0
    assert check["unmatched_before"] == check["unmatched_after"] == 1
    assert report["source_integrity_status"] == "WARNINGS" and report["integrity_status"] == "PASSED"


@pytest.mark.parametrize("nulls,expected", [("sql", 1), ("match_simple", 1), ("null_safe", 2)])
def test_empty_text_is_value_and_null_semantics_are_explicit(tmp_path, nulls, expected):
    rel = relation(nulls=nulls)
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    rows = [[""], [None]]
    report = run_case(tmp_path, [(name, [field("code")], rows, rows) for name in ("child", "master")], contract)
    check = result_for(report, rel["id"])
    assert check["status"] == "PASS" and check["before_count"] == check["after_count"] == expected


@pytest.mark.parametrize("projection,expected", [("bag", 2), ("set", 1)])
def test_projection_set_and_bag_have_distinct_multiplicity(tmp_path, projection, expected):
    rel = relation(projection=projection)
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    report = run_case(tmp_path, [
        ("child", [field("code")], [["A"]], [["X"]]),
        ("master", [field("code")], [["A"], ["A"]], [["X"], ["X"]]),
    ], contract)
    check = result_for(report, rel["id"])
    assert check["status"] == "PASS" and check["before_count"] == check["after_count"] == expected


def test_set_projection_membership_change_is_detected(tmp_path):
    rel = relation(projection="set", kind="join")
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    report = run_case(tmp_path, [
        ("child", [field("code")], [["A"], ["B"]], [["X"], ["Y"]]),
        ("master", [field("code")], [["A"], ["A"], ["B"], ["B"]], [["X"], ["Y"], ["X"], ["Y"]]),
    ], contract)
    check = result_for(report, rel["id"])
    assert check["status"] == "FAIL" and check["projection_membership_changes"] > 0
    assert check["before_count"] == check["after_count"] == 2


def test_key_partition_changes_are_detected_even_with_same_duplicate_counts(tmp_path):
    key = {"id": "key:test:ns1", "table": ("private_source", "child"), "columns": ["code"],
           "kind": "bk", "nulls": "not_distinct", "status": "PENDING", "required": True}
    report = run_case(tmp_path, [
        ("child", [field("code")], [["A"], ["A"], ["B"], ["B"]], [["X"], ["Y"], ["X"], ["Y"]]),
    ], Contract(keys=[key], required_checks=[key["id"]]))
    check = result_for(report, key["id"])
    assert check["before_count"] == check["after_count"] == 2
    assert check["status"] == "FAIL" and check["membership_changes"] > 0


@pytest.mark.parametrize("nulls,duplicates", [("distinct", 0), ("not_distinct", 1)])
def test_key_null_distinct_policy_and_source_warnings(tmp_path, nulls, duplicates):
    key = {"id": "key:test:ns1", "table": ("private_source", "child"), "columns": ["code"],
           "kind": "unique", "nulls": nulls, "status": "PENDING", "required": True}
    report = run_case(tmp_path, [("child", [field("code")], [[None], [None]], [[None], [None]])],
                      Contract(keys=[key], required_checks=[key["id"]]))
    check = result_for(report, key["id"])
    assert check["status"] == "PASS" and check["before_count"] == check["after_count"] == duplicates
    assert report["source_integrity_status"] == ("WARNINGS" if duplicates else "PASSED")


@pytest.mark.parametrize("mode", ["csv", "dump", "inserts"])
def test_each_actual_artifact_is_read_and_tampering_blocks_output(tmp_path, mode):
    def mutate(directory):
        path = directory / {"csv": "csv/snp_a000001/child.csv", "dump": "dump.sql", "inserts": "inserts.sql"}[mode]
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("SYN_NEW", "SYN_BAD"), encoding="utf-8")
    report = run_case(tmp_path, [("child", [field("code")], [["SYN_SOURCE"]], [["SYN_NEW"]])], Contract(), mutate=mutate)
    assert result_for(report, "export:" + mode)["status"] == "FAIL"
    assert report["integrity_status"] == "FAILED" and not report["dataset_published"]


def test_numeric_null_backslash_and_system_projection_match_three_artifacts(tmp_path):
    fields = [field("note"), field("quantity", "NUMBER"), field("sys_id", "NUMBER", sql_type="bigint"),
              field("change_author"), field("update_date_time")]
    old = [[None, None, "99", "SYN_AUTHOR", r"\N"], ["", "0", None, None, ""], [r"\N", "1234567890.123456789", "1", "", None]]
    new = [[None, None, None, "snp", r"\N"], ["", "0", None, "snp", ""], [r"\N", "1234567890.123456789", None, "snp", None]]
    report = run_case(tmp_path, [("child", fields, old, new)], Contract())
    assert all(item["status"] == "PASS" for item in report["checks"])
    assert report["integrity_status"] == "NOT_CHECKED" and report["publication_mode"] == "UNVERIFIED_INTEGRITY"


@pytest.mark.parametrize("key_only", [False, True])
@pytest.mark.parametrize("before,after", [(None, "SYN_CODE"), ("SYN_CODE", None), ("", "SYN_CODE"), ("SYN_CODE", "")])
def test_faulty_planned_null_or_empty_change_blocks_even_unverified_output(tmp_path, key_only, before, after):
    # All exports faithfully reproduce the faulty mapper's planned row. A
    # singleton UNIQUE NULLS DISTINCT key also retains its exact partition.
    key = {"id": "key:test:ns1", "table": ("private_source", "child"), "columns": ["code"],
           "kind": "unique", "nulls": "distinct", "status": "PENDING", "required": True}
    contract = Contract(profile={} if key_only else None, keys=[key] if key_only else [],
                        required_checks=[key["id"]] if key_only else [])
    report = run_case(tmp_path, [("child", [field("code")], [[before]], [[after]])], contract)
    for artifact in ("csv", "dump", "inserts"):
        check = result_for(report, "export:" + artifact)
        assert check["status"] == "FAIL" and check["preserved_value_changes"] == 1
    assert report["integrity_status"] == "FAILED"
    assert not report["dataset_published"] and report["publication_mode"] is None
    assert "SYN_CODE" not in json.dumps(report)


@pytest.mark.parametrize("before,after", [(None, "SYN_CODE"), ("SYN_CODE", None), ("", "SYN_CODE")])
def test_assigned_text_domain_preserves_nullness_but_may_replace_empty_identifier(tmp_path, before, after):
    domain = {"id": "domain:CODE:ns1", "domain_id": "CODE", "namespace": "private_source",
              "members": [{"table": ("private_source", "child"), "column": "code"}],
              "kind": "TEXT", "equality": "exact", "status": "PENDING", "required": True}
    contract = Contract(profile={}, domains=[domain], required_checks=[domain["id"]],
                        assignments={("private_source", "child", "code"): domain})
    report = run_case(tmp_path, [("child", [field("code")], [[before]], [[after]])], contract)
    expected = "PASSED" if before == "" else "FAILED"
    assert report["integrity_status"] == expected
    assert report["dataset_published"] == (expected == "PASSED")


@pytest.mark.parametrize("with_profile", [False, True])
def test_mutated_input_argument_cannot_replace_authoritative_source_spool(tmp_path, with_profile):
    db, verifier, directory, schemas = prepare(tmp_path, [
        ("child", [field("quantity", "NUMBER")], [["10.000001"]], [["20.000001"]]),
    ], Contract(profile={} if with_profile else None))
    try:
        # Recreate the original bug: a mapper changes the very list later
        # supplied as both old and planned. The immutable rows table stays old.
        db.execute("DELETE FROM integrity_rows")
        verifier.ordinals[("private_source", "child")] = 0
        row = ["10.000001"]
        def faulty_mapper(value):
            value[0] = "20.000001"
            return value
        new = faulty_mapper(row)
        verifier.expected_row(("private_source", "child"), 1, row, new)
        report = verifier.verify(directory, schemas)
        assert report["integrity_status"] == "FAILED" and not report["dataset_published"]
        assert all(result_for(report, "export:" + artifact)["preserved_value_changes"] == 1
                   for artifact in ("csv", "dump", "inserts"))
    finally:
        db.close()


@pytest.mark.parametrize("with_profile", [False, True])
def test_pipeline_in_place_quantity_mutation_publishes_only_diagnostic(tmp_path, monkeypatch, with_profile):
    from anonymizer.errors import IntegrityError
    from anonymizer.mapping import Transformer
    from anonymizer.pipeline import Options, run

    source = tmp_path / "source"
    source.mkdir()
    (source / "child.csv").write_text("quantity\n10.000001\n", encoding="utf-8")
    def faulty_mapper(self, schema, table, fields, row):
        row[next(i for i, value in enumerate(fields) if value["name"] == "quantity")] = "20.000001"
        return row
    monkeypatch.setattr(Transformer, "transform", faulty_mapper)
    output = tmp_path / "out"
    with pytest.raises(IntegrityError):
        run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True, delimiter=",",
                    connectivity_profile="builtin:optimizer-v1" if with_profile else None))
    assert [path.name for path in output.iterdir()] == ["report.json"]
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["integrity_status"] == "FAILED" and not report["dataset_published"]
    # The legacy transform guard may stop before exports: both independent
    # guards must reject, and this full-pipeline test permits the earlier gate.
    assert any(item["status"] == "FAIL" for item in report["checks"])


def test_changed_rows_counts_a_row_once_when_export_and_system_policy_both_fail(tmp_path):
    db, verifier, _, _ = prepare(tmp_path, [
        ("child", [field("sys_id", "NUMBER", sql_type="bigint")], [["99"]], [[None]]),
    ], Contract())
    try:
        verifier._read_artifact("csv", lambda emit: emit(("private_source", "child"), ["1"]))
        check = result_for({"checks": verifier.checks}, "export:csv")
        assert check["status"] == "FAIL" and check["changed_rows"] == 1
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["split", "merge", "numeric"])
def test_domain_equivalence_and_injectivity_are_independently_checked(tmp_path, mode):
    domain = {"id": "domain:LOCATION:ns1", "domain_id": "LOCATION", "namespace": "private_source",
              "members": [{"table": ("private_source", "child"), "column": "code"}],
              "kind": "NUMBER" if mode == "numeric" else "TEXT", "status": "PENDING", "required": True}
    if mode == "split":
        before, after = [["A"], ["A"]], [["X"], ["Y"]]
    elif mode == "merge":
        before, after = [["A"], ["B"]], [["X"], ["X"]]
    else:
        before, after = [["1"], ["1.0"]], [["2"], ["3"]]
    contract = Contract(profile={}, domains=[domain], required_checks=[domain["id"]],
                        assignments={("private_source", "child", "code"): domain})
    report = run_case(tmp_path, [("child", [field("code", "NUMBER" if mode == "numeric" else "TEXT")], before, after)], contract)
    assert result_for(report, domain["id"])["status"] == "FAIL"


def test_missing_target_is_not_an_empty_target(tmp_path):
    rel = relation()
    contract = Contract(relations=[rel], required_checks=[rel["id"]])
    missing = run_case(tmp_path, [("child", [field("code")], [["A"]], [["X"]])], contract)
    assert result_for(missing, rel["id"])["status"] == "NOT_CHECKED"
    other = tmp_path / "empty"; other.mkdir()
    empty = run_case(other, [("child", [field("code")], [["A"]], [["X"]]), ("master", [field("code")], [], [])], contract)
    assert result_for(empty, rel["id"])["status"] == "PASS"
    assert result_for(empty, rel["id"])["source_defects"] == 1


def test_unsupported_checks_and_system_fields_never_claim_pass(tmp_path):
    key = {"id": "audit", "table": ("private_source", "child"), "columns": ["sys_id"], "status": "PENDING", "required": True}
    rel = {"id": "future", "status": "UNSUPPORTED", "reason": "CHECK_UNSUPPORTED", "required": True}
    report = run_case(tmp_path, [("child", [field("sys_id", "NUMBER", sql_type="bigint"), field("note")], [["99", ""]], [[None, ""]])],
                      Contract(keys=[key], relations=[rel], required_checks=["audit", "future"]))
    assert result_for(report, "audit")["status"] == "NOT_CHECKED"
    assert result_for(report, "future")["status"] == "UNSUPPORTED"
    assert not report["relationships_checked"] and not report["keys_checked"]
    assert report["integrity_status"] == "NOT_CHECKED"


def test_sql_extra_expression_is_rejected_without_echoing_data(tmp_path):
    def mutate(directory):
        path = directory / "inserts.sql"
        path.write_text(path.read_text().replace("E'SYN_NEW'", "upper(E'SYN_NEW')"))
    report = run_case(tmp_path, [("child", [field("code")], [["SYN_SOURCE"]], [["SYN_NEW"]])], Contract(), mutate=mutate)
    assert result_for(report, "export:inserts")["status"] == "FAIL"
    assert "SYN_NEW" not in json.dumps(report) and "SYN_SOURCE" not in json.dumps(report)


@pytest.mark.parametrize("bad", ["identifier", "quoted_identifier", "number", "comment", "dollar", "psql", "block_comment"])
def test_damaged_sql_control_tokens_are_bounded_and_fail_safely(tmp_path, bad):
    def mutate(directory):
        path = directory / "inserts.sql"
        with path.open("w", encoding="utf-8") as stream:
            stream.write({"identifier": "A", "quoted_identifier": '"A', "number": "1", "comment": "--",
                          "dollar": "$tag$", "psql": "\\", "block_comment": "/*"}[bad])
            for _ in range(32):
                stream.write("9" * 65536)
    report = run_case(tmp_path, [("child", [field("code")], [["SYN_SOURCE"]], [["SYN_NEW"]])], Contract(), mutate=mutate)
    assert result_for(report, "export:inserts")["status"] == "FAIL"
    assert report["integrity_status"] == "FAILED" and not report["dataset_published"]
    assert "SYN_SOURCE" not in json.dumps(report)


@pytest.mark.parametrize("token", ["A", "1", "--"])
def test_bad_control_token_memory_is_independent_of_remaining_file_size(tmp_path, token):
    import tracemalloc
    from anonymizer.errors import AnonymizerError
    from anonymizer.export_readback import _OutputReader, _OutputLexer

    peaks = []
    for blocks in (1, 128):
        path = tmp_path / (str(blocks) + ".sql")
        with path.open("w", encoding="utf-8") as stream:
            stream.write(token)
            for _ in range(blocks):
                stream.write("9" * 65536)
        with path.open(encoding="utf-8") as stream:
            lex = _OutputLexer(_OutputReader(stream))
            tracemalloc.start()
            try:
                with pytest.raises(AnonymizerError):
                    lex.pop()
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
    # Both fail within one fixed 64 KiB Reader chunk; an 8 MiB file is not read.
    assert max(peaks) < 1024 * 1024
    assert abs(peaks[0] - peaks[1]) < 256 * 1024


def test_output_value_above_input_token_overhead_limit_is_still_valid(tmp_path):
    from anonymizer.export_readback import _OutputReader, _OutputLexer, _groups

    path = tmp_path / "large_literal.sql"
    size = 2 * 1024**2 + 128
    with path.open("w", encoding="utf-8") as stream:
        stream.write("(E'")
        stream.write("x" * size)
        stream.write("')")
    with path.open(encoding="utf-8") as stream:
        groups = list(_groups(_OutputLexer(_OutputReader(stream))))
    assert len(groups) == 1 and len(groups[0]) == 1
    assert groups[0][0].kind == "string" and len(groups[0][0].value) == size


def test_serialized_string_token_has_its_own_byte_limit(monkeypatch):
    import io
    from anonymizer import export_readback
    from anonymizer.errors import AnonymizerError

    monkeypatch.setattr(export_readback, "MAX_RECORD", 1024)
    lex = export_readback._OutputLexer(export_readback._OutputReader(io.StringIO("E'" + "Ж" * 1024 + "'")))
    with pytest.raises(AnonymizerError, match="EXPORT_READBACK_INVALID"):
        lex.pop()


def test_sqlite_progress_handler_propagates_cancellation(tmp_path):
    db, verifier, _, _ = prepare(tmp_path, [("child", [field("code")], [["A"]], [["B"]])], Contract())
    def cancel():
        raise Cancelled("synthetic")
    verifier.check = cancel
    try:
        with pytest.raises(Cancelled):
            with verifier._progress():
                db.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(x) FROM n").fetchone()
        assert db.execute("SELECT 1").fetchone() == (1,)
    finally:
        db.close()


def test_complete_optimizer_profile_preserves_transit_and_supplier_alias(tmp_path):
    import csv
    from anonymizer.pipeline import Options, run

    source = tmp_path / "source"
    source.mkdir()
    loc_a, loc_t, loc_b = "SYN_SOURCE_LOC_A", "SYN_SOURCE_LOC_T", "SYN_SOURCE_LOC_B"
    product, resource, bom = "SYN_PRODUCT", "SYN_RESOURCE", "SYN_BOM"
    rows = {
        "optimizer_periods": [["datasetid", "period"], [11, 0]],
        "optimizer_demand": [["datasetid", "period", "location", "product", "client", "quantity"], [11, 0, loc_b, product, loc_a, "10.000001"]],
        "optimizer_procurement": [["datasetid", "period", "location", "product", "supplier"], [11, 0, loc_a, product, loc_a]],
        "optimizer_production": [["datasetid", "period", "location", "product", "bomnum", "resource", "extra"], [11, 0, loc_a, product, bom, resource, "SYN_PRIVATE_NOTE"]],
        "optimizer_storage": [["datasetid", "period", "location", "product", "resource"], [11, 0, loc_b, product, resource]],
        "optimizer_bom": [["datasetid", "period", "location", "product", "bomnum", "input_output"], [11, 0, loc_a, product, bom, 1]],
        "optimizer_transportation": [["datasetid", "period", "loc_from", "loc_to", "product", "transport_type", "resource"],
                                     [11, 0, loc_a, loc_t, product, loc_a, resource], [11, 0, loc_t, loc_b, product, loc_a, resource]],
    }
    for table, data in rows.items():
        with (source / (table + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(data)
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon", bypass_model=True,
                         csv_schema="private_source", connectivity_profile="builtin:optimizer-v1", require_integrity=True))
    assert report["integrity_status"] == "PASSED" and report["source_integrity_status"] == "PASSED"
    assert not report["model_applied"] and report["relationships_checked"] and report["keys_checked"]
    assert report["publication_mode"] == "VERIFIED_INTEGRITY"
    manifest = json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))
    actual = {}
    for entry in manifest["tables"]:
        with (output / "csv" / entry["file"]).open(encoding="utf-8", newline="") as stream:
            actual[entry["table"]] = list(csv.DictReader(stream))
    procurement, = actual["optimizer_procurement"]
    demand, = actual["optimizer_demand"]
    first, second = actual["optimizer_transportation"]
    assert procurement["supplier"] == procurement["location"] == first["loc_from"]
    assert first["loc_to"] == second["loc_from"]  # T exists only as transit
    assert second["loc_to"] == demand["location"]
    assert len({procurement["location"], demand["client"], first["transport_type"]}) == 3
    assert demand["quantity"] == "10.000001"
    assert "extra" in actual["optimizer_production"][0]
    for path in output.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert all(secret not in text for secret in ("private_source", loc_a, loc_t, loc_b, product, resource, bom, "SYN_PRIVATE_NOTE"))

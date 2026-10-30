"""A refused dataset exposes only a safe diagnostic, never a partial package."""
import json

import pytest

from anonymizer.errors import IntegrityError
from anonymizer.integrity_publication import failure_report, publish_diagnostic
from anonymizer.pipeline import Options, run


def test_diagnostic_publication_is_new_directory_only(tmp_path):
    output = tmp_path / "out"
    report = failure_report("CHECK_INCOMPLETE")
    path = publish_diagnostic(output, report)
    assert path == output / "report.json"
    assert [p.name for p in output.iterdir()] == ["report.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["dataset_published"] is False
    with pytest.raises(OSError):
        publish_diagnostic(output, failure_report("CONTRACT_CONFLICT"))
    assert json.loads(path.read_text(encoding="utf-8"))["error_code"] == "CHECK_INCOMPLETE"
    assert not list(tmp_path.glob(".snp-diagnostic-*"))


def test_cancel_after_completed_checks_never_reports_passed():
    result = {"checks": [{"id": "source_link", "kind": "relation", "status": "PASS", "required": True}],
              "coverage": {"required": ["source_link"]}, "relationships_checked": True,
              "keys_checked": False, "integrity_status": "PASSED"}
    report = failure_report("CHECK_INCOMPLETE", result=result)
    assert report["integrity_status"] == "NOT_CHECKED"
    assert report["publication_mode"] is None
    assert report["coverage"]["skipped"] == 1
    assert "completion_gate" in report["coverage"]["required"]
    assert report["relationships_checked"] is True  # actual execution, not success flag


def test_require_profileless_bypass_publishes_only_diagnostic(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("note,quantity\nSYN_NOTE,1\n", encoding="utf-8")
    output, project = tmp_path / "out", tmp_path / "private.snpanon"
    with pytest.raises(IntegrityError) as caught:
        run(Options(source, output, project, bypass_model=True, require_integrity=True))
    report = json.loads(caught.value.report_path.read_text(encoding="utf-8"))
    assert report["integrity_status"] == "NOT_CHECKED" and not report["model_applied"]
    assert report["publication_mode"] is None and not report["dataset_published"]
    assert [p.name for p in output.iterdir()] == ["report.json"]
    assert not list(tmp_path.glob(".snp-anon-*"))
    assert "SYN_NOTE" not in caught.value.report_path.read_text(encoding="utf-8")


def test_invalid_profile_has_no_dataset_or_private_mapping(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("CREATE TABLE public.custom(note text); INSERT INTO public.custom VALUES ('SYN_PRIVATE');", encoding="utf-8")
    output = tmp_path / "out"
    with pytest.raises(IntegrityError) as caught:
        run(Options(source, output, tmp_path / "private.snpanon", connectivity_profile={"SYN_PRIVATE_KEY": 1}))
    assert caught.value.code == "CONTRACT_CONFLICT"
    assert [p.name for p in output.iterdir()] == ["report.json"]
    assert "SYN_PRIVATE" not in (output / "report.json").read_text(encoding="utf-8")


def test_skipped_table_report_does_not_disclose_its_source_schema(tmp_path):
    source = tmp_path / "source.sql"
    source.write_text("INSERT INTO client_hidden_namespace.sku(item) VALUES ('SYN_ITEM'); "
                      "INSERT INTO public.item(item,proddescr) VALUES ('SYN_ITEM','SYN_DESCRIPTION');", encoding="utf-8")
    output = tmp_path / "out"
    report = run(Options(source, output, tmp_path / "private.snpanon"))
    assert report["skipped_tables"][0]["table"].startswith("excluded_namespace_")
    assert all("client_hidden_namespace" not in file.read_text(encoding="utf-8")
               for file in output.rglob("*") if file.is_file())

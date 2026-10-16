"""Synthetic fault injection at filesystem/PostgreSQL publication boundaries.

The driver models both acknowledged and lost-acknowledgement commits. No network.
"""
import json
import sqlite3
from pathlib import Path
from threading import Event

import pytest

from anonymizer import pipeline
from anonymizer.errors import AnonymizerError, Cancelled
from anonymizer.pipeline import Options, run
from anonymizer.runlog import RunLog
from test_pg_loader import FakeConnection, FakePsycopg2, install, pg_options


SOURCE = """
CREATE TABLE public.parent(a text PRIMARY KEY, qty numeric);
CREATE TABLE public.child(x text REFERENCES public.parent(a), qty numeric);
INSERT INTO public.parent VALUES ('SYN_A', 10);
INSERT INTO public.child VALUES ('SYN_A', 3);
"""


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    source = tmp_path / "source.sql"
    source.write_text(SOURCE, encoding="utf-8")
    fake = install(FakePsycopg2(existing_tables={"parent", "child"}), monkeypatch)
    options = Options(source, tmp_path / "out", tmp_path / "private.snpanon",
                      pg=pg_options(), mapping_xlsx=tmp_path / "private.xlsx")
    return options, fake


def mapping_count(project):
    with sqlite3.connect(project) as db:
        return db.execute("SELECT count(*) FROM mapping").fetchone()[0]


def test_mapping_failure_happens_before_connect(setup_run, monkeypatch):
    options, fake = setup_run

    def fail_mapping(*args, **kwargs):
        raise AnonymizerError("synthetic mapping failure")

    monkeypatch.setattr(pipeline, "write_mapping_xlsx", fail_mapping)
    with pytest.raises(AnonymizerError, match="mapping failure"):
        run(options)
    assert fake.connect_kwargs == {} and fake.commits == 0
    assert not options.output.exists() and not options.mapping_xlsx.exists()
    assert mapping_count(options.project) == 0


def test_failed_mapping_preparation_preserves_existing_private_file(setup_run, monkeypatch):
    options, fake = setup_run
    options.mapping_overwrite = True
    options.mapping_xlsx.write_bytes(b"OLD_PRIVATE_MAP")

    def fail_mapping(path, rows, **kwargs):
        Path(path).write_bytes(b"INCOMPLETE_NEW_MAP")
        raise AnonymizerError("synthetic mapping write failure")

    monkeypatch.setattr(pipeline, "write_mapping_xlsx", fail_mapping)
    with pytest.raises(AnonymizerError, match="mapping write failure"):
        run(options)
    assert fake.connect_kwargs == {} and not options.output.exists()
    assert options.mapping_xlsx.read_bytes() == b"OLD_PRIVATE_MAP"
    assert not list(options.mapping_xlsx.parent.glob(".snp-map-*"))


@pytest.mark.parametrize("failure", ["report", "mapping_replace", "project", "rename"])
def test_local_publication_failure_rolls_back_pg(setup_run, monkeypatch, failure):
    options, fake = setup_run
    before = {name: list(rows) for name, rows in fake.rows.items()}
    original_write = Path.write_text
    original_finish = pipeline.Project.finish
    original_rename = Path.rename
    original_replace = pipeline.os.replace

    def write(path, *args, **kwargs):
        if failure == "report" and path.name == "report.json":
            raise OSError("synthetic report failure")
        return original_write(path, *args, **kwargs)

    def finish(project, success):
        if failure == "project" and success:
            raise AnonymizerError("synthetic project commit failure")
        return original_finish(project, success)

    def rename(path, target):
        if failure == "rename" and Path(target) == options.output:
            raise OSError("synthetic package rename failure")
        return original_rename(path, target)

    def replace(source, target):
        if failure == "mapping_replace" and Path(target) == options.mapping_xlsx:
            raise OSError("synthetic mapping publication failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(pipeline.Project, "finish", finish)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(pipeline.os, "replace", replace)
    with pytest.raises(AnonymizerError):
        run(options)
    assert fake.commits == 0 and fake.rollbacks == 1
    assert fake.rows == before and fake.pending == before
    assert not options.output.exists()
    if failure != "rename":
        assert mapping_count(options.project) == 0
    else:
        assert mapping_count(options.project) > 0  # durable extra mappings are safe


def test_cancel_after_publication_before_commit_rolls_back(setup_run, monkeypatch):
    options, fake = setup_run
    before = {name: list(rows) for name, rows in fake.rows.items()}
    cancel = Event()
    original_rename = Path.rename

    def rename(path, target):
        result = original_rename(path, target)
        if Path(target) == options.output:
            cancel.set()
        return result

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(Cancelled):
        run(options, cancel=cancel)
    assert fake.commits == 0 and fake.rollbacks == 1 and fake.rows == before
    assert not options.output.exists()


@pytest.mark.parametrize("commit_applied", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_commit_unknown_keeps_package_project_and_mapping(setup_run, commit_applied, error_type):
    from anonymizer.errors import PgCommitUncertain

    options, fake = setup_run
    fake.commit_error = error_type("SECRET_PASSWORD internal.host")
    fake.commit_applied = commit_applied
    before = {name: list(rows) for name, rows in fake.rows.items()}
    with pytest.raises(PgCommitUncertain) as error:
        run(options)
    assert fake.commits == 1 and (fake.rows != before) == commit_applied
    assert options.output.is_dir() and options.mapping_xlsx.is_file()
    assert mapping_count(options.project) > 0
    report = json.loads((options.output / "report.json").read_text(encoding="utf-8"))
    assert report["pg_commit_status"] == "not_confirmed"
    assert report["pg_load"] == {"schema": "public_5", "tables": 2, "rows": 2}
    assert "SECRET_PASSWORD" not in str(error.value) and "internal.host" not in str(error.value)


def test_success_commits_only_after_all_local_artifacts_exist(setup_run, monkeypatch):
    options, fake = setup_run
    original_commit = FakeConnection.commit

    def commit(connection):
        assert options.output.is_dir() and options.mapping_xlsx.is_file()
        assert mapping_count(options.project) > 0
        report = json.loads((options.output / "report.json").read_text(encoding="utf-8"))
        assert report["pg_commit_status"] == "not_confirmed"
        original_commit(connection)

    monkeypatch.setattr(FakeConnection, "commit", commit)
    report = run(options)
    assert fake.commits == 1 and fake.rollbacks == 0
    assert report["pg_commit_status"] == "committed"
    assert json.loads((options.output / "report.json").read_text(encoding="utf-8"))["pg_commit_status"] == "committed"


def test_cancel_and_progress_failure_after_commit_do_not_fail_run(setup_run, monkeypatch):
    options, fake = setup_run
    original_commit = FakeConnection.commit
    cancel = Event()

    def commit(connection):
        original_commit(connection)
        cancel.set()

    def progress(stage, amount, message):
        if fake.commits:
            raise RuntimeError("synthetic notification failure")

    monkeypatch.setattr(FakeConnection, "commit", commit)
    report = run(options, progress=progress, cancel=cancel)
    assert report["pg_commit_status"] == "committed"
    assert fake.commits == 1 and options.output.is_dir()


def test_interrupt_during_connection_close_keeps_committed_package(setup_run, monkeypatch):
    options, fake = setup_run

    def close(connection):
        raise KeyboardInterrupt()

    monkeypatch.setattr(FakeConnection, "close", close)
    report = run(options)
    assert report["pg_commit_status"] == "committed"
    assert fake.commits == 1 and options.output.is_dir()
    assert options.mapping_xlsx.is_file() and mapping_count(options.project) > 0


def test_report_confirmation_write_failure_is_warning_not_failed_run(setup_run, monkeypatch):
    options, fake = setup_run
    options.log = RunLog()
    original_replace = Path.replace
    original_write = Path.write_text

    def replace(path, target):
        if fake.commits and Path(target).name == "report.json":
            raise OSError("synthetic confirmation replace failure")
        return original_replace(path, target)

    def write(path, *args, **kwargs):
        if fake.commits and "report" in path.name:
            raise OSError("synthetic confirmation write failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "write_text", write)
    report = run(options)
    assert fake.commits == 1 and options.output.is_dir()
    assert report["pg_commit_status"] == "committed"
    assert any(entry[1] == "warning" for entry in options.log.entries)
    saved = json.loads((options.output / "report.json").read_text(encoding="utf-8"))
    assert saved["pg_commit_status"] == "not_confirmed"

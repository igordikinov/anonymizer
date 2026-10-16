"""Unit tests for pg_loader with a fake driver: SQL shape, rollback, no credential leaks.

The loader consumes the staged dump.sql COPY sections — the same transformed
rows the file package contains — so tests build a synthetic dump fixture.
"""
import sys
import re

import pytest

from anonymizer import pg_loader
from anonymizer.errors import AnonymizerError
from anonymizer.pipeline import PgOptions

DUMP = (
    "-- SNP Anonymizer: standalone table/data dump\n"
    'CREATE SCHEMA IF NOT EXISTS "snp_a000001";\n'
    'COPY "snp_a000001"."parent" (a, b) FROM stdin;\n'
    "A000001\t1\n"
    "A000002\t2\n"
    "\\.\n"
    'COPY "snp_a000001"."child" (x, y) FROM stdin;\n'
    "A000001\t2.0\n"
    "\\.\n"
)


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.owner.executed.append((sql.strip(), params))
        if "information_schema.tables" in sql:
            self.owner.results.append([(name,) for name in self.owner.existing_tables])
        if sql.startswith("TRUNCATE TABLE"):
            for name in re.findall(r'"[^" ]+"\."([^" ]+)"', sql):
                self.owner.pending[name] = []

    def fetchall(self):
        return self.owner.results.pop(0)

    def copy_expert(self, sql, stream, size=0):
        data = stream.read()
        self.owner.copied.append((sql.strip(), data.decode("utf-8")))
        if self.owner.fail_on_copy:
            raise RuntimeError("simulated COPY failure")
        table = re.search(r'COPY "[^" ]+"\."([^" ]+)"', sql).group(1)
        self.owner.pending[table].extend(data.decode("utf-8").splitlines())


class FakeConnection:
    def __init__(self, owner):
        self.owner = owner
        self.cursor_class = FakeCursor

    def cursor(self):
        return self.owner.cursor_class(self.owner)

    def commit(self):
        self.owner.commits += 1
        if not self.owner.commit_error or self.owner.commit_applied:
            self.owner.rows = {name: list(rows) for name, rows in self.owner.pending.items()}
        if self.owner.commit_error:
            raise self.owner.commit_error

    def rollback(self):
        self.owner.rollbacks += 1
        self.owner.pending = {name: list(rows) for name, rows in self.owner.rows.items()}

    def close(self):
        self.owner.closed += 1


class FakePsycopg2:
    def __init__(self, existing_tables=(), fail_on_copy=False, fail_connect=None,
                 commit_error=None, commit_applied=False):
        self.existing_tables, self.fail_on_copy, self.fail_connect = set(existing_tables), fail_on_copy, fail_connect
        self.connect_kwargs = {}
        self.executed, self.results, self.copied = [], [], []
        self.commits = self.rollbacks = self.closed = 0
        self.cursor_class = FakeCursor
        self.rows = {name: [f"OLD_{name}"] for name in self.existing_tables}
        self.pending = {name: list(rows) for name, rows in self.rows.items()}
        self.commit_error, self.commit_applied = commit_error, commit_applied

    def connect(self, **kwargs):
        self.connect_kwargs.update(kwargs)
        if self.fail_connect:
            raise self.fail_connect
        return FakeConnection(self)


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "dump.sql"
    path.write_text(DUMP, encoding="utf-8", newline="")
    return path


def tables():
    return {("snp_a000001", "parent"): object(), ("snp_a000001", "child"): object()}


def install(fake, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    return fake


def pg_options(schema="public_5"):
    return PgOptions("db.host.internal", 5433, "snpdb", "svc_user", "SECRET_PASSWORD", schema)


def test_happy_path_sql_shape_and_streaming(dump, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"parent", "child"}), monkeypatch)
    summary, skipped_pg = pg_loader.load(tables(), dump, pg_options())
    assert summary == {"schema": "public_5", "tables": 2, "rows": 3}
    sqls = [sql for sql, _ in fake.executed]
    assert any(sql.startswith("CREATE SCHEMA IF NOT EXISTS") for sql in sqls)
    truncate = next(sql for sql in sqls if sql.startswith("TRUNCATE"))
    assert truncate == 'TRUNCATE TABLE "public_5"."child", "public_5"."parent" RESTART IDENTITY'
    assert "CASCADE" not in truncate
    assert fake.copied[0][0] == 'COPY "public_5"."parent" (a, b) FROM STDIN'
    assert fake.copied[0][1] == "A000001\t1\nA000002\t2\n"  # transformed dump rows, never the spool
    assert fake.copied[1][1] == "A000001\t2.0\n"
    assert fake.commits == 1 and fake.rollbacks == 0
    assert fake.connect_kwargs["password"] == "SECRET_PASSWORD"
    assert fake.connect_kwargs["application_name"] == "snp-anonymizer"


def test_missing_table_blocks_before_truncate(dump, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"parent"}), monkeypatch)
    with pytest.raises(AnonymizerError, match="нет таблиц датасета: child"):
        pg_loader.load(tables(), dump, pg_options())
    assert fake.rollbacks == 1 and not fake.copied
    assert not any(sql.startswith("TRUNCATE") for sql, _ in fake.executed)


def test_collision_across_dataset_schemas_detected_before_connect(dump, monkeypatch):
    fake = install(FakePsycopg2(), monkeypatch)
    with pytest.raises(AnonymizerError, match="нескольких схемах"):
        pg_loader.load({("public", "item"): object(), ("client2", "item"): object()}, dump, pg_options())
    assert fake.connect_kwargs == {}


def test_invalid_schema_name_rejected(dump, monkeypatch):
    fake = install(FakePsycopg2(), monkeypatch)
    with pytest.raises(AnonymizerError, match="имя целевой схемы"):
        pg_loader.load(tables(), dump, pg_options(schema="bad schema"))


def test_connect_failure_hides_credentials_and_dsn(dump, monkeypatch):
    fake = install(FakePsycopg2(fail_connect=RuntimeError('password "SECRET_PASSWORD" for db.host.internal failed')), monkeypatch)
    with pytest.raises(AnonymizerError) as error:
        pg_loader.load(tables(), dump, pg_options())
    assert "SECRET_PASSWORD" not in str(error.value) and "db.host.internal" not in str(error.value)


def test_copy_failure_rolls_back(dump, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"parent", "child"}, fail_on_copy=True), monkeypatch)
    with pytest.raises(AnonymizerError, match="отменена"):
        pg_loader.load(tables(), dump, pg_options())
    assert fake.rollbacks == 1 and fake.commits == 0


def test_copy_failure_with_skip_preserves_all_existing_rows(dump, monkeypatch):
    class FailChildCursor(FakeCursor):
        def copy_expert(self, sql, stream, size=0):
            super().copy_expert(sql, stream, size)
            if '"child"' in sql:
                raise RuntimeError("synthetic failure after parent and child COPY")

    fake = install(FakePsycopg2(existing_tables={"parent", "child"}), monkeypatch)
    fake.cursor_class = FailChildCursor
    before = {name: list(rows) for name, rows in fake.rows.items()}
    with pytest.raises(AnonymizerError, match="отменена"):
        pg_loader.load(tables(), dump, pg_options(), skip_failed_tables=True)
    assert fake.rows == before and fake.pending == before
    assert fake.rollbacks == 1 and fake.commits == 0


def test_before_commit_runs_after_copy_and_failure_rolls_back(dump, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"parent", "child"}), monkeypatch)
    before = {name: list(rows) for name, rows in fake.rows.items()}

    def fail_publication(summary, skipped):
        assert summary == {"schema": "public_5", "tables": 2, "rows": 3}
        assert skipped == [] and len(fake.copied) == 2 and fake.commits == 0
        assert fake.rows == before and fake.pending != before
        raise AnonymizerError("synthetic local publication failure")

    with pytest.raises(AnonymizerError, match="publication failure"):
        pg_loader.load(tables(), dump, pg_options(), before_commit=fail_publication)
    assert fake.rows == before and fake.rollbacks == 1 and fake.commits == 0


@pytest.mark.parametrize("commit_applied", [False, True])
def test_commit_failure_reports_unknown_outcome(dump, monkeypatch, commit_applied):
    from anonymizer.errors import PgCommitUncertain

    fake = install(FakePsycopg2(existing_tables={"parent", "child"},
                              commit_error=RuntimeError("SECRET_PASSWORD db.host.internal"),
                              commit_applied=commit_applied), monkeypatch)
    before = {name: list(rows) for name, rows in fake.rows.items()}
    with pytest.raises(PgCommitUncertain) as error:
        pg_loader.load(tables(), dump, pg_options())
    assert fake.commits == 1
    assert (fake.rows != before) == commit_applied
    assert "SECRET_PASSWORD" not in str(error.value) and "db.host.internal" not in str(error.value)
    assert "не изменено" not in str(error.value)


def test_dump_missing_a_table_section_is_rejected(dump, monkeypatch):
    fake = install(FakePsycopg2(existing_tables={"parent", "child"}), monkeypatch)
    truncated = dump.with_name("short.sql")
    truncated.write_text(DUMP.split('COPY "snp_a000001"."child"')[0], encoding="utf-8", newline="")
    with pytest.raises(AnonymizerError, match="не найдены секции COPY"):
        pg_loader.load(tables(), truncated, pg_options())
    assert fake.rollbacks == 1


def test_missing_driver_gives_safe_error(dump, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    original_import = __import__

    def no_psycopg2(name, *args, **kwargs):
        if name == "psycopg2":
            raise ImportError("No module named psycopg2")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_psycopg2)
    with pytest.raises(AnonymizerError, match="psycopg2-binary"):
        pg_loader.load(tables(), dump, pg_options())


def test_cancel_inside_copy_is_not_skipped_but_raised(dump, monkeypatch):
    """psycopg2 оборачивает Cancelled из стримера в QueryCanceled; повторный check()
    в except-ветке поднимает Cancelled наружу — без коммита частичных данных."""
    from anonymizer.errors import Cancelled

    class WrappingCursor(FakeCursor):
        def copy_expert(self, sql, stream, size=0):
            try:
                while stream.read(4096):
                    pass
            except Cancelled:
                raise RuntimeError("canceling statement due to user request")  # как psycopg2

    fake = FakePsycopg2(existing_tables={"parent", "child"})
    fake.cursor_class = WrappingCursor
    install(fake, monkeypatch)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] > 1:  # первый check — чтение секции, второй — повторный в except
            raise Cancelled("Обработка отменена.")

    with pytest.raises(Cancelled):
        pg_loader.load(tables(), dump, pg_options(), check=check, skip_failed_tables=True)
    assert fake.commits == 0 and fake.rollbacks == 1


def test_section_reader_stops_at_terminator(tmp_path):
    dump = tmp_path / "dump.sql"
    dump.write_text('COPY "s"."t" (a) FROM stdin;\n1\n2\n\\.\nTAIL\n', encoding="utf-8", newline="")
    with dump.open(encoding="utf-8", newline="") as stream:
        assert stream.readline().startswith("COPY")
        section = pg_loader._SectionReader(stream, lambda: None)
        data = section.read()
        assert data == b"1\n2\n" and section.count == 2 and section.done
        assert section.read() == b""  # psycopg2 sees EOF at the terminator

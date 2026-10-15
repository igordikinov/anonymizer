"""«Пропускать таблицы с ошибками»: табличные ошибки не валяют прогон. Synthetic data only."""
import pytest

from anonymizer.errors import AnonymizerError
from anonymizer.pipeline import Options, run

GOOD_SQL = """
      CREATE TABLE public.parent(a text PRIMARY KEY, note text);
      INSERT INTO public.parent VALUES ('SYN_OK','SECRET_OK');
"""


def read_manifest(output):
    import json
    return json.loads((output / "csv/manifest.json").read_text(encoding="utf-8"))


def test_csv_broken_file_skipped_good_processed(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    log_target = tmp_path
    (src / "item.csv").write_text("item,proddescr\nSYN_OK,SECRET_OK\n", encoding="utf-8")
    (src / "broken.csv").write_text("loc\nrow,with,wrong,count\n", encoding="utf-8")  # число полей скачет
    log = __import__("anonymizer.runlog", fromlist=["RunLog"]).RunLog()
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log, skip_error_tables=True))
    tables = [entry["table"] for entry in report["skipped_tables"]]
    assert any("broken" in table for table in tables)
    manifest = read_manifest(tmp_path / "out")
    names = {table["table"] for table in manifest["tables"]}
    assert "item" in names and "broken" not in names
    warnings = [e for e in log.entries if e[3] == "unknown_column" or e[3] == "table_skipped"]
    assert warnings


def test_csv_broken_file_fails_without_flag(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "broken.csv").write_text("loc\nrow,with,wrong,count\n", encoding="utf-8")
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon"))
    assert not (tmp_path / "out").exists()


def test_sql_broken_insert_skipped_good_processed(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text(GOOD_SQL + """
      CREATE TABLE public.child(x text, note text);
      INSERT INTO public.child VALUES ('SYN_C','SECRET_C');
      INSERT INTO public.child VALUES ('ONLY_ONE_VALUE');
      INSERT INTO public.other VALUES ('SYN_O','SECRET_O');
      CREATE TABLE public.other(x text, note text);
      INSERT INTO public.other VALUES ('SYN_O2','SECRET_O2');
    """, encoding="utf-8")
    log = __import__("anonymizer.runlog", fromlist=["RunLog"]).RunLog()
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log, skip_error_tables=True))
    names = {entry["table"] for entry in report["skipped_tables"]}
    # Гранулярность — таблица: битый INSERT роняет child целиком (частичные строки
    # вычищаются), other упала до CREATE; годная parent опубликована.
    assert {"public.child", "public.other"} <= names
    manifest = read_manifest(tmp_path / "out")
    table_names = {table["table"] for table in manifest["tables"]}
    assert table_names == {"parent"}


def test_mandatory_null_table_skipped_with_flag_only(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    # item — обязательное поле модели (BK, NOT NULL); \N даёт NULL и валит таблицу.
    (src / "item.csv").write_text("item,proddescr\nSYN_OK,SECRET_OK\n\\N,SECRET_BAD\n", encoding="utf-8")
    (src / "loc.csv").write_text("loc,locdescr\nSYN_P,SECRET_PLANT\n", encoding="utf-8")
    with pytest.raises(AnonymizerError, match="обязательном поле"):
        run(Options(src, tmp_path / "out1", tmp_path / "p1.snpanon"))
    assert not (tmp_path / "out1").exists()
    report = run(Options(src, tmp_path / "out2", tmp_path / "p2.snpanon", skip_error_tables=True))
    assert any("item" in entry["table"] for entry in report["skipped_tables"])
    manifest = read_manifest(tmp_path / "out2")
    assert {t["table"] for t in manifest["tables"]} == {"loc"}  # годная таблица опубликована


def test_broken_create_is_skipped_and_not_resurrected(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text(GOOD_SQL + """
      CREATE TABLE public.bad1(a text PRIMARY KEY, b numeric, FOREIGN KEY (a,b));
      CREATE TABLE public.bad1(a text PRIMARY KEY);
      INSERT INTO public.bad1 VALUES ('SYN_LATE');
    """, encoding="utf-8")
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", skip_error_tables=True))
    skipped = {entry["table"] for entry in report["skipped_tables"]}
    assert skipped == {"public.bad1"}  # одна запись, имя — не кортеж
    manifest = read_manifest(tmp_path / "out")
    assert {t["table"] for t in manifest["tables"]} == {"parent"}  # поздний CREATE не воскресил


def test_cancel_in_skip_mode_still_cancels(tmp_path):
    from threading import Event

    from anonymizer.errors import Cancelled
    src = tmp_path / "source"
    src.mkdir()
    (src / "item.csv").write_text("item,proddescr\nSYN_A,SECRET_A\n", encoding="utf-8")
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        run(Options(src, tmp_path / "out", tmp_path / "p.snpanon", skip_error_tables=True), cancel=cancel)
    assert not (tmp_path / "out").exists()


def test_all_tables_skipped_blocks(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "broken.csv").write_text("loc\nrow,with,wrong,count\n", encoding="utf-8")
    with pytest.raises(AnonymizerError, match="пропущены"):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", skip_error_tables=True))
    assert not (tmp_path / "out").exists()


def test_pg_copy_failure_skips_table_only(tmp_path):
    pytest.importorskip("psycopg2")
    import os
    import psycopg2
    host = os.environ.get("SNP_PG_HOST", "localhost")
    port = int(os.environ.get("SNP_PG_PORT", "54329"))
    dbname = os.environ.get("SNP_PG_DB", "snp_test")
    user = os.environ.get("SNP_PG_USER", "postgres")
    try:
        psycopg2.connect(host=host, port=port, dbname=dbname, user=user, connect_timeout=2).close()
    except Exception:
        pytest.skip("локальный тестовый PostgreSQL не запущен")
    schema = "public_skiptest"

    def connect():
        return psycopg2.connect(host=host, port=port, dbname=dbname, user=user)

    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cursor.execute(f"CREATE SCHEMA {schema}")
        cursor.execute(f"CREATE TABLE {schema}.parent(sys_id bigint GENERATED BY DEFAULT AS IDENTITY, a text PRIMARY KEY, note text)")
        cursor.execute(f"CREATE TABLE {schema}.child(sys_id bigint GENERATED BY DEFAULT AS IDENTITY, x text, qty numeric)")
        # NOT VALID: существующие строки не проверяются, COPY-вставки — проверяются.
        cursor.execute(f"ALTER TABLE {schema}.child ADD CONSTRAINT guard_chk CHECK (qty < 0) NOT VALID")
    connection.close()

    src = tmp_path / "source.sql"
    src.write_text("""
      CREATE TABLE public.parent(a text PRIMARY KEY, note text);
      CREATE TABLE public.child(x text, qty numeric);
      INSERT INTO public.parent VALUES ('SYN_A','SECRET_A'),('SYN_B','SECRET_B');
      INSERT INTO public.child VALUES ('SYN_A',1),('SYN_B',2);
    """, encoding="utf-8")
    from anonymizer.pipeline import PgOptions
    pg = PgOptions(host, port, dbname, user, "", schema)
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", pg=pg, skip_error_tables=True))
    assert report["pg_load"]["tables"] == 2 and report["pg_load"]["rows"] == 2
    assert report["pg_load"]["skipped"][0]["table"] == "child"

    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {schema}.parent WHERE note LIKE '%SECRET%'")
        parent_rows = cursor.fetchall()[0][0]
        cursor.execute(f"SELECT count(*) FROM {schema}.child")
        child_rows = cursor.fetchall()[0][0]
    connection.close()
    assert parent_rows == 0  # child не загружена (CHECK), parent загружена
    assert child_rows == 0 and (tmp_path / "out").is_dir()
    with psycopg2.connect(host=host, port=port, dbname=dbname, user=user) as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")  # cleanup тестовой схемы
    psycopg2.connect(host=host, port=port, dbname=dbname, user=user).close()

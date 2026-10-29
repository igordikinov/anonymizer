"""Optional load of the anonymized dataset into a target PostgreSQL schema.

Runs after all validations and file exports. The COPY sections of the staged
dump.sql are the single source of truth: they already hold the transformed rows
with dump.sql escaping. Cleared sys_id is omitted from target COPY so an existing
identity/default can generate it; all other values match the file package.
One transaction: verify every dataset
table exists in the target schema, TRUNCATE exactly those tables (RESTART
IDENTITY, no CASCADE), stream each dump COPY section to the server, COMMIT.
COPY failures roll the whole dataset back, including when input-table skipping
is enabled. before_commit publishes prepared local artifacts before COMMIT.
An exception during COMMIT has an unknown outcome and must retain those artifacts.
"""
from __future__ import annotations

import io
import re

from .errors import AnonymizerError, PgCommitUncertain
from .metadata import quote

CONNECT_TIMEOUT = 10
COPY_HEADER = re.compile(r'COPY "([^"]+)"\."([^"]+)" ')
MAX_COPY_ROW_BYTES = 32 * 1024**2
MAX_COPY_CHUNK_BYTES = 8 * 1024**2


def valid_name(name):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9$]{0,62}", name or ""))


class _SectionReader(io.RawIOBase):
    """Feeds one dump COPY section to the server; EOF at the \\ terminator."""

    def __init__(self, stream, check, omit_index=None, expected_width=None):
        self.stream, self.check, self.done, self.count = stream, check, False, 0
        self.omit_index, self.expected_width = omit_index, expected_width

    def readable(self):
        return True

    def read(self, size=-1):
        if self.done:
            return b""
        chunks, total = [], 0
        budget = min(size, MAX_COPY_CHUNK_BYTES) if size and size > 0 else MAX_COPY_CHUNK_BYTES
        while total < budget:
            self.check()
            # Generated COPY escaping can expand an 8 MiB input row. Bound the
            # record even if a caller supplies a damaged staged dump instead.
            line = self.stream.readline(MAX_COPY_ROW_BYTES + 1)
            if not line:
                raise AnonymizerError("Незавершённая секция COPY в дампе.")
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_COPY_ROW_BYTES:
                raise AnonymizerError("Превышен лимит строки COPY в дампе.")
            if line.rstrip("\r\n") == "\\.":  # dump terminator: report EOF to psycopg2
                self.done = True
                break
            if self.expected_width is not None:
                if line.count("\t") + 1 != self.expected_width:
                    raise AnonymizerError("Неправильное число колонок COPY в дампе.")
                if self.omit_index is not None:
                    values = line.rstrip("\r\n").split("\t")
                    if values[self.omit_index] != r"\N":
                        raise AnonymizerError("Системный sys_id не очищен в подготовленном дампе.")
                    del values[self.omit_index]
                    line = "\t".join(values) + "\n"
                    encoded = line.encode("utf-8")
            chunks.append(encoded)
            total += len(encoded)
            self.count += 1
        return b"".join(chunks)


def _quiet_rollback(connection):
    try:
        connection.rollback()
    except BaseException:
        pass  # a broken connection has nothing left to roll back


def connect(pg, psycopg2):
    try:
        connection = psycopg2.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.user,
                                      password=pg.password, connect_timeout=CONNECT_TIMEOUT,
                                      application_name="snp-anonymizer")
    except Exception as error:
        # Exception text can contain DSN parts; never echo it.
        raise AnonymizerError(f"Не удалось подключиться к PostgreSQL ({type(error).__name__}). "
                              "Проверьте host, port, базу, пользователя и пароль.") from None
    connection.autocommit = False
    with connection.cursor() as cursor:
        cursor.execute("SET client_encoding TO 'UTF8'")
    return connection


def load(tables, dump_path, pg, progress=lambda done: None, check=lambda: None,
         skip_failed_tables=False, before_commit=None):
    """Load one atomic dataset, publish via before_commit, then commit last.

    skip_failed_tables remains accepted for API compatibility; PG errors always
    abort the complete transaction. Input-table skipping belongs to the readers.
    """
    try:
        import psycopg2
    except ImportError:
        raise AnonymizerError("Драйвер PostgreSQL не установлен. Пересоберите приложение или установите psycopg2-binary.") from None
    if not valid_name(pg.schema):
        raise AnonymizerError("Некорректное имя целевой схемы. Поддерживаются технические имена SNP.")
    names = {table for (schema, table) in tables}
    if len(names) != len(tables):
        raise AnonymizerError("Таблица с одинаковым именем встречается в нескольких схемах датасета; "
                              "загрузка в одну целевую схему невозможна без переименования.")
    check()
    connection = connect(pg, psycopg2)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(pg.schema)}")
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (pg.schema,))
            missing = sorted(names - {row[0] for row in cursor.fetchall()})
            if missing:
                raise AnonymizerError(f"В целевой схеме {pg.schema} нет таблиц датасета: {', '.join(missing)}. "
                                      "Загрузка требует существующую схему с таблицами In.Plan.")
            quoted = ", ".join(f"{quote(pg.schema)}.{quote(table)}" for table in sorted(names))
            cursor.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY")
        total = 0
        copied = set()
        by_name = {name[1]: data for name, data in tables.items()}
        with dump_path.open(encoding="utf-8", newline="") as stream:
            header = stream.readline()
            while header:
                if header.startswith("COPY "):
                    match = COPY_HEADER.match(header)
                    if not match or match.group(2) not in names:
                        raise AnonymizerError("Дамп содержит неожиданную секцию COPY; загрузка отменена.")
                    statement = (header.strip()
                                 .replace("FROM stdin", "FROM STDIN", 1)
                                 .replace(f'"{match.group(1)}".', f'{quote(pg.schema)}.', 1)
                                 .rstrip(";"))
                    data = by_name[match.group(2)]
                    fields = data.get("fields") if isinstance(data, dict) else None
                    omit_index, expected_width = None, None
                    if fields is not None:
                        columns = [field["name"] for field in fields]
                        source_columns = ", ".join(quote(column) for column in columns)
                        expected = f'COPY {quote(match.group(1))}.{quote(match.group(2))} ({source_columns}) FROM stdin;'
                        if header.strip() != expected:
                            raise AnonymizerError("Колонки COPY не совпадают со структурой датасета.")
                        expected_width = len(columns)
                        if "sys_id" in columns:
                            omit_index = columns.index("sys_id")
                        target_columns = ", ".join(quote(column) for column in columns if column != "sys_id")
                        if not target_columns:
                            raise AnonymizerError("В таблице нет колонок для загрузки в PostgreSQL.")
                        statement = f'COPY {quote(pg.schema)}.{quote(match.group(2))} ({target_columns}) FROM STDIN'
                    section = _SectionReader(stream, check, omit_index, expected_width)
                    with connection.cursor() as cursor:
                        try:
                            cursor.copy_expert(statement, section, size=8 * 1024 * 1024)
                        except Exception:
                            # psycopg2 can wrap Cancelled raised by the streamer.
                            check()
                            raise
                    copied.add(match.group(2))
                    total += section.count
                    progress(total)
                    header = stream.readline()
                else:
                    header = stream.readline()
        if copied != names or total == 0:
            raise AnonymizerError("В дампе не найдены секции COPY всех таблиц датасета; загрузка отменена.")
        summary = {"schema": pg.schema, "tables": len(names), "rows": total}
        check()
        if before_commit is not None:
            before_commit(summary, [])
        check()  # last cancellation boundary; never promise rollback after COMMIT
        try:
            connection.commit()
        except BaseException as error:
            # Ctrl+C/SystemExit cannot establish whether the server committed
            # either. Preserve the same evidence as for a lost network reply.
            raise PgCommitUncertain(
                f"Результат COMMIT PostgreSQL не подтверждён ({type(error).__name__}). "
                "Проверьте состояние целевой базы перед повторной загрузкой. "
                "Подготовленный пакет, проект и карта замен сохранены.") from None
        return summary, []
    except AnonymizerError:
        _quiet_rollback(connection)
        raise
    except Exception as error:
        _quiet_rollback(connection)
        raise AnonymizerError(f"Загрузка в PostgreSQL прервана и отменена ({type(error).__name__}). "
                              "Содержимое целевой базы не изменено; пакет файлов не создан.") from None
    finally:
        try:
            connection.close()
        except BaseException:
            # Cleanup must not hide a successful/uncertain COMMIT (or replace
            # the original failure) with a late user interruption.
            pass

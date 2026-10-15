"""Optional load of the anonymized dataset into a target PostgreSQL schema.

Runs after all validations and file exports. The COPY sections of the staged
dump.sql are the single source of truth: they already hold the transformed,
validated rows with dump.sql escaping and no sys_id, so the database receives
exactly what the file package contains. One transaction: verify every dataset
table exists in the target schema, TRUNCATE exactly those tables (RESTART
IDENTITY, no CASCADE), stream each dump COPY section to the server, COMMIT.
Any failure rolls the database back; the caller then refuses to publish.
"""
from __future__ import annotations

import io
import re

from .errors import AnonymizerError, Cancelled
from .metadata import quote

CONNECT_TIMEOUT = 10
COPY_HEADER = re.compile(r'COPY "([^"]+)"\."([^"]+)" ')


def valid_name(name):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9$]{0,62}", name or ""))


class _SectionReader(io.RawIOBase):
    """Feeds one dump COPY section to the server; EOF at the \\ terminator."""

    def __init__(self, stream, check):
        self.stream, self.check, self.done, self.count = stream, check, False, 0

    def readable(self):
        return True

    def read(self, size=-1):
        if self.done:
            return b""
        chunks, total = [], 0
        while total < (size if size and size > 0 else 8 * 1024 * 1024):
            self.check()
            line = self.stream.readline()
            if not line:
                raise AnonymizerError("Незавершённая секция COPY в дампе.")
            if line.rstrip("\r\n") == "\\.":  # dump terminator: report EOF to psycopg2
                self.done = True
                break
            chunks.append(line)
            total += len(line)
            self.count += 1
        return "".join(chunks).encode("utf-8")


def _quiet_rollback(connection):
    try:
        connection.rollback()
    except Exception:
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


def load(tables, dump_path, pg, progress=lambda done: None, check=lambda: None, skip_failed_tables=False):
    """Loads every dataset table into pg.schema; returns (summary, skipped tables)."""
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
    connection = connect(pg, psycopg2)
    skipped = []
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
                    section = _SectionReader(stream, check)
                    savepoint = f"snp_table_{match.group(2)}"
                    with connection.cursor() as cursor:
                        cursor.execute(f"SAVEPOINT {quote(savepoint)}")
                        try:
                            cursor.copy_expert(statement, section, size=8 * 1024 * 1024)
                        except Cancelled:
                            raise  # отмена — не табличная ошибка; частичные данные не коммитятся
                        except Exception as error:
                            # psycopg2 оборачивает исключение из стримера (check() -> Cancelled)
                            # в QueryCanceled: единственный способ узнать об отмене — спросить снова.
                            check()
                            if not skip_failed_tables:
                                raise
                            cursor.execute(f"ROLLBACK TO SAVEPOINT {quote(savepoint)}")
                            skipped.append({"table": match.group(2),
                                            "reason": f"загрузка не удалась ({type(error).__name__}); таблица пропущена"})
                            copied.add(match.group(2))  # таблица обработана (пропущена осознанно)
                            if not section.done:  # секция могла быть прочитана до конца до отказа сервера
                                while True:
                                    rest = stream.readline()
                                    if not rest or rest.rstrip("\r\n") == "\\.": break
                            header = stream.readline()
                            continue
                    copied.add(match.group(2))
                    total += section.count
                    progress(total)
                    header = stream.readline()
                else:
                    header = stream.readline()
        if copied != names or total == 0:
            raise AnonymizerError("В дампе не найдены секции COPY всех таблиц датасета; загрузка отменена.")
        connection.commit()
        summary = {"schema": pg.schema, "tables": len(names), "rows": total}
        if skipped:
            summary["skipped"] = skipped
        return summary, skipped
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
        except Exception:
            pass

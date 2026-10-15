from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from . import __version__
from .csv_reader import parse_csv
from .errors import AnonymizerError, Cancelled
from .exporters import Exporter
from .mapping import Project, Transformer
from .mapping_export import write_mapping_xlsx
from .metadata import Model
from .pg_loader import load as pg_load
from .runlog import RunLog
from .sql_reader import parse_sql
from .store import Store
from .validation import Validator, validate_value
from .zip_input import extract_dataset


@dataclass
class PgOptions:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    schema: str


@dataclass
class Options:
    source: Path
    output: Path
    project: Path
    industry: str = "food"
    encoding: str = "utf-8-sig"
    delimiter: str | None = None
    schemas: tuple[str, ...] = ()
    csv_schema: str = "public"
    pg: PgOptions | None = None
    mapping_xlsx: Path | None = None
    mapping_overwrite: bool = False
    log: RunLog | None = None
    skip_error_tables: bool = False


def run(options: Options, progress=lambda stage, amount, message: None, cancel: Event | None = None):
    source, output, project_path = [p.resolve() for p in (options.source, options.output, options.project)]
    if not source.exists() or (source.is_file() and source.suffix.lower() not in {".sql", ".zip"}):
        raise AnonymizerError("Источник должен быть файлом .sql, ZIP-архивом или папкой CSV.")
    if output.exists():
        raise AnonymizerError("Папка результата уже существует. Выберите новую папку.")
    if output == source or (source.is_dir() and output.is_relative_to(source)):
        raise AnonymizerError("Результат должен находиться вне папки исходных CSV.")
    if project_path == source or project_path.is_relative_to(output) or (source.is_dir() and project_path.is_relative_to(source)):
        raise AnonymizerError("Локальный проект с исходными значениями должен храниться отдельно от входа и результата.")
    if project_path.suffix != ".snpanon":
        raise AnonymizerError("Файл проекта должен иметь расширение .snpanon.")
    if options.mapping_xlsx is not None:
        mapping_path = options.mapping_xlsx.resolve()
        if not options.mapping_overwrite and mapping_path.exists():
            raise AnonymizerError("Файл карты замен уже существует. Выберите другое имя или разрешите замену.")
        if mapping_path in {project_path, source.resolve()} or (source.is_file() and mapping_path == source.resolve()):
            raise AnonymizerError("Файл карты замен не может совпадать с проектом или исходным файлом: карта содержит исходные значения и пишется поверх живой базы проекта.")
        if mapping_path == output or mapping_path.is_relative_to(output) or (source.is_dir() and mapping_path.is_relative_to(source)):
            raise AnonymizerError("Файл карты замен содержит исходные значения и должен находиться вне папки результата и исходных данных.")
    model = Model()
    log = options.log if options.log is not None else RunLog()
    progress_cb = progress
    stage = {"current": "import"}

    def progress(stage_name, amount, message):
        stage["current"] = stage_name
        progress_cb(stage_name, amount, message)

    log.add("info", "import", "start", "", f"версия {__version__}; отрасль {options.industry}; "
            f"источник {'zip' if source.suffix.lower() == '.zip' else 'sql' if source.is_file() else 'csv'}")
    project, store, exporter = None, None, None
    success = False
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snp-anon-", dir=output.parent))

    def check():
        if cancel and cancel.is_set():
            raise Cancelled("Обработка отменена. Итоговый пакет не создан.")

    try:
        with tempfile.TemporaryDirectory(prefix="snp-anonymizer-") as temp, Store(Path(temp) / "spool.sqlite", model) as store:
            data_source = source
            if source.suffix.lower() == ".zip":
                progress("import", 0, "Распаковка архива")
                data_source = extract_dataset(source, Path(temp) / "zip", check,
                                              progress=lambda done: progress("import", 0, f"Распаковка архива: {done:,} байт"))
            total = data_source.stat().st_size if data_source.is_file() else sum(p.stat().st_size for p in data_source.rglob("*.csv"))
            progress("import", 0, "Чтение и проверка структуры")
            callback = lambda n: progress("import", min(n / max(total, 1), 1), "Чтение данных")
            if data_source.is_file():
                parse_sql(data_source, store, check, options.encoding, callback, skip_error_tables=options.skip_error_tables)
            else:
                parse_csv(data_source, store, check, options.encoding, options.delimiter, options.csv_schema, callback,
                          skip_error_tables=options.skip_error_tables)
            store.flush()
            if options.schemas:
                present = {name[0] for name in store.tables}
                if set(options.schemas) - present:
                    raise AnonymizerError("Указанная схема отсутствует во входных данных.")
                store.tables = {name: table for name, table in store.tables.items() if name[0] in options.schemas}
                store.count = sum(t["rows"] for t in store.tables.values())
            # Пропущенные таблицы — в журнал до проверки «нечего выдавать»,
            # чтобы причина оставалась в журнале даже при фатальном исходе.
            for table, reason in store.dropped_tables:
                log.add("error", "import", "table_skipped", table, reason)
            if not store.tables or not store.count:
                if store.dropped_tables:
                    raise AnonymizerError("Все таблицы пропущены из-за ошибок; выдавать нечего.")
                raise AnonymizerError("Не найдены строки таблиц для обезличивания.")
            log.add("info", "import", "imported", "", f"таблиц: {len(store.tables)}; строк: {store.count:,}")
            for table, column in store.dropped_columns:
                log.add("warning", "import", "unknown_column", f"{table}: {column}",
                        "колонки нет в модели/DDL — колонка пропущена и не входит в выдачу")
            for skipped_kind, count in sorted(store.skipped.items()):
                log.add("warning", "import", "skipped_sql", skipped_kind, f"конструкций пропущено: {count}")
            if any(not [f for f in t["fields"] if f["name"] != "sys_id"] for t in store.tables.values()):
                raise AnonymizerError("Обнаружена таблица, состоящая только из sys_id; её нельзя экспортировать без данных.")
            project = Project(project_path, options.industry)
            transformer = Transformer(model, project, store)
            logged_drops = len(store.dropped_tables)  # уже в журнале (import-стадия)
            if options.skip_error_tables:
                # Предпроверка таблиц: типы, обязательные поля, нулевой байт, правила
                # (JSON в ключе). Упавшая таблица исключается из выдачи с записью в журнал.
                progress("validate", 0, "Предпроверка таблиц")
                for name, table in list(store.tables.items()):
                    try:
                        for f in table["fields"]:
                            transformer.rule(name[1], f, name[0])
                        for old in store.rows(table["id"]):
                            check()
                            for f, value in zip(table["fields"], old):
                                if f["name"] != "sys_id":
                                    validate_value(name[1], f, value)
                    except Cancelled:
                        raise  # отмена — не табличная ошибка
                    except AnonymizerError as error:
                        store.drop_table(name, str(error))
                progress("validate", 1, "Предпроверка таблиц завершена")
            skipped_entries = [{"table": name, "stage": "import", "reason": reason} for name, reason in store.dropped_tables]
            for name, reason in store.dropped_tables[logged_drops:]:
                log.add("error", "validate", "table_skipped", name, reason)  # добавленные предпроверкой
            if not store.tables:
                raise AnonymizerError("Все таблицы пропущены из-за ошибок; выдавать нечего.")
            schema_names = {s: project.pseudonym("schema", s, label=None) for s in sorted({n[0] for n in store.tables})}
            schema_names = {s: "snp_" + value.lower() for s, value in schema_names.items()}
            validator = Validator(store, transformer)
            exporter = Exporter(staging, store.tables, schema_names)
            completed = 0
            output_digest = hashlib.sha256()
            for name, table in store.tables.items():
                check()
                exporter.start(name, table)
                for old in store.rows(table["id"]):
                    check()
                    row = transformer.transform(name[0], name[1], table["fields"], old)
                    validator.row(name, table, old, row)
                    exporter.row(row)
                    output_digest.update(json.dumps(row, ensure_ascii=False).encode())
                    completed += 1
                    if completed % 1000 == 0:
                        progress("transform", completed / store.count, f"Обработано строк: {completed:,}")
                exporter.end()
            progress("validate", 0, "Проверка связей, ключей и сохраняемых полей")
            id_to_name = {data["id"]: f"{name[0]}.{name[1]}" for name, data in store.tables.items()}
            validations = validator.finish()
            log.add_checks(validations, id_to_name)
            log.add("check", "validate", "preserved_fields", "", "сохраняемые поля и числа/даты JSON совпали")
            stage["current"] = "publish"
            exporter.close(True)
            exporter = None
            report = {"format": "snp-anonymizer-report-v1", "app_version": __version__, "model": model.data["meta"],
                      "industry": options.industry, "tables": len(store.tables), "rows": completed,
                      "changed_cells": transformer.changed, "sys_id": "omitted; generated in dump DDL",
                      "output_values_sha256": output_digest.hexdigest(), "validation": validations,
                      "skipped_tables": skipped_entries,
                      "skipped_sql_statements": store.skipped,
                      "scope": "reconstructed tables and data; no original functions, grants, comments, defaults or indexes",
                      "privacy": "nontechnical text and JSON strings/keys pseudonymized; JSON numbers/dates preserved; project mappings excluded",
                      "fields": [{"schema": schema_names[name[0]], "table": name[1], "columns": [
                          {"name": f["name"], "rule": transformer.rule(name[1], f, name[0]), "type": f["sql_type"]} for f in table["fields"]]} for name, table in store.tables.items()]}
            check()
            if options.pg:
                progress("pg", 0, "Загрузка в PostgreSQL")
                pg_summary, pg_skipped = pg_load(store.tables, staging / "dump.sql", options.pg,
                                                 progress=lambda done: progress("pg", min(done / max(store.count, 1), 1), f"Загружено строк: {done:,}"),
                                                 check=check, skip_failed_tables=options.skip_error_tables)
                report["pg_load"] = pg_summary
                for item in pg_skipped:
                    log.add("warning", "pg", "table_skipped", item["table"], item["reason"])
                    skipped_entries.append({"table": item["table"], "stage": "pg", "reason": item["reason"]})
                progress("pg", 1, f"Загружено строк: {report['pg_load']['rows']:,}")
                log.add("info", "pg", "pg_load", report["pg_load"]["schema"],
                        f"таблиц: {report['pg_load']['tables']}; строк: {report['pg_load']['rows']:,}")
            check()
            if options.mapping_xlsx:
                progress("mapping", 0, "Запись карты замен в Excel")

                def mapping_rows():
                    for domain, old, new in project.iter_mapping():
                        # The published schema name is the raw replacement plus
                        # the snp_ prefix; the map shows what the output contains.
                        yield (domain, old, "snp_" + str(new).lower()) if domain == "schema" else (domain, old, new)

                write_mapping_xlsx(options.mapping_xlsx, mapping_rows(), check=check)
                log.add("info", "mapping", "mapping_export", options.mapping_xlsx.name, "карта замен выгружена")
                progress("mapping", 1, "Карта замен записана")
            (staging / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            check()
            store.close()
            store = None
        # Existing project mappings persist only after all validations have passed.
        project.finish(True)
        project = None
        staging.rename(output)
        success = True
        log.add("info", "publish", "published", output.name, f"пакет опубликован; строк: {completed:,}")
        progress("done", 1, f"Готово: {completed:,} строк")
        return report
    except Cancelled:
        log.add("warning", stage["current"], "cancelled", "", "Обработка отменена. Итоговый пакет не создан.")
        raise
    except UnicodeError:
        log.add("error", stage["current"], "error", "", "Ошибка кодировки. Выберите UTF-8 или Windows-1251.")
        raise AnonymizerError("Ошибка кодировки. Выберите UTF-8 или Windows-1251.") from None
    except AnonymizerError as error:
        log.add("error", stage["current"], "error", "", str(error))
        raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        # Do not echo raw exception messages: they may contain input content/paths.
        log.add("error", stage["current"], "error", "", f"Не удалось обработать файлы ({type(error).__name__}). "
                "Проверьте формат, свободное место и доступ к папкам.")
        raise AnonymizerError(f"Не удалось обработать файлы ({type(error).__name__}). Проверьте формат, свободное место и доступ к папкам.") from None
    except Exception as error:
        # Внутренняя ошибка: в журнал идёт только тип исключения, значение не пишется.
        log.add("error", stage["current"], "error", "", f"Внутренняя ошибка обработки ({type(error).__name__}).")
        raise
    finally:
        if exporter:
            exporter.close(False)
        if store:
            store.close()
        if project:
            project.finish(False)
        if not success:
            shutil.rmtree(staging, ignore_errors=True)

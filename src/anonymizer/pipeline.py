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
from .metadata import Model
from .pg_loader import load as pg_load
from .sql_reader import parse_sql
from .store import Store
from .validation import Validator


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


def run(options: Options, progress=lambda stage, amount, message: None, cancel: Event | None = None):
    source, output, project_path = [p.resolve() for p in (options.source, options.output, options.project)]
    if not source.exists() or (source.is_file() and source.suffix.lower() != ".sql"):
        raise AnonymizerError("Источник должен быть файлом .sql или папкой CSV.")
    if output.exists():
        raise AnonymizerError("Папка результата уже существует. Выберите новую папку.")
    if output == source or (source.is_dir() and output.is_relative_to(source)):
        raise AnonymizerError("Результат должен находиться вне папки исходных CSV.")
    if project_path == source or project_path.is_relative_to(output) or (source.is_dir() and project_path.is_relative_to(source)):
        raise AnonymizerError("Локальный проект с исходными значениями должен храниться отдельно от входа и результата.")
    if project_path.suffix != ".snpanon":
        raise AnonymizerError("Файл проекта должен иметь расширение .snpanon.")
    model = Model()
    project, store, exporter = None, None, None
    success = False
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snp-anon-", dir=output.parent))

    def check():
        if cancel and cancel.is_set():
            raise Cancelled("Обработка отменена. Итоговый пакет не создан.")

    try:
        with tempfile.TemporaryDirectory(prefix="snp-anonymizer-") as temp, Store(Path(temp) / "spool.sqlite", model) as store:
            total = source.stat().st_size if source.is_file() else sum(p.stat().st_size for p in source.rglob("*.csv"))
            progress("import", 0, "Чтение и проверка структуры")
            callback = lambda n: progress("import", min(n / max(total, 1), 1), "Чтение данных")
            if source.is_file():
                parse_sql(source, store, check, options.encoding, callback)
            else:
                parse_csv(source, store, check, options.encoding, options.delimiter, options.csv_schema, callback)
            store.flush()
            if options.schemas:
                present = {name[0] for name in store.tables}
                if set(options.schemas) - present:
                    raise AnonymizerError("Указанная схема отсутствует во входных данных.")
                store.tables = {name: table for name, table in store.tables.items() if name[0] in options.schemas}
                store.count = sum(t["rows"] for t in store.tables.values())
            if not store.tables or not store.count:
                raise AnonymizerError("Не найдены строки таблиц для обезличивания.")
            if any(not [f for f in t["fields"] if f["name"] != "sys_id"] for t in store.tables.values()):
                raise AnonymizerError("Обнаружена таблица, состоящая только из sys_id; её нельзя экспортировать без данных.")
            project = Project(project_path, options.industry)
            transformer = Transformer(model, project, store)
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
            validations = validator.finish()
            exporter.close(True)
            exporter = None
            report = {"format": "snp-anonymizer-report-v1", "app_version": __version__, "model": model.data["meta"],
                      "industry": options.industry, "tables": len(store.tables), "rows": completed,
                      "changed_cells": transformer.changed, "sys_id": "omitted; generated in dump DDL",
                      "output_values_sha256": output_digest.hexdigest(), "validation": validations,
                      "skipped_sql_statements": store.skipped,
                      "scope": "reconstructed tables and data; no original functions, grants, comments, defaults or indexes",
                      "privacy": "nontechnical text and JSON strings/keys pseudonymized; JSON numbers/dates preserved; project mappings excluded",
                      "fields": [{"schema": schema_names[name[0]], "table": name[1], "columns": [
                          {"name": f["name"], "rule": transformer.rule(name[1], f, name[0]), "type": f["sql_type"]} for f in table["fields"]]} for name, table in store.tables.items()]}
            check()
            if options.pg:
                progress("pg", 0, "Загрузка в PostgreSQL")
                report["pg_load"] = pg_load(store.tables, staging / "dump.sql", options.pg,
                                            progress=lambda done: progress("pg", min(done / max(store.count, 1), 1), f"Загружено строк: {done:,}"),
                                            check=check)
                progress("pg", 1, f"Загружено строк: {report['pg_load']['rows']:,}")
            (staging / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            check()
            store.close()
            store = None
        # Existing project mappings persist only after all validations have passed.
        project.finish(True)
        project = None
        staging.rename(output)
        success = True
        progress("done", 1, f"Готово: {completed:,} строк")
        return report
    except UnicodeError:
        raise AnonymizerError("Ошибка кодировки. Выберите UTF-8 или Windows-1251.") from None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        # Do not echo raw exception messages: they may contain input content/paths.
        raise AnonymizerError(f"Не удалось обработать файлы ({type(error).__name__}). Проверьте формат, свободное место и доступ к папкам.") from None
    finally:
        if exporter:
            exporter.close(False)
        if store:
            store.close()
        if project:
            project.finish(False)
        if not success:
            shutil.rmtree(staging, ignore_errors=True)

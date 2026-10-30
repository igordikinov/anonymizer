from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from . import __version__
from .tabular_reader import parse_tabular
from .errors import AnonymizerError, Cancelled, PgCommitUncertain, IntegrityError
from .connectivity import ContractError, compile_contract, load_profile, MAX_PROFILE_BYTES
from .integrity import IntegrityVerifier
from .integrity_publication import failure_report, publish_diagnostic, profile_fields
from .exporters import Exporter
from .mapping import Project, Transformer
from .mapping_export import write_mapping_xlsx
from .metadata import Model, SYSTEM
from .pg_loader import load as pg_load
from .runlog import RunLog
from .sql_reader import parse_sql
from .store import Store
from .validation import Validator, validate_value
from .zip_input import extract_dataset
from .generic_input import InputLimitError


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
    bypass_model: bool = False
    connectivity_profile: str | Path | dict | None = None
    require_integrity: bool = False


def run(options: Options, progress=lambda stage, amount, message: None, cancel: Event | None = None):
    source, output, project_path = [p.resolve() for p in (options.source, options.output, options.project)]
    if not source.exists() or (source.is_file() and source.suffix.lower() not in {".sql", ".zip", ".xlsx"}):
        raise AnonymizerError("Источник должен быть файлом .sql/.xlsx, ZIP-архивом или папкой CSV/XLSX.")
    if output.exists():
        raise AnonymizerError("Папка результата уже существует. Выберите новую папку.")
    if output == source or (source.is_dir() and output.is_relative_to(source)):
        raise AnonymizerError("Результат должен находиться вне папки исходных CSV/XLSX.")
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
            f"источник {source.suffix.lower()[1:] if source.is_file() else 'csv/xlsx'}")
    if options.bypass_model:
        detail = ("Эталон не применяется; идентификаторы и проверки задаёт профиль связности."
                  if options.connectivity_profile else
                  "Эталон не применяется; числовые коды сохраняются, связность не проверена.")
        log.add("info", "import", "without_model", "", detail)
    project, store, exporter = None, None, None
    contract, integrity, profile, integrity_result = None, None, None, None
    success, published, retain_output = False, False, False
    mapping_temp = None
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snp-anon-", dir=output.parent))

    def check():
        if cancel and cancel.is_set():
            raise Cancelled("Обработка отменена. Итоговый пакет не создан.")

    def refuse_integrity(code, result=None):
        nonlocal published
        # A late cancellation may happen after our own directory rename but
        # before PG COMMIT. Remove that unpublished dataset from its public
        # location before exposing only the diagnostic report.
        if published and not retain_output:
            output.rename(staging)
            published = False
        diagnostic = failure_report(code, contract, result, bypass_model=options.bypass_model)
        try:
            report_path = publish_diagnostic(output, diagnostic)
        except OSError:
            raise AnonymizerError(f"{code}: набор данных не опубликован; не удалось записать диагностический отчёт.") from None
        raise IntegrityError(code, report_path, diagnostic)

    try:
        stage["current"] = "connectivity"
        profile = load_profile(options.connectivity_profile)
        stage["current"] = "import"
        with tempfile.TemporaryDirectory(prefix="snp-anonymizer-") as temp, Store(Path(temp) / "spool.sqlite", model, bypass_model=options.bypass_model) as store:
            data_source = source
            if source.suffix.lower() == ".zip":
                progress("import", 0, "Распаковка архива")
                data_source = extract_dataset(source, Path(temp) / "zip", check,
                                              progress=lambda done: progress("import", 0, f"Распаковка архива: {done:,} байт"))
            total = data_source.stat().st_size if data_source.is_file() else sum(
                p.stat().st_size for p in data_source.iterdir() if p.is_file() and p.suffix.lower() in {".csv", ".xlsx"})
            progress("import", 0, "Чтение и проверка структуры")
            callback = lambda n: progress("import", min(n / max(total, 1), 1), "Чтение данных")
            if data_source.is_file() and data_source.suffix.lower() == ".sql":
                parse_sql(data_source, store, check, options.encoding, callback, skip_error_tables=options.skip_error_tables)
            else:
                parse_tabular(data_source, store, check, options.encoding, options.delimiter, options.csv_schema, callback,
                          skip_error_tables=options.skip_error_tables)
            store.finish_import()
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
            logged_drops = len(store.dropped_tables)  # уже в журнале (import-стадия)
            if options.skip_error_tables:
                # Предпроверка таблиц: типы, обязательные поля, нулевой байт, правила
                # (JSON в ключе). Упавшая таблица исключается из выдачи с записью в журнал.
                progress("prevalidate", 0, "Предпроверка таблиц")
                precheck_transformer = Transformer(model, None, store)
                for name, table in list(store.tables.items()):
                    try:
                        for f in table["fields"]:
                            precheck_transformer.rule(name[1], f, name[0])
                        for old in store.rows(table["id"]):
                            check()
                            for f, value in zip(table["fields"], old):
                                if f["name"] not in SYSTEM:
                                    validate_value(name[1], f, value)
                    except Cancelled:
                        raise  # отмена — не табличная ошибка
                    except AnonymizerError as error:
                        store.drop_table(name, str(error))
                progress("prevalidate", 1, "Предпроверка таблиц завершена")
            skipped_entries = [{"table": name, "stage": "import", "reason": reason} for name, reason in store.dropped_tables]
            for name, reason in store.dropped_tables[logged_drops:]:
                log.add("error", "validate", "table_skipped", name, reason)  # добавленные предпроверкой
            if not store.tables:
                raise AnonymizerError("Все таблицы пропущены из-за ошибок; выдавать нечего.")
            progress("connectivity", 0, "Привязка профиля и деклараций связности")
            contract = compile_contract(store, profile, check=check)
            project = Project(project_path, options.industry, contract=contract if profile else None, check=check)
            if profile is not None:
                project.prepare_domains(store, check)
            transformer = Transformer(model, project, store, contract=contract)
            schema_names = {s: project.pseudonym("schema", s, label=None) for s in sorted({n[0] for n in store.tables})}
            schema_names = {s: "snp_" + value.lower() for s, value in schema_names.items()}
            public_namespaces = dict(schema_names)
            for index, schema in enumerate(sorted({name[0] for name in store.declarations} - set(schema_names)), start=1):
                public_namespaces[schema] = f"excluded_namespace_{index:06d}"
            for skipped in skipped_entries:
                if "." in skipped["table"]:
                    schema, table_name = skipped["table"].split(".", 1)
                    skipped["table"] = public_namespaces.get(schema, "excluded_namespace") + "." + table_name
                for original, alias in public_namespaces.items():
                    skipped["reason"] = re.sub(r"(?<![A-Za-z_0-9$])" + re.escape(original) + r"\.",
                                               alias + ".", skipped["reason"])
            validator = Validator(store, transformer)
            integrity = IntegrityVerifier(store, contract, check=check,
                                          progress=lambda amount, message: progress("verify_exports", amount, message))
            exporter = Exporter(staging, store.tables, schema_names)
            completed = 0
            output_digest = hashlib.sha256()
            progress("transform", 0, "Согласованные замены и запись трёх экспортов")
            for name, table in store.tables.items():
                check()
                exporter.start(name, table)
                for row_id, old in store.rows_with_ids(table["id"]):
                    check()
                    # The generator must not be the source-of-truth oracle:
                    # a mistaken in-place mutation cannot alter this snapshot.
                    source_row = tuple(old)
                    for field, value in zip(table["fields"], source_row):
                        if field["name"] not in SYSTEM:
                            validate_value(name[1], field, value)
                    row = transformer.transform(name[0], name[1], table["fields"], old)
                    if (not isinstance(row, (list, tuple)) or len(row) != len(table["fields"])
                            or any(value is not None and not isinstance(value, str) for value in row)):
                        refuse_integrity("RELATION_CHANGED", {"checks": [{"id": "transformation", "kind": "transformation",
                            "status": "FAIL", "required": True, "reason": "ROW_SHAPE_CHANGED"}]})
                    try:
                        validator.row(name, table, source_row, row)
                    except AnonymizerError:
                        # The original row was validated before generation;
                        # this failure is evidence against the transformation.
                        refuse_integrity("RELATION_CHANGED", {"checks": [{"id": "transformation", "kind": "transformation",
                            "status": "FAIL", "required": True, "reason": "PRESERVED_VALUES_CHANGED"}]})
                    integrity.expected_row(name, row_id, source_row, row)
                    exporter.row(row)
                    output_digest.update(json.dumps(row, ensure_ascii=False).encode())
                    completed += 1
                    if completed % 1000 == 0:
                        progress("transform", completed / store.count, f"Обработано строк: {completed:,}")
                exporter.end()
            exporter.close(True)
            exporter = None
            progress("verify_exports", 0, "Повторное чтение CSV, COPY и INSERT; проверка связности")
            integrity_result = integrity.verify(staging, schema_names)
            for item in integrity_result.get("checks", ()):
                status = item.get("status", "NOT_CHECKED")
                log.add("error" if status == "FAIL" else "check" if status == "PASS" else "warning",
                        "verify_exports", "integrity_check", item["id"],
                        f"{status}; lost={item.get('lost', 0)}, added={item.get('added', 0)}")
            if (integrity_result["integrity_status"] == "FAILED"
                    or any(item.get("status") == "FAIL" for item in integrity_result.get("checks", ()))):
                codes = {"DOMAIN_MAPPING_CONFLICT", "KEY_GROUP_CHANGED", "RELATION_CHANGED"}
                failure_code = next((item.get("reason") for item in integrity_result.get("checks", ())
                                     if item.get("status") == "FAIL" and item.get("reason") in codes), "RELATION_CHANGED")
                refuse_integrity(integrity_result.get("error_code", failure_code), integrity_result)
            if options.require_integrity and integrity_result["integrity_status"] != "PASSED":
                unsupported = any(item.get("required") and (item.get("status") == "UNSUPPORTED"
                                  or item.get("reason") in {"SYSTEM_FIELD_POLICY", "SYSTEM_FIELD_EXCLUDED"})
                                  for item in integrity_result.get("checks", ()))
                refuse_integrity("CHECK_UNSUPPORTED" if unsupported else "RELATION_TARGET_MISSING", integrity_result)
            progress("validate", 0, "Проверка связей, ключей и сохраняемых полей")
            id_to_name = {data["id"]: f"{schema_names[name[0]]}.{name[1]}" for name, data in store.tables.items()}
            validations = validator.finish()
            log.add_checks(validations, id_to_name)
            log.add("check", "validate", "preserved_fields", "", "сохраняемые поля и числа/даты JSON совпали")
            stage["current"] = "publish"
            report = {"format": "snp-anonymizer-report-v2", "app_version": __version__, "model": model.data["meta"],
                      "model_applied": not options.bypass_model,
                      "structure_mode": "automatic_without_model" if options.bypass_model else "standard",
                      "relationships_checked": integrity_result["relationships_checked"],
                      "keys_checked": integrity_result["keys_checked"],
                      "industry": options.industry, "tables": len(store.tables), "rows": completed,
                      "changed_cells": transformer.changed, "sys_id": "cleared to NULL in CSV/dump; omitted from INSERT and PostgreSQL COPY",
                      "system_fields": {"sys_id": "clear", "update_date_time": "passthrough",
                                        "change_author": "constant:snp", "validation": "excluded"},
                      "output_values_sha256": output_digest.hexdigest(), "validation": validations,
                      "skipped_tables": skipped_entries,
                      "skipped_sql_statements": store.skipped,
                      "scope": "reconstructed tables and data; no original functions, grants, comments, defaults or indexes",
                      "privacy": "nontechnical text and JSON strings/keys pseudonymized; JSON numbers/dates preserved; project mappings excluded",
                      "fields": [{"schema": schema_names[name[0]], "table": name[1], "columns": [
                          {"name": f["name"], "rule": transformer.rule(name[1], f, name[0]), "type": f["sql_type"]} for f in table["fields"]]} for name, table in store.tables.items()]}
            report.update(integrity_result)
            report.update(profile_fields(contract))
            report["dataset_published"] = True
            if profile is not None:
                profile_file = staging / "connectivity-profile.json"
                encoded_profile = json.dumps(contract.public_profile(), ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")
                if len(encoded_profile) > MAX_PROFILE_BYTES:
                    raise ContractError("Превышен лимит публикуемого профиля.")
                profile_file.write_bytes(encoded_profile)
                with profile_file.open("rb") as stream:
                    published_profile = stream.read(MAX_PROFILE_BYTES + 1)
                if hashlib.sha256(published_profile).hexdigest() != contract.profile_hash:
                    raise ContractError("Профиль в результате отличается от проверенного контракта.")
            check()
            if options.mapping_xlsx:
                progress("mapping", 0, "Запись карты замен в Excel")

                def mapping_rows():
                    for domain, old, new in project.iter_mapping():
                        # The published schema name is the raw replacement plus
                        # the snp_ prefix; the map shows what the output contains.
                        yield (domain, old, "snp_" + str(new).lower()) if domain == "schema" else (domain, old, new)

                # Private sibling staging: never put originals into the output
                # package and never truncate an existing map on a failed save.
                try:
                    mapping_temp = Path(tempfile.mkdtemp(prefix=".snp-map-", dir=options.mapping_xlsx.resolve().parent))
                except OSError as error:
                    raise AnonymizerError(f"Не удалось подготовить файл карты замен ({type(error).__name__}). "
                                          "Проверьте путь и свободное место.") from None
                write_mapping_xlsx(mapping_temp / "mapping.xlsx", mapping_rows(), check=check)
                progress("mapping", 1, "Карта замен записана")
            check()
            pg_tables = store.tables
            store.close()
            store = None
        # Spool and temporary input cleanup must also succeed before PG COMMIT.

        def publish(pg_summary=None, pg_skipped=()):
            nonlocal project, published, mapping_temp
            if pg_summary is not None:
                report["pg_load"] = pg_summary
                # Written before COMMIT; remains honest even on a lost reply.
                report["pg_commit_status"] = "not_confirmed"
            (staging / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            check()
            if mapping_temp is not None:
                os.replace(mapping_temp / "mapping.xlsx", options.mapping_xlsx)
                mapping_temp.rmdir()
                mapping_temp = None
                log.add("info", "mapping", "mapping_export", options.mapping_xlsx.name, "карта замен выгружена")
            # Persist correspondences before any DB commit can become durable.
            # On a later local failure, extra mappings are safe to reuse.
            project.finish(True)
            project = None
            check()
            staging.rename(output)
            published = True

        if options.pg:
            progress("pg", 0, "Загрузка в PostgreSQL")
            pg_load(pg_tables, staging / "dump.sql", options.pg,
                    progress=lambda done: progress("pg", min(done / max(completed, 1), 1), f"Загружено строк: {done:,}"),
                    check=check, before_commit=publish)
        else:
            publish()
        success = True
        # All mandatory work is complete. Reporting failures must not disguise a
        # successful PG commit as a failed run or delete its evidence.
        if options.pg:
            report["pg_commit_status"] = "committed"
            try:
                status_file = output / ".report.json.tmp"
                status_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                status_file.replace(output / "report.json")
            except OSError:
                log.add("warning", "pg", "commit_status_report", "",
                        "COMMIT подтверждён; не удалось обновить его статус в report.json.")
                try:
                    status_file.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            log.add("info", "publish", "published", output.name, f"пакет опубликован; строк: {completed:,}")
            progress("done", 1, f"Готово: {completed:,} строк")
        except Exception:
            pass  # a notification cannot undo a completed publication/COMMIT
        return report
    except PgCommitUncertain as error:
        retain_output = True
        log.add("warning", "pg", "commit_not_confirmed", "", str(error))
        raise
    except IntegrityError as error:
        log.add("error", "verify_exports", "integrity_refused", "", str(error))
        raise
    except ContractError as error:
        log.add("error", "connectivity", "contract_refused", "", error.code)
        refuse_integrity(error.code)
    except InputLimitError:
        if options.connectivity_profile or options.require_integrity or stage["current"] == "verify_exports":
            refuse_integrity("CHECK_INCOMPLETE", integrity_result)
        raise
    except Cancelled:
        log.add("warning", stage["current"], "cancelled", "", "Обработка отменена. Итоговый пакет не создан.")
        if options.connectivity_profile or options.require_integrity or stage["current"] == "verify_exports":
            refuse_integrity("CHECK_INCOMPLETE", integrity_result)
        raise
    except UnicodeError:
        log.add("error", stage["current"], "error", "", "Ошибка кодировки. Выберите UTF-8 или Windows-1251.")
        raise AnonymizerError("Ошибка кодировки. Выберите UTF-8 или Windows-1251.") from None
    except AnonymizerError as error:
        log.add("error", stage["current"], "error", "", str(error))
        if options.connectivity_profile and stage["current"] == "connectivity":
            refuse_integrity("CONTRACT_CONFLICT", integrity_result)
        if stage["current"] == "verify_exports" or (options.connectivity_profile and stage["current"] in {"transform", "validate", "mapping", "publish"}):
            refuse_integrity("CHECK_INCOMPLETE", integrity_result)
        raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        # Do not echo raw exception messages: they may contain input content/paths.
        log.add("error", stage["current"], "error", "", f"Не удалось обработать файлы ({type(error).__name__}). "
                "Проверьте формат, свободное место и доступ к папкам.")
        if stage["current"] == "verify_exports" or (options.connectivity_profile and stage["current"] in {"connectivity", "transform", "validate", "mapping", "publish"}):
            refuse_integrity("CHECK_INCOMPLETE", integrity_result)
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
        if mapping_temp:
            shutil.rmtree(mapping_temp, ignore_errors=True)
        if not success and not retain_output:
            shutil.rmtree(staging, ignore_errors=True)
            if published:
                shutil.rmtree(output, ignore_errors=True)

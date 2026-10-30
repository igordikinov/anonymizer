import argparse
import getpass
import json
import sys
from pathlib import Path

from .errors import AnonymizerError
from .mapping import INDUSTRIES
from .pipeline import Options, PgOptions, run
from .runlog import RunLog


def main(argv=None):
    parser = argparse.ArgumentParser(description="SNP Anonymizer — автономное обезличивание данных")
    parser.add_argument("--input", type=Path, help="SQL/XLSX-файл, ZIP-архив или папка CSV/XLSX")
    parser.add_argument("--output", type=Path, help="Новая папка результата")
    parser.add_argument("--project", type=Path, help="Локальный файл соответствий .snpanon")
    parser.add_argument("--industry", choices=INDUSTRIES, default="food")
    parser.add_argument("--encoding", choices=["utf-8-sig", "cp1251"], default="utf-8-sig")
    parser.add_argument("--delimiter", choices=[",", ";", "tab", "|"], default=None)
    parser.add_argument("--schema", action="append", default=[], help="Обрабатывать только указанную схему SQL; можно повторить")
    parser.add_argument("--csv-schema", default="public", help="Исходная схема для CSV/XLSX без manifest (по умолчанию public)")
    parser.add_argument("--pg-host", help="Загрузить результат в PostgreSQL: хост")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-db", dest="pg_db", help="Целевая база данных")
    parser.add_argument("--pg-user", help="Пользователь")
    parser.add_argument("--pg-password", default=None, help="Пароль; без флага запрашивается скрыто")
    parser.add_argument("--pg-schema", default="public", help="Целевая схема (создаётся при отсутствии)")
    parser.add_argument("--mapping-xlsx", type=Path, default=None, help="Записать карту замен (что на что изменено) в отдельный Excel-файл; путь вне папки результата")
    parser.add_argument("--mapping-xlsx-overwrite", action="store_true", help="Разрешить перезапись существующего файла карты")
    parser.add_argument("--log-csv", type=Path, default=None, help="Записать журнал проверок и ошибок в CSV в конце прогона")
    parser.add_argument("--skip-error-tables", action="store_true", help="Пропускать таблицы с ошибками вместо остановки обработки")
    parser.add_argument("--bypass-model", action="store_true", help="Обходить эталон: без профиля тексты заменяются независимо, числа/даты сохраняются; профиль связности применяется отдельно")
    parser.add_argument("--connectivity-profile", default=None, metavar="builtin:optimizer-v2|PATH", help="Встроенный Optimizer v2 или путь к JSON-профилю связности")
    parser.add_argument("--require-integrity", action="store_true", help="Выдавать датасет только при полном PASS обязательных проверок связности")
    parser.add_argument("--smoke-gui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-gui-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_gui_report and not args.smoke_gui:
        parser.error("--smoke-gui-report требует --smoke-gui")
    if args.smoke_gui or not args.input:
        from .gui import launch
        try:
            launch(smoke=args.smoke_gui, smoke_report=args.smoke_gui_report)
        except Exception:
            if not args.smoke_gui:
                raise
            # A windowed PyInstaller executable must fail smoke with an exit
            # code rather than leave its unhandled-exception dialog open.
            if sys.stderr is not None:
                print("Smoke GUI: оформление или встроенные шрифты не прошли проверку.", file=sys.stderr)
            return 1
        return 0
    if not args.output or not args.project:
        parser.error("Для CLI обязательны --input, --output и --project")
    pg = None
    if args.pg_host:
        if not args.pg_db or not args.pg_user:
            parser.error("Для загрузки в PostgreSQL обязательны --pg-db и --pg-user")
        password = args.pg_password if args.pg_password is not None else getpass.getpass(f"Пароль PostgreSQL для {args.pg_user}@{args.pg_host}: ")
        pg = PgOptions(args.pg_host, args.pg_port, args.pg_db, args.pg_user, password, args.pg_schema)
    elif any([args.pg_db, args.pg_user, args.pg_password is not None, args.pg_schema != "public"]):
        parser.error("Флаги --pg-* требуют --pg-host; без него загрузка в PostgreSQL не выполняется")
    runlog = RunLog()
    try:
        report = run(Options(args.input, args.output, args.project, args.industry, args.encoding,
                             "\t" if args.delimiter == "tab" else args.delimiter, tuple(args.schema), args.csv_schema, pg,
                             args.mapping_xlsx, args.mapping_xlsx_overwrite, runlog,
                             args.skip_error_tables, args.bypass_model,
                             connectivity_profile=args.connectivity_profile, require_integrity=args.require_integrity))
        if args.log_csv:
            _write_log(runlog, args.log_csv)
        summary = {"tables": report["tables"], "rows": report["rows"], "output": str(args.output)}
        for key in ("integrity_status", "source_integrity_status", "publication_mode"):
            if key in report:
                summary[key] = report[key]
        if "pg_load" in report:
            summary["pg_load"] = report["pg_load"]
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except AnonymizerError as error:
        if args.log_csv:
            _write_log(runlog, args.log_csv)
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        if args.log_csv:
            _write_log(runlog, args.log_csv)
        print("Внутренняя ошибка обработки; клиентские значения не записаны в журнал.", file=sys.stderr)
        return 2


def _write_log(runlog, path):
    try:
        written = runlog.write_csv(path)
        print(f"Журнал записан: {path} ({written} записей)", file=sys.stderr)
    except OSError as io_error:
        print(f"Не удалось записать журнал ({type(io_error).__name__}).", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

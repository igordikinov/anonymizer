import argparse
import getpass
import json
import sys
from pathlib import Path

from .errors import AnonymizerError
from .mapping import INDUSTRIES
from .pipeline import Options, PgOptions, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="SNP Anonymizer — автономное обезличивание данных")
    parser.add_argument("--input", type=Path, help="SQL-файл или папка CSV")
    parser.add_argument("--output", type=Path, help="Новая папка результата")
    parser.add_argument("--project", type=Path, help="Локальный файл соответствий .snpanon")
    parser.add_argument("--industry", choices=INDUSTRIES, default="food")
    parser.add_argument("--encoding", choices=["utf-8-sig", "cp1251"], default="utf-8-sig")
    parser.add_argument("--delimiter", choices=[",", ";", "tab", "|"], default=None)
    parser.add_argument("--schema", action="append", default=[], help="Обрабатывать только указанную схему SQL; можно повторить")
    parser.add_argument("--csv-schema", default="public")
    parser.add_argument("--pg-host", help="Загрузить результат в PostgreSQL: хост")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-db", dest="pg_db", help="Целевая база данных")
    parser.add_argument("--pg-user", help="Пользователь")
    parser.add_argument("--pg-password", default=None, help="Пароль; без флага запрашивается скрыто")
    parser.add_argument("--pg-schema", default="public", help="Целевая схема (создаётся при отсутствии)")
    parser.add_argument("--smoke-gui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_gui or not args.input:
        from .gui import launch
        launch(smoke=args.smoke_gui)
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
    try:
        report = run(Options(args.input, args.output, args.project, args.industry, args.encoding,
                             "\t" if args.delimiter == "tab" else args.delimiter, tuple(args.schema), args.csv_schema, pg))
        summary = {"tables": report["tables"], "rows": report["rows"], "output": str(args.output)}
        if "pg_load" in report:
            summary["pg_load"] = report["pg_load"]
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except AnonymizerError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Внутренняя ошибка обработки; клиентские значения не записаны в журнал.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

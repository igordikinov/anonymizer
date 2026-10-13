import argparse
import json
import sys
from pathlib import Path

from .errors import AnonymizerError
from .mapping import INDUSTRIES
from .pipeline import Options, run


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
    parser.add_argument("--smoke-gui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_gui or not args.input:
        from .gui import launch
        launch(smoke=args.smoke_gui)
        return 0
    if not args.output or not args.project:
        parser.error("Для CLI обязательны --input, --output и --project")
    try:
        report = run(Options(args.input, args.output, args.project, args.industry, args.encoding,
                             "\t" if args.delimiter == "tab" else args.delimiter, tuple(args.schema), args.csv_schema))
        print(json.dumps({"tables": report["tables"], "rows": report["rows"], "output": str(args.output)}, ensure_ascii=False))
        return 0
    except AnonymizerError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Внутренняя ошибка обработки; клиентские значения не записаны в журнал.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

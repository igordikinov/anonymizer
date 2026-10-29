"""Route a flat CSV/XLSX dataset after validating identities for the entire set."""
from __future__ import annotations

import re

from .csv_reader import parse_csv
from .errors import AnonymizerError
from .xlsx_reader import parse_xlsx


MAX_DATASET_FILES = 4096
MAX_NAMES_BYTES = 8 * 1024 * 1024


def table_name(path):
    return re.sub(r"_\d{12,}$", "", path.stem).lower()


def parse_tabular(path, store, check=lambda: None, encoding="utf-8-sig", delimiter=None,
                  schema="public", progress=lambda n: None, skip_error_tables=False):
    if path.is_file():
        return parse_xlsx(path, store, check, schema=schema, progress=progress,
                          skip_error_tables=skip_error_tables)
    files, names_bytes = [], 0
    for file in path.iterdir():
        check()
        if not file.is_file() or file.suffix.lower() not in {".csv", ".xlsx"}:
            continue
        if not file.resolve().is_relative_to(path.resolve()):
            raise AnonymizerError("Файл CSV/XLSX находится вне папки исходных данных; обработка запрещена.")
        names_bytes += len(file.name.encode("utf-8"))
        if len(files) >= MAX_DATASET_FILES or names_bytes > MAX_NAMES_BYTES:
            raise AnonymizerError("Слишком много файлов или слишком длинные имена в наборе CSV/XLSX.")
        files.append(file)
    files.sort(key=lambda file: file.name)
    has_manifest = (path / "manifest.json").exists()
    if has_manifest:
        if any(file.suffix.lower() == ".xlsx" for file in files):
            raise AnonymizerError("Набор с XLSX и manifest.json не поддерживается: manifest описывает только CSV.")
        return parse_csv(path, store, check, encoding, delimiter, schema, progress, skip_error_tables)
    if not files:
        raise AnonymizerError("В выбранной папке нет CSV или XLSX.")
    seen = set()
    for file in files:
        name = table_name(file)
        if name in seen:
            raise AnonymizerError("Таблица повторяется в нескольких файлах CSV/XLSX; оставьте один файл на таблицу.")
        seen.add(name)
    done = 0
    for file in files:
        check()
        callback = lambda amount: progress(done + amount)
        if file.suffix.lower() == ".xlsx":
            parse_xlsx(file, store, check, schema=schema, progress=callback,
                       skip_error_tables=skip_error_tables, table=table_name(file))
        else:
            parse_csv(path, store, check, encoding, delimiter, schema, callback,
                      skip_error_tables, files=[file])
        done += file.stat().st_size
        progress(done)

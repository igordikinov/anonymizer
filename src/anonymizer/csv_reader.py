from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .errors import AnonymizerError, Cancelled
from .generic_input import InputLimitError
from .metadata import SYSTEM
from .sql_reader import MAX_FIELD

MAX_INPUT_MANIFEST = 8 * 1024**2


class _BoundedRecordLines:
    """Prevent csv.reader from accumulating an unbounded logical record."""
    def __init__(self, stream, check, table):
        self.stream, self.check, self.table = stream, check, table
        self.bytes = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.check()
        # Limit characters before creating a physical-line string; UTF-8 bytes
        # are checked before handing the line to the CSV parser as well.
        line = self.stream.readline(MAX_FIELD + 1)
        if not line:
            raise StopIteration
        self.bytes += len(line.encode("utf-8"))
        if self.bytes > MAX_FIELD:
            raise AnonymizerError(f"CSV {self.table}: запись превышает лимит размера.")
        return line

    def reset(self):
        self.bytes = 0


def parse_csv(path, store, check=lambda: None, encoding="utf-8-sig", delimiter=None, schema="public", progress=lambda n: None, skip_error_tables=False, *, files=None):
    csv.field_size_limit(MAX_FIELD)
    manifest = None
    if (path / "manifest.json").exists():
        if store.bypass_model:
            with (path / "manifest.json").open("rb") as stream:
                raw_manifest = stream.read(MAX_INPUT_MANIFEST + 1)
            if len(raw_manifest) > MAX_INPUT_MANIFEST:
                raise InputLimitError("Превышен лимит manifest.json без эталона.")
            manifest = json.loads(raw_manifest.decode("utf-8"))
        else:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") not in ("snp-anonymizer-csv-v1", "snp-anonymizer-csv-v2"):
            raise AnonymizerError("Неподдерживаемый manifest.json.")
    entries = manifest["tables"] if manifest else [{"file": p.name, "schema": schema,
               "table": re.sub(r"_\d{12,}$", "", p.stem).lower()} for p in (files if files is not None else sorted(path.glob("*.csv")))]
    if not entries:
        raise AnonymizerError("В выбранной папке нет CSV.")
    identities = [(entry["schema"], entry["table"]) for entry in entries]
    if len(set(identities)) != len(identities):
        raise AnonymizerError("Таблица повторяется в нескольких файлах CSV; оставьте один файл на таблицу.")
    done = 0
    for entry in entries:
        check()
        file = (path / entry["file"]).resolve()
        if not file.is_relative_to(path.resolve()) or not file.is_file():
            raise AnonymizerError("Некорректный путь CSV в manifest.json.")
        try:
            _read_csv_file(store, check, entry, file, encoding, delimiter, manifest, path)
        except (Cancelled, InputLimitError):
            raise
        except (AnonymizerError, OSError, ValueError, csv.Error) as error:
            # Таблично-атрибутируемая ошибка: в skip-режиме файл пропускается целиком.
            if not skip_error_tables:
                raise
            if isinstance(error, AnonymizerError):
                reason = str(error)
            else:
                reason = f"Файл не читается ({type(error).__name__})."
            store.drop_table((entry["schema"], entry["table"]), reason)
        done += file.stat().st_size
        progress(done)


def _read_csv_file(store, check, entry, file, encoding, delimiter, manifest, path):
    with file.open(encoding=encoding, newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        sep = "," if manifest else delimiter
        if not sep:
            try:
                sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                raise AnonymizerError(f"CSV {entry['table']}: не удалось определить разделитель; выберите его вручную.")
        bounded = _BoundedRecordLines(stream, check, entry["table"]) if store.bypass_model else None
        reader = csv.reader(bounded if bounded is not None else stream, delimiter=sep, strict=True)
        header = next(reader, None)
        if bounded is not None:
            bounded.reset()
        if not header:
            raise AnonymizerError(f"CSV {entry['table']}: отсутствует заголовок.")
        if len(header) != len(set(header)):
            raise AnonymizerError(f"CSV {entry['table']}: повторяющиеся колонки.")
        name = (entry["schema"], entry["table"])
        if manifest and entry.get("fields"):
            store.define(name, entry["fields"], entry.get("foreign_keys", ()), entry.get("key_groups", ()))
        elif store.bypass_model:
            store.define_input(name, header)
        else:
            # CSV has no physical DDL: build the complete selected model table.
            store.define(name, store.model.fields(name[1]), from_model=True)
        full_header = list(header)
        marked_empty_null = (entry.get("empty_null_columns", [])
                             if manifest and manifest["format"] == "snp-anonymizer-csv-v2" else [])
        known_columns = set(full_header)
        table = store.tables.get(name)
        if table is not None:
            known_columns.intersection_update(f["name"] for f in table["fields"])
        if (not isinstance(marked_empty_null, list) or len(marked_empty_null) > len(full_header)
                or any(not isinstance(col, str) or col not in known_columns for col in marked_empty_null)
                or len(set(marked_empty_null)) != len(marked_empty_null)):
            raise AnonymizerError(f"CSV {entry['table']}: некорректный список empty_null_columns в manifest.json.")
        header = store.ensure(name, header)
        store.note_input_columns(name, full_header)
        table = store.tables.get(name)
        if manifest and table is not None:
            table["csv_empty_null_columns"] = frozenset(marked_empty_null).intersection(header) - {"update_date_time"}
        kept_indexes = [i for i, col in enumerate(full_header) if col in set(header)]
        # External CSV commonly uses empty cells for absent numeric values.
        # Resolve only nullable numeric columns once, from the effective model.
        # A table can already be dropped by ensure() for a missing business key.
        nullable_numbers = {f["name"] for f in store.tables.get(name, {}).get("fields", ())
                            if f["name"] not in SYSTEM and f["type"] in {"NUMBER", "INTEGER"}
                            and f.get("nullable", True)} if manifest is None else set()
        null_columns = nullable_numbers | set(marked_empty_null)
        empty_as_null = {i for i, col in enumerate(full_header) if col in null_columns}
        for row in reader:
            if bounded is not None:
                bounded.reset()
            check()
            if len(row) != len(full_header):
                raise AnonymizerError(f"CSV {entry['table']}, запись {reader.line_num}: неправильное число полей.")
            # Only explicit manifest columns or external nullable numbers
            # decode blanks as NULL; other TEXT and escaping remain unchanged.
            # csv.reader returns both quoted and unquoted empty cells as "".
            values = [None if value == r"\N" or (value == "" and i in empty_as_null)
                      else (value[1:] if manifest and value.startswith("\\\\") else value)
                      for i, value in enumerate(row)]
            if store.bypass_model:
                store.observe_input(name, values)
            store.add_row(name, header, [values[i] for i in kept_indexes])
        if store.bypass_model:
            store.finish_input(name, check)

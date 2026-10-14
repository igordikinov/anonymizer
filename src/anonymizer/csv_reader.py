from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .errors import AnonymizerError
from .sql_reader import MAX_FIELD


def parse_csv(path, store, check=lambda: None, encoding="utf-8-sig", delimiter=None, schema="public", progress=lambda n: None):
    csv.field_size_limit(MAX_FIELD)
    manifest = None
    if (path / "manifest.json").exists():
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != "snp-anonymizer-csv-v1":
            raise AnonymizerError("Неподдерживаемый manifest.json.")
    entries = manifest["tables"] if manifest else [{"file": p.name, "schema": schema,
               "table": re.sub(r"_\d{12,}$", "", p.stem).lower()} for p in sorted(path.glob("*.csv"))]
    if not entries:
        raise AnonymizerError("В выбранной папке нет CSV.")
    done = 0
    for entry in entries:
        check()
        file = (path / entry["file"]).resolve()
        if not file.is_relative_to(path.resolve()) or not file.is_file():
            raise AnonymizerError("Некорректный путь CSV в manifest.json.")
        with file.open(encoding=encoding, newline="") as stream:
            sample = stream.read(8192)
            stream.seek(0)
            sep = "," if manifest else delimiter
            if not sep:
                try:
                    sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                except csv.Error:
                    raise AnonymizerError(f"CSV {entry['table']}: не удалось определить разделитель; выберите его вручную.")
            reader = csv.reader(stream, delimiter=sep, strict=True)
            header = next(reader, None)
            if not header:
                raise AnonymizerError(f"CSV {entry['table']}: отсутствует заголовок.")
            if len(header) != len(set(header)):
                raise AnonymizerError(f"CSV {entry['table']}: повторяющиеся колонки.")
            name = (entry["schema"], entry["table"])
            if manifest and entry.get("fields"):
                store.define(name, entry["fields"], entry.get("foreign_keys", ()), entry.get("key_groups", ()))
            else:
                # CSV has no physical DDL: build the complete selected model table.
                store.define(name, store.model.fields(name[1]))
            full_header = list(header)
            header = store.ensure(name, header)
            kept_indexes = [i for i, col in enumerate(full_header) if col in set(header)]
            for row in reader:
                check()
                if len(row) != len(full_header):
                    raise AnonymizerError(f"CSV {entry['table']}, запись {reader.line_num}: неправильное число полей.")
                # Explicit NULL token. Empty strings remain empty, including quoted ones.
                values = [None if value == r"\N" else (value[1:] if manifest and value.startswith("\\\\") else value) for value in row]
                store.add_row(name, header, [values[i] for i in kept_indexes])
        done += file.stat().st_size
        progress(done)

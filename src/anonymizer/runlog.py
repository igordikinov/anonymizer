"""In-memory run journal: every check result and error of one processing run.

Entries carry only technical names, counters, stages and the already
value-free error texts — never customer values, source paths or passwords.
The journal lives in memory; the GUI exports it to CSV on demand and the CLI
writes it at the end of the run. It is never part of the published package.
"""
from __future__ import annotations

import csv
from datetime import datetime

HEADER = ("время", "уровень", "стадия", "событие", "объект", "сообщение")


class RunLog:
    def __init__(self):
        self.entries: list[tuple[str, str, str, str, str, str]] = []

    def add(self, level, stage, event, object="", detail=""):
        self.entries.append((datetime.now().isoformat(timespec="seconds"), level, stage, event, object, detail))

    def add_checks(self, validations, id_to_name):
        """Converts the validator report into per-check journal entries."""
        self.add("check", "validate", "checked_rows", "", str(validations["checked_rows"]))
        for fk in validations["foreign_keys"]:
            name = id_to_name.get(fk["table_id"], str(fk["table_id"]))
            columns = fk.get("column") or ", ".join(fk.get("columns", []))
            obj = f"{name}: {columns}"
            if fk.get("status") == "target_not_in_input":
                self.add("warning", "validate", "fk_target_not_in_input", obj,
                         "master отсутствует во входных данных; связь не проверена")
            else:
                level = "warning" if fk.get("missing_before") or fk.get("missing_after") else "check"
                self.add(level, "validate", "fk", obj,
                         f"отсутствующих ссылок: {fk['missing_before']} -> {fk['missing_after']}")
        for group in validations.get("business_key_duplicates", []):
            name = id_to_name.get(group["table_id"], str(group["table_id"]))
            self.add("check", "validate", "key_group", f"{name}: {', '.join(group['columns'])}",
                     f"дубликатов: {group['extra_rows_before']} -> {group['extra_rows_after']} "
                     f"({group['kind']}, nulls {group['nulls']})")

    def write_csv(self, path) -> int:
        """UTF-8 with BOM and ';' so RU Excel opens it by double-click."""
        safe = [tuple(self._formula_safe(cell) for cell in row) for row in self.entries]
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(HEADER)
            writer.writerows(safe)
        return len(self.entries)

    @staticmethod
    def _formula_safe(cell):
        # Идентификаторы приходят из входного SQL; Excel исполняет ячейки,
        # начинающиеся с =, +, -, @, как формулы — нейтрализуем префиксом.
        return "'" + cell if cell[:1] in {"=", "+", "-", "@"} else cell

from __future__ import annotations

import json
import re
import sqlite3

from .errors import AnonymizerError


class Store:
    def __init__(self, path, model):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA cache_size=-16384")
        self.db.executescript("""
          CREATE TABLE rows (id INTEGER PRIMARY KEY, tid INTEGER NOT NULL, body TEXT NOT NULL);
          CREATE INDEX row_table ON rows(tid,id);
          CREATE TABLE refvalues (tid INTEGER, col TEXT, old TEXT, new TEXT, PRIMARY KEY(tid,col,old)) WITHOUT ROWID;
          CREATE INDEX ref_new ON refvalues(tid,col,new);
        """)
        self.model, self.tables, self.skipped = model, {}, {}
        self.count, self.batch, self.batch_bytes = 0, [], 0

    def define(self, name, fields):
        identifiers = [*name, *(f["name"] for f in fields)]
        if any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9$]{0,62}", n) for n in identifiers):
            raise AnonymizerError("Некорректное имя схемы, таблицы или колонки. Поддерживаются технические имена SNP.")
        if not fields or len({f['name'] for f in fields}) != len(fields):
            raise AnonymizerError("Пустой или повторяющийся список колонок в структуре.")
        if name in self.tables:
            raise AnonymizerError(f"Повторное CREATE TABLE: {name[1]}.")
        tid = len(self.tables) + 1
        self.tables[name] = {"id": tid, "fields": [self.model.enrich(name[1], f) for f in fields], "rows": 0}

    def columns(self, name):
        if name not in self.tables:
            raise AnonymizerError(f"Таблица {name[1]}: нет DDL и списка колонок.")
        return [f["name"] for f in self.tables[name]["fields"]]

    def ensure(self, name, columns):
        if not columns or len(set(columns)) != len(columns):
            raise AnonymizerError(f"Таблица {name[1]}: пустые или повторяющиеся колонки.")
        if name not in self.tables:
            fields = []
            for col in columns:
                field = self.model.field(name[1], col)
                if not field and col in {"sys_id", "change_author", "update_date_time"}:
                    field = {"name": col, "type": {"sys_id": "INTEGER", "change_author": "UUID", "update_date_time": "DATE_TIME"}[col], "nullable": True}
                if not field:
                    raise AnonymizerError(f"Таблица {name[1]}, колонка {col}: нет типа в модели. Нужен SQL с CREATE TABLE.")
                fields.append(field)
            self.define(name, fields)
        available = self.columns(name)
        if any(c not in available for c in columns):
            raise AnonymizerError(f"Таблица {name[1]}: колонка данных отсутствует в DDL.")

    def add_row(self, name, columns, row):
        table = self.tables[name]
        values = dict(zip(columns, row, strict=True))
        body = [values.get(f["name"]) for f in table["fields"]]
        for f, value in zip(table["fields"], body):
            if value is None and not f.get("nullable", True) and f["name"] != "sys_id":
                raise AnonymizerError(f"Таблица {name[1]}, колонка {f['name']}: NULL в обязательном поле или пропущен DEFAULT.")
        encoded = json.dumps(body, ensure_ascii=False)
        self.batch.append((table["id"], encoded))
        self.batch_bytes += len(encoded) * 4
        self.count += 1
        table["rows"] += 1
        if len(self.batch) >= 1000 or self.batch_bytes >= 4 * 1024 * 1024:
            self.flush()

    def flush(self):
        if self.batch:
            self.db.executemany("INSERT INTO rows(tid,body) VALUES (?,?)", self.batch)
            self.batch.clear()
            self.batch_bytes = 0
            self.db.commit()

    def rows(self, tid):
        self.flush()
        cursor = self.db.execute("SELECT body FROM rows WHERE tid=? ORDER BY id", (tid,))
        for (body,) in cursor:
            yield json.loads(body)

    def close(self):
        self.db.close()

    def root(self, schema, table, column):
        seen = set()
        while (schema, table, column) not in seen:
            seen.add((schema, table, column))
            field = next((f for f in self.tables.get((schema, table), {}).get("fields", []) if f["name"] == column), None)
            if field and field.get("fk"):
                fk = field["fk"]
                target = fk.get("schema", schema), fk["table"], fk["field"]
                if target == (schema, table, column):
                    return target
                schema, table, column = target
            else:
                root = self.model.root(table, column)
                return schema, *root
        raise AnonymizerError("Цикл ссылок между полями. Нужен исправленный профиль модели.")

    def field(self, schema, table, column):
        return next((f for f in self.tables.get((schema, table), {}).get("fields", []) if f["name"] == column), None) or self.model.field(table, column)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

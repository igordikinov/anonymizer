from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext
from datetime import date, datetime, time
from uuid import UUID

from .errors import AnonymizerError
from . import json_data
from . import canonical


def validate_value(table, field, value):
    if value is None:
        if not field.get("nullable", True) and field["name"] != "sys_id":
            raise AnonymizerError(f"Таблица {table}, колонка {field['name']}: NULL в обязательном поле.")
        return
    if "\0" in value:
        raise AnonymizerError(f"Таблица {table}, колонка {field['name']}: нулевой байт не поддерживается PostgreSQL.")
    kind = field["type"]
    try:
        if kind in {"NUMBER", "INTEGER"}:
            number = Decimal(value)
            if kind == "INTEGER" or field["sql_type"] in {"integer", "bigint", "smallint", "int", "int2", "int4", "int8"}:
                if not number.is_finite() or number != number.to_integral_value():
                    raise ValueError()
                bounds = {"smallint": 2**15, "int2": 2**15, "integer": 2**31, "int": 2**31, "int4": 2**31}
                limit = bounds.get(field["sql_type"], 2**63)
                if not -limit <= number < limit:
                    raise ValueError()
            if field["sql_type"].startswith(("numeric(", "decimal(")):
                import re
                precision, _, scale = re.search(r"\(([^)]+)\)", field["sql_type"])[1].partition(",")
                scale = int(scale or 0)
                if number.is_infinite():
                    raise ValueError()
                if number.is_finite():
                    with localcontext() as ctx:
                        ctx.prec = max(28, len(number.as_tuple().digits) + scale + 2)
                        if number.quantize(Decimal(1).scaleb(-scale)) != number or abs(number) >= Decimal(10) ** (int(precision)-scale):
                            raise ValueError()
        elif kind == "BOOLEAN":
            if value.lower() not in {"true", "false", "t", "f", "0", "1", "yes", "no", "on", "off"}:
                raise ValueError()
        elif kind == "UUID":
            UUID(value)
        elif kind == "JSONB":
            json_data.load(value)
        elif kind in {"DATE", "DATE_TIME", "TIME"} and value not in {"infinity", "-infinity"}:
            {"DATE": date.fromisoformat, "DATE_TIME": datetime.fromisoformat, "TIME": time.fromisoformat}[kind](value)
        elif kind == "TEXT" and field.get("length") and len(value) > field["length"]:
            raise ValueError()
    except (ValueError, InvalidOperation, OverflowError):
        raise AnonymizerError(f"Таблица {table}, колонка {field['name']}: значение не соответствует типу или размеру поля.") from None


class Validator:
    def __init__(self, store, transform):
        self.store, self.transform = store, transform
        self.source_hash, self.output_hash = hashlib.sha256(), hashlib.sha256()
        self.db = store.db
        self.db.execute("CREATE TABLE bkeys(tid INTEGER, phase INTEGER, body TEXT, n INTEGER NOT NULL, PRIMARY KEY(tid,phase,body)) WITHOUT ROWID")
        self.links, self.checked_rows = [], 0
        self.pending_refs, self.pending_keys, self.pending_bytes = [], [], 0
        for (schema, table), data in store.tables.items():
            for f in data["fields"]:
                fk = f.get("fk")
                if fk and f["name"] != "sys_id":
                    target = store.tables.get((fk.get("schema", schema), fk["table"]))
                    if fk["field"] == "sys_id":
                        raise AnonymizerError("Обнаружена ссылка на sys_id. Исключить его без перенумерации ссылки нельзя.")
                    self.links.append((data["id"], f["name"], target["id"] if target else None, fk["field"]))

    def row(self, name, table, old, new):
        fields = table["fields"]
        outfields = [f for f in fields if f["name"] != "sys_id"]
        old_kept = [v for f, v in zip(fields, old) if f["name"] != "sys_id"]
        source_preserved, output_preserved = [], []
        for f, before, after in zip(outfields, old_kept, new):
            validate_value(name[1], f, before)
            validate_value(name[1], f, after)
            if self.transform.rule(name[1], f, name[0]) == "preserve":
                source_preserved.append(before)
                output_preserved.append(after)
                if before != after:
                    raise AnonymizerError("Изменено сохраняемое количество, цена или дата.")
            elif self.transform.rule(name[1], f, name[0]) == "json" and before is not None:
                source_preserved.append(json_data.signature(json_data.load(before)))
                output_preserved.append(json_data.signature(json_data.load(after)))
                if source_preserved[-1] != output_preserved[-1]:
                    raise AnonymizerError("Изменены числа, даты или структура JSON.")
            if before is not None:
                self.pending_refs.append((table["id"], f["name"], canonical.value(f, before), canonical.value(f, after)))
                self.pending_bytes += (len(before) + len(after)) * 4
        self.source_hash.update(json.dumps(source_preserved, ensure_ascii=False).encode())
        self.output_hash.update(json.dumps(output_preserved, ensure_ascii=False).encode())
        bk = [i for i, f in enumerate(outfields) if set(f.get("keys") or []) & {"BK", "PK", "UK"}]
        if bk:
            keys = [(table["id"], 0, json.dumps([canonical.value(outfields[i], old_kept[i]) for i in bk], ensure_ascii=False)),
                    (table["id"], 1, json.dumps([canonical.value(outfields[i], new[i]) for i in bk], ensure_ascii=False))]
            self.pending_keys.extend(keys)
            self.pending_bytes += sum(len(k[2]) * 4 for k in keys)
        self.checked_rows += 1
        if len(self.pending_refs) >= 5000 or self.pending_bytes >= 4 * 1024 * 1024:
            self.flush()

    def flush(self):
        self.db.executemany("INSERT OR IGNORE INTO refvalues VALUES (?,?,?,?)", self.pending_refs)
        self.db.executemany("INSERT INTO bkeys VALUES (?,?,?,1) ON CONFLICT(tid,phase,body) DO UPDATE SET n=n+1", self.pending_keys)
        self.pending_refs.clear()
        self.pending_keys.clear()
        self.pending_bytes = 0
        self.db.commit()

    def finish(self):
        self.flush()
        if self.checked_rows != self.store.count or self.source_hash.digest() != self.output_hash.digest():
            raise AnonymizerError("Не совпало число строк или контрольная сумма сохраняемых полей.")
        ref_report = []
        for tid, col, target, key in self.links:
            if target is None or not any(f["name"] == key for t in self.store.tables.values() if t["id"] == target for f in t["fields"]):
                ref_report.append({"table_id": tid, "column": col, "status": "target_not_in_input"})
                continue
            counts = []
            for phase in ("old", "new"):
                counts.append(self.db.execute(f"SELECT count(*) FROM refvalues a WHERE a.tid=? AND a.col=? AND a.{phase}<>'' AND NOT EXISTS (SELECT 1 FROM refvalues b WHERE b.tid=? AND b.col=? AND b.{phase}=a.{phase})", (tid, col, target, key)).fetchone()[0])
            if counts[1] != counts[0]:
                raise AnonymizerError(f"Изменена ссылочная целостность: таблица {tid}, колонка {col}.")
            ref_report.append({"table_id": tid, "column": col, "missing_before": counts[0], "missing_after": counts[1]})
        duplicates = []
        for tid in (t["id"] for t in self.store.tables.values()):
            before, after = [self.db.execute("SELECT COALESCE(SUM(n-1),0) FROM bkeys WHERE tid=? AND phase=? AND n>1", (tid, phase)).fetchone()[0] for phase in (0, 1)]
            if before != after:
                raise AnonymizerError(f"Изменена уникальность бизнес-ключа таблицы {tid}.")
            duplicates.append({"table_id": tid, "extra_rows_before": before, "extra_rows_after": after})
        return {"checked_rows": self.checked_rows, "preserved_fields_sha256": self.source_hash.hexdigest(),
                "preserved_fields_equal": True, "foreign_keys": ref_report, "business_key_duplicates": duplicates}

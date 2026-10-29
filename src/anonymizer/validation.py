from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext
from datetime import date, datetime, time
from uuid import UUID

from .errors import AnonymizerError
from .metadata import SYSTEM
from . import json_data
from . import canonical


def validate_value(table, field, value):
    if field["name"] in SYSTEM:
        return
    if value is None:
        if not field.get("nullable", True):
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
        self.db.execute("CREATE TABLE keyvals(gid INTEGER, phase INTEGER, body TEXT, n INTEGER NOT NULL, PRIMARY KEY(gid,phase,body)) WITHOUT ROWID")
        # One tuple store for every reference link: single-column FK is a 1-tuple.
        # side 0 = child values, side 1 = master values; self-reference fills both.
        self.db.execute("CREATE TABLE fkvals(link INTEGER, side INTEGER, old TEXT, new TEXT, PRIMARY KEY(link,side,old)) WITHOUT ROWID")
        self.db.execute("CREATE INDEX fk_new ON fkvals(link,side,new)")
        self.links, self.checked_rows = [], 0
        self.pending_fk, self.pending_keys, self.pending_bytes = [], [], 0
        for (schema, table), data in store.tables.items():
            grouped = set()
            for group in data.get("fk_groups", []):
                # Ignoring an audit-bearing composite reference must not turn
                # its remaining components into separate single-column links.
                grouped.update(group["columns"])
                if SYSTEM.intersection(group["columns"]) or SYSTEM.intersection(group["target_columns"]):
                    continue
                target = store.tables.get((group["target_schema"], group["target_table"]))
                self.links.append({"tid": data["id"], "columns": list(group["columns"]),
                                   "target": target["id"] if target else None,
                                   "target_columns": list(group["target_columns"])})
            for f in data["fields"]:
                fk = f.get("fk")
                if fk and f["name"] not in SYSTEM and fk["field"] not in SYSTEM:
                    if f["name"] not in grouped:
                        target = store.tables.get((fk.get("schema", schema), fk["table"]))
                        self.links.append({"tid": data["id"], "columns": [f["name"]],
                                           "target": target["id"] if target else None,
                                           "target_columns": [fk["field"]]})
        self.as_child, self.as_master = self._side_maps()
        self.key_links = self._key_links()
        self.key_by_tid = {}
        for gid, link in enumerate(self.key_links):
            self.key_by_tid.setdefault(link["tid"], []).append(gid)

    def _key_links(self):
        """Every business PK/UNIQUE/model grain as a separate group."""
        links = []
        for data in self.store.tables.values():
            outindex = {f["name"]: i for i, f in enumerate(data["fields"])}
            for group in data.get("key_groups", []):
                if any(col in SYSTEM or col not in outindex for col in group["columns"]):
                    continue
                pairs = [(next(f for f in data["fields"] if f["name"] == col), outindex[col]) for col in group["columns"]]
                links.append({"tid": data["id"], "columns": list(group["columns"]),
                              "kind": group["kind"], "nulls": group["nulls"], "pairs": pairs})
        return links

    def _side_maps(self):
        """tid -> [(business link index, [(field, output index)]...)]."""
        names = {data["id"]: {f["name"] for f in data["fields"]} for data in self.store.tables.values()}
        outindex = {data["id"]: {f["name"]: i for i, f in enumerate(data["fields"])}
                    for (schema, table), data in self.store.tables.items()}
        bytid = {data["id"]: ((schema, table), data) for (schema, table), data in self.store.tables.items()}
        as_child, as_master = {}, {}
        for link_id, link in enumerate(self.links):
            child = bytid[link["tid"]]
            pairs = [(next(f for f in child[1]["fields"] if f["name"] == col), outindex[link["tid"]][col]) for col in link["columns"]]
            as_child.setdefault(link["tid"], []).append((link_id, pairs))
            if link["target"] in names:
                pairs = [(self.store.field(*bytid[link["target"]][0], col), outindex[link["target"]][col])
                         for col in link["target_columns"] if col in outindex.get(link["target"], {})]
                if len(pairs) == len(link["target_columns"]):
                    as_master.setdefault(link["target"], []).append((link_id, pairs))
        return as_child, as_master

    def row(self, name, table, old, new):
        fields = table["fields"]
        outfields = fields
        old_kept = old
        source_preserved, output_preserved = [], []
        for f, before, after in zip(outfields, old_kept, new):
            if f["name"] in SYSTEM:
                continue
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
        self.source_hash.update(json.dumps(source_preserved, ensure_ascii=False).encode())
        self.output_hash.update(json.dumps(output_preserved, ensure_ascii=False).encode())
        for side, sides in ((0, self.as_child), (1, self.as_master)):
            for link_id, pairs in sides.get(table["id"], ()):
                # MATCH SIMPLE: any NULL drops the reference; empty components follow
                # the established per-column convention and are not counted either.
                before_tuple = [canonical.value(f, old_kept[i]) for f, i in pairs]
                after_tuple = [canonical.value(f, new[i]) for f, i in pairs]
                if any(v is None or v == "" for v in before_tuple + after_tuple):
                    continue
                body_old = json.dumps(before_tuple, ensure_ascii=False)
                body_new = json.dumps(after_tuple, ensure_ascii=False)
                self.pending_fk.append((link_id, side, body_old, body_new))
                self.pending_bytes += (len(body_old) + len(body_new)) * 4
        for gid in self.key_by_tid.get(table["id"], ()):
            link = self.key_links[gid]
            before_tuple = [canonical.value(f, old_kept[i]) for f, i in link["pairs"]]
            after_tuple = [canonical.value(f, new[i]) for f, i in link["pairs"]]
            # PostgreSQL default NULLS DISTINCT: any NULL component never
            # duplicates. An empty string is a value and still duplicates.
            if link["nulls"] == "distinct" and any(v is None for v in before_tuple + after_tuple):
                continue
            body_old = json.dumps(before_tuple, ensure_ascii=False)
            body_new = json.dumps(after_tuple, ensure_ascii=False)
            self.pending_keys.extend([(gid, 0, body_old), (gid, 1, body_new)])
            self.pending_bytes += (len(body_old) + len(body_new)) * 4
        self.checked_rows += 1
        if len(self.pending_fk) + len(self.pending_keys) >= 5000 or self.pending_bytes >= 4 * 1024 * 1024:
            self.flush()

    def flush(self):
        self.db.executemany("INSERT OR IGNORE INTO fkvals VALUES (?,?,?,?)", self.pending_fk)
        self.db.executemany("INSERT OR IGNORE INTO keyvals VALUES (?,?,?,1) ON CONFLICT(gid,phase,body) DO UPDATE SET n=n+1", self.pending_keys)
        self.pending_fk.clear()
        self.pending_keys.clear()
        self.pending_bytes = 0
        self.db.commit()

    def finish(self):
        self.flush()
        if self.checked_rows != self.store.count or self.source_hash.digest() != self.output_hash.digest():
            raise AnonymizerError("Не совпало число строк или контрольная сумма сохраняемых полей.")
        target_names = {data["id"]: {f["name"] for f in data["fields"]} for data in self.store.tables.values()}
        ref_report = []
        for link_id, link in enumerate(self.links):
            label = {"table_id": link["tid"]}
            label.update({"column": link["columns"][0]} if len(link["columns"]) == 1 else {"columns": link["columns"]})
            if link["target"] is None or not set(link["target_columns"]) <= target_names.get(link["target"], set()):
                label["status"] = "target_not_in_input"
                ref_report.append(label)
                continue
            counts = []
            for phase in ("old", "new"):
                counts.append(self.db.execute(f"SELECT count(*) FROM fkvals a WHERE a.link=? AND a.side=0 AND NOT EXISTS (SELECT 1 FROM fkvals b WHERE b.link=? AND b.side=1 AND b.{phase}=a.{phase})", (link_id, link_id)).fetchone()[0])
            if counts[1] != counts[0]:
                raise AnonymizerError(f"Изменена ссылочная целостность: таблица {link['tid']}, колонки {', '.join(link['columns'])}.")
            label.update(missing_before=counts[0], missing_after=counts[1],
                         unit="distinct values" if len(link["columns"]) == 1 else "distinct tuples")
            ref_report.append(label)
        duplicates = []
        for gid, link in enumerate(self.key_links):
            before, after = [self.db.execute("SELECT COALESCE(SUM(n-1),0) FROM keyvals WHERE gid=? AND phase=? AND n>1", (gid, phase)).fetchone()[0] for phase in (0, 1)]
            if before != after:
                raise AnonymizerError(f"Изменена уникальность ключа таблицы {link['tid']}: {', '.join(link['columns'])}.")
            duplicates.append({"table_id": link["tid"], "columns": link["columns"], "kind": link["kind"],
                               "nulls": link["nulls"], "extra_rows_before": before, "extra_rows_after": after,
                               "unit": "rows"})
        return {"checked_rows": self.checked_rows, "preserved_fields_sha256": self.source_hash.hexdigest(),
                "preserved_fields_equal": True, "foreign_keys": ref_report, "business_key_duplicates": duplicates}

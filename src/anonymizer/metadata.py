from __future__ import annotations

import json
import re
from importlib.resources import files

from .errors import AnonymizerError

SYSTEM = {"sys_id", "change_author", "update_date_time"}
ALIASES = {"item": ("item", "item"), "comp": ("item", "item"),
           "loc": ("loc", "loc"), "locfr": ("loc", "loc"), "locto": ("loc", "loc"),
           "manufloc": ("loc", "loc"), "vendorloc": ("loc", "loc"), "storloc": ("loc", "loc"),
           "res": ("res", "res"), "prodsource": ("prodsource_sp", "prodsource"),
           "dmdstream": ("demand_stream", "dmdstream")}


def system_field(field):
    """Output audit metadata; input values never define business constraints."""
    name = field["name"]
    if name not in SYSTEM:
        if field.get("fk") and field["fk"]["field"] in SYSTEM:
            result = dict(field)
            result["fk"] = None
            return result
        return field
    result = dict(field)
    result.update(nullable=True, keys=[], fk=None)
    result.pop("source_pk", None)
    if name in {"sys_id", "change_author"}:
        result.update(type="INTEGER" if name == "sys_id" else "TEXT",
                      sql_type="bigint" if name == "sys_id" else "text", length=None)
        result.pop("size", None)
    return result


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sql_type(field):
    kind = field["type"]
    size = field.get("size") or ""
    if kind == "TEXT":
        return f"character varying({int(size)})" if size.isdigit() and int(size) else "text"
    if kind == "NUMBER":
        # The YAML is a logical model. Do not invent a physical scale or round
        # client quantities when its size does not specify fractional digits.
        return f"numeric({size})" if re.fullmatch(r"\d+,\d+", size) else "numeric"
    return {"INTEGER": "bigint", "BOOLEAN": "boolean", "DATE": "date", "DATE_TIME": "timestamp without time zone",
            "TIME": "time without time zone", "JSONB": "jsonb", "UUID": "uuid"}.get(kind, "text")


def physical_type(text):
    """Accept a fixed built-in type grammar; never concatenate arbitrary SQL."""
    s = re.sub(r"\s+", " ", text.lower()).strip().replace("pg_catalog . ", "")
    s = re.sub(r"\s*([(),])\s*", r"\1", s)
    s = {"serial": "integer", "bigserial": "bigint", "smallserial": "smallint"}.get(s, s)
    allowed = r"(?:character varying|varchar|character|char|numeric|decimal)(?:\(\d+(?:,\d+)?\))?|text|smallint|integer|bigint|int[248]?|real|double precision|float[48]?|boolean|bool|date|(?:timestamp|time)(?:\(\d+\))?(?: with(?:out)? time zone)?|uuid|jsonb?"
    if not re.fullmatch(allowed, s):
        raise AnonymizerError("Неподдерживаемый физический тип. Поддерживаются скалярные встроенные типы PostgreSQL.")
    if s.startswith(("character", "varchar", "char", "text")):
        kind = "TEXT"
    elif s.startswith(("numeric", "decimal", "int", "smallint", "bigint", "real", "double", "float")):
        kind = "NUMBER"
    elif s in {"bool", "boolean"}:
        kind = "BOOLEAN"
    elif s == "date":
        kind = "DATE"
    elif s.startswith("timestamp"):
        kind = "DATE_TIME"
    elif s.startswith("time"):
        kind = "TIME"
    elif s in {"json", "jsonb"}:
        kind = "JSONB"
    else:
        kind = "UUID"
    m = re.search(r"\((\d+)\)", s)
    length = int(m[1]) if kind == "TEXT" and m else 1 if s in {"char", "character"} else None
    return s, kind, length


class Model:
    def __init__(self, path=None):
        content = path.read_text(encoding="utf-8") if path else files("anonymizer").joinpath("resources/model.json").read_text(encoding="utf-8")
        self.data = json.loads(content)
        self.tables = self.data["tables"]

    def field(self, table, column):
        return next((f for f in self.tables.get(table, {}).get("fields", []) if f["name"] == column), None)

    def fields(self, table):
        if table not in self.tables:
            raise AnonymizerError(f"Таблица {table}: отсутствует в модели SNP. Для CSV требуется обновить снимок модели.")
        return self.tables[table]["fields"]

    def root(self, table, column):
        seen = set()
        while (table, column) not in seen:
            seen.add((table, column))
            field = self.field(table, column)
            if column in SYSTEM:
                return table, column
            if field and field.get("fk") and field["fk"]["field"] not in SYSTEM:
                target = field["fk"]["table"], field["fk"]["field"]
                if target == (table, column):
                    return target
                table, column = target
            else:
                if column in ALIASES:
                    return ALIASES[column]
                return table, column
        raise AnonymizerError(f"Цикл ссылок в модели: {table}.{column}.")

    def enrich(self, table, field):
        known = self.field(table, field["name"])
        result = dict(known or {"keys": [], "fk": None, "nullable": True})
        result.update(field)
        result["keys"] = sorted(set((known or {}).get("keys") or []) | set(field.get("keys") or []))
        if "sql_type" in field:
            safe, kind, length = physical_type(field["sql_type"])
            result.update(sql_type=safe, type=kind, length=length)
        else:
            result.update(sql_type=sql_type(result), length=int(result["size"]) if result.get("type") == "TEXT" and str(result.get("size", "")).isdigit() else None)
        return system_field(result)

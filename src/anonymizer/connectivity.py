"""Portable connectivity profiles and a data-free, compiled binding contract."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from importlib.resources import files
from pathlib import Path
from uuid import UUID

from .errors import AnonymizerError
from . import canonical
from .metadata import SYSTEM

PROFILE_FORMAT = "snp-anonymizer-connectivity-v1"
COMPILER_VERSION = "1"
MAX_PROFILE_BYTES = 1024 * 1024
MAX_CONTRACT_BYTES = 16 * 1024 * 1024


class ContractError(AnonymizerError):
    def __init__(self, message="Некорректный или противоречивый профиль связности.", code="CONTRACT_CONFLICT"):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class Contract:
    profile: dict | None = None
    profile_id: str | None = None
    version: str | None = None
    profile_hash: str | None = None
    fingerprint: str | None = None
    assignments: dict = field(default_factory=dict)
    domains: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    required_checks: list = field(default_factory=list)

    def public_profile(self):
        return self.profile


def domain_value(domain, value):
    """Canonical ID; NULL is deliberately handled by the caller."""
    try:
        if domain["kind"] == "NUMBER":
            if not Decimal(value).is_finite():
                raise ValueError()
            return canonical.numeric(value)
        if domain["kind"] == "UUID":
            return str(UUID(value))
        if domain.get("equality") == "pg_char":
            return value.rstrip(" ")
        return value
    except (ValueError, ArithmeticError, TypeError):
        raise ContractError("Неподдерживаемое значение идентификатора.") from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9$]{0,62}\Z")
_CHECK_ID = re.compile(r"[A-Za-z_][A-Za-z_0-9.:\-]{0,127}\Z")
_FLOAT_TYPES = {"real", "float", "float4", "double precision", "float8"}


def _fail(message="Некорректная структура JSON-профиля."):
    raise ContractError(message)


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail()
    return dict(value)


def _identifier(value, *, check_id=False):
    if not isinstance(value, str) or not (_CHECK_ID if check_id else _IDENTIFIER).fullmatch(value):
        _fail("Некорректное техническое имя в профиле.")
    return value


def _list(value):
    if not isinstance(value, list) or len(value) > 16384:
        _fail()
    return value


def _columns(value):
    columns = [_identifier(v) for v in _list(value)]
    if not columns or len(set(columns)) != len(columns):
        _fail("Пустой или повторяющийся список колонок профиля.")
    return columns


def _side(value):
    result = _object(value, {"members", "projection"}, {"members"})
    members = []
    for item in _list(result["members"]):
        item = _object(item, {"table", "columns"}, {"table", "columns"})
        members.append({"table": _identifier(item["table"]), "columns": _columns(item["columns"])})
    if not members or len({len(m["columns"]) for m in members}) != 1:
        _fail("Несогласованная ширина проекции профиля.")
    if len({_json(m) for m in members}) != len(members):
        _fail("Проекция повторяется в профиле.")
    projection = result.get("projection", "bag")
    if projection not in ("bag", "set"):
        _fail("Неизвестная семантика проекции профиля.")
    return {"members": sorted(members, key=_json), "projection": projection}


def _normalize_profile(value):
    p = _object(value, {"format", "profile_id", "version", "namespace_rule", "domains", "relations", "keys",
                        "required_checks", "missing_target_policy"}, {"format", "profile_id", "version", "domains"})
    if p["format"] != PROFILE_FORMAT:
        _fail("Неподдерживаемая версия профиля связности.")
    _identifier(p["profile_id"], check_id=True)
    if not isinstance(p["version"], str) or not _CHECK_ID.fullmatch("v" + p["version"]):
        _fail("Некорректная версия профиля связности.")
    namespace = p.get("namespace_rule", "source_schema")
    if namespace not in ("source_schema", "shared"):
        _fail("Неподдерживаемое правило namespace.")
    missing = p.get("missing_target_policy", "not_checked")
    if missing not in ("block", "not_checked"):
        _fail("Неизвестная политика отсутствующей цели.")
    domains, relations, keys = [], [], []
    all_ids = []
    for value in _list(p["domains"]):
        d = _object(value, {"id", "domain_id", "kind", "equality", "prefix", "members"}, {"members"})
        name = d.get("id", d.get("domain_id"))
        _identifier(name, check_id=True)
        if "id" in d and "domain_id" in d and d["id"] != d["domain_id"]:
            _fail()
        kind = d.get("kind", "auto")
        kinds = {"auto": "auto", "text": "TEXT", "TEXT": "TEXT", "numeric": "NUMBER", "number": "NUMBER",
                 "NUMBER": "NUMBER", "INTEGER": "NUMBER", "uuid": "UUID", "UUID": "UUID"}
        if not isinstance(kind, str) or kind not in kinds:
            _fail("Неподдерживаемый тип домена.")
        equality = d.get("equality", "auto")
        if equality not in ("auto", "exact", "numeric", "uuid", "pg_char"):
            _fail("Неподдерживаемое правило равенства домена.")
        prefix = d.get("prefix", name.upper().replace(":", "_").replace(".", "_").replace("-", "_"))
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z][A-Za-z_0-9]{0,31}", prefix):
            _fail("Префикс замены должен быть техническим ASCII-идентификатором.")
        members = []
        for item in _list(d["members"]):
            item = _object(item, {"table", "column"}, {"table", "column"})
            members.append({"table": _identifier(item["table"]), "column": _identifier(item["column"])})
        if not members or len({_json(m) for m in members}) != len(members):
            _fail("Пустой или повторяющийся домен.")
        domains.append({"id": name, "kind": kinds[kind], "equality": equality, "prefix": prefix,
                        "members": sorted(members, key=_json)})
        all_ids.append("domain:" + name)
    for value in _list(p.get("relations", [])):
        r = _object(value, {"id", "relation_id", "kind", "source", "target", "nulls", "predicate"}, {"source", "target"})
        name = _identifier(r.get("id", r.get("relation_id")), check_id=True)
        if "id" in r and "relation_id" in r and r["id"] != r["relation_id"]:
            _fail()
        kind = r.get("kind", "join")
        if kind not in ("join", "fk"):
            _fail("Неизвестный вид связи.")
        nulls = r.get("nulls", "match_simple" if kind == "fk" else "sql")
        if nulls not in ("sql", "null_safe", "match_simple"):
            _fail("Неизвестная NULL-семантика связи.")
        predicate = r.get("predicate", "equality")
        _identifier(predicate, check_id=True)
        source, target = _side(r["source"]), _side(r["target"])
        if len(source["members"][0]["columns"]) != len(target["members"][0]["columns"]):
            _fail("Несогласованная ширина связи.")
        relations.append({"id": name, "kind": kind, "source": source, "target": target,
                          "nulls": nulls, "predicate": predicate})
        all_ids.append(name)
    for value in _list(p.get("keys", [])):
        k = _object(value, {"id", "key_id", "table", "columns", "kind", "nulls"}, {"table", "columns"})
        name = _identifier(k.get("id", k.get("key_id")), check_id=True)
        if "id" in k and "key_id" in k and k["id"] != k["key_id"]:
            _fail()
        kind, nulls = k.get("kind", "bk"), k.get("nulls", "distinct")
        if kind not in ("pk", "unique", "bk") or nulls not in ("distinct", "not_distinct"):
            _fail("Неизвестная семантика ключа.")
        keys.append({"id": name, "table": _identifier(k["table"]), "columns": _columns(k["columns"]),
                     "kind": kind, "nulls": nulls})
        all_ids.append(name)
    if len(set(all_ids)) != len(all_ids):
        _fail("Идентификаторы проверок профиля повторяются.")
    required = [_identifier(v, check_id=True) for v in _list(p.get("required_checks", []))]
    domain_ids = {d["id"] for d in domains}
    required = ["domain:" + v if v in domain_ids else v for v in required]
    if len(set(required)) != len(required) or set(required) - set(all_ids):
        _fail("Неизвестная или повторяющаяся обязательная проверка.")
    return {"format": PROFILE_FORMAT, "profile_id": p["profile_id"], "version": p["version"],
            "namespace_rule": namespace, "domains": sorted(domains, key=lambda d: d["id"]),
            "relations": sorted(relations, key=lambda r: r["id"]), "keys": sorted(keys, key=lambda k: k["id"]),
            "required_checks": sorted(required), "missing_target_policy": missing}


def load_profile(value=None):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, dict):
            raw = _json(value).encode("utf-8")
        else:
            if str(value) == "builtin:optimizer-v1":
                resource = files("anonymizer").joinpath("resources/connectivity/optimizer-v1.json")
                with resource.open("rb") as stream:
                    raw = stream.read(MAX_PROFILE_BYTES + 1)
            else:
                with Path(value).open("rb") as stream:
                    raw = stream.read(MAX_PROFILE_BYTES + 1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ContractError("Профиль связности превышает 1 МиБ.")
        return _normalize_profile(json.loads(raw.decode("utf-8-sig")))
    except ContractError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise ContractError("Не удалось прочитать JSON-профиль связности.") from None


def _physical(store, name):
    table = store.tables.get(tuple(name))
    if table is None:
        return set()
    return set(table.get("physical_columns", (f["name"] for f in table["fields"])))


def _field(store, node):
    schema, table, column = node
    data = store.tables.get((schema, table))
    if data is None or column not in _physical(store, (schema, table)):
        return None
    return next((f for f in data["fields"] if f["name"] == column), None)


def _state(store, sides):
    for side in sides:
        for member in side["members"]:
            if SYSTEM.intersection(member["columns"]):
                return "NOT_CHECKED", "SYSTEM_FIELD_POLICY"
            if not set(member["columns"]).issubset(_physical(store, member["table"])):
                return "NOT_CHECKED", "RELATION_TARGET_MISSING"
    # PostgreSQL equality is typed; comparing SQLite strings must never make
    # a NUMBER and a TEXT identifier appear comparable by coincidence.
    signatures = {}
    for side in sides:
        for member in side["members"]:
            table = store.tables[tuple(member["table"])]
            by_name = {f["name"]: f for f in table["fields"]}
            for index, column in enumerate(member["columns"]):
                f = by_name[column]
                if f.get("collation_declared"):
                    return "UNSUPPORTED", "COLLATION_COMPARISON_UNSUPPORTED"
                if f["type"] in {"JSONB", "TIME", "DATE_TIME"}:
                    return "UNSUPPORTED", "COMPARISON_SEMANTICS_UNSUPPORTED"
                if by_name[column]["sql_type"] in _FLOAT_TYPES:
                    return "UNSUPPORTED", "FLOAT_COMPARISON_UNSUPPORTED"
                if "nonnull_columns" in table and column not in table["nonnull_columns"]:
                    continue
                f = by_name[column]
                kind = "NUMBER" if f["type"] == "INTEGER" else f["type"]
                if kind == "TEXT" and re.match(r"(?:char|character)(?:\(|$)", f["sql_type"]):
                    kind = "PG_CHAR"
                signatures.setdefault(index, set()).add(kind)
    if any(len(kinds) > 1 for kinds in signatures.values()):
        return "UNSUPPORTED", "INCOMPATIBLE_COMPARISON_TYPES"
    return "PENDING", None


def _domain_type(fields, spec):
    # Untyped all-NULL inference columns cannot establish an ID's type.
    if any(f.get("collation_declared") for f in fields):
        raise ContractError("COLLATE требует отдельной семантики сравнения.", "CHECK_UNSUPPORTED")
    if any(f["sql_type"] in _FLOAT_TYPES for f in fields):
        raise ContractError("IEEE FLOAT-идентификаторы требуют отдельной семантики сравнения.", "CHECK_UNSUPPORTED")
    observed = [f for f in fields if not f.get("_null_only")]
    kinds = {"NUMBER" if f["type"] == "INTEGER" else f["type"] for f in observed}
    declared = spec["kind"]
    if declared != "auto":
        if kinds and kinds != {declared}:
            raise ContractError("Типы полей не соответствуют типу общего домена.")
        kind = declared
    elif not kinds:
        kind = "TEXT"
    elif len(kinds) == 1:
        kind = next(iter(kinds))
    else:
        raise ContractError("Смешанные типы идентификаторов общего домена.")
    if kind not in {"TEXT", "NUMBER", "UUID"}:
        raise ContractError("Неподдерживаемый тип идентификатора общего домена.")
    eq = spec["equality"]
    char_fields = [f for f in observed if re.match(r"(?:char|character)(?:\(|$)", f["sql_type"])]
    if eq == "auto":
        eq = {"NUMBER": "numeric", "UUID": "uuid", "TEXT": "pg_char" if char_fields else "exact"}[kind]
    allowed = {"TEXT": {"exact", "pg_char"}, "NUMBER": {"numeric"}, "UUID": {"uuid"}}
    if eq not in allowed[kind] or (char_fields and len(char_fields) != len(observed)):
        raise ContractError("Несогласованная семантика сравнения общего домена.")
    if char_fields and eq != "pg_char":
        raise ContractError("CHAR требует явной семантики сравнения без завершающих пробелов.")
    if eq == "pg_char" and observed and not char_fields:
        raise ContractError("pg_char применим только к CHAR-полям.")
    return kind, eq


def _encoding(fields, kind):
    length, quantum, capacity = None, "1", None
    positive_capacity, negative_capacity = None, None
    if kind == "TEXT":
        lengths = [f["length"] for f in fields if f.get("length") is not None]
        length = min(lengths) if lengths else None
        if length is not None and length < 1:
            raise ContractError("Недостаточная длина общего домена.", "DOMAIN_CAPACITY_EXHAUSTED")
        if length is not None and length <= 12:
            capacity = 36 ** length
    elif kind == "NUMBER":
        scales = []
        bounds = []
        for f in fields:
            if f.get("_null_only"):
                continue
            physical = f["sql_type"]
            integers = {"smallint": 2**15 - 1, "int2": 2**15 - 1, "integer": 2**31 - 1,
                        "int": 2**31 - 1, "int4": 2**31 - 1, "bigint": 2**63 - 1, "int8": 2**63 - 1}
            if physical in integers or f["type"] == "INTEGER":
                scales.append(0)
                maximum = integers.get(physical, 2**63 - 1)
                bounds.append((maximum, maximum + 1, 0))
            else:
                match = re.fullmatch(r"(?:numeric|decimal)\((\d+)(?:,(\d+))?\)", physical)
                if match:
                    precision, scale = int(match[1]), int(match[2] or 0)
                    if not 1 <= precision <= 1000 or not 0 <= scale <= 1000:
                        raise ContractError("Неподдерживаемые precision/scale идентификатора.")
                    scales.append(scale)
                    bounds.append((10**precision - 1, 10**precision - 1, scale))
        scale = min(scales) if scales else 0
        quantum = "1" if scale == 0 else "0." + "0" * (scale - 1) + "1"
        for positive, negative, physical_scale in bounds:
            divisor = 10 ** (physical_scale - scale)
            positive //= divisor
            negative //= divisor
            positive_capacity = positive if positive_capacity is None else min(positive_capacity, positive)
            negative_capacity = negative if negative_capacity is None else min(negative_capacity, negative)
        if positive_capacity is not None:
            capacity = positive_capacity + negative_capacity + 1
    return length, quantum, capacity, positive_capacity, negative_capacity


def _input_declarations(store):
    """Retain grouping; field UK flags are never invented as independent keys."""
    result = []
    declarations = getattr(store, "declarations", {})
    for name in sorted(set(store.tables) | set(declarations)):
        data = store.tables.get(name, {"fields": [], "fk_groups": [], "key_groups": []})
        raw = declarations.get(name, {})
        fields = raw.get("fields", data["fields"])
        fks = list(raw.get("fk_groups", data.get("fk_groups", [])))
        for f in [*fields, *([] if store.bypass_model else data["fields"])]:
            fk = f.get("fk")
            if fk:
                group = {"columns": [f["name"]], "target_schema": fk.get("schema", name[0]),
                         "target_table": fk["table"], "target_columns": [fk["field"]], "match": "simple"}
                # Group hints on components must not weaken composite FK.
                if any(f["name"] in g["columns"] for g in fks):
                    continue
                if group not in fks:
                    fks.append(group)
        keys = list(raw.get("key_groups", data.get("key_groups", [])))
        for g in data.get("key_groups", []):
            if g not in keys:
                keys.append(g)
        result.append((name, fields, fks, keys))
    return result


def compile_contract(store, profile_spec=None, check=lambda: None):
    profile = load_profile(profile_spec)
    contract = Contract(profile=profile)
    if profile is not None:
        contract.profile_id, contract.version = profile["profile_id"], profile["version"]
        contract.profile_hash = _hash(profile)
    schemas = sorted({name[0] for name in store.tables})
    declared_schemas = sorted(set(schemas) | {name[0] for name in getattr(store, "declarations", {})})
    ns_labels = {schema: "ns" + str(i + 1) for i, schema in enumerate(declared_schemas)}
    declarations = _input_declarations(store)
    # Reject namespace/profile fan-out before allocating an expanded catalog.
    # The multiplier includes dict/list/string overhead and compiler graphs,
    # not just the compact JSON wire size. The final actual catalog is checked
    # below as well. Neither budget depends on the number of input rows.
    base_cost = getattr(store, "_input_metadata_used", 0) + getattr(store, "_declaration_bytes", 0)
    profile_cost = len(_json(profile).encode("utf-8")) * 16 * max(1, len(schemas)) if profile is not None else 0
    declared_cost = sum(1024 + len(_json([fks, keys]).encode("utf-8")) * 8 for _, _, fks, keys in declarations)
    if base_cost + profile_cost + declared_cost > MAX_CONTRACT_BYTES:
        raise ContractError("Превышен лимит метаданных связности.", "CHECK_INCOMPLETE")
    # Explicit FK declarations form components only for domain unification;
    # they do not infer a domain from a column name or ALIASES fallback.
    parents = {}

    def find(node):
        parents.setdefault(node, node)
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parents[b] = a

    for name, _, fks, _ in declarations:
        for group in fks:
            if SYSTEM.intersection(group["columns"]) or SYSTEM.intersection(group["target_columns"]):
                continue
            for col, target in zip(group["columns"], group["target_columns"], strict=True):
                if _field(store, (*name, col)) is None:
                    continue
                union((*name, col), (group.get("target_schema", name[0]), group["target_table"], target))
    if profile is not None:
        scopes = [(schema, [schema], ns_labels[schema]) for schema in schemas]
        if profile["namespace_rule"] == "shared":
            scopes = [("shared", schemas, "ns1")]
        # Explicit equality relations can bind an ID role not repeated in the
        # domain's member list. Compile all positions before allocating codes.
        for _, scope_schemas, _ in scopes:
            for relation in profile["relations"]:
                if relation["predicate"] != "equality":
                    continue
                available = {side: [((s, m["table"]), m["columns"]) for s in scope_schemas
                                   for m in relation[side]["members"]
                                   if set(m["columns"]).issubset(_physical(store, (s, m["table"])))]
                             for side in ("source", "target")}
                if not available["source"] or not available["target"]:
                    continue
                for index in range(len(relation["source"]["members"][0]["columns"])):
                    nodes = [(*table, columns[index]) for side in ("source", "target")
                             for table, columns in available[side]]
                    for node in nodes[1:]:
                        union(nodes[0], node)
        component_domains = {}
        for namespace, scope_schemas, label in scopes:
            for spec in profile["domains"]:
                check()
                nodes = [(s, m["table"], m["column"]) for s in scope_schemas for m in spec["members"]]
                for node in nodes:
                    component = find(node)
                    owner = component_domains.setdefault(component, (spec["id"], namespace))
                    if owner != (spec["id"], namespace):
                        raise ContractError("Декларации назначают одному полю разные домены или namespace.")
                components = {find(n) for n in nodes}
                for node in list(parents):
                    if find(node) in components and node not in nodes and _field(store, node):
                        nodes.append(node)
                if any(n[2] in SYSTEM for n in nodes):
                    raise ContractError("Системное поле нельзя включать в домен идентификаторов.")
                protected = {"datasetid", "period", "quantity", "qty", "cost", "price", "capacity", "duration", "volume", "weight"}
                if any(n[2] in protected for n in nodes):
                    raise ContractError("Сохраняемое поле нельзя включать в домен идентификаторов.")
                bound = []
                missing = False
                for node in nodes:
                    field = _field(store, node)
                    if field is None:
                        missing = True
                    else:
                        field = dict(field)
                        data = store.tables[node[:2]]
                        field["_null_only"] = (field["type"] == "TEXT" and
                                               field["name"] in data.get("inferred_null_only_columns", ()))
                        bound.append((node, field))
                kind, equality = _domain_type([f for _, f in bound], spec)
                length, quantum, capacity, positive_capacity, negative_capacity = _encoding([f for _, f in bound], kind)
                ident = "domain:" + spec["id"] + ":" + label
                required = "domain:" + spec["id"] in profile["required_checks"]
                domain = {"id": ident, "domain_id": spec["id"], "namespace": namespace,
                          "members": [{"table": node[:2], "column": node[2]} for node, _ in bound],
                          "kind": kind, "equality": equality, "prefix": spec["prefix"], "length": length,
                          "quantum": quantum, "capacity": capacity,
                          "positive_capacity": positive_capacity, "negative_capacity": negative_capacity,
                          "mapping_domain": "connectivity:" + contract.profile_hash + ":" + _hash([namespace, spec["id"]]),
                          "status": "NOT_CHECKED" if missing else "PENDING", "reason": "DOMAIN_MEMBER_MISSING" if missing else None,
                          "required": required, "apply_mapping": True,
                          "physical_fields": [{k: f.get(k) for k in ("type", "sql_type", "length")} for _, f in bound]}
                for node, _ in bound:
                    old = contract.assignments.get(node)
                    if old is not None and old["mapping_domain"] != domain["mapping_domain"]:
                        raise ContractError("Одно поле назначено разным доменам.")
                    contract.assignments[node] = domain
                contract.domains.append(domain)
                if required:
                    contract.required_checks.append(ident)
            for spec in profile["relations"]:
                check()
                relation = {k: spec[k] for k in ("kind", "nulls")}
                for side in ("source", "target"):
                    relation[side] = {"members": [{"table": (s, m["table"]), "columns": list(m["columns"])}
                                                          for s in scope_schemas for m in spec[side]["members"]],
                                      "projection": spec[side]["projection"]}
                relation.update(id=spec["id"] + ":" + label, required=spec["id"] in profile["required_checks"])
                relation["status"], relation["reason"] = _state(store, [relation["source"], relation["target"]])
                if spec["predicate"] != "equality":
                    relation.update(status="UNSUPPORTED", reason="CHECK_UNSUPPORTED")
                contract.relations.append(relation)
                if relation["required"]:
                    contract.required_checks.append(relation["id"])
        for schema in schemas:
            for spec in profile["keys"]:
                key = {k: spec[k] for k in ("columns", "kind", "nulls")}
                key.update(id=spec["id"] + ":" + ns_labels[schema], table=(schema, spec["table"]),
                           required=spec["id"] in profile["required_checks"])
                side = {"members": [{"table": key["table"], "columns": key["columns"]}]}
                key["status"], key["reason"] = _state(store, [side])
                contract.keys.append(key)
                if key["required"]:
                    contract.required_checks.append(key["id"])
    # Physical DDL constraints and model declarations remain separate checks.
    for name, _, fks, keys in declarations:
        label = ns_labels[name[0]]
        active = not store.bypass_model or profile is not None
        for i, group in enumerate(fks):
            source = {"members": [{"table": name, "columns": list(group["columns"])}], "projection": "bag"}
            target = {"members": [{"table": (group.get("target_schema", name[0]), group["target_table"]),
                                   "columns": list(group["target_columns"])}], "projection": "bag"}
            status, reason = _state(store, [source, target])
            if group.get("match", "simple") != "simple":
                status, reason = "UNSUPPORTED", "CHECK_UNSUPPORTED"
            if not active:
                status, reason = "NOT_CHECKED", "LEGACY_BYPASS_WITHOUT_PROFILE"
            relation = {"id": f"decl-fk:{name[1]}:{i}:{label}", "kind": "fk", "source": source, "target": target,
                        "nulls": "match_simple", "status": status, "reason": reason,
                        "required": profile is None and active and name in store.tables and reason != "SYSTEM_FIELD_POLICY"}
            contract.relations.append(relation)
            if relation["required"]:
                contract.required_checks.append(relation["id"])
        for i, group in enumerate(keys):
            cols = list(group["columns"])
            status, reason = _state(store, [{"members": [{"table": name, "columns": cols}]}])
            if not active:
                status, reason = "NOT_CHECKED", "LEGACY_BYPASS_WITHOUT_PROFILE"
            key = {"id": f"decl-key:{name[1]}:{i}:{label}", "table": name, "columns": cols,
                   "kind": "bk" if group["kind"] == "model_bk" else group["kind"],
                   "nulls": "not_distinct" if group.get("nulls") in {"not distinct", "not_distinct"} else "distinct",
                   "status": status, "reason": reason,
                   "required": profile is None and active and name in store.tables and reason != "SYSTEM_FIELD_POLICY"}
            contract.keys.append(key)
            if key["required"]:
                contract.required_checks.append(key["id"])
    if profile is not None:
        contract.fingerprint = _hash({"compiler": COMPILER_VERSION, "profile": contract.profile_hash,
                                      "bindings": [(list(n), {k: d[k] for k in ("domain_id", "kind", "equality", "namespace", "length", "quantum", "capacity", "positive_capacity", "negative_capacity")})
                                                   for n, d in sorted(contract.assignments.items())],
                                      "relations": contract.relations, "keys": contract.keys})
        if profile["missing_target_policy"] == "block":
            for item in [*contract.domains, *contract.relations, *contract.keys]:
                if item["status"] == "NOT_CHECKED" and item.get("reason") in {"DOMAIN_MEMBER_MISSING", "RELATION_TARGET_MISSING"}:
                    item["required"] = True
                    if item["id"] not in contract.required_checks:
                        contract.required_checks.append(item["id"])
    cost = len(_json({"domains": contract.domains, "relations": contract.relations, "keys": contract.keys}).encode("utf-8")) * 4
    cost += sum(1024 + sum(len(str(v)) * 4 for v in n) for n in contract.assignments)
    if cost + getattr(store, "_input_metadata_used", 0) + getattr(store, "_declaration_bytes", 0) > MAX_CONTRACT_BYTES:
        raise ContractError("Превышен лимит метаданных связности.", "CHECK_INCOMPLETE")
    return contract

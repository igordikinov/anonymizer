from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections import OrderedDict
from decimal import Decimal, localcontext

from .errors import AnonymizerError, Cancelled
from . import json_data
from . import canonical

INDUSTRIES = {
    "food": {"title": "Пищевая промышленность", "products": ["Milk chocolate", "Oat cookies", "Cream candy", "Cocoa powder", "Sugar", "Cocoa butter", "Carton packaging", "Vanilla syrup"]},
    "brewery": {"title": "Напитки", "products": ["Amber drink", "Light drink", "Classic kvass", "Sparkling water", "Light malt", "Aroma hops", "Glass bottle", "Aluminum can"]},
    "chem": {"title": "Химическая промышленность", "products": ["Interior paint", "Universal primer", "Base resin", "Solvent", "White pigment", "Protective lacquer", "Plastic bucket", "Polymer coating"]},
    "consumer": {"title": "Потребительские товары", "products": ["Laundry detergent", "Liquid soap", "Mild shampoo", "Cleaning gel", "Detergent base", "Fragrance", "Plastic bottle", "Transport carton"]},
    "industrial": {"title": "Машиностроение", "products": ["Centrifugal pump", "Control valve", "Cast housing", "Drive shaft", "Bearing", "Steel sheet", "Fastener kit", "Electric motor"]},
}
PREFIX = {("item", "item"): "ITEM", ("loc", "loc"): "LOC", ("res", "res"): "RES", ("prodsource_sp", "prodsource"): "PS", ("demand_stream", "dmdstream"): "DS"}
UNITS = {"KG", "G", "T", "TON", "L", "ML", "HL", "M", "M2", "M3", "CM", "MM", "PC", "PCS", "EA", "ST", "H", "HR", "MIN", "S", "PAL", "BOX", "ШТ", "КГ", "Л", "М"}
FLAGS = {"", "X", "0", "1", "Y", "N", "true", "false", "TRUE", "FALSE", "x"}
MAX_GENERIC_RETRIES = 1024
DESCRIPTIONS = {"proddescr": ("item", "item"), "locdescr": ("loc", "loc"), "resdescr": ("res", "res"),
                "prodsourcedescr": ("prodsource_sp", "prodsource"), "dmdstreamdescr": ("demand_stream", "dmdstream")}


def _check_latin(value):
    if not isinstance(value, str) or not value.isascii():
        raise AnonymizerError("Замена содержит нелатинские символы. Создайте новый проект .snpanon; прежняя карта замен не изменена.")


class Project:
    def __init__(self, path, industry, contract=None, check=lambda: None):
        from .connectivity import ContractError
        if industry not in INDUSTRIES:
            raise AnonymizerError("Неизвестная отрасль.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.check, self._progress_error = check, None
        if contract is not None and contract.profile is not None:
            self.db.set_progress_handler(self._progress, 1000)
        try:
            self._initialize_project(industry, contract)
        except BaseException:
            self.db.close()
            if self._progress_error is not None:
                raise self._progress_error from None
            raise

    def _progress(self):
        try:
            self.check()
            return 0
        except BaseException as error:
            self._progress_error = error
            return 1

    def _initialize_project(self, industry, contract):
        from .connectivity import ContractError

        self.db.execute("PRAGMA cache_size=-8192")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS mapping (domain TEXT, old TEXT, new TEXT NOT NULL,
            ordinal INTEGER NOT NULL, PRIMARY KEY(domain,old), UNIQUE(domain,new));
          CREATE INDEX IF NOT EXISTS map_ordinal ON mapping(domain,ordinal);
        """)
        settings = dict(self.db.execute("SELECT name,value FROM settings"))
        if settings and settings.get("format") not in {"snp-anonymizer-project-v1", "snp-anonymizer-project-v2"}:
            self.db.close()
            raise AnonymizerError("Неподдерживаемая версия проекта.")
        if settings and settings.get("industry") != industry:
            self.db.close()
            raise AnonymizerError("Отрасль отличается от отрасли проекта. Выберите прежнюю отрасль или новый проект.")
        self.contract = contract if contract is not None and contract.profile is not None else None
        if settings.get("format") == "snp-anonymizer-project-v2" and self.contract is None:
            self.db.close()
            raise ContractError("Для этого проекта требуется прежний профиль связности.")
        if self.contract is not None and settings:
            if settings.get("format") == "snp-anonymizer-project-v1" and self.db.execute("SELECT 1 FROM mapping LIMIT 1").fetchone():
                self.db.close()
                raise ContractError("Непустой прежний проект несовместим с профильным режимом. Создайте новый проект.")
            if settings.get("format") == "snp-anonymizer-project-v2" and (
                    settings.get("connectivity_profile_hash") != self.contract.profile_hash
                    or settings.get("connectivity_compiler") != "1"):
                self.db.close()
                raise ContractError("Профиль связности проекта изменился. Создайте новый проект.")
        if not settings:
            settings = {"format": "snp-anonymizer-project-v2" if self.contract else "snp-anonymizer-project-v1",
                        "industry": industry, "secret": secrets.token_hex(32)}
            if self.contract:
                settings.update(connectivity_profile_hash=self.contract.profile_hash, connectivity_compiler="1")
            self.db.executemany("INSERT INTO settings VALUES (?,?)", settings.items())
            self.db.commit()
        try:
            self.secret = bytes.fromhex(settings["secret"])
        except (KeyError, ValueError, TypeError):
            if self.contract:
                raise ContractError("Некорректный сохранённый проект связности.") from None
            raise AnonymizerError("Некорректный сохранённый проект.") from None
        self.industry, self.cache, self.counters, self.cache_bytes = industry, OrderedDict(), {}, 0
        if self.contract:
            self.db.execute("PRAGMA temp_store=FILE")
        try:
            self.db.execute("BEGIN IMMEDIATE" if self.contract else "BEGIN")
            if self.contract:
                self._bind_contract()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _bind_contract(self):
        """Check immutable field semantics before retaining compatible new bounds."""
        from .connectivity import ContractError
        self.db.execute("CREATE TABLE IF NOT EXISTS connectivity_bindings(node TEXT PRIMARY KEY, domain TEXT, semantic TEXT NOT NULL)")
        for node, domain in sorted(self.contract.assignments.items()):
            self.check()
            key = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
            semantic = json.dumps({k: domain[k] for k in ("mapping_domain", "kind", "equality", "namespace", "prefix")},
                                  sort_keys=True, separators=(",", ":"))
            old = self.db.execute("SELECT domain,semantic FROM connectivity_bindings WHERE node=?", (key,)).fetchone()
            if old and old[1] != semantic:
                has_values = self.db.execute("SELECT 1 FROM mapping WHERE domain=? LIMIT 1", (old[0],)).fetchone()
                if has_values or old[0] != domain["mapping_domain"]:
                    raise ContractError("Изменилась семантика ранее связанного поля. Создайте новый проект.")
            self.db.execute("INSERT OR REPLACE INTO connectivity_bindings VALUES (?,?,?)", (key, domain["mapping_domain"], semantic))
        for domain in self.contract.domains:
            from .connectivity import ContractError, domain_value
            self.db.execute("CREATE TEMP TABLE connectivity_existing_codes(value TEXT PRIMARY KEY) WITHOUT ROWID")
            try:
                for old, value in self.db.execute("SELECT old,new FROM mapping WHERE domain=?", (domain["mapping_domain"],)):
                    self.check()
                    self._validate_domain_code(domain, value, conflict=True)
                    if domain_value(domain, old) != old:
                        raise ContractError("Неканоническая идентичность в сохранённой карте.")
                    try:
                        self.db.execute("INSERT INTO connectivity_existing_codes VALUES (?)", (domain_value(domain, value),))
                    except sqlite3.IntegrityError:
                        raise ContractError("Коллизия сохранённых кодов по объявленному равенству.", "DOMAIN_MAPPING_CONFLICT") from None
            finally:
                self.db.execute("DROP TABLE connectivity_existing_codes")
        updates = {"format": "snp-anonymizer-project-v2", "connectivity_profile_hash": self.contract.profile_hash,
                   "connectivity_compiler": "1", "connectivity_fingerprint": self.contract.fingerprint}
        self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", updates.items())

    @staticmethod
    def _validate_domain_code(domain, value, *, conflict=False):
        from .connectivity import ContractError, domain_value
        from .validation import validate_value

        try:
            _check_latin(value)
            required_prefix = domain.get("required_prefix")
            if required_prefix:
                head = required_prefix + "_"
                suffix = value[len(head):] if value.startswith(head) else ""
                if len(suffix) < 6 or not suffix.isdecimal():
                    raise ValueError()
            domain_value(domain, value)
            if domain["kind"] == "TEXT" and domain.get("length") and len(value) > domain["length"]:
                raise ValueError()
            for physical in domain.get("physical_fields", ()):
                field = dict(physical, name="identifier", nullable=True)
                validate_value("domain", field, value)
                if physical["sql_type"] in {"real", "float4", "double precision", "float8"}:
                    n = Decimal(value)
                    bits = 24 if physical["sql_type"] in {"real", "float4"} else 53
                    if n != n.to_integral_value() or abs(n) > 2**bits:
                        raise ValueError()
        except (ValueError, ArithmeticError, AnonymizerError):
            code = "CONTRACT_CONFLICT" if conflict else "DOMAIN_CAPACITY_EXHAUSTED"
            raise ContractError("Код общего домена не помещается в согласованное представление.", code) from None

    def _domain_candidate(self, domain, original, ordinal):
        from .connectivity import ContractError

        capacity = domain.get("capacity")
        if capacity is not None and ordinal > capacity:
            raise ContractError("Исчерпано общее пространство кодов.", "DOMAIN_CAPACITY_EXHAUSTED")
        if domain["kind"] == "UUID":
            message = domain["mapping_domain"] + "\0" + original + "\0" + str(ordinal)
            digest = hmac.new(self.secret, message.encode("utf-8"), hashlib.sha256).digest()[:16]
            return str(uuid.UUID(bytes=digest, version=4))
        if domain["kind"] == "NUMBER":
            q = Decimal(domain.get("quantum", "1"))
            rank = ordinal - 1
            positive, negative = domain.get("positive_capacity"), domain.get("negative_capacity")
            paired = min(positive, negative) if positive is not None and negative is not None else None
            if paired is not None and rank > 2 * paired:
                index = (paired + rank - 2 * paired) * (1 if positive > negative else -1)
            else:
                index = (rank + 1) // 2 * (1 if rank % 2 else -1)
            with localcontext() as context:
                context.prec = max(32, len(str(ordinal)) + abs(q.as_tuple().exponent) + 4)
                value = format(q * index, "f")
                return value.rstrip("0").rstrip(".") if "." in value else value
        candidate = f"{domain['prefix']}_{ordinal:06d}"
        if domain.get("length") and len(candidate) > domain["length"]:
            if domain.get("required_prefix"):
                raise ContractError("Код ресурса с обязательным префиксом не помещается в поле.", "DOMAIN_CAPACITY_EXHAUSTED")
            candidate = base36(ordinal - 1)
        self._validate_domain_code(domain, candidate)
        return candidate

    def prepare_domains(self, store, check=lambda: None):
        """Allocate the current package before writing output; all sets stay on disk.

        A final candidate equal to its source can be swapped with another new
        assignment. Existing durable correspondences are never changed.
        """
        if self.contract is None:
            return
        from .connectivity import ContractError, domain_value

        self.db.execute("CREATE TEMP TABLE connectivity_input(domain TEXT, old TEXT, PRIMARY KEY(domain,old)) WITHOUT ROWID")
        self.db.execute("CREATE TEMP TABLE connectivity_pending(domain TEXT,old TEXT,new TEXT,ordinal INTEGER,PRIMARY KEY(domain,old),UNIQUE(domain,new)) WITHOUT ROWID")
        self.db.execute("CREATE TEMP TABLE connectivity_free(domain TEXT,ordinal INTEGER,new TEXT,PRIMARY KEY(domain,ordinal)) WITHOUT ROWID")
        self.check, self._progress_error = check, None
        self.db.set_progress_handler(self._progress, 1000)
        batch, size = [], 0
        values = None
        try:
            for name, table in store.tables.items():
                fields = [(i, self.contract.assignments.get((*name, f["name"]))) for i, f in enumerate(table["fields"])]
                fields = [(i, d) for i, d in fields if d is not None]
                if not fields:
                    continue
                for row in store.rows(table["id"]):
                    check()
                    for index, domain in fields:
                        value = row[index]
                        if value is None:
                            continue
                        old = domain_value(domain, value)
                        batch.append((domain["mapping_domain"], old))
                        size += (len(domain["mapping_domain"]) + len(old)) * 4 + 128
                        if len(batch) >= 500 or size >= 1024 * 1024:
                            self.db.executemany("INSERT OR IGNORE INTO connectivity_input VALUES (?,?)", batch)
                            batch, size = [], 0
            if batch:
                self.db.executemany("INSERT OR IGNORE INTO connectivity_input VALUES (?,?)", batch)
            for domain in self.contract.domains:
                check()
                mapping_domain = domain["mapping_domain"]
                next_ordinal = 1
                values = self.db.execute("SELECT i.old FROM connectivity_input i WHERE i.domain=? AND NOT EXISTS (SELECT 1 FROM mapping m WHERE m.domain=i.domain AND m.old=i.old) ORDER BY i.old", (mapping_domain,))
                for old, in values:
                    check()
                    chosen = None
                    free = self.db.execute("SELECT ordinal,new FROM connectivity_free WHERE domain=? ORDER BY ordinal LIMIT 1", (mapping_domain,)).fetchone()
                    if free and domain_value(domain, free[1]) != old:
                        chosen = free
                        self.db.execute("DELETE FROM connectivity_free WHERE domain=? AND ordinal=?", (mapping_domain, free[0]))
                    while chosen is None:
                        check()
                        try:
                            candidate = self._domain_candidate(domain, old, next_ordinal)
                        except ContractError as error:
                            free = self.db.execute("SELECT ordinal,new FROM connectivity_free WHERE domain=? ORDER BY ordinal LIMIT 1", (mapping_domain,)).fetchone()
                            prior = self.db.execute("SELECT old,new,ordinal FROM connectivity_pending WHERE domain=? LIMIT 1", (mapping_domain,)).fetchone()
                            if error.code != "DOMAIN_CAPACITY_EXHAUSTED" or free is None or prior is None:
                                raise
                            # Only an unassigned self-code remains: swap a pair
                            # inside this package before any row is exported.
                            self.db.execute("DELETE FROM connectivity_pending WHERE domain=? AND old=?", (mapping_domain, prior[0]))
                            self.db.execute("INSERT INTO connectivity_pending VALUES (?,?,?,?)", (mapping_domain, prior[0], free[1], free[0]))
                            self.db.execute("DELETE FROM connectivity_free WHERE domain=? AND ordinal=?", (mapping_domain, free[0]))
                            chosen = (prior[2], prior[1])
                            break
                        ordinal = next_ordinal
                        next_ordinal += 1
                        if self.db.execute("SELECT 1 FROM mapping WHERE domain=? AND new=? UNION ALL SELECT 1 FROM connectivity_pending WHERE domain=? AND new=? LIMIT 1", (mapping_domain, candidate, mapping_domain, candidate)).fetchone():
                            continue
                        if domain_value(domain, candidate) == old:
                            self.db.execute("INSERT OR IGNORE INTO connectivity_free VALUES (?,?,?)", (mapping_domain, ordinal, candidate))
                            continue
                        chosen = ordinal, candidate
                    self._validate_domain_code(domain, chosen[1])
                    self.db.execute("INSERT INTO connectivity_pending VALUES (?,?,?,?)", (mapping_domain, old, chosen[1], chosen[0]))
                values.close()
                values = None
            self.db.execute("INSERT INTO mapping SELECT domain,old,new,ordinal FROM connectivity_pending")
        except sqlite3.OperationalError:
            if self._progress_error is not None:
                raise self._progress_error from None
            raise
        finally:
            self.db.set_progress_handler(None, 0)
            if values is not None:
                values.close()
            self.db.execute("DROP TABLE connectivity_input")
            self.db.execute("DROP TABLE connectivity_pending")
            self.db.execute("DROP TABLE connectivity_free")

    def domain_pseudonym(self, domain, original):
        from .connectivity import ContractError, domain_value

        original = domain_value(domain, original)
        key = domain["mapping_domain"], original
        if key in self.cache:
            value, _ = self.cache[key]
            self._validate_domain_code(domain, value, conflict=True)
            self.cache.move_to_end(key)
            return value
        found = self.db.execute("SELECT new,ordinal FROM mapping WHERE domain=? AND old=?", key).fetchone()
        if found is None:
            # Public Transformer use can be incremental outside the pipeline.
            # Pipeline preallocates all IDs with prepare_domains().
            ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM mapping WHERE domain=?", (key[0],)).fetchone()[0]
            while True:
                self.check()
                value = self._domain_candidate(domain, original, ordinal)
                if domain_value(domain, value) != original and not self.db.execute("SELECT 1 FROM mapping WHERE domain=? AND new=?", (key[0], value)).fetchone():
                    break
                ordinal += 1
            self.db.execute("INSERT INTO mapping VALUES (?,?,?,?)", (*key, value, ordinal))
            found = value, ordinal
        value, ordinal = found
        self._validate_domain_code(domain, value, conflict=True)
        self.cache[key] = value, ordinal
        self.cache_bytes += (len(key[0]) + len(original) + len(value)) * 4
        while len(self.cache) > 10000 or self.cache_bytes > 4 * 1024 * 1024:
            (old_domain, old_key), (old_value, _) = self.cache.popitem(last=False)
            self.cache_bytes -= (len(old_domain) + len(old_key) + len(old_value)) * 4
        return value

    def pseudonym(self, domain, original, length=None, root=None, uuid_type=False, label=None, numeric=False,
                  avoid_original=False, text_prefix=None):
        if numeric:
            original = canonical.numeric(original)
        elif uuid_type:
            original = str(uuid.UUID(original))
        key = (domain, original)
        if key in self.cache:
            value, ordinal = self.cache[key]
            _check_latin(value)
            self.cache.move_to_end(key)
        else:
            found = self.db.execute("SELECT new,ordinal FROM mapping WHERE domain=? AND old=?", key).fetchone()
            if found:
                value, ordinal = found
                _check_latin(value)
            else:
                if domain not in self.counters:
                    self.counters[domain] = self.db.execute("SELECT COALESCE(MAX(ordinal),0) FROM mapping WHERE domain=?", (domain,)).fetchone()[0]
                self.counters[domain] += 1
                ordinal = self.counters[domain]
                if avoid_original and not numeric and not uuid_type:
                    value, ordinal = self._fresh_text(domain, original, length, root, label, ordinal, text_prefix)
                    self.counters[domain] = ordinal
                elif numeric:
                    candidate = ordinal
                    while canonical.numeric(str(candidate)) == original or self.db.execute("SELECT 1 FROM mapping WHERE domain=? AND new=?", (domain, str(candidate))).fetchone():
                        candidate += 1
                    value = str(candidate)
                elif uuid_type:
                    digest = hmac.new(self.secret, (domain + "\0" + original).encode(), hashlib.sha256).digest()[:16]
                    value = str(uuid.UUID(bytes=digest, version=4))
                    if value == original:
                        digest = hmac.new(self.secret, (domain + "\0" + original + "\0retry").encode(), hashlib.sha256).digest()[:16]
                        value = str(uuid.UUID(bytes=digest, version=4))
                elif label:
                    value = self.label(label, ordinal)
                else:
                    value = PREFIX.get(root, "A") + f"{ordinal:06d}"
                if length and len(value) > length:
                    # ASCII code with dense base-36 ordinal; no truncation collisions.
                    value = "A" + base36(ordinal)
                    if len(value) > length:
                        value = base36(ordinal)
                    if len(value) > length:
                        raise AnonymizerError("Недостаточная длина текстового поля для уникальных замен.")
                _check_latin(value)
                try:
                    self.db.execute("INSERT INTO mapping VALUES (?,?,?,?)", (domain, original, value, ordinal))
                except sqlite3.IntegrityError:
                    raise AnonymizerError("Конфликт карты замен. Создайте новый проект.") from None
            if length and len(value) > length:
                raise AnonymizerError("Сохранённая замена не помещается в поле этой выгрузки. Нужен новый проект или согласованная модель.")
            self.cache[key] = (value, ordinal)
            self.cache_bytes += (len(domain) + len(original) + len(value)) * 4
            while len(self.cache) > 10000 or self.cache_bytes > 4 * 1024 * 1024:
                (removed_domain, removed_old), (removed_value, _) = self.cache.popitem(last=False)
                self.cache_bytes -= (len(removed_domain) + len(removed_old) + len(removed_value)) * 4
        return value

    def _fresh_text(self, domain, original, length, root, label, ordinal, text_prefix=None):
        # A short field has a finite code space. Bound both arithmetic and SQL
        # probes, and never insert/cache a candidate equal to its source value.
        space = 36 ** length if length and length <= 6 else None
        attempts = min(MAX_GENERIC_RETRIES, space) if space else MAX_GENERIC_RETRIES
        for _ in range(attempts):
            value = (f"{text_prefix}_{ordinal:06d}" if text_prefix is not None else
                     self.label(label, ordinal) if label else PREFIX.get(root, "A") + f"{ordinal:06d}")
            if length and len(value) > length:
                value = base36(ordinal % space if space else ordinal)
                if len(value) > length:
                    break
            if value != original and not self.db.execute("SELECT 1 FROM mapping WHERE domain=? AND new=?", (domain, value)).fetchone():
                return value, ordinal
            ordinal += 1
        raise AnonymizerError("Недостаточно уникальных замен для длины текстового поля.")

    def label(self, kind, n):
        if kind == "item":
            pool = INDUSTRIES[self.industry]["products"]
            return f"{pool[(n-1) % len(pool)]} - series {n:04d}"
        prefix = {"loc": "North site", "res": "Production line", "prodsource_sp": "Process route", "demand_stream": "Regional channel", "brand": "Alta brand", "category": "Product group", "supplier": "Vector supplier", "planner": "Planner", "address": "Industrial street", "text": "Attribute"}.get(kind, "Attribute")
        return f"{prefix} {n:04d}"

    def iter_mapping(self):
        """Stream checked mappings one row at a time; originals retain Unicode."""
        cursor = self.db.execute("SELECT domain, old, new FROM mapping ORDER BY domain, ordinal")
        try:
            for domain, old, new in cursor:
                _check_latin(new)
                yield domain, old, new
        finally:
            cursor.close()

    def finish(self, success):
        if success:
            self.db.commit()
        else:
            self.db.rollback()
        self.db.close()


def base36(n):
    chars, result = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", ""
    while n:
        n, digit = divmod(n, 36)
        result = chars[digit] + result
    return result or "0"


class Transformer:
    def __init__(self, model, project, store=None, contract=None):
        self.model, self.project, self.store = model, project, store
        self.contract = contract
        self.referenced_roots = {store.root(schema, table, field["name"]) for (schema, table), data in store.tables.items() for field in data["fields"] if field.get("fk")} if store else set()
        self.changed = 0

    def rule(self, table, field, schema=None):
        col = field["name"]
        if col == "sys_id":
            return "clear"
        if col == "update_date_time":
            return "passthrough"
        if col == "change_author":
            return "constant"
        if self.contract and (schema, table, col) in self.contract.assignments:
            return "connectivity-id"
        if self.store and self.store.bypass_model:
            if field["type"] in {"NUMBER", "INTEGER", "BOOLEAN", "DATE", "DATE_TIME", "TIME"}:
                return "preserve"
            return "json" if field["type"] == "JSONB" else "uuid" if field["type"] == "UUID" else "text"
        if field["type"] in {"NUMBER", "INTEGER"}:
            rschema, *root = self.store.root(schema, table, col) if self.store and schema else (schema, *self.model.root(table, col))
            root = tuple(root)
            root_field = self.store.field(rschema, *root) if self.store else self.model.field(*root)
            root_keys = set((root_field or {}).get("keys") or [])
            metrics = ("qty", "quantity", "volume", "weight", "cost", "price", "capacity", "duration", "utilization", "multiplier", "rate", "factor")
            measure = any(word in root[1] for word in metrics) and not root[1].endswith(("id", "_num"))
            technical = root[0] in {"time_profile", "sp_periodtypes", "loctypes", "itemtypes", "restypes", "sp_status", "sp_demand_mode", "sp_origin", "sp_stockmethods", "uomtype"} or root[1] in {"periodid", "periodtype", "rsl"} or root[1].endswith("flag") or measure
            numeric_ids = {"ord_num", "orderid", "order_id", "ordid", "order_no", "batch_id", "batchid", "contract_id", "document_id", "resequipnum"}
            if not technical and (root in PREFIX or (rschema, *root) in self.referenced_roots or root_keys & {"PK", "BK"} or set(field.get("keys") or []) & {"PK", "BK"} or field.get("fk") or root[1] in numeric_ids):
                return "numeric-id"
            return "preserve"
        if field["type"] in {"BOOLEAN", "DATE", "DATE_TIME", "TIME"}:
            return "preserve"
        if field["type"] == "JSONB":
            if field.get("keys") or field.get("fk"):
                raise AnonymizerError("JSON в ключе/ссылке требует отдельного профиля и не поддерживается.")
            return "json"
        if field["type"] == "UUID":
            return "uuid"
        return "text"

    def transform(self, schema, table, fields, row):
        bycol = {f["name"]: value for f, value in zip(fields, row)}
        result = []
        for field, old in zip(fields, row):
            col, rule = field["name"], self.rule(table, field, schema)
            if rule == "clear":
                value = None
            elif rule == "constant":
                value = "snp"
            elif rule == "connectivity-id":
                value = None if old is None else self.project.domain_pseudonym(self.contract.assignments[(schema, table, col)], old)
            elif old is None or old == "" or rule in {"preserve", "passthrough"}:
                value = old
            elif self.store and self.store.bypass_model:
                domain = f"generic:{schema}:{table}.{col}"
                if rule == "json":
                    value = json_data.dump(self.json_transform(json_data.load(old), schema, table, col))
                else:
                    value = self.project.pseudonym(domain, old, field.get("length"), uuid_type=rule == "uuid",
                                                   label=None if rule == "uuid" else "text", avoid_original=rule == "text",
                                                   text_prefix=col if rule == "text" else None)
                    if value == old:
                        raise AnonymizerError(f"Таблица {table}, колонка {col}: замена совпала с исходным текстом. Нужен другой проект.")
            elif rule == "json":
                value = json_data.dump(self.json_transform(json_data.load(old), schema, table, col))
            elif rule in {"uuid", "numeric-id"}:
                rschema, rtable, rcolumn = self.store.root(schema, table, col) if self.store else (schema, *self.model.root(table, col))
                value = self.project.pseudonym(f"{rschema}:{rtable}.{rcolumn}", old, uuid_type=rule == "uuid", numeric=rule == "numeric-id")
            else:
                rschema, rtable, rcolumn = self.store.root(schema, table, col) if self.store else (schema, *self.model.root(table, col))
                root = (rtable, rcolumn)
                if root[0] == "uom" and old.upper() in UNITS:
                    value = old
                elif (col.endswith("flag") or col in {"fixed", "active", "finprod"}) and old in FLAGS:
                    value = old
                elif (col == "abc" and old in {"A", "B", "C"}) or (col == "xyz" and old in {"X", "Y", "Z"}):
                    value = old
                elif col in DESCRIPTIONS and bycol.get(DESCRIPTIONS[col][1]):
                    entity, key = DESCRIPTIONS[col]
                    origin = bycol[key]
                    value = self.project.pseudonym(f"{schema}:descr:{entity}", origin, field.get("length"), label=entity)
                else:
                    is_key = bool(field.get("keys")) or root != (table, col) or root in PREFIX
                    label = None if is_key else ("brand" if "brand" in col else "category" if "category" in col else "planner" if "planner" in col else "address" if "address" in col else "supplier" if "supplier" in col else "text")
                    value = self.project.pseudonym(f"{rschema}:{root[0]}.{root[1]}", old, field.get("length"), root=root, label=label)
                technical = (root[0] == "uom" and old.upper() in UNITS) or ((col.endswith("flag") or col in {"fixed", "active", "finprod"}) and old in FLAGS) or (col == "abc" and old in {"A", "B", "C"}) or (col == "xyz" and old in {"X", "Y", "Z"})
                if value == old and not technical:
                    raise AnonymizerError(f"Таблица {table}, колонка {col}: сгенерированная замена совпала с исходным текстом. Нужен другой проект.")
            self.changed += value != old
            result.append(value)
        return result

    def json_transform(self, value, schema, table, column, key=None):
        technical_keys = {"qty", "quantity", "date", "cost", "price", "item", "loc", "res", "comp", "key", "value", "name", "description", "data", "values", "rows", "columns", "properties", "enabled", "version", "start", "end"}
        generic = bool(self.store and self.store.bypass_model)
        domain = ("generic:" if generic else "") + f"{schema}:{table}.{column}:json"

        def replaced(original, *args, **kwargs):
            if generic:
                kwargs["avoid_original"] = True
            replacement = self.project.pseudonym(*args, **kwargs)
            if replacement == original:
                raise AnonymizerError(f"Таблица {table}, колонка {column}: сгенерированная замена JSON совпала с исходным текстом. Нужен другой проект.")
            return replacement

        if isinstance(value, dict):
            return {(k if k in technical_keys and not generic else replaced(k, domain + ":keys", k)): self.json_transform(v, schema, table, column, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json_transform(v, schema, table, column, key) for v in value]
        if isinstance(value, json_data.Number) or not isinstance(value, str) or not value or json_data.is_date(value):
            return value
        if not generic and key in {"item", "loc", "res", "comp"}:
            root = self.model.root(table, key)
            return replaced(value, f"{schema}:{root[0]}.{root[1]}", value, root=root)
        return replaced(value, domain + ":values", value, label="text")

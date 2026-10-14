from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections import OrderedDict

from .errors import AnonymizerError
from . import json_data
from . import canonical

INDUSTRIES = {
    "food": {"title": "Пищевая промышленность", "products": ["Шоколад молочный", "Печенье овсяное", "Конфеты сливочные", "Какао-порошок", "Сахар", "Масло какао", "Упаковка картонная", "Сироп ванильный"]},
    "brewery": {"title": "Напитки", "products": ["Напиток янтарный", "Напиток светлый", "Квас классический", "Вода газированная", "Солод светлый", "Хмель ароматный", "Бутылка стеклянная", "Банка алюминиевая"]},
    "chem": {"title": "Химическая промышленность", "products": ["Краска интерьерная", "Грунтовка универсальная", "Смола базовая", "Растворитель", "Пигмент белый", "Лак защитный", "Ведро пластиковое", "Покрытие полимерное"]},
    "consumer": {"title": "Потребительские товары", "products": ["Средство для стирки", "Мыло жидкое", "Шампунь мягкий", "Гель для уборки", "Сырьё моющее", "Отдушка цветочная", "Флакон пластиковый", "Короб транспортный"]},
    "industrial": {"title": "Машиностроение", "products": ["Насос центробежный", "Клапан регулирующий", "Корпус литой", "Вал приводной", "Подшипник", "Лист стальной", "Комплект крепежа", "Двигатель электрический"]},
}
PREFIX = {("item", "item"): "ITEM", ("loc", "loc"): "LOC", ("res", "res"): "RES", ("prodsource_sp", "prodsource"): "PS", ("demand_stream", "dmdstream"): "DS"}
UNITS = {"KG", "G", "T", "TON", "L", "ML", "HL", "M", "M2", "M3", "CM", "MM", "PC", "PCS", "EA", "ST", "H", "HR", "MIN", "S", "PAL", "BOX", "ШТ", "КГ", "Л", "М"}
FLAGS = {"", "X", "0", "1", "Y", "N", "true", "false", "TRUE", "FALSE", "x"}
DESCRIPTIONS = {"proddescr": ("item", "item"), "locdescr": ("loc", "loc"), "resdescr": ("res", "res"),
                "prodsourcedescr": ("prodsource_sp", "prodsource"), "dmdstreamdescr": ("demand_stream", "dmdstream")}


class Project:
    def __init__(self, path, industry):
        if industry not in INDUSTRIES:
            raise AnonymizerError("Неизвестная отрасль.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA cache_size=-8192")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS mapping (domain TEXT, old TEXT, new TEXT NOT NULL,
            ordinal INTEGER NOT NULL, PRIMARY KEY(domain,old), UNIQUE(domain,new));
          CREATE INDEX IF NOT EXISTS map_ordinal ON mapping(domain,ordinal);
        """)
        settings = dict(self.db.execute("SELECT name,value FROM settings"))
        if settings and settings.get("format") != "snp-anonymizer-project-v1":
            raise AnonymizerError("Неподдерживаемая версия проекта.")
        if settings and settings["industry"] != industry:
            self.db.close()
            raise AnonymizerError("Отрасль отличается от отрасли проекта. Выберите прежнюю отрасль или новый проект.")
        if not settings:
            settings = {"format": "snp-anonymizer-project-v1", "industry": industry, "secret": secrets.token_hex(32)}
            self.db.executemany("INSERT INTO settings VALUES (?,?)", settings.items())
            self.db.commit()
        self.secret = bytes.fromhex(settings["secret"])
        self.industry, self.cache, self.counters, self.cache_bytes = industry, OrderedDict(), {}, 0
        self.db.execute("BEGIN")

    def pseudonym(self, domain, original, length=None, root=None, uuid_type=False, label=None, numeric=False):
        if numeric:
            original = canonical.numeric(original)
        elif uuid_type:
            original = str(uuid.UUID(original))
        key = (domain, original)
        if key in self.cache:
            value, ordinal = self.cache[key]
            self.cache.move_to_end(key)
        else:
            found = self.db.execute("SELECT new,ordinal FROM mapping WHERE domain=? AND old=?", key).fetchone()
            if found:
                value, ordinal = found
            else:
                if domain not in self.counters:
                    self.counters[domain] = self.db.execute("SELECT COALESCE(MAX(ordinal),0) FROM mapping WHERE domain=?", (domain,)).fetchone()[0]
                self.counters[domain] += 1
                ordinal = self.counters[domain]
                if numeric:
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

    def label(self, kind, n):
        if kind == "item":
            pool = INDUSTRIES[self.industry]["products"]
            return f"{pool[(n-1) % len(pool)]} — серия {n:04d}"
        prefix = {"loc": "Площадка Север", "res": "Линия производства", "prodsource_sp": "Технологический маршрут", "demand_stream": "Канал Регион", "brand": "Марка Альта", "category": "Группа продукции", "supplier": "Поставщик Вектор", "planner": "Планировщик", "address": "Улица Промышленная", "text": "Атрибут"}.get(kind, "Атрибут")
        return f"{prefix} {n:04d}"

    def iter_mapping(self):
        """Streams (domain, old, new) rows in batches; uncommitted data included."""
        cursor = self.db.execute("SELECT domain, old, new FROM mapping ORDER BY domain, ordinal")
        try:
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                yield from batch
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
    def __init__(self, model, project, store=None):
        self.model, self.project, self.store = model, project, store
        self.referenced_roots = {store.root(schema, table, field["name"]) for (schema, table), data in store.tables.items() for field in data["fields"] if field.get("fk")} if store else set()
        self.changed = 0

    def rule(self, table, field, schema=None):
        col = field["name"]
        if col == "sys_id":
            return "omit"
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
            if rule == "omit":
                continue
            if old is None or old == "" or rule == "preserve":
                value = old
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
        domain = f"{schema}:{table}.{column}:json"

        def replaced(original, *args, **kwargs):
            replacement = self.project.pseudonym(*args, **kwargs)
            if replacement == original:
                raise AnonymizerError(f"Таблица {table}, колонка {column}: сгенерированная замена JSON совпала с исходным текстом. Нужен другой проект.")
            return replacement

        if isinstance(value, dict):
            return {(k if k in technical_keys else replaced(k, domain + ":keys", k)): self.json_transform(v, schema, table, column, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json_transform(v, schema, table, column, key) for v in value]
        if isinstance(value, json_data.Number) or not isinstance(value, str) or not value or json_data.is_date(value):
            return value
        if key in {"item", "loc", "res", "comp"}:
            root = self.model.root(table, key)
            return replaced(value, f"{schema}:{root[0]}.{root[1]}", value, root=root)
        return replaced(value, domain + ":values", value, label="text")

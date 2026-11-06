"""Bounded technical diagnostics: never row values, source schemas or paths."""
from __future__ import annotations

from .metadata import SYSTEM

MISSING_REASONS = frozenset({"DOMAIN_MEMBER_MISSING", "RELATION_TARGET_MISSING", "COLUMN_MISSING", "PHYSICAL_COLUMN_MISSING"})
DOMAIN_TITLES = {
    "ITEM": "Товары и компоненты", "PRODUCT": "Товары", "LOCATION": "Локации",
    "MOT": "Способы перевозки", "RESOURCE": "Ресурсы SNP",
    "RESOURCE_PROD": "Ресурсы производства", "RESOURCE_STOCK": "Ресурсы хранения",
    "RESOURCE_TRANSP": "Ресурсы перевозки", "PRODSOURCE": "Источники производства",
    "DEMAND_STREAM": "Потоки спроса", "DEMAND_TYPE": "Типы спроса", "CLIENT": "Клиенты",
    "TRANSPORT_TYPE": "Типы перевозки", "BOM": "Спецификации производства",
    "UOM": "Единицы измерения", "CALENDAR": "Календари", "RESOURCE_GROUP": "Группы ресурсов",
}
CONTEXT_TITLES = {"SKU": "Совпадения товара и локации", "RESOURCE_LOC": "Совпадения ресурса и локации",
                  "PRODSOURCE": "Совпадения товара, локации и источника производства",
                  "LANE": "Совпадения направлений перевозки", "DEMAND": "Совпадения параметров спроса"}
REASONS = {
    "SYSTEM_FIELD_POLICY": "Системные поля исключены из проверки по правилам обработки.",
    "SYSTEM_FIELD_EXCLUDED": "Системные поля исключены из проверки по правилам обработки.",
    "COLLATION_COMPARISON_UNSUPPORTED": "Сравнение с указанным COLLATE не поддерживается.",
    "COMPARISON_SEMANTICS_UNSUPPORTED": "Сравнение JSON, времени или даты со временем не поддерживается.",
    "FLOAT_COMPARISON_UNSUPPORTED": "Точное сравнение FLOAT/REAL не поддерживается.",
    "INCOMPATIBLE_COMPARISON_TYPES": "Типы сравниваемых полей несовместимы.",
    "CHECK_UNSUPPORTED": "Условие этой проверки не поддерживается.",
    "CHECK_NOT_APPLICABLE": "Нет условий для выполнения этой проверки.",
    "EXPORT_READBACK_FAILED": "Файлы выдачи не прошли повторное чтение; проверка не выполнена.",
    "LEGACY_BYPASS_WITHOUT_PROFILE": "В режиме без эталона профиль связности не выбран.",
    "NO_REQUIRED_CHECKS": "Не задан обязательный набор проверок связности.",
    "CHECK_INCOMPLETE": "Проверка не завершена: обработка отменена, исчерпаны ресурсы или файлы недоступны.",
    "CONTRACT_CONFLICT": "Настройки профиля, входной структуры или сохранённого проекта несовместимы.",
    "RELATION_TARGET_MISSING": "Для полной проверки не хватает таблиц или колонок.",
    "DOMAIN_MEMBER_MISSING": "Во входном наборе отсутствуют некоторые поля общего домена.",
    "RELATION_CHANGED": "После обезличивания изменились строки, значения или связи.",
    "KEY_GROUP_CHANGED": "После обезличивания изменился состав групп ключа.",
    "DOMAIN_MAPPING_CONFLICT": "Замены общего домена не сохранили соответствие исходным кодам.",
}


def _members(descriptor, kind):
    if kind == "domain":
        return descriptor.get("_declared_members", [
            {"table": m["table"], "columns": [m["column"]]} for m in descriptor.get("members", ())])
    if kind == "key":
        return [{"table": descriptor["table"], "columns": descriptor["columns"]}]
    return [m for side in ("source", "target") for m in descriptor.get(side, {}).get("members", ())]


def _refs(side):
    return "; ".join(f"{m['table'][1]}." + (m["columns"][0] if len(m["columns"]) == 1
                    else "(" + ", ".join(m["columns"]) + ")") for m in side.get("members", ()))


def annotate_contract(store, contract, namespace_labels, check=lambda: None):
    """Called after semantic fingerprinting, before the compiler budget check."""
    def descriptors():
        for kind, rows in (("domain", contract.domains), ("relation", contract.relations), ("key", contract.keys)):
            for descriptor in rows:
                yield kind, descriptor
    labels = dict(namespace_labels)
    extra = set()
    for kind, descriptor in descriptors():
        check()
        for member in _members(descriptor, kind):
            if member["table"][0] not in labels:
                extra.add(member["table"][0])
    for schema in sorted(extra):
        labels[schema] = "ns" + str(len(labels) + 1)
    qualify = len(labels) > 1
    for kind, descriptor in descriptors():
        check()
        ident = descriptor["id"].split(":")
        if kind == "domain":
            name = descriptor.get("domain_id", descriptor["id"])
            title = DOMAIN_TITLES.get(name, "Общий домен " + name)
        elif kind == "key":
            prefix = {"pk": "Первичный ключ", "unique": "Уникальный ключ"}.get(descriptor.get("kind"), "Бизнес-ключ")
            title = f"{prefix} {descriptor['table'][1]} ({', '.join(descriptor['columns'])})"
        elif descriptor.get("kind") == "fk":
            title = f"Ссылка {_refs(descriptor['source'])} → {_refs(descriptor['target'])}"
        elif ident[0] == "roles" and len(ident) > 1:
            title = "Совпадения кодов: " + DOMAIN_TITLES.get(ident[1], ident[1])
        elif ident[0] == "context" and len(ident) > 1:
            title = CONTEXT_TITLES.get(ident[1], "Совпадения " + ident[1])
        else:
            title = f"Совпадения {_refs(descriptor.get('source', {}))} ↔ {_refs(descriptor.get('target', {}))}"
        if qualify:
            scopes = sorted({labels[m["table"][0]] for m in _members(descriptor, kind)})
            title += " [" + ", ".join(scopes) + "]"
        descriptor["title"] = title
        missing = {}
        for member in _members(descriptor, kind):
            check()
            name = tuple(member["table"])
            columns = member["columns"]
            table = store.tables.get(name)
            if table is None:
                reason = "TABLE_SKIPPED" if name in getattr(store, "_dropped_names", ()) else "TABLE_MISSING"
                absent = columns
            else:
                physical = table.get("physical_columns", {f["name"] for f in table["fields"]})
                absent = [c for c in columns if c not in physical and c not in SYSTEM]
                reason = "COLUMN_MISSING"
            if absent:
                key = (labels[name[0]], name[1], reason)
                missing.setdefault(key, set()).update(absent)
        descriptor["missing"] = [{"namespace": ns, "table": table, "columns": sorted(columns), "reason": reason,
                                  "qualified": qualify}
                                 for (ns, table, reason), columns in sorted(missing.items())]
        descriptor.pop("_declared_members", None)


def missing_message(item):
    table = _missing_ref(item)
    if item["reason"] == "TABLE_MISSING":
        return f"таблица {table} отсутствует во входном наборе"
    if item["reason"] == "TABLE_SKIPPED":
        return f"таблица {table} пропущена при чтении входа; причина указана в журнале импорта"
    return f"в переданной таблице {table} отсутствуют колонки: {', '.join(item['columns'])}"


def _missing_ref(item):
    return (item["namespace"] + "." if item.get("qualified") else "") + item["table"]


def check_message(item):
    status, kind = item.get("status"), item.get("kind")
    if status == "PASS":
        if kind == "export":
            return f"Проверено строк: {item.get('after_count', 0)}; потерь, лишних строк и изменений сохраняемых значений нет."
        if kind == "key" and item.get("source_defects"):
            return (f"Состав групп ключа сохранён. Повторных строк в источнике: {item.get('before_count', 0)}; "
                    f"после обработки: {item.get('after_count', 0)}; NULL в первичном ключе: {item.get('source_nulls', 0)}.")
        if item.get("relation_kind") == "fk" and item.get("source_defects"):
            return (f"Связи сохранены. Исходных ссылок без родителя: {item.get('unmatched_before', 0)}; "
                    f"после обработки: {item.get('unmatched_after', 0)}; новых разрывов нет.")
        return "Проверка пройдена; соответствия и сохраняемые значения не изменились."
    reason = item.get("reason", "CHECK_NOT_APPLICABLE")
    if status == "FAIL":
        return (REASONS.get(reason, "Проверка обнаружила изменение данных.") +
                f" Потеряно соответствий: {item.get('lost', 0)}; добавлено: {item.get('added', 0)}.")
    details = "; ".join(missing_message(m) for m in item.get("missing", ()))
    if item.get("partial"):
        prefix = "Доступная часть проверена, изменений не обнаружено. Полная проверка не выполнена: "
    else:
        prefix = "Не проверено: "
    if status == "UNSUPPORTED" or reason not in MISSING_REASONS:
        return prefix + REASONS.get(reason, "Причина: " + reason) + (" " + details + "." if details else "")
    return prefix + (details or REASONS.get(reason, "Недостаточно входных данных.")) + "."


def enrich_results(checks):
    for item in checks:
        if "title" not in item:
            item["title"] = {"export:csv": "Выдача CSV", "export:dump": "Выдача SQL COPY",
                             "export:inserts": "Выдача SQL INSERT"}.get(item.get("id"), "Проверка " + item["id"])
        item["message"] = check_message(item)


def summary_message(result):
    checks = [c for c in result.get("checks", ()) if c.get("kind") != "export"]
    done = sum(c.get("status") in {"PASS", "FAIL"} for c in checks)
    skipped = len(checks) - done
    text = f"Проверено проверок связей и ключей: {done} из {len(checks)}; не выполнено: {skipped}."
    missing = [m for c in checks for m in c.get("missing", ())]
    tables = sorted({_missing_ref(m) for m in missing if m["reason"] in {"TABLE_MISSING", "TABLE_SKIPPED"}})
    columns = sorted({_missing_ref(m) + "." + col for m in missing if m["reason"] == "COLUMN_MISSING" for col in m["columns"]})
    for label, refs in ((" Недоступны таблицы: ", tables), (" Отсутствуют колонки: ", columns)):
        if refs:
            text += label + ", ".join(refs[:6]) + (f" и ещё {len(refs)-6}" if len(refs) > 6 else "") + "."
    if not checks:
        text += " Проверены только файлы выдачи; проверки связей и ключей не объявлены."
    elif skipped:
        text += " Подробные причины указаны у каждой проверки. Полная связность не подтверждена."
    return text


def missing_only_coverage(result, contract):
    """Only a known absence may relax the full-coverage publication gate."""
    if (not contract or not contract.profile or contract.profile.get("missing_target_policy") == "block"
            or result.get("integrity_status") != "NOT_CHECKED" or result.get("error_code")
            or not result.get("coverage", {}).get("required")):
        return False
    checks = result.get("checks", ())
    if any(c.get("status") == "FAIL" for c in checks):
        return False
    by_id = {c["id"]: c for c in checks}
    if any(by_id.get("export:" + artifact, {}).get("status") != "PASS" for artifact in ("csv", "dump", "inserts")):
        return False
    required = set(result["coverage"]["required"])
    required.update(c["id"] for c in checks if c.get("required"))
    return all(by_id.get(ident, {}).get("status") == "PASS" or (
               by_id.get(ident, {}).get("status") == "NOT_CHECKED"
               and by_id.get(ident, {}).get("reason") in MISSING_REASONS) for ident in required)

"""Lossless numeric JSON: no binary floating-point conversions."""
import json
import re
from datetime import date, datetime


class Number(str):
    pass


def load(text):
    def invalid(value):
        raise ValueError("Invalid JSON numeric constant")
    return json.loads(text, parse_int=Number, parse_float=Number, parse_constant=invalid)


def dump(value):
    if isinstance(value, Number):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + dump(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(dump(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def is_date(value):
    if not isinstance(value, str) or isinstance(value, Number) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
        return False
    try:
        (date.fromisoformat if len(value) == 10 else datetime.fromisoformat)(value)
        return True
    except ValueError:
        return False


def signature(value):
    if isinstance(value, Number):
        return ["number", str(value)]
    if isinstance(value, dict):
        return ["object", [signature(v) for v in value.values()]]
    if isinstance(value, list):
        return ["array", [signature(v) for v in value]]
    if isinstance(value, str):
        return ["date", value] if is_date(value) else ["text"]
    return ["scalar", value]

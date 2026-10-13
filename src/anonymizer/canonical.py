"""Canonical identity comparisons; original measures remain untouched."""
from decimal import Decimal
from uuid import UUID


def numeric(value):
    number = Decimal(value)
    if not number.is_finite():
        return str(number)
    sign, digits, exponent = number.as_tuple()
    if not any(digits):
        return "0"
    digits = list(digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = ("-" if sign else "") + "".join(str(d) for d in digits)
    return text if exponent == 0 else text + "e" + str(exponent)


def value(field, text):
    if text is None:
        return None
    if field["type"] in {"NUMBER", "INTEGER"}:
        return numeric(text)
    if field["type"] == "UUID":
        return str(UUID(text))
    if field["type"] == "BOOLEAN":
        return "true" if text.lower() in {"true", "t", "1", "yes", "on"} else "false"
    if field["sql_type"].startswith(("character(", "char(")):
        return text.rstrip(" ")
    return text

"""Bounded, conservative type inference for explicit model-free input."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID

from .errors import AnonymizerError
from .metadata import SYSTEM

MAX_COLUMNS = 16384
MAX_HEADER_BYTES = 8 * 1024**2
NUMBER = re.compile(r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
DECIMAL_COMMA = re.compile(r"[+-]?(?:[0-9]+,[0-9]*|,[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
NATIVE_TYPES = {"TEXT", "NUMBER", "INTEGER", "BOOLEAN", "DATE", "DATE_TIME", "TIME", "UUID"}
# Explicit user-defined code columns, even when every submitted code is digits.
# This is local input policy, not a lookup in the disabled reference model.
TEXT_CODE_COLUMNS = frozenset({"comp", "item", "loc"})


class InputLimitError(AnonymizerError):
    """Global model-free input resource limit; cannot be hidden by table skip."""


def normalize_decimal_comma(value):
    """One decimal separator, no grouping/whitespace and no float conversion.

    Only readers of external tabular data call this; SQL and typed TEXT keep
    their original grammar and spelling. Leading zeros are left to inference
    (codes stay TEXT) or the explicitly declared numeric type.
    """
    return value.replace(",", ".") if value is not None and "," in value and DECIMAL_COMMA.fullmatch(value) else value


def normalize_number_cell(value, field):
    """Declared integers need an integer input lexeme for PostgreSQL COPY.

    Convert only a newly accepted comma decimal, after a bounded int64 check.
    Invalid fractions/ranges still reach the normal typed validator unchanged.
    """
    normalized = normalize_decimal_comma(value)
    if normalized != value and (field["type"] == "INTEGER" or field.get("sql_type") in {
            "smallint", "int2", "integer", "int", "int4", "bigint", "int8"}):
        try:
            number = Decimal(normalized)
            if number.is_finite() and -(2**63) <= number < 2**63 and number == number.to_integral_value():
                return str(int(number))
        except InvalidOperation:
            pass
    return normalized


def inferred_type(value):
    """No trimming, float conversion, name heuristics, or retained sample values."""
    number_text = normalize_decimal_comma(value)
    if NUMBER.fullmatch(number_text):
        try:
            if Decimal(number_text).is_finite():
                return "NUMBER"
        except InvalidOperation:
            pass
    if value.lower() in {"true", "false"}:
        return "BOOLEAN"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
            return "DATE"
        except ValueError:
            return "TEXT"
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", value):
        try:
            datetime.fromisoformat(value)
            return "DATE_TIME"
        except ValueError:
            pass
    if re.match(r"\d{2}:\d{2}", value):
        try:
            time.fromisoformat(value)
            return "TIME"
        except ValueError:
            pass
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        try:
            UUID(value)
            return "UUID"
        except ValueError:
            pass
    return "TEXT"


class InputInference:
    def __init__(self, table, columns, *, text_columns=()):
        if not columns or any(not isinstance(c, str) for c in columns):
            raise AnonymizerError("Неправильный заголовок таблицы без эталона.")
        if len(columns) > MAX_COLUMNS or sum(len(c.encode("utf-8")) + 128 for c in columns) > MAX_HEADER_BYTES:
            raise InputLimitError("Превышен лимит структуры таблицы без эталона.")
        self.table, self.columns = table, tuple(columns)
        text_columns = frozenset(text_columns)
        self.text_codes = frozenset(index for index, column in enumerate(columns)
                                    if column.casefold() in TEXT_CODE_COLUMNS or column in text_columns)
        self.types = ["TEXT" if index in self.text_codes else None for index in range(len(columns))]
        self.empty = [False] * len(columns)
        self.aware = [None] * len(columns)

    def observe(self, values, native_types=None):
        if len(values) != len(self.columns) or (native_types is not None and len(native_types) != len(values)):
            raise AnonymizerError("Неправильное число колонок таблицы без эталона.")
        for index, value in enumerate(values):
            if self.columns[index] in SYSTEM or value is None:
                continue
            if not isinstance(value, str):
                raise AnonymizerError("Внутренняя ошибка типа входной ячейки.")
            if value == "":
                self.empty[index] = True
                continue
            if index in self.text_codes:
                kind = "TEXT"
            else:
                kind = native_types[index] if native_types is not None else None
            if kind is None:
                kind = inferred_type(value)
            if kind not in NATIVE_TYPES:
                raise AnonymizerError("Неподдерживаемый тип ячейки таблицы без эталона.")
            if kind == "INTEGER":
                kind = "NUMBER"
            if kind in {"DATE_TIME", "TIME"}:
                try:
                    parsed = (datetime.fromisoformat if kind == "DATE_TIME" else time.fromisoformat)(value)
                except ValueError:
                    raise AnonymizerError(f"Таблица {self.table}, колонка {self.columns[index]}: некорректная дата или время.") from None
                aware = parsed.tzinfo is not None
                if self.aware[index] is not None and self.aware[index] != aware:
                    raise AnonymizerError(f"Таблица {self.table}, колонка {self.columns[index]}: неоднородные часовые пояса дат или времени.")
                self.aware[index] = aware
            previous = self.types[index]
            if previous is not None and previous != kind:
                raise AnonymizerError(f"Таблица {self.table}, колонка {self.columns[index]}: неоднородные типы; приведите колонку к одному типу.")
            self.types[index] = kind

    def fields(self):
        fields = []
        for column, kind, aware in zip(self.columns, self.types, self.aware):
            field = {"name": column, "type": kind or "TEXT", "nullable": True, "keys": [], "fk": None}
            if aware:
                field["sql_type"] = "timestamp with time zone" if kind == "DATE_TIME" else "time with time zone"
            fields.append(field)
        return fields

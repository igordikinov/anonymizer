"""Bounded decoders for this application's published representations.

This is intentionally not an input Store.  The output catalogue is fixed and
no model enrichment, column filtering, inference or anonymization is performed.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .errors import AnonymizerError
from .metadata import SYSTEM, physical_type
from .sql_reader import Reader, Lexer, Token, qualified, identifier, expect, decode_escape

MAX_MANIFEST = 8 * 1024**2
MAX_RECORD = 64 * 1024**2  # serialized escaping may expand an 8 MiB input row
MAX_FIELD = 8 * 1024**2
MAX_COLUMNS = 16384
MAX_IDENTIFIER = 63
MAX_TYPEMOD_DIGITS = 16
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9$]{0,62}\Z")
_HEADERS = {"-- SNP Anonymizer: standalone table/data dump", "-- SNP Anonymizer: data only; target tables must exist"}


def _fail():
    raise AnonymizerError("EXPORT_READBACK_INVALID: фактический экспорт не соответствует каталогу.")


class _OutputReader(Reader):
    def take(self, n=1):
        value = super().take(n)
        if getattr(self, "token_limit", None) is not None:
            self.token_bytes += len(value.encode("utf-8"))
            if self.token_bytes > self.token_limit:
                _fail()
        return value

    def readline(self):
        pieces, size = [], 0
        while self.peek():
            end = self.buf.find("\n", self.pos)
            part = self.take(end - self.pos + 1 if end >= 0 else len(self.buf) - self.pos)
            size += len(part.encode("utf-8"))
            if size > MAX_RECORD:
                _fail()
            pieces.append(part)
            if end >= 0:
                break
        return "".join(pieces)


class _OutputLexer(Lexer):
    """Only generated token forms, with limits before token accumulation."""
    def next(self):
        r = self.r
        r.token_limit = None
        while True:
            char = r.peek()
            if char and (char.isspace() or char == "\ufeff"):
                r.take()
            elif r.peek(2) == "--":
                comment = []
                while r.peek() and r.peek() not in "\r\n":
                    comment.append(r.take())
                    if len(comment) > 128:
                        _fail()
                if "".join(comment) not in _HEADERS:
                    _fail()
            else:
                break
        char, line = r.peek(), r.line
        if not char:
            return Token("eof", "", line)
        if char == "'" or (char in "eE" and r.peek(2)[1:] == "'"):
            # The inherited data-string decoder bounds decoded characters;
            # this independent cap also bounds its serialized UTF-8 token.
            r.token_limit, r.token_bytes = MAX_RECORD, 0
            try:
                token = super().next()
                token.serialized_bytes = r.token_bytes
                return token
            finally:
                r.token_limit = None
        if char == '"':
            r.take()
            value = []
            while r.peek():
                char = r.take()
                if char == '"':
                    if r.peek() == '"':
                        _fail()  # generated technical identifiers never quote quotes
                    text = "".join(value)
                    if not _IDENTIFIER.fullmatch(text):
                        _fail()
                    return Token("quoted", text, line)
                if not char.isascii() or len(value) >= MAX_IDENTIFIER:
                    _fail()
                value.append(char)
            _fail()
        if char.isascii() and (char.isalpha() or char == "_"):
            value = []
            while r.peek() and (r.peek().isalnum() or r.peek() in "_$"):
                char = r.take()
                if not char.isascii() or len(value) >= MAX_IDENTIFIER:
                    _fail()
                value.append(char)
            return Token("id", "".join(value).lower(), line)
        if char.isascii() and char.isdigit():
            value = []
            while r.peek() and r.peek().isdigit():
                char = r.take()
                if not char.isascii() or len(value) >= MAX_TYPEMOD_DIGITS:
                    _fail()
                value.append(char)
            if r.peek() and r.peek() in ".eE+-":
                _fail()
            return Token("number", "".join(value), line)
        if char not in "().,;=":
            _fail()
        return Token("punct", r.take(), line)


def _groups(lex):
    """Bound output rows including token overhead, independently of input limits."""
    expect(lex, "(")
    group, depth, size, columns = [], 0, 0, 0
    while True:
        token = lex.pop()
        size += getattr(token, "serialized_bytes", len(token.value.encode("utf-8"))) + 64
        if size > MAX_RECORD or token.kind == "eof":
            _fail()
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            if not depth:
                if group:
                    yield group
                return
            depth -= 1
        if token.kind == "punct" and token.value == "," and not depth:
            columns += 1
            if columns >= MAX_COLUMNS:
                _fail()
            yield group
            group = []
        else:
            group.append(token)


class _RecordLines:
    def __init__(self, stream, check):
        self.stream, self.check, self.size = stream, check, 0

    def __iter__(self):
        return self

    def __next__(self):
        self.check()
        line = self.stream.readline(MAX_RECORD + 1)
        if not line:
            raise StopIteration
        self.size += len(line.encode("utf-8"))
        if self.size > MAX_RECORD:
            _fail()
        return line


def _catalogue(tables, schema_names):
    result = {}
    for name, table in tables.items():
        published = (schema_names[name[0]], name[1])
        if published in result:
            _fail()
        result[published] = (name, table)
    return result


def _empty_null_columns(table):
    inherited = table.get("csv_empty_null_columns", ())
    return [f["name"] for f in table["fields"] if f["name"] != "update_date_time" and
            (f["type"] in {"NUMBER", "INTEGER"} or (f["name"] not in SYSTEM and f["name"] in inherited))]


def read_csv_output(directory, tables, schema_names, emit, check=lambda: None):
    """Emit (source_table_name, decoded_row) in each table's physical order."""
    catalogue = _catalogue(tables, schema_names)
    directory = Path(directory)
    if (directory / "manifest.json").is_symlink():
        _fail()
    with (directory / "manifest.json").open("rb") as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        _fail()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        _fail()
    if (not isinstance(manifest, dict) or manifest.get("format") != "snp-anonymizer-csv-v2"
            or manifest.get("encoding") != "utf-8" or manifest.get("delimiter") != ","
            or manifest.get("null") != r"\N" or manifest.get("leading_backslash") != "doubled"
            or not isinstance(manifest.get("tables"), list)
            or len(manifest["tables"]) != len(catalogue)):
        _fail()
    csv.field_size_limit(MAX_FIELD + 1)
    seen, paths = set(), {"manifest.json"}
    for entry in manifest["tables"]:
        check()
        if not isinstance(entry, dict):
            _fail()
        published = (entry.get("schema"), entry.get("table"))
        if any(not isinstance(v, str) for v in published) or published not in catalogue or published in seen:
            _fail()
        seen.add(published)
        name, table = catalogue[published]
        expected_path = f"{published[0]}/{published[1]}.csv"
        fields = table["fields"]
        expected_fields = [{k: f[k] for k in ("name", "sql_type", "nullable")} for f in fields]
        marked = _empty_null_columns(table)
        if (entry.get("file") != expected_path or entry.get("fields") != expected_fields
                or entry.get("rows") != table["rows"] or entry.get("empty_null_columns", []) != marked):
            _fail()
        paths.add(expected_path)
        file = directory / expected_path
        if file.is_symlink() or not file.resolve().is_relative_to(directory.resolve()):
            _fail()
        with file.open(encoding="utf-8", newline="") as stream:
            lines = _RecordLines(stream, check)
            reader = csv.reader(lines, delimiter=",", strict=True)
            header = next(reader, None)
            lines.size = 0
            if header != [f["name"] for f in fields]:
                _fail()
            null_indexes = {i for i, field in enumerate(fields) if field["name"] in marked}
            for row in reader:
                lines.size = 0
                check()
                if len(row) != len(fields):
                    _fail()
                values = [None if value == r"\N" or (value == "" and i in null_indexes)
                          else value[1:] if value.startswith("\\\\") else value
                          for i, value in enumerate(row)]
                emit(name, values)
    for path in directory.rglob("*"):
        check()
        if path.is_symlink() or (path.is_file() and path.relative_to(directory).as_posix() not in paths):
            _fail()


def _columns(lex):
    result = []
    for group in _groups(lex):
        if len(group) != 1 or group[0].kind not in {"id", "quoted"}:
            _fail()
        result.append(group[0].value)
    return result


def _literal(group):
    # Generated SQL uses only NULL or E-strings, never casts or expressions.
    if len(group) != 1:
        _fail()
    token = group[0]
    if token.is_("NULL"):
        return None
    if token.kind != "string":
        _fail()
    return token.value


def read_sql_output(path, tables, schema_names, emit, *, inserts=False, check=lambda: None):
    catalogue = _catalogue(tables, schema_names)
    if Path(path).is_symlink():
        _fail()
    defined, schemas = set(), set()
    begun = committed = False
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = _OutputReader(stream, check)
        lex = _OutputLexer(reader)
        while lex.peek().kind != "eof":
            check()
            token = lex.pop()
            if committed:
                _fail()
            if token.is_("SET"):
                expect(lex, "STANDARD_CONFORMING_STRINGS")
                expect(lex, "="); expect(lex, "ON"); expect(lex, ";")
            elif token.is_("BEGIN"):
                if begun:
                    _fail()
                expect(lex, ";"); begun = True
            elif token.is_("COMMIT"):
                if not begun:
                    _fail()
                expect(lex, ";"); committed = True
            elif token.is_("CREATE") and not inserts and begun:
                kind = lex.pop()
                if kind.is_("SCHEMA"):
                    expect(lex, "IF"); expect(lex, "NOT"); expect(lex, "EXISTS")
                    schema = identifier(lex)
                    if schema not in schema_names.values() or schema in schemas:
                        _fail()
                    schemas.add(schema); expect(lex, ";")
                    continue
                if not kind.is_("TABLE"):
                    _fail()
                published = qualified(lex)
                if published not in catalogue or published in defined:
                    _fail()
                fields = catalogue[published][1]["fields"]
                ordinal = 0
                for group in _groups(lex):
                    if ordinal >= len(fields) or not group:
                        _fail()
                    field = fields[ordinal]
                    if group[0].value != field["name"]:
                        _fail()
                    tokens = group[1:]
                    nullable = True
                    if len(tokens) >= 2 and tokens[-2].is_("NOT") and tokens[-1].is_("NULL"):
                        tokens = tokens[:-2]; nullable = False
                    declared, _, _ = physical_type(" ".join(t.value for t in tokens))
                    if declared != field["sql_type"] or nullable != field.get("nullable", True):
                        _fail()
                    ordinal += 1
                if ordinal != len(fields):
                    _fail()
                expect(lex, ";"); defined.add(published)
            elif token.is_("COPY") and not inserts and begun:
                published = qualified(lex)
                if published not in defined:
                    _fail()
                name, table = catalogue[published]
                columns = _columns(lex)
                if columns != [f["name"] for f in table["fields"]]:
                    _fail()
                expect(lex, "FROM"); expect(lex, "STDIN"); expect(lex, ";")
                if reader.readline().strip():
                    _fail()
                while True:
                    check()
                    line = reader.readline()
                    if not line:
                        _fail()
                    if line.rstrip("\r\n") == "\\.":
                        break
                    raw = line.rstrip("\r\n").split("\t")
                    if len(raw) != len(columns):
                        _fail()
                    emit(name, [None if value == r"\N" else decode_escape(value) for value in raw])
            elif token.is_("INSERT") and inserts and begun:
                expect(lex, "INTO")
                published = qualified(lex)
                if published not in catalogue:
                    _fail()
                name, table = catalogue[published]
                fields = table["fields"]
                columns = _columns(lex)
                if columns != [f["name"] for f in fields if f["name"] != "sys_id"]:
                    _fail()
                expect(lex, "VALUES")
                while True:
                    values = [_literal(group) for group in _groups(lex)]
                    if len(values) != len(columns):
                        _fail()
                    iterator = iter(values)
                    emit(name, [None if f["name"] == "sys_id" else next(iterator) for f in fields])
                    if lex.peek().value == ",":
                        lex.pop()
                    else:
                        expect(lex, ";"); break
            else:
                _fail()
        if not begun or not committed or (not inserts and (defined != set(catalogue) or schemas != set(schema_names.values()))):
            _fail()

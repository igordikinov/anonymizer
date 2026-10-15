"""Streaming PostgreSQL data subset. Never executes input SQL."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import AnonymizerError, Cancelled

MAX_FIELD = 8 * 1024 * 1024


@dataclass
class Token:
    kind: str
    value: str
    line: int

    def is_(self, word):
        return self.kind == "id" and self.value.upper() == word.upper()


class Reader:
    def __init__(self, stream, check=lambda: None, encoding="utf-8"):
        self.stream, self.check = stream, check
        self.encoding = encoding.replace("-sig", "")
        self.buf, self.pos, self.line = "", 0, 1
        self.read_bytes = 0

    def peek(self, n=1):
        while len(self.buf) - self.pos < n:
            self.check()
            part = self.stream.read(65536)
            if not part:
                break
            self.read_bytes += len(part.encode(self.encoding))
            self.buf = self.buf[self.pos:] + part
            self.pos = 0
        return self.buf[self.pos:self.pos + n]

    def take(self, n=1):
        value = self.peek(n)
        self.pos += len(value)
        self.line += value.count("\n")
        return value

    def readline(self):
        chunks, size = [], 0
        while self.peek():
            end = self.buf.find("\n", self.pos)
            n = end - self.pos + 1 if end >= 0 else len(self.buf) - self.pos
            chunk = self.take(n)
            chunks.append(chunk)
            size += n
            if size > MAX_FIELD:
                raise AnonymizerError(f"Строка {self.line}: запись больше 8 МиБ.")
            if end >= 0:
                break
        return "".join(chunks)


def decode_escape(value, encoding="utf-8"):
    """COPY text and E-string escapes, including octal and hex."""
    result, pos = bytearray(), 0
    for match in re.finditer(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", value, flags=re.S):
        result.extend(value[pos:match.start()].encode(encoding))
        s = match.group(1)
        if s.startswith("x"):
            result.append(int(s[1:], 16))
        elif s.isdigit():
            result.append(int(s, 8) & 255)
        elif s.startswith(("u", "U")) and len(s) in {5, 9}:
            result.extend(chr(int(s[1:], 16)).encode(encoding))
        else:
            result.extend({"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}.get(s, s).encode(encoding))
        pos = match.end()
    result.extend(value[pos:].encode(encoding))
    return result.decode(encoding)


class Lexer:
    def __init__(self, reader):
        self.r, self.saved = reader, None

    def peek(self):
        if self.saved is None:
            self.saved = self.next()
        return self.saved

    def pop(self):
        if self.saved is not None:
            t, self.saved = self.saved, None
            return t
        return self.next()

    def next(self):
        r = self.r
        while r.peek():
            c = r.peek()
            if c.isspace() or c == "\ufeff":
                r.take()
            elif r.peek(2) == "--":
                r.readline()
            elif r.peek(2) == "/*":
                r.take(2)
                depth = 1
                while depth:
                    p = r.peek(2)
                    if not p:
                        self.fail("Незавершённый комментарий")
                    if p == "/*":
                        depth += 1
                        r.take(2)
                    elif p == "*/":
                        depth -= 1
                        r.take(2)
                    else:
                        r.take()
            else:
                break
        line, c = r.line, r.peek()
        if not c:
            return Token("eof", "", line)
        if c == "\\":
            command = r.readline().strip().split(maxsplit=1)[0]
            if command not in {"\\restrict", "\\unrestrict"}:
                self.fail("Неподдерживаемая команда psql")
            return self.next()
        escaped = c in "eE" and r.peek(2)[1:] == "'"
        if escaped:
            r.take()
            c = "'"
        if c in {"'", '"'}:
            quote = r.take()
            result, size = [], 0
            while r.peek():
                char = r.take()
                if char == quote:
                    if r.peek() == quote:
                        r.take()
                        result.append(quote)
                    else:
                        return Token("string" if quote == "'" else "quoted", decode_escape("".join(result), r.encoding) if escaped else "".join(result), line)
                elif char == "\\" and escaped:
                    result.extend([char, r.take()])
                else:
                    result.append(char)
                size += 1
                if size > MAX_FIELD:
                    self.fail("Поле больше 8 МиБ")
            self.fail("Незавершённая строка")
        if c == "$":
            prefix = r.peek(128)
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", prefix)
            if match:
                tag = match[0]
                r.take(len(tag))
                result = []
                while r.peek(len(tag)) != tag:
                    if not r.peek():
                        self.fail("Незавершённая dollar-строка")
                    result.append(r.take())
                    if len(result) > MAX_FIELD:
                        self.fail("Поле больше 8 МиБ")
                r.take(len(tag))
                return Token("string", "".join(result), line)
        if c.isalpha() or c == "_":
            result = []
            while r.peek() and (r.peek().isalnum() or r.peek() in "_$"):
                result.append(r.take())
            return Token("id", "".join(result).lower(), line)
        if c.isdigit():
            result = []
            while r.peek() and (r.peek().isdigit() or r.peek() in ".eE+-"):
                result.append(r.take())
            return Token("number", "".join(result), line)
        return Token("punct", r.take(), line)

    def fail(self, reason):
        raise AnonymizerError(f"SQL, строка {self.r.line}: {reason}.")


def identifier(lexer):
    t = lexer.pop()
    if t.kind not in {"id", "quoted"}:
        lexer.fail("Ожидалось имя объекта")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9$]*", t.value):
        lexer.fail("Имена объектов с пробелами/спецсимволами не поддерживаются")
    return t.value


def qualified(lexer, schema="public"):
    first = identifier(lexer)
    if lexer.peek().value == ".":
        lexer.pop()
        return first, identifier(lexer)
    return schema, first


def expect(lexer, value):
    t = lexer.pop()
    if t.value.upper() != value.upper():
        lexer.fail(f"Ожидался {value}")


def comma_groups(lexer):
    """Tokens inside (...) grouped by top-level commas."""
    expect(lexer, "(")
    group, depth = [], 0
    while True:
        t = lexer.pop()
        if t.kind == "eof":
            lexer.fail("Незавершённый список")
        if t.value == "(" and t.kind == "punct":
            depth += 1
        elif t.value == ")" and t.kind == "punct":
            if not depth:
                if group:
                    yield group
                return
            depth -= 1
        if t.value == "," and t.kind == "punct" and not depth:
            yield group
            group = []
        else:
            group.append(t)


def literal(tokens, lexer):
    if not tokens:
        lexer.fail("Пустой литерал")
    # Optional typed literal/cast; expressions are never evaluated.
    if tokens[0].is_("DATE") or tokens[0].is_("TIMESTAMP") or tokens[0].is_("TIME"):
        tokens = tokens[1:]
    cast = next((i for i in range(len(tokens)-1) if tokens[i].value == ":" and tokens[i+1].value == ":"), None)
    if cast is not None:
        suffix = tokens[cast+2:]
        if not suffix or any(t.kind not in {"id", "quoted"} for t in suffix):
            lexer.fail("Неподдерживаемый cast")
        tokens = tokens[:cast]
    if len(tokens) == 2 and tokens[0].value in {"-", "+"} and tokens[1].kind == "number":
        return tokens[0].value + tokens[1].value
    if len(tokens) != 1:
        lexer.fail("INSERT поддерживает только литералы, без выражений")
    t = tokens[0]
    if t.is_("NULL"):
        return None
    if t.kind in {"string", "number"}:
        return t.value
    if t.is_("TRUE") or t.is_("FALSE"):
        return t.value
    lexer.fail("INSERT поддерживает только литералы")


def parse_sql(path: Path, store, check=lambda: None, encoding="utf-8-sig", progress=lambda n: None, skip_error_tables=False):
    """Store interface: define, columns, add_row, ensure, skipped, drop_table."""
    schema = "public"
    with path.open(encoding=encoding, newline="") as stream:
        reader = Reader(stream, check, encoding)
        lex = Lexer(reader)
        while lex.peek().kind != "eof":
            check()
            t = lex.pop()
            if t.value == ";":
                continue
            if t.is_("CREATE"):
                kind = lex.pop()
                if kind.is_("UNLOGGED"):
                    kind = lex.pop()
                if kind.is_("TABLE"):
                    name = None
                    try:
                        if lex.peek().is_("IF"):
                            expect(lex, "IF"); expect(lex, "NOT"); expect(lex, "EXISTS")
                        name = qualified(lex, schema)
                        fields, constraints, fk_groups, key_groups = [], [], [], []
                        for group in comma_groups(lex):
                            if group[0].is_("CONSTRAINT") or group[0].is_("PRIMARY") or group[0].is_("UNIQUE") or group[0].is_("FOREIGN") or group[0].is_("CHECK"):
                                constraints.append(group)
                                continue
                            column = group[0].value
                            type_tokens = []
                            for token in group[1:]:
                                if token.kind == "id" and token.value.upper() in {"DEFAULT", "NOT", "NULL", "COLLATE", "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "REFERENCES", "GENERATED"}:
                                    break
                                type_tokens.append(token.value)
                            field = {"name": column, "sql_type": " ".join(type_tokens), "nullable": not any(group[i].is_("NOT") and group[i+1].is_("NULL") for i in range(len(group)-1))}
                            key = apply_column_constraints(field, group, name[0], lex)
                            fields.append(field)
                            if key:
                                key_groups.append(key)
                        for constraint in constraints:
                            fks, keys = apply_table_constraint(fields, constraint, name[0], lex)
                            fk_groups.extend(fks)
                            key_groups.extend(keys)
                        store.define(name, fields, fk_groups, key_groups)
                    except Cancelled:
                        raise
                    except AnonymizerError as error:
                        if not skip_error_tables:
                            raise
                        if name is not None:
                            store.drop_table(name, str(error))
                        else:
                            store.dropped_tables.append((f"{schema}.<неразобранная таблица>", str(error)))
                        skip_statement(lex)
                        continue
                else:
                    store.skipped["create_" + kind.value] = store.skipped.get("create_" + kind.value, 0) + 1
                skip_statement(lex)
            elif t.is_("COPY"):
                name = qualified(lex, schema)
                try:
                    full_columns = [g[0].value for g in comma_groups(lex)] if lex.peek().value == "(" else store.columns(name)
                    expect(lex, "FROM"); expect(lex, "STDIN"); expect(lex, ";")
                    if reader.readline().strip():
                        lex.fail("Данные COPY должны начинаться со следующей строки")
                    columns = store.ensure(name, full_columns)
                    kept_indexes = [i for i, col in enumerate(full_columns) if col in set(columns)]
                    while True:
                        check()
                        line = reader.readline()
                        if not line:
                            lex.fail("Незавершённый COPY")
                        if line.rstrip("\r\n") == "\\.":
                            break
                        raw = line.rstrip("\r\n").split("\t")
                        if len(raw) != len(full_columns):
                            lex.fail("Число полей COPY не совпадает со списком колонок")
                        values = [None if raw[i] == r"\N" else decode_escape(raw[i], reader.encoding) for i in kept_indexes]
                        store.add_row(name, columns, values)
                except Cancelled:
                    raise
                except AnonymizerError as error:
                    if not skip_error_tables:
                        raise
                    store.drop_table(name, str(error))
                    while True:  # ресинк до конца тела COPY
                        line = reader.readline()
                        if not line or line.rstrip("\r\n") == "\\.": break
                progress(reader.read_bytes)
            elif t.is_("INSERT"):
                expect(lex, "INTO")
                name = qualified(lex, schema)
                try:
                    full_columns = [g[0].value for g in comma_groups(lex)] if lex.peek().value == "(" else store.columns(name)
                    expect(lex, "VALUES")
                    columns = store.ensure(name, full_columns)
                    kept_indexes = [i for i, col in enumerate(full_columns) if col in set(columns)]
                    while True:
                        row = [literal(g, lex) for g in comma_groups(lex)]
                        if len(row) != len(full_columns):
                            lex.fail("Число значений INSERT не совпадает с колонками")
                        store.add_row(name, columns, [row[i] for i in kept_indexes])
                        if store.count % 1000 == 0:
                            progress(reader.read_bytes)
                        if lex.peek().value == ",":
                            lex.pop()
                        else:
                            expect(lex, ";")
                            break
                except Cancelled:
                    raise
                except AnonymizerError as error:
                    if not skip_error_tables:
                        raise
                    store.drop_table(name, str(error))
                    skip_statement(lex)  # ресинк до конца инструкции
                progress(reader.read_bytes)
            elif t.is_("SET"):
                group = skip_statement(lex, collect=True)
                if group and group[0].is_("STANDARD_CONFORMING_STRINGS") and any(v.value.lower() in {"off", "false", "0"} for v in group[1:]):
                    lex.fail("standard_conforming_strings=off не поддерживается; используйте стандартный pg_dump")
                if group and group[0].is_("SEARCH_PATH"):
                    values = [v.value for v in group[1:] if v.kind in {"id", "quoted", "string"} and v.value not in {"to", "pg_catalog"}]
                    if values:
                        schema = values[0]
            elif t.kind == "id" and t.value in {"alter", "select", "comment", "grant", "revoke", "begin", "commit", "end"}:
                store.skipped[t.value] = store.skipped.get(t.value, 0) + 1
                if t.is_("ALTER"):
                    group = skip_statement(lex, collect=True)
                    if group and group[0].is_("TABLE"):
                        alter_constraint(store, group[1:], schema, lex)
                else:
                    skip_statement(lex)
            else:
                lex.fail("Неподдерживаемая SQL-конструкция; нужен pg_dump с COPY или INSERT")
        progress(reader.read_bytes)


def skip_statement(lexer, collect=False):
    tokens = []
    while True:
        t = lexer.pop()
        if t.kind == "eof":
            lexer.fail("SQL-конструкция без завершающей точки с запятой")
        if t.kind == "punct" and t.value == ";":
            return tokens
        if collect:
            tokens.append(t)


def token_columns(tokens, start, lexer):
    """Column list starting at '('; returns (columns, index of closing paren)."""
    if start >= len(tokens) or tokens[start].value != "(":
        lexer.fail("Неподдерживаемое описание ключа")
    columns = []
    for pos in range(start + 1, len(tokens)):
        token = tokens[pos]
        if token.value == ")":
            return columns, pos
        if token.value != ",":
            if token.kind not in {"id", "quoted"}:
                lexer.fail("Неподдерживаемый ключ")
            columns.append(token.value)
    lexer.fail("Незавершённое описание ключа")


def reference(tokens, schema, lexer):
    pos = next((i for i, token in enumerate(tokens) if token.is_("REFERENCES")), None)
    if pos is None:
        lexer.fail("FOREIGN KEY без REFERENCES не поддерживается")
    pos += 1
    table = tokens[pos].value
    pos += 1
    if pos < len(tokens) and tokens[pos].value == ".":
        schema, table = table, tokens[pos+1].value
        pos += 2
    columns, end = token_columns(tokens, pos, lexer)
    # Tail options: ON DELETE/UPDATE, DEFERRABLE, NOT VALID are accepted and
    # ignored; only MATCH participates in validation semantics.
    for i in range(end + 1, len(tokens)):
        if tokens[i].is_("MATCH"):
            if i + 1 >= len(tokens) or tokens[i+1].value.lower() not in {"simple", "full", "partial"}:
                lexer.fail("Неподдерживаемое уточнение MATCH")
            if tokens[i+1].value.lower() != "simple":
                lexer.fail("MATCH FULL/PARTIAL не поддерживается; переизвлеките данные или уберите MATCH")
    return schema, table, columns


def parse_nulls_tail(tokens, start, lexer):
    """Positional scan of a key-constraint tail: NULLS [NOT] DISTINCT, INCLUDE."""
    for i in range(start, len(tokens)):
        if tokens[i].is_("NULLS"):
            if i + 2 < len(tokens) and tokens[i+1].is_("NOT") and tokens[i+2].is_("DISTINCT"):
                return "not distinct"
            if i + 1 < len(tokens) and tokens[i+1].is_("DISTINCT"):
                return "distinct"
            lexer.fail("Неподдерживаемое уточнение NULLS")
        if tokens[i].is_("INCLUDE"):
            lexer.fail("UNIQUE INCLUDE не поддерживается; переизвлеките данные без INCLUDE")
    return "distinct"


def apply_column_constraints(field, tokens, schema, lexer):
    """Applies flags; returns a single-column key group descriptor or None.

    A column can be both keyed and referencing (e.g. UNIQUE REFERENCES):
    the reference is processed even when a key group is returned.
    """
    key = None
    primary = next((i for i, t in enumerate(tokens) if t.is_("PRIMARY")), None)
    unique = next((i for i, t in enumerate(tokens) if t.is_("UNIQUE")), None)
    if primary is not None:
        field.update(keys=["PK"], source_pk=True, nullable=False)
        if parse_nulls_tail(tokens, primary + 1, lexer) != "distinct":
            lexer.fail("PRIMARY KEY не принимает NULLS NOT DISTINCT")
        key = {"columns": [field["name"]], "kind": "pk", "nulls": "distinct"}
    elif unique is not None:
        field.update(keys=["UK"])
        key = {"columns": [field["name"]], "kind": "unique", "nulls": parse_nulls_tail(tokens, unique + 1, lexer)}
    if any(t.is_("REFERENCES") for t in tokens):
        target_schema, target_table, columns = reference(tokens, schema, lexer)
        if len(columns) != 1:
            lexer.fail("Неподдерживаемая ссылка колонки")
        field["fk"] = {"schema": target_schema, "table": target_table, "field": columns[0]}
    return key


def apply_table_constraint(fields, tokens, schema, lexer):
    """Mutates field flags; returns (composite FK groups, key groups)."""
    if tokens and tokens[0].is_("CONSTRAINT"):
        tokens = tokens[2:]
    bycol = {f["name"]: f for f in fields}
    if not tokens:
        return [], []
    if tokens[0].is_("CHECK"):
        # CHECK predicates can contain customer literals; export does not retain them.
        return [], []
    if tokens[0].is_("PRIMARY") or tokens[0].is_("UNIQUE"):
        start = 2 if tokens[0].is_("PRIMARY") else 1
        columns, end = token_columns(tokens, start, lexer)
        kind = "pk" if tokens[0].is_("PRIMARY") else "unique"
        nulls = parse_nulls_tail(tokens, end + 1, lexer)
        if kind == "pk" and nulls != "distinct":
            lexer.fail("PRIMARY KEY не принимает NULLS NOT DISTINCT")
        for col in columns:
            if col not in bycol:
                lexer.fail("Ключ ссылается на отсутствующую колонку")
            f = bycol[col]
            f["keys"] = sorted(set(f.get("keys", [])) | {kind.upper()})
            if kind == "pk":
                f.update(source_pk=True, nullable=False)
        return [], [{"columns": columns, "kind": kind, "nulls": nulls}]
    if tokens[0].is_("FOREIGN"):
        columns = token_columns(tokens, 2, lexer)[0]
        target_schema, target_table, targets = reference(tokens, schema, lexer)
        if len(columns) != len(targets):
            lexer.fail("Несовпадающие колонки FOREIGN KEY")
        for col, key in zip(columns, targets):
            if col not in bycol:
                lexer.fail("Ссылка на отсутствующую колонку")
            # A column keeps its own column-level REFERENCES; a table-level group
            # must not silently replace it (both constraints coexist in PostgreSQL).
            # enrich() pre-sets fk=None, so only a real reference blocks the write.
            if not bycol[col].get("fk"):
                bycol[col]["fk"] = {"schema": target_schema, "table": target_table, "field": key}
        return [{"columns": columns, "target_schema": target_schema, "target_table": target_table,
                 "target_columns": targets, "match": "simple"}], []
    return [], []


def alter_constraint(store, tokens, default_schema, lexer):
    if tokens and tokens[0].is_("ONLY"):
        tokens = tokens[1:]
    if not tokens:
        return
    schema, table, pos = default_schema, tokens[0].value, 1
    if len(tokens) > 2 and tokens[1].value == ".":
        schema, table, pos = table, tokens[2].value, 3
    if (schema, table) not in store.tables:
        return
    if pos < len(tokens) and tokens[pos].is_("ADD"):
        table_entry = store.tables[(schema, table)]
        fks, keys = apply_table_constraint(table_entry["fields"], tokens[pos+1:], schema, lexer)
        for group in fks:
            if group not in table_entry["fk_groups"]:
                table_entry["fk_groups"].append(group)
        for group in keys:
            if group not in table_entry["key_groups"]:
                table_entry["key_groups"].append(group)

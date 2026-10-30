"""Bounded, non-executing OOXML import. Numeric XML lexemes never pass through float."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
import struct
import tempfile
import zipfile
from xml.parsers import expat

from .errors import AnonymizerError, Cancelled
from .generic_input import InputLimitError
from .metadata import SYSTEM
from .sql_reader import MAX_FIELD
from .validation import validate_value

MAX_UNCOMPRESSED = 4 * 1024**3
MAX_METADATA = 8 * 1024**2
MAX_DIRECTORY = 8 * 1024**2
MAX_ENTRIES = 10000
MAX_CACHE = 8 * 1024**2
CHUNK = 65536
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"
DAY_US = 86400 * 1000000


class XLSXLimitError(AnonymizerError):
    """A resource violation is a dataset-wide failure, including skip mode."""


def _fail(message="некорректная структура книги"):
    raise AnonymizerError("XLSX: " + message + ".")


def _limit():
    raise XLSXLimitError("XLSX: превышен лимит размера или сложности книги.")


def _local(name):
    return name.rsplit("|", 1)[-1]


def _decode(value):
    value = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m[1], 16)), value)
    try:
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        _fail("некорректный текст ячейки")


class _Package:
    def __init__(self, path, check):
        self.check, self.used = check, 0
        # zipfile materializes its central directory: bound it BEFORE constructing it.
        with path.open("rb") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            # Include the ZIP64 locator even with the maximum 65535-byte comment.
            stream.seek(max(0, length - 65577))
            tail = stream.read(65577)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            _fail("файл не является корректной книгой XLSX")
        # zipfile follows this locator even when ordinary EOCD values are not
        # sentinel values. Reject before ZipFile can materialize that catalogue.
        if offset >= 20 and tail[offset - 20:offset - 16] == b"PK\x06\x07":
            _fail("ZIP64-каталог не поддерживается")
        eocd = struct.unpack_from("<4s4H2IH", tail, offset)
        if eocd[1] or eocd[2] or eocd[3] != eocd[4]:
            _fail("многотомный архив не поддерживается")
        if eocd[4] > MAX_ENTRIES or eocd[5] > MAX_DIRECTORY:
            _limit()
        self.archive = zipfile.ZipFile(path)
        try:
            infos = self.archive.infolist()
            if len(infos) > MAX_ENTRIES:
                _limit()
            self.parts = {}
            total = 0
            for info in infos:
                check()
                name = info.filename
                if ("\\" in name or name.startswith("/") or ":" in name
                        or ".." in PurePosixPath(name).parts or name in self.parts):
                    _fail("небезопасные или повторяющиеся части книги")
                if info.flag_bits & 1:
                    _fail("защищённые книги не поддерживаются")
                total += info.file_size
                if total > MAX_UNCOMPRESSED:
                    _limit()
                self.parts[name] = info
                lowered = name.lower()
                if ("vbaproject" in lowered or "/externallinks/" in lowered
                        or lowered.endswith(".bin")):
                    _fail("макросы и внешние ссылки не поддерживаются")
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def xml(self, name, start=lambda n, a: None, end=lambda n: None,
            text=lambda t: None, *, metadata=False):
        info = self.parts.get(name)
        if info is None:
            _fail("отсутствует обязательная часть книги")
        if metadata and info.file_size > MAX_METADATA:
            _limit()
        parser = expat.ParserCreate(namespace_separator="|")
        depth = 0
        consumed = 0

        def opening(tag, attrs):
            nonlocal depth
            depth += 1
            if depth > 128:
                _limit()
            start(tag, attrs)

        def closing(tag):
            nonlocal depth
            end(tag)
            depth -= 1

        def forbidden(*args):
            _fail("DTD, сущности и внешние обращения запрещены")

        parser.StartElementHandler = opening
        parser.EndElementHandler = closing
        parser.CharacterDataHandler = text
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = forbidden
        with self.archive.open(info) as stream:
            while True:
                self.check()
                block = stream.read(CHUNK)
                if not block:
                    break
                consumed += len(block)
                self.used += len(block)
                if self.used > MAX_UNCOMPRESSED or (metadata and consumed > MAX_METADATA):
                    _limit()
                parser.Parse(block, False)
                # Expat can buffer an unfinished token, e.g. a giant attribute/comment.
                if consumed - parser.CurrentByteIndex > MAX_FIELD:
                    _limit()
            parser.Parse(b"", True)
        if consumed != info.file_size:
            _fail("размер части книги не совпал с заявленным")


class _Strings:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("CREATE TABLE strings(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        self.cache, self.bytes, self.count = OrderedDict(), 0, 0

    def close(self):
        self.db.close()

    def add(self, value):
        self.db.execute("INSERT INTO strings VALUES (?,?)", (self.count, value))
        self.count += 1

    def get(self, index):
        if not re.fullmatch(r"[0-9]{1,10}", index):
            _fail("некорректная ссылка на текст ячейки")
        index = int(index)
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index][0]
        row = self.db.execute("SELECT value FROM strings WHERE id=?", (index,)).fetchone()
        if row is None:
            _fail("текст ячейки не найден")
        value = row[0]
        cost = len(value) * 4 + 512
        if cost <= MAX_CACHE:
            while self.bytes + cost > MAX_CACHE:
                _, (_, oldcost) = self.cache.popitem(last=False)
                self.bytes -= oldcost
            self.cache[index] = value, cost
            self.bytes += cost
        return value

    def load(self, package, part):
        pieces, size, buffer_bytes, inside, capture, phonetic = [], 0, 0, False, False, 0

        def start(tag, attrs):
            nonlocal pieces, size, buffer_bytes, inside, capture, phonetic
            if tag == MAIN + "|si":
                if inside:
                    _fail()
                pieces, size, buffer_bytes, inside = [], 0, 0, True
            elif inside and tag == MAIN + "|rPh":
                phonetic += 1
            elif inside and tag == MAIN + "|t" and not phonetic:
                capture = True

        def text(value):
            nonlocal size, buffer_bytes
            if capture:
                size += len(value.encode("utf-8"))
                buffer_bytes += len(value) * 4 + 64
                if size > MAX_FIELD or buffer_bytes > MAX_FIELD * 5:
                    _limit()
                pieces.append(value)

        def end(tag):
            nonlocal inside, capture, phonetic
            if tag == MAIN + "|t":
                capture = False
            elif tag == MAIN + "|rPh":
                phonetic -= 1
            elif tag == MAIN + "|si":
                self.add(_decode("".join(pieces)))
                inside = False

        package.xml(part, start, end, text)
        self.db.commit()
        # The completed SST is immutable. Keep one read snapshot for all
        # lookups rather than reopening a Windows file-lock transaction per cell.
        self.db.execute("BEGIN")


def _date_value(raw, kind, epoch1904):
    try:
        with localcontext() as context:
            context.prec = max(50, min(len(raw) + 20, MAX_FIELD + 20))
            serial = Decimal(raw)
            if (not serial.is_finite() or serial.copy_abs() > 4000000
                    or serial.as_tuple().exponent < -100):
                raise ValueError()
            micros = serial * DAY_US
            if micros != micros.to_integral_value():
                raise ValueError()
            micros = int(micros)
        if kind == "TIME":
            if not 0 <= micros < DAY_US:
                raise ValueError()
            return (datetime(2000, 1, 1) + timedelta(microseconds=micros)).time().isoformat()
        days, remainder = divmod(micros, DAY_US)
        if kind == "DATE" and remainder:
            raise ValueError()
        if epoch1904:
            origin = datetime(1904, 1, 1)
        else:
            # Excel's fictitious 1900-02-29 must never silently become another date.
            if days == 60:
                raise ValueError()
            origin = datetime(1899, 12, 30)
            if 0 < days < 60:
                days += 1
        result = origin + timedelta(days=days, microseconds=remainder)
        return result.date().isoformat() if kind == "DATE" else result.isoformat()
    except (ValueError, DecimalException, OverflowError):
        _fail("числовая дата Excel некорректна или требует потери точности")


def _relationships(package, part, base):
    result = {}
    def start(tag, attrs):
        if tag != PKGREL + "|Relationship":
            return
        if attrs.get("TargetMode", "Internal") != "Internal":
            _fail("внешние ссылки не поддерживаются")
        rid, target, kind = (attrs.get(k, "") for k in ("Id", "Target", "Type"))
        if not rid or rid in result or len(rid) > 256:
            _fail()
        if "\\" in target or ":" in target or "?" in target or "#" in target:
            _fail("небезопасная ссылка на часть книги")
        resolved = posixpath.normpath(posixpath.join(base, target.lstrip("/"))) if not target.startswith("/") else posixpath.normpath(target.lstrip("/"))
        if resolved.startswith("../") or resolved == ".." or resolved not in package.parts:
            _fail("некорректная ссылка на часть книги")
        result[rid] = kind, resolved
    package.xml(part, start, metadata=True)
    return result


def _styles(package, relationships):
    """Only generic import needs format-based date types; never materialize styles XML."""
    parts = [part for kind, part in relationships.values() if kind == REL + "/styles"]
    if len(parts) > 1:
        _fail("повторяющиеся стили книги")
    if not parts:
        return [None]
    from openpyxl.styles.numbers import BUILTIN_FORMATS, STRIP_RE, is_datetime, is_timedelta_format
    custom, formats = {}, []
    in_cell_xfs = False
    memory = 0

    def number(value):
        if not re.fullmatch(r"[0-9]{1,10}", value):
            _fail("некорректный номер формата ячейки")
        return int(value)

    def start(tag, attrs):
        nonlocal in_cell_xfs, memory
        if tag == MAIN + "|numFmt":
            index = number(attrs.get("numFmtId", ""))
            fmt = attrs.get("formatCode")
            if fmt is None or index in custom:
                _fail("некорректное описание формата ячейки")
            memory += len(fmt) * 4 + 256
            if memory > MAX_METADATA:
                _limit()
            custom[index] = fmt
        elif tag == MAIN + "|cellXfs":
            if in_cell_xfs or formats:
                _fail("некорректная структура стилей ячеек")
            in_cell_xfs = True
        elif tag == MAIN + "|xf" and in_cell_xfs:
            memory += 128
            if memory > MAX_METADATA:
                _limit()
            formats.append(number(attrs.get("numFmtId", "0")))

    def end(tag):
        nonlocal in_cell_xfs
        if tag == MAIN + "|cellXfs":
            in_cell_xfs = False

    package.xml(parts[0], start, end, metadata=True)
    kinds = []
    for index in formats:
        fmt = custom.get(index, BUILTIN_FORMATS.get(index))
        if fmt is None:
            _fail("неизвестный формат ячейки")
        if is_timedelta_format(fmt):
            kinds.append("DURATION")
            continue
        # Ignore literals and escaped characters when deciding date vs time.
        clean = STRIP_RE.sub("", fmt.split(";", 1)[0]).lower()
        clean = re.sub(r"[_\\*].", "", clean)
        kind = is_datetime(clean)
        has_meridiem = bool(re.search(r"am/pm|a/p", clean))
        if has_meridiem:
            kind = "datetime" if kind == "date" else kind or "time"
        # Excel uses an isolated m/mm/mmm/... token for a calendar month.
        # openpyxl calls month-only formats "time" because they lack d/y.
        # Minute formats also contain an hour or second token (h:mm, mm:ss).
        month_tokens = re.sub(r"am/pm|a/p", "", clean)
        if kind == "time" and "m" in month_tokens and not any(token in month_tokens for token in "hs"):
            kind = "datetime" if has_meridiem else "date"
        kinds.append({"date": "DATE", "time": "TIME", "datetime": "DATE_TIME"}.get(kind))
    return kinds or [None]


def _iso_kind(value):
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return "DATE"
        if "T" in value or " " in value:
            datetime.fromisoformat(value)
            return "DATE_TIME"
        time.fromisoformat(value)
        return "TIME"
    except ValueError:
        _fail("некорректная ISO-дата ячейки")


def _read(path, store, check, schema, table):
    name = schema, table
    package = _Package(path, check)
    try:
        if "_rels/.rels" not in package.parts:
            _fail("отсутствуют связи книги")
        root = _relationships(package, "_rels/.rels", "")
        books = [part for kind, part in root.values() if kind == REL + "/officeDocument"]
        if len(books) != 1:
            _fail("не найдена единственная книга XLSX")
        workbook = books[0]
        base, filename = posixpath.split(workbook)
        relation_part = posixpath.join(base, "_rels", filename + ".rels")
        relationships = _relationships(package, relation_part, base)
        sheets, epoch = [], False
        def metadata(tag, attrs):
            nonlocal epoch
            if tag == MAIN + "|workbookProtection":
                if any(attrs.get(key) in {"1", "true"} for key in ("lockStructure", "lockWindows", "lockRevision")) or any("password" in key.lower() and value for key, value in attrs.items()):
                    _fail("защищённые книги не поддерживаются")
            elif tag == MAIN + "|workbookPr":
                flag = attrs.get("date1904", "0")
                if flag not in {"0", "1", "false", "true"}:
                    _fail("неизвестная система дат")
                epoch = flag in {"1", "true"}
            elif tag == MAIN + "|sheet":
                if sheets:
                    _fail("книга должна содержать ровно один лист")
                sheets.append(attrs.get(REL + "|id"))
        package.xml(workbook, metadata, metadata=True)
        if len(sheets) != 1 or sheets[0] not in relationships:
            _fail("книга должна содержать ровно один лист")
        kind, worksheet = relationships[sheets[0]]
        if kind != REL + "/worksheet":
            _fail("в книге нужен лист с таблицей")
        # Check every relationship document, including worksheet hyperlinks, without following them.
        for part in package.parts:
            if part.endswith(".rels") and part not in {"_rels/.rels", relation_part}:
                partbase = posixpath.dirname(posixpath.dirname(part))
                _relationships(package, part, partbase)
        with tempfile.TemporaryDirectory(prefix="snp-xlsx-") as temp:
            strings = _Strings(Path(temp) / "strings.sqlite")
            try:
                shared = [p for k, p in relationships.values() if k == REL + "/sharedStrings"]
                if len(shared) > 1:
                    _fail()
                if shared:
                    strings.load(package, shared[0])
                styles = _styles(package, relationships) if store.bypass_model else None
                _worksheet(package, worksheet, strings, store, name, epoch, check, styles)
            finally:
                strings.close()
    finally:
        package.close()


def _worksheet(package, part, strings, store, name, epoch, check, styles=None):
    generic = store.bypass_model
    header = None
    kept = []
    fields = {}
    row = {}
    row_number, previous_row, previous_col = 0, 0, 0
    cell, cell_type, cell_parts, cell_size = None, None, [], 0
    capture, in_cell, in_row, phonetic = False, False, False, 0
    seen_value, in_inline, seen_inline = False, False, False
    row_bytes, cell_buffer_bytes = 0, 0
    row_style, cell_style = None, None
    column_styles, row_styles = {}, {}

    def style_number(value):
        if not re.fullmatch(r"[0-9]{1,10}", value):
            problem("некорректный номер стиля ячейки")
        index = int(value)
        if index >= len(styles):
            problem("неизвестный стиль ячейки")
        return index

    def problem(message, coordinate=None):
        suffix = f", ячейка {coordinate}" if coordinate else ""
        raise AnonymizerError(f"XLSX {name[1]}{suffix}: {message}.")

    def start(tag, attrs):
        nonlocal row, row_number, previous_col, cell, cell_type, cell_parts, cell_size
        nonlocal capture, in_cell, in_row, row_bytes, phonetic, seen_value, in_inline, seen_inline, cell_buffer_bytes
        nonlocal row_style, cell_style, row_styles
        if tag == MAIN + "|row":
            if in_row:
                problem("вложенные строки запрещены")
            raw = attrs.get("r", str(previous_row + 1))
            if not re.fullmatch(r"[1-9][0-9]{0,6}", raw):
                problem("неверный номер строки")
            row_number = int(raw)
            if row_number <= previous_row or row_number > 1048576:
                problem("неверный порядок строк")
            row, previous_col, row_bytes, in_row = {}, 0, 0, True
            if generic:
                row_style = style_number(attrs["s"]) if "s" in attrs else None
                row_styles = {}
        elif tag == MAIN + "|c":
            if not in_row or in_cell:
                problem("некорректная структура ячеек")
            reference = attrs.get("r", "")
            match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", reference)
            if not match or int(match[2]) != row_number:
                problem("некорректная координата ячейки")
            col = 0
            for character in match[1]:
                col = col * 26 + ord(character) - 64
            if col <= previous_col or col > 16384:
                problem("неверный порядок колонок")
            previous_col = col
            if generic:
                cell_style = style_number(attrs["s"]) if "s" in attrs else (
                    row_style if row_style is not None else column_styles.get(col, 0))
            cell = reference
            cell_type = attrs.get("t", "n")
            if cell_type not in {"n", "s", "inlineStr", "str", "b", "d", "e"}:
                problem("неподдерживаемый тип ячейки", cell)
            if cell_type == "e":
                problem("ячейка содержит ошибку Excel", cell)
            cell_parts, cell_size, cell_buffer_bytes, in_cell, phonetic = [], 0, 0, True, 0
            seen_value, in_inline, seen_inline = False, False, False
        elif tag == MAIN + "|f":
            problem("формулы не поддерживаются", cell)
        elif generic and tag == MAIN + "|col" and "style" in attrs:
            raw_min, raw_max = attrs.get("min", ""), attrs.get("max", "")
            if (not re.fullmatch(r"[1-9][0-9]{0,4}", raw_min)
                    or not re.fullmatch(r"[1-9][0-9]{0,4}", raw_max)):
                problem("некорректный диапазон стиля колонок")
            first, last = int(raw_min), int(raw_max)
            if not 1 <= first <= last <= 16384 or previous_row:
                problem("некорректный диапазон стиля колонок")
            style = style_number(attrs["style"])
            for column in range(first, last + 1):
                column_styles[column] = style
        elif tag == MAIN + "|mergeCell":
            problem("объединённые ячейки не поддерживаются")
        elif tag == MAIN + "|sheetProtection":
            if attrs.get("sheet") in {"1", "true"} or any("password" in key.lower() and value for key, value in attrs.items()):
                problem("защищённые листы не поддерживаются")
        elif in_cell and tag == MAIN + "|rPh":
            phonetic += 1
        elif tag == MAIN + "|is":
            if not in_cell or cell_type != "inlineStr" or seen_inline:
                problem("некорректная структура текста ячейки", cell)
            seen_inline, in_inline = True, True
        elif tag == MAIN + "|v":
            if not in_cell or cell_type == "inlineStr" or seen_value:
                problem("некорректная структура значения ячейки", cell)
            seen_value, capture = True, True
        elif tag == MAIN + "|t" and in_cell and not phonetic:
            if not in_inline:
                problem("некорректная структура текста ячейки", cell)
            capture = True
        elif tag in {MAIN + "|oleObject", MAIN + "|externalReference"}:
            problem("внешние объекты не поддерживаются")

    def text(value):
        nonlocal cell_size, cell_buffer_bytes
        if capture:
            cell_size += len(value.encode("utf-8"))
            cell_buffer_bytes += len(value) * 4 + 64
            if cell_size > MAX_FIELD or cell_buffer_bytes > MAX_FIELD * 5:
                _limit()
            cell_parts.append(value)

    def end(tag):
        nonlocal capture, in_cell, in_row, header, kept, fields, previous_row, row_bytes, phonetic, in_inline
        if tag in {MAIN + "|v", MAIN + "|t"}:
            capture = False
        elif tag == MAIN + "|rPh":
            phonetic -= 1
        elif tag == MAIN + "|is":
            in_inline = False
        elif tag == MAIN + "|c":
            raw = "".join(cell_parts)
            if cell_type == "s":
                value = strings.get(raw)
            elif cell_type in {"inlineStr", "str"}:
                value = _decode(raw)
            else:
                value = raw if cell_parts else None
            if cell_type == "n" and value is not None and not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
                problem("некорректное числовое значение", cell)
            row_bytes += 0 if value is None else len(value.encode("utf-8"))
            if row_bytes > MAX_FIELD:
                _limit()
            row[previous_col] = value, cell_type, cell
            if generic:
                row_styles[previous_col] = styles[cell_style]
            in_cell = False
        elif tag == MAIN + "|row":
            check()
            if row_number == 1:
                if not row:
                    problem("отсутствует заголовок")
                width = max((index for index, (value, _, _) in row.items() if value is not None), default=0)
                if not width:
                    problem("отсутствует заголовок")
                header = []
                for col in range(1, width + 1):
                    value, typ, coordinate = row.get(col, (None, None, None))
                    if typ not in {"s", "inlineStr", "str"} or not value or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9$]{0,62}", value):
                        problem("заголовок должен содержать технические имена колонок", coordinate)
                    header.append(value)
                if len(set(header)) != len(header):
                    problem("повторяющиеся колонки")
                if generic:
                    store.define_input(name, header)
                else:
                    store.define(name, store.model.fields(name[1]), from_model=True)
                selected = set(store.ensure(name, header))
                store.note_input_columns(name, header)
                kept = [(i + 1, col) for i, col in enumerate(header) if col in selected]
                fields = {f["name"]: f for f in store.tables.get(name, {}).get("fields", ())}
            else:
                if header is None:
                    problem("первая строка должна содержать заголовок")
                # Blank physical rows and formatting-only cells are not dataset rows.
                if any(v is not None for v, _, _ in row.values()):
                    if any(index > len(header) and value is not None for index, (value, _, _) in row.items()):
                        problem("данные за пределами заголовка")
                    values = []
                    native_types = []
                    for index, column in kept:
                        value, typ, coordinate = row.get(index, (None, None, None))
                        field = fields[column]
                        if column in SYSTEM:
                            # Audit values do not participate in type inference
                            # or date/boolean conversion. File-level OOXML
                            # validation and resource limits still apply.
                            values.append(None if value == r"\N" else value)
                            if generic:
                                native_types.append(None)
                            continue
                        if generic:
                            native = {"n": "NUMBER", "b": "BOOLEAN"}.get(typ, "TEXT")
                            if value == r"\N":
                                value = None
                            if value is not None:
                                if typ == "n" and row_styles.get(index):
                                    native = row_styles[index]
                                    if native == "DURATION":
                                        problem("формат длительности не поддерживается", coordinate)
                                    value = _date_value(value, native, epoch)
                                elif typ == "d":
                                    native = _iso_kind(value)
                                elif typ == "b":
                                    if value not in {"0", "1"}:
                                        problem("некорректное логическое значение", coordinate)
                                    value = {"0": "false", "1": "true"}[value]
                            values.append(value)
                            native_types.append(native if value is not None else None)
                            continue
                        if value == r"\N" or (value == "" and field["type"] in {"NUMBER", "INTEGER"} and field.get("nullable", True)):
                            value = None
                        if value is not None:
                            if typ == "n" and field["type"] in {"DATE", "DATE_TIME", "TIME"}:
                                value = _date_value(value, field["type"], epoch)
                            elif typ == "b":
                                if value not in {"0", "1"}:
                                    problem("некорректное логическое значение", coordinate)
                                value = {"0": "false", "1": "true"}[value]
                        try:
                            validate_value(name[1], field, value)
                        except AnonymizerError:
                            problem("значение не соответствует типу или размеру поля", coordinate)
                        values.append(value)
                    if generic:
                        store.observe_input(name, values, native_types=native_types)
                    store.add_row(name, [col for _, col in kept], values)
            previous_row, in_row = row_number, False
    package.xml(part, start, end, text)
    if header is None:
        problem("отсутствует заголовок")
    if generic:
        store.finish_input(name, check)


def parse_xlsx(path, store, check=lambda: None, schema="public", progress=lambda n: None,
               skip_error_tables=False, table=None):
    """Read exactly one table; errors remove all its partial rows only in skip mode."""
    path = Path(path)
    table = table if table is not None else re.sub(r"_\d{12,}$", "", path.stem).lower()
    name = schema, table
    try:
        check()
        _read(path, store, check, schema, table)
    except (Cancelled, XLSXLimitError, InputLimitError):
        raise
    except (AnonymizerError, OSError, ValueError, RuntimeError, NotImplementedError,
            zipfile.BadZipFile, expat.ExpatError, OverflowError) as error:
        reason = str(error) if isinstance(error, AnonymizerError) else "XLSX: книга повреждена или использует неподдерживаемый формат."
        if not skip_error_tables:
            raise AnonymizerError(reason) from None
        store.drop_table(name, reason)
    progress(path.stat().st_size)

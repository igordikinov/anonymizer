"""Optional Excel export of the replacement mapping (old -> new per domain).

The file contains ORIGINAL client values: it is written only to a user-chosen
path outside the published package (pipeline validates the path) and must be
handled like the .snpanon project. Streaming both ways: rows arrive from
SQLite in batches and the workbook is written in write_only mode, so memory
stays bounded. Excel cell and sheet limits are handled explicitly
(UTF-16-safe truncation, control-character cleanup, sheet rollover).
"""
from __future__ import annotations

import re

from .errors import AnonymizerError

HEADER = ("Домен", "Исходное значение", "Замена")
CELL_LIMIT = 32_000
CELL_MARKER = " …[усечено]"
CELL_UTF16_LIMIT = 32_767
ROWS_PER_SHEET = 1_000_000
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\uFFFE\uFFFF]")


def utf16_units(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def cell_value(text):
    """Excel rejects control characters and caps cells in UTF-16 units; make any value safe."""
    clean = ILLEGAL.sub("", text)
    if len(clean) <= CELL_LIMIT and utf16_units(clean) <= CELL_UTF16_LIMIT:
        return clean
    cut = clean[:CELL_LIMIT - len(CELL_MARKER)]
    if utf16_units(cut + CELL_MARKER) > CELL_UTF16_LIMIT:
        # Astral characters count as two units; find the largest safe prefix.
        low, high = 0, len(cut)
        while low < high:
            middle = (low + high + 1) // 2
            if utf16_units(cut[:middle] + CELL_MARKER) <= CELL_UTF16_LIMIT:
                low = middle
            else:
                high = middle - 1
        cut = cut[:low]
    return cut + CELL_MARKER


def write_mapping_xlsx(path, rows, check=lambda: None):
    """rows: iterable of (domain, old, new); returns the number of data rows."""
    try:
        from openpyxl import Workbook
    except ImportError:
        raise AnonymizerError("Библиотека Excel не установлена. Пересоберите приложение или установите openpyxl.") from None
    workbook = Workbook(write_only=True)
    sheets = []
    try:
        sheet, sheet_number, written, total = None, 0, 0, 0
        for domain, old, new in rows:
            check()
            if sheet is None or written >= ROWS_PER_SHEET:
                sheet_number += 1
                sheet = workbook.create_sheet("mapping" if sheet_number == 1 else f"mapping_{sheet_number}")
                sheets.append(sheet)
                sheet.append(HEADER)
                written = 0
            sheet.append((cell_value(domain), cell_value(old), cell_value(new)))
            written += 1
            total += 1
        if sheet is None:
            sheet = workbook.create_sheet("mapping")
            sheets.append(sheet)
            sheet.append(HEADER)
        workbook.save(str(path))
    except AnonymizerError:
        raise
    except OSError as error:
        raise AnonymizerError(f"Не удалось записать файл карты замен ({type(error).__name__}). "
                              "Проверьте путь и свободное место.") from None
    finally:
        # Aborted write_only sheets keep row-writer generators; GC of an open
        # one raises "I/O operation on closed file" unless closed here.
        for sheet in sheets:
            try:
                sheet.close()
            except Exception:
                pass
        try:
            workbook.close()
        except Exception:
            pass  # a failed save leaves nothing to finalise
    return total

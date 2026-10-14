"""ZIP archive as a dataset source: safe extraction into a private temp dir.

The archive may hold CSV files (flat or one level deep, optional
manifest.json) or exactly one .sql dump. Extraction guards against zip-slip
entry names and decompression bombs: entry paths are validated as relative
PurePosixPath members (no backslashes, colons or Windows device names), the
claimed total size is capped, and a byte counter enforces the same cap while
streaming. Only .csv/.sql/.json entries are extracted; anything else only
counts toward the limit. Mixed depths that would silently drop files are
rejected instead of partially processed.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import PurePosixPath

from .errors import AnonymizerError

MAX_UNCOMPRESSED = 4 * 1024**3
DATA_SUFFIXES = {".csv", ".sql", ".json"}
CHUNK = 1024 * 1024
RESERVED = {"aux", "con", "prn", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _safe_name(name):
    """Normalised relative path or None for service entries (junk still counts toward the cap)."""
    if not name or name.endswith("/"):
        return None
    if "\\" in name:
        raise AnonymizerError("Архив содержит небезопасный путь внутри; обработка запрещена.")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or any(":" in part for part in pure.parts):
        raise AnonymizerError("Архив содержит небезопасный путь внутри; обработка запрещена.")
    parts = []
    for part in pure.parts:
        stem = part.split(".", 1)[0].lower()
        if part.startswith(".") or stem in RESERVED:
            return None  # skip dotfiles and Windows device names entirely
        parts.append(part)
    if not parts or "__MACOSX" in pure.parts:
        return None
    return PurePosixPath(*parts)


def extract_dataset(zip_path, dest_dir, check=lambda: None, progress=lambda done: None, max_uncompressed=None):
    """Extracts the dataset from the archive; returns a CSV folder or the .sql file."""
    limit = MAX_UNCOMPRESSED if max_uncompressed is None else max_uncompressed
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as error:
        raise AnonymizerError(f"Файл не является корректным ZIP-архивом ({type(error).__name__}).") from None
    with archive:
        entries, claimed = [], 0
        for entry in archive.infolist():
            check()
            clean = _safe_name(entry.filename)
            if clean is not None:
                entries.append((entry, clean))
            claimed += entry.file_size
        if claimed > limit:
            raise AnonymizerError("Распакованное содержимое архива превышает 4 ГиБ; обработка запрещена.")
        extracted, copied = [], 0
        for entry, clean in entries:
            check()
            if clean.suffix.lower() not in DATA_SUFFIXES:
                continue
            target = dest_dir.joinpath(*clean.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = archive.open(entry)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
                raise AnonymizerError(f"Архив повреждён или использует неподдерживаемое сжатие/шифрование ({type(error).__name__}).") from None
            with source, target.open("wb") as output:
                while True:
                    check()
                    try:
                        chunk = source.read(CHUNK)
                    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
                        raise AnonymizerError(f"Архив повреждён или использует неподдерживаемое сжатие/шифрование ({type(error).__name__}).") from None
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > limit:
                        raise AnonymizerError("Распакованное содержимое архива превышает 4 ГиБ; обработка запрещена.")
                    output.write(chunk)
                    progress(copied)
            if target.stat().st_size != entry.file_size:
                raise AnonymizerError("Реальный размер содержимого архива не совпадает с заявленным; обработка запрещена.")
            extracted.append((clean, target))
        sqls = [target for clean, target in extracted if clean.suffix.lower() == ".sql"]
        csvs = [clean for clean, _ in extracted if clean.suffix.lower() == ".csv"]
        if sqls and csvs:
            raise AnonymizerError("В архиве одновременно SQL-дамп и CSV; оставьте что-то одно.")
        if len(sqls) > 1:
            raise AnonymizerError("В архиве несколько SQL-файлов; нужен ровно один дамп.")
        if sqls:
            return sqls[0]
        if not csvs:
            raise AnonymizerError("В архиве нет CSV или SQL-дампа; нечего обрабатывать.")
        flat = [clean for clean in csvs if len(clean.parts) == 1]
        deep = [clean for clean in csvs if len(clean.parts) > 1]
        if flat and deep:
            # One of the two sets would be silently dropped by a folder choice.
            raise AnonymizerError("CSV лежат и в корне архива, и в подпапках; оставьте один уровень.")
        if flat:
            return dest_dir
        tops = {clean.parts[0] for clean in deep}
        if len(tops) == 1 and all(len(clean.parts) == 2 for clean in deep):
            folder = dest_dir / tops.pop()
            if (dest_dir / "manifest.json").is_file():
                # The manifest governs the parse and must sit beside the CSVs.
                raise AnonymizerError("manifest.json лежит в корне архива, а CSV — в подпапке; положите их вместе.")
            return folder
        raise AnonymizerError("CSV лежат глубже одного уровня папок; распакуйте набор плоско или в одну подпапку.")

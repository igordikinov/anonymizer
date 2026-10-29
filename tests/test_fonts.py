"""Bundled font identity, Unicode coverage and private process registration."""
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import struct
import sys
import tarfile

import pytest

from anonymizer import fonts

ROOT = Path(__file__).resolve().parents[1]


def _tables(data):
    assert data[:4] == b"\0\1\0\0"
    count = struct.unpack_from(">H", data, 4)[0]
    return {tag.decode(): data[offset:offset + length]
            for tag, _, offset, length in (struct.unpack_from(">4sIII", data, 12 + 16 * i) for i in range(count))}


def _glyph(cmap, codepoint):
    """Read BMP Unicode cmap (format 4) without runtime fontTools."""
    count = struct.unpack_from(">H", cmap, 2)[0]
    for index in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", cmap, 4 + index * 8)
        if platform not in {0, 3} or struct.unpack_from(">H", cmap, offset)[0] != 4:
            continue
        table = cmap[offset:]
        segments = struct.unpack_from(">H", table, 6)[0] // 2
        start = 16 + segments * 2
        delta = start + segments * 2
        ranges = delta + segments * 2
        for segment in range(segments):
            end = struct.unpack_from(">H", table, 14 + segment * 2)[0]
            first = struct.unpack_from(">H", table, start + segment * 2)[0]
            if first <= codepoint <= end:
                shift = struct.unpack_from(">h", table, delta + segment * 2)[0]
                relative = struct.unpack_from(">H", table, ranges + segment * 2)[0]
                if not relative:
                    return (codepoint + shift) & 0xffff
                address = ranges + segment * 2 + relative + (codepoint - first) * 2
                glyph = struct.unpack_from(">H", table, address)[0]
                return (glyph + shift) & 0xffff if glyph else 0
    return 0


def test_manrope_matches_supplied_archive_license_weights_and_glyphs():
    resources = files("anonymizer.resources").joinpath("fonts")
    manifest = json.loads(resources.joinpath("manrope-provenance.json").read_text(encoding="utf-8"))
    source = ROOT / manifest["source_archive"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["archive_sha256"]
    assert {entry["name"] for entry in manifest["files"]} == {*fonts.FONT_FILES, "Manrope-OFL.txt"}
    assert manifest["tools"]["fonttools"] == "4.68.0"
    with tarfile.open(source) as archive:
        for entry in manifest["inputs"]:
            content = archive.extractfile(entry["archive_member"]).read()
            assert len(content) == entry["bytes"] and hashlib.sha256(content).hexdigest() == entry["sha256"]
    for entry in manifest["files"]:
        data = resources.joinpath(entry["name"]).read_bytes()
        assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]
        if entry["name"].endswith(".ttf"):
            tables = _tables(data)
            assert struct.unpack_from(">H", tables["OS/2"], 4)[0] == entry["weight"]
            assert all(_glyph(tables["cmap"], ord(char)) for char in manifest["required_characters"])
    assert "SIL OPEN FONT LICENSE Version 1.1" in resources.joinpath("Manrope-OFL.txt").read_text(encoding="utf-8")


def test_open_sans_fallback_keeps_original_upstream_files_and_license():
    resources = files("anonymizer.resources").joinpath("fonts")
    manifest = json.loads(resources.joinpath("provenance.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "https://github.com/googlefonts/opensans"
    assert len(manifest["revision"]) == 40
    assert {row["name"] for row in manifest["files"]} == {*fonts.FALLBACK_FONT_FILES, "OFL.txt"}
    for entry in manifest["files"]:
        content = resources.joinpath(entry["name"]).read_bytes()
        assert len(content) == entry["bytes"] and hashlib.sha256(content).hexdigest() == entry["sha256"]
    assert "SIL OPEN FONT LICENSE Version 1.1" in resources.joinpath("OFL.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="private GDI registration is Windows-only")
def test_manrope_is_available_without_system_installation(tk_root):
    resolved = fonts.resolve_fonts(tk_root)
    assert fonts.register_private_fonts()
    assert resolved == {"body": fonts.GDI_FAMILY, "semibold": fonts.GDI_SEMIBOLD_FAMILY, "bold": fonts.GDI_FAMILY, "bundled": True}
    assert len(fonts._registered) == len(fonts.FONT_FILES) + len(fonts.FALLBACK_FONT_FILES)
    before = list(fonts._registered)
    assert fonts.prepare_gui() and fonts._registered == before


@pytest.mark.skipif(sys.platform != "win32", reason="GDI font selection is Windows-only")
def test_gdi_selects_bundled_static_weights_not_system_variable(tk_root):
    evidence = fonts.selected_font_evidence()
    assert {role: row["weight"] for role, row in evidence.items()} == {"body": 400, "semibold": 600, "bold": 700}
    assert all(row["matches_bundled"] for row in evidence.values())


def test_primary_failure_uses_bundled_open_sans_but_is_not_brand_pass(tk_root, monkeypatch):
    monkeypatch.setattr(fonts, "register_private_fonts", lambda: False)
    monkeypatch.setattr(fonts, "_registered", ["/synthetic/" + name for name in fonts.FALLBACK_FONT_FILES])
    monkeypatch.setattr(fonts.tkfont, "families", lambda root: ("Segoe UI", "Open Sans", "Open Sans SemiBold", "Manrope", "Manrope SemiBold"))
    assert fonts.resolve_fonts(tk_root) == {"body": "Open Sans", "semibold": "Open Sans SemiBold", "bold": "Open Sans", "bundled": False}


def test_system_manrope_cannot_mask_failed_private_registration(tk_root, monkeypatch):
    monkeypatch.setattr(fonts, "register_private_fonts", lambda: False)
    monkeypatch.setattr(fonts, "_registered", [])
    monkeypatch.setattr(fonts.tkfont, "families", lambda root: ("Segoe UI", "Manrope", "Manrope SemiBold"))
    assert fonts.resolve_fonts(tk_root) == {"body": "Segoe UI", "semibold": "Segoe UI", "bold": "Segoe UI", "bundled": False}

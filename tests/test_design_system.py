"""In.Plan design system vendoring: canonical sources, no legacy ad-hoc colors."""
import re
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest

from anonymizer import ds_tokens, gui

CANONICAL_ZIP = Path(r"C:\Git\scg\In.Plan Design System.zip")
VENDORED = ("colors.css", "typography.css", "spacing.css", "base.css", "fonts.css",
            "header-logo-in.png", "logo-in.png", "app-icon.png")


def test_vendored_files_present_and_are_png():
    for name in VENDORED:
        path = files("anonymizer.resources").joinpath(f"ds/{name}")
        assert path.is_file(), name
    for logo in ("header-logo-in.png", "logo-in.png"):
        logo_bytes = files("anonymizer.resources").joinpath(f"ds/{logo}").read_bytes()
        assert logo_bytes.startswith(b"\x89PNG")
    assert not files("anonymizer.resources").joinpath("ds/header-logo.png").is_file()  # полный In PLAN не используется


def test_app_icon_is_white_letters_on_dark_background(tk_root):
    import tkinter as tk

    icon = tk.PhotoImage(master=tk_root, file=str(files("anonymizer.resources").joinpath("ds/app-icon.png")))
    assert icon.get(2, 2) == (0x1B, 0x1B, 0x1B)  # фон композиции — тёмный рельс DS
    whites = sum(
        1 for y in range(0, icon.height(), 4) for x in range(0, icon.width(), 4)
        if icon.get(x, y) == (255, 255, 255)
    )
    assert whites > 0  # белые буквы In. присутствуют на тёмном фоне
    ico = Path(__file__).parent.parent / "packaging" / "app-icon.ico"
    assert ico.read_bytes()[:4] == b"\x00\x00\x01\x00"  # ICO magic


def test_vendored_tokens_match_canonical_archive():
    """Regen-guard (как tokens-check в mrp-cockpit): вендоренные css == каноническому архиву."""
    if not CANONICAL_ZIP.is_file():
        pytest.skip("канонический архив дизайн-системы недоступен на этой машине")
    archive = zipfile.ZipFile(CANONICAL_ZIP)
    for name in ("colors.css", "typography.css", "spacing.css", "base.css", "fonts.css"):
        canonical = archive.read(f"tokens/{name}")
        vendored = files("anonymizer.resources").joinpath(f"ds/{name}").read_bytes()
        assert vendored == canonical, name


def test_palette_carries_inplan_brand():
    assert ds_tokens.BRAND_600 == "#9000ff"  # --scp-accent-primary
    assert ds_tokens.BRAND_700 == "#6f00ce" and ds_tokens.BRAND_800 == "#4f0096"
    assert ds_tokens.BG_CANVAS == "#f5f6f8" and ds_tokens.BG_SURFACE == "#ffffff"
    assert ds_tokens.CONTENT_PRIMARY == "#212529" and ds_tokens.FONT_FAMILY == "Open Sans"


def test_gui_has_no_legacy_adhoc_colors():
    source = Path(gui.__file__).read_text(encoding="utf-8")
    legacy = ["#315ed0", "#254bb0", "#f4f6fa", "#20304b", "#a8b4cf", "#e5eaf3", "#69758a"]
    for color in legacy:
        assert color not in source, color
    # Все цвета gui берутся из ds_tokens, кроме канонического white у Card.TFrame —
    # и тот выражен токеном; прямой ввод hex в gui не допускается.
    assert not re.findall(r'["\']#[0-9a-fA-F]{3,8}["\']', source)

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

    # Создание изображения на общем корне изредка ловит транзиентный TclError
    # (состязание событий после разрушения виджетов предыдущего теста) —
    # инфраструктурный сбой ретраится, ассерты падают строго.
    icon = None
    for attempt in range(3):
        try:
            icon = tk.PhotoImage(master=tk_root, file=str(files("anonymizer.resources").joinpath("ds/app-icon.png")))
            break
        except tk.TclError:
            if attempt == 2:
                raise
            tk_root.update()
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


def test_palette_carries_inplan_product_canon():
    # Канон продукта (scp-front/theme-source-old/core.json + вендор mrp-cockpit):
    # Open Sans, accent.primary #9000ff, background #ffffff/#f5f6f8, content #212529.
    assert ds_tokens.BRAND_600 == "#9000ff"  # --scp-accent-primary
    assert ds_tokens.BRAND_700 == "#6f00ce" and ds_tokens.BRAND_800 == "#4f0096"
    assert ds_tokens.BG_CANVAS == "#f5f6f8" and ds_tokens.BG_SURFACE == "#ffffff"
    assert ds_tokens.TEXT_PRIMARY == "#212529"
    assert ds_tokens.FONT_FAMILY == "Open Sans"
    source = Path(gui.__file__).read_text(encoding="utf-8")
    assert "Manrope" not in source  # Manrope — шрифт презентационного среза DS, не продукта


def test_component_values_match_canonical_sources():
    if not CANONICAL_ZIP.is_file():
        pytest.skip("канонический архив дизайн-системы недоступен на этой машине")
    with zipfile.ZipFile(CANONICAL_ZIP) as archive:
        colors = archive.read("tokens/colors.css").decode("utf-8")
        button = archive.read("components/buttons/Button.jsx").decode("utf-8")
        field = archive.read("components/forms/Input.jsx").decode("utf-8")
        spacing = archive.read("tokens/spacing.css").decode("utf-8")
    for token, value in [("--scp-text-neutral-disable", ds_tokens.BUTTON_TEXT_DISABLED),
                         ("--scp-border-neutral-hover", ds_tokens.BORDER_HOVER),
                         ("--scp-border-neutral-default", ds_tokens.BORDER_NEUTRAL)]:
        assert re.search(re.escape(token) + r"\s*:\s*" + re.escape(value), colors)
    assert "color:var(--scp-text-neutral-disable)" in button
    assert "font-weight:600" in button
    assert f"height:{ds_tokens.CONTROL_HEIGHT}px;font-size:14px" in button
    assert f"border-radius:{ds_tokens.RADIUS_INPUT}px" in field
    assert f".scp-field--md{{height:{ds_tokens.CONTROL_HEIGHT}px}}" in field
    assert f"--scp-radius-button:{ds_tokens.RADIUS_BUTTON}px" in spacing
    assert f"--scp-border-radius-md:{ds_tokens.RADIUS_CARD}px" in spacing


def test_caption_moved_to_top_bar_with_halved_font(tk_root):
    from anonymizer.gui import Application

    for child in tk_root.winfo_children():
        child.destroy()
    app = Application(tk_root)
    tk_root.update()
    assert app.font_caption[1] == -11  # 22 / 2
    assert app.caption.cget("text") == "SNP Anonymizer"
    assert app.caption.cget("background") == ds_tokens.RAIL == "#1b1b1b"


def test_gui_has_no_legacy_adhoc_colors():
    source = Path(gui.__file__).read_text(encoding="utf-8")
    legacy = ["#315ed0", "#254bb0", "#f4f6fa", "#20304b", "#a8b4cf", "#e5eaf3", "#69758a"]
    for color in legacy:
        assert color not in source, color
    # Все цвета gui берутся из ds_tokens, кроме канонического white у Card.TFrame —
    # и тот выражен токеном; прямой ввод hex в gui не допускается.
    assert not re.findall(r'["\']#[0-9a-fA-F]{3,8}["\']', source)

"""In.Plan design system vendoring: canonical sources, no legacy ad-hoc colors."""
import re
import hashlib
import tarfile
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest

from anonymizer import ds_tokens, gui

CANONICAL_ZIP = Path(r"C:\Git\scg\In.Plan Design System.zip")
VENDORED = ("colors.css", "typography.css", "spacing.css", "base.css", "fonts.css",
            "header-logo-in.png", "logo-in.png", "app-icon.png", "brand.scss", "brand-palette.ts")


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
    assert icon.get(2, 2) == (0x04, 0x14, 0x3A)  # фон композиции — тёмный рельс DS
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


def test_palette_carries_frontend_brand_canon():
    brand = files("anonymizer.resources").joinpath("ds/brand.scss").read_text(encoding="utf-8")
    assert ds_tokens.BRAND_600 == "#4769ff"
    assert ds_tokens.RAIL == ds_tokens.TEXT_PRIMARY == "#04143a"
    assert ds_tokens.BG_CANVAS == "#f4f6fa" and ds_tokens.BG_SURFACE == "#ffffff"
    assert ds_tokens.FONT_FAMILY == "Manrope"
    assert ds_tokens.FIELD_FONT_SIZE == 12 and ds_tokens.BODY_FONT_SIZE == 14
    assert "--scp-form-field-font: 400 12px/16px 'Manrope'" in brand
    for value in (ds_tokens.BRAND, ds_tokens.BG_CANVAS, ds_tokens.RAIL):
        assert value in brand


def test_primary_interaction_colors_are_documented_desktop_blends():
    primary = tuple(int(ds_tokens.BRAND[i:i+2], 16) for i in (1, 3, 5))
    dark = tuple(int(ds_tokens.RAIL[i:i+2], 16) for i in (1, 3, 5))
    def blend(ratio):
        return "#" + "".join(f"{int(a*(1-ratio)+b*ratio+.5):02x}" for a,b in zip(primary,dark))
    assert ds_tokens.BRAND_HOVER == blend(.15) == "#3d5ce1"
    assert ds_tokens.BRAND_ACTIVE == blend(.30) == "#3350c4"


def test_status_palette_preserves_the_source_semantic_groups():
    source = files("anonymizer.resources").joinpath("ds/brand-palette.ts").read_text(encoding="utf-8")
    foreground = source.split("const FOREGROUND", 1)[1]
    for group, values in ds_tokens.STATUS_BACKGROUNDS.items():
        background_match = re.search(r"\b" + group + r":\s*\[([^]]+)\]", source)
        assert tuple(re.findall(r"#[0-9a-f]{6}", background_match[1])) == values
        foreground_match = re.search(r"\b" + group + r":\s*'([^']+)'", foreground)
        assert foreground_match[1] == ds_tokens.STATUS_FOREGROUNDS[group]
    assert ds_tokens.SUCCESS == "#00576a" and ds_tokens.ERROR == "#c4002a"
    assert ds_tokens.WARNING == "#ff8c00" and ds_tokens.WARNING_TEXT == "#a35a00"


def test_brand_layer_matches_the_supplied_archive():
    path = Path(__file__).resolve().parents[1] / "resources/brand/frontend-brand.tgz"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "904780db9b8a39671939560e013793e043eb76de4be68e640803f282c8af0ce5"
    with tarfile.open(path, "r:gz") as archive:
        for name, source in (
            ("brand.scss", "files/projects/scp-shell/src/app/styles/brand.scss"),
            ("brand-palette.ts", "files/projects/scp-shared/src/lib/common/utils/brand-palette.ts"),
            ("header-logo-in.png", "files/projects/scp-shell/src/assets/images/header-logo-in.png"),
        ):
            assert files("anonymizer.resources").joinpath("ds/" + name).read_bytes() == archive.extractfile("frontend-brand/" + source).read()
        icon = archive.extractfile("frontend-brand/files/projects/scp-shell/src/favicon.ico").read()
        assert (Path(__file__).resolve().parents[1] / "packaging/app-icon.ico").read_bytes() == icon


def test_effective_tokens_have_no_legacy_purple():
    forbidden = {"#9000ff", "#6f00ce", "#4f0096", "#d899ff", "#d8cfde", "#f4e3ff", "#faf3ff"}
    def values(item):
        if isinstance(item, dict):
            for value in item.values():
                yield from values(value)
        elif isinstance(item, (tuple, list)):
            for value in item:
                yield from values(value)
        elif isinstance(item, str):
            yield item.lower()
    for name, value in vars(ds_tokens).items():
        if name.isupper():
            assert not (set(values(value)) & forbidden), name


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
    assert app.font_caption[1] == -app.px(11)  # original logical size, scaled with the window
    assert app.caption.cget("text") == "SNP Anonymizer"
    assert app.caption.cget("background") == ds_tokens.RAIL == "#04143a"


def test_gui_has_no_legacy_adhoc_colors():
    source = Path(gui.__file__).read_text(encoding="utf-8")
    legacy = ["#315ed0", "#254bb0", "#20304b", "#a8b4cf", "#e5eaf3", "#69758a"]
    for color in legacy:
        assert color not in source, color
    # Все цвета gui берутся из ds_tokens, кроме канонического white у Card.TFrame —
    # и тот выражен токеном; прямой ввод hex в gui не допускается.
    assert not re.findall(r'["\']#[0-9a-fA-F]{3,8}["\']', source)

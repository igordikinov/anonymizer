"""In.Plan Design System tokens for the desktop GUI.

Vendored from the canonical archive (C:/Git/scg/In.Plan Design System.zip,
tokens/colors.css + typography.css + spacing.css; light theme). The same
tokens are vendored in mrp-cockpit/web/src/styles/ds-tokens. Do not edit
values by hand — regenerate from the archive; the shipped copies live in
resources/ds/ and tests/test_design_system.py checks them against it.
"""
from __future__ import annotations

# Brand (purple) scale — colors.css
BRAND_300 = "#d899ff"
BRAND_100 = "#f4e3ff"
BRAND_500 = "#af35ff"
BRAND_600 = "#9000ff"   # --scp-accent-primary / button default
BRAND_700 = "#6f00ce"   # hover
BRAND_800 = "#4f0096"   # active
ACCENT_DISABLED = "#d8cfde"

# Backgrounds
BG_CANVAS = "#f5f6f8"       # --scp-bg-canvas / background-secondary
BG_SURFACE = "#ffffff"      # cards, inputs
BG_HOVER = "#eaeaea"        # secondary button hover
BG_DISABLED = "#ebebeb"
RAIL_BACKGROUND = "#1b1b1b"  # scp-shell sidebar--closed (тёмный рельс под белой иконкой In.)

# Content (text)
CONTENT_PRIMARY = "#212529"
CONTENT_SUB = "#444647"
CONTENT_SECONDARY = "#adb0b4"
CONTENT_ON_BRAND = "#ffffff"
CONTENT_DISABLED = "#d9d9d9"

# Borders
BORDER_TERTIARY = "#eaeaea"  # subtle separators
BORDER_NEUTRAL = "#949598"   # default control border
BORDER_BRAND = "#9000ff"     # focused control

# System states
SUCCESS = "#22bb55"
ERROR = "#f22a41"
WARNING = "#ff9a3b"

LINK = "#0d56e2"
SCROLLBAR_THUMB = CONTENT_SECONDARY  # base.css uses --scp-scrollbar-thumb-color; value is not in colors.css, the neutral scale is used

# Typography — typography.css: Open Sans everywhere; body 14/20, title 700 18/28, secondary 400 12/16.
# GUI резолвит семейство: Open Sans при наличии, иначе FONT_FALLBACK; размеры в пикселях
# (отрицательные в Tk): title 22, heading 15, body 14, secondary 12.
FONT_FAMILY = "Open Sans"
FONT_FALLBACK = "Segoe UI"

# Spacing / radii — spacing.css
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24
RADIUS_BUTTON = 4
RADIUS_CARD = 8

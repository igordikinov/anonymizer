"""In.Plan Design System tokens for the desktop GUI.

Source of truth — the same DS bundle the product front-end vendores
(mrp-cockpit/web/src/styles/ds-tokens from C:/Git/scg/In.Plan Design
System.zip): Open Sans, brand #9000ff, surfaces #ffffff/#f5f6f8, corners
4/8/16. Shipped copies live in resources/ds/ and tests/test_design_system.py
checks them against the archive. Do not edit values by hand — regenerate.
"""
from __future__ import annotations

# Brand (purple) scale — colors.css
BRAND_100 = "#f4e3ff"        # tertiary button pressed
BRAND_50 = "#faf3ff"         # tertiary button hover
BRAND_300 = "#d899ff"
BRAND = "#9000ff"            # --scp-accent-primary / --scp-background-action-brand-primary-default
BRAND_HOVER = "#6f00ce"      # --scp-background-action-brand-primary-hover
BRAND_ACTIVE = "#4f0096"     # --scp-background-action-brand-primary-active
BRAND_300 = "#d899ff"
BRAND_600 = BRAND
BRAND_700 = BRAND_HOVER
BRAND_800 = BRAND_ACTIVE
ACCENT_DISABLED = "#d8cfde"

# Surfaces — background-action-brand-* и background-primary/secondary
PAGE = "#ffffff"             # --scp-background-primary (content surface)
BG_CANVAS = "#f5f6f8"        # --scp-background-secondary (page around cards)
BG_SURFACE = "#ffffff"       # cards, inputs
BG_HOVER = "#eaeaea"         # --scp-background-action-brand-secondary-hover
BG_PRESSED = "#dfdfe0"       # --scp-background-action-brand-secondary-active
BG_DISABLED = "#f5f6f8"      # --scp-background-action-*-disable
BAR = "#1b1b1b"              # scp-shell sidebar--closed / top bar (тёмный хром)
RAIL = BAR
HOVER_ON_DARK = "#404040"    # hover элементов на тёмном хроме

# Content (text) — content-* и --scp-main-font-color
TEXT_PRIMARY = "#212529"     # --scp-content-primary
TEXT_HEADING = "#000000"     # --scp-main-font-color (лейблы кнопок stroked)
TEXT_SUB = "#444647"         # hints on surface
TEXT_SECONDARY = "#adb0b4"   # --scp-content-secondary
TEXT_ON_BRAND = "#ffffff"    # --scp-content-constant
TEXT_DISABLED = "#d9d9d9"    # --scp-content-disabled
BUTTON_TEXT_DISABLED = "#bfbfc1"  # Button.jsx stroked / primary disabled text

# Borders — border-neutral-*
BORDER_TERTIARY = "#eaeaea"  # --scp-border-neutral-tertiary (inputs, hairlines)
BORDER_NEUTRAL = "#949598"   # --scp-border-neutral-default (обводка stroked-кнопок)
BORDER_BRAND = "#9000ff"     # focused control
BORDER_HOVER = "#5a5a5c"     # input hover

# System states — system-*
SUCCESS = "#22bb55"
ERROR = "#f22a41"
WARNING = "#ff9a3b"

FONT_FAMILY = "Open Sans"    # --scp-font-display-font-family (self-hosted во фронте)
FONT_FALLBACK = "Segoe UI"

# Spacing / corners — spacing.css и Material corner-small/medium/large
SPACE_S = 8
SPACE_XS = 4
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24
RADIUS_BUTTON = 4
RADIUS_CARD = 8
RADIUS_INPUT = 5             # product Input component
CONTROL_HEIGHT = 40

# Верхняя чёрная полоса: надпись вдвое мельче прежнего титула 22px (запрос владельца).
CAPTION_SIZE = -11

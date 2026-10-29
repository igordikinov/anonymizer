"""Effective In.Plan desktop tokens: frontend-brand over the retained base DS.

Source: resources/brand/frontend-brand.tgz; source SCSS/TS are vendored in
resources/ds. Primary hover/pressed are the agreed desktop adaptation: blend
#4769ff with #04143a by 15%/30%. Neutral geometry remains the base DS contract.
"""
from __future__ import annotations

BRAND_50 = "#e3e9ff"
BRAND_100 = "#c2cfff"
BRAND_300 = "#c8d3ff"
BRAND = "#4769ff"
BRAND_HOVER = "#3d5ce1"
BRAND_ACTIVE = "#3350c4"
BRAND_600 = BRAND
BRAND_700 = BRAND_HOVER
BRAND_800 = BRAND_ACTIVE
ACCENT_DISABLED = "#c9cfda"

PAGE = "#ffffff"
BG_CANVAS = "#f4f6fa"
BG_SURFACE = "#ffffff"
BG_HOVER = "#e3e7ee"
BG_PRESSED = "#c9cfda"
BG_DISABLED = "#f4f6fa"
BAR = "#04143a"
RAIL = BAR
HOVER_ON_DARK = "#0c1f4f"

TEXT_PRIMARY = "#04143a"
TEXT_HEADING = TEXT_PRIMARY
TEXT_SUB = TEXT_PRIMARY
# The web content-secondary override is a light color for dark contexts.
# Desktop subtitle and scrollbar text sit on a light surface, so use the
# foreground-neutral token from brand-palette.ts instead.
TEXT_SECONDARY = TEXT_PRIMARY
TEXT_ON_BRAND = "#ffffff"
TEXT_DISABLED = TEXT_PRIMARY
BUTTON_TEXT_DISABLED = "#bfbfc1"  # base button component, not overridden by brand

BORDER_TERTIARY = "#eaeaea"
BORDER_NEUTRAL = "#949598"
BORDER_BRAND = BRAND
BORDER_HOVER = "#5a5a5c"

STATUS_BACKGROUNDS = {
    "norm": ("#eefbfe", "#d6f5fc", "#b5ecf8"),
    "warning": ("#fff1dc", "#ffd9a3", "#ffb54d"),
    "problem": ("#fde4ec", "#f5b3c6", "#e6779a"),
    "accent": ("#e3e9ff", "#c2cfff", "#91a6ff"),
    "neutral": ("#f4f6fa", "#e3e7ee", "#c9cfda"),
}
STATUS_FOREGROUNDS = {
    "norm": "#00576a", "warning": "#a35a00", "problem": "#c4002a",
    "accent": BRAND, "neutral": TEXT_PRIMARY,
}
SUCCESS = STATUS_FOREGROUNDS["norm"]
ERROR = STATUS_FOREGROUNDS["problem"]
WARNING = "#ff8c00"  # warning icon/accent; text uses WARNING_TEXT
WARNING_TEXT = STATUS_FOREGROUNDS["warning"]

FONT_FAMILY = "Manrope"
FONT_FALLBACK = "Open Sans"
BODY_FONT_SIZE = 14
FIELD_FONT_SIZE = 12
BUTTON_FONT_SIZE = 14
HEADING_FONT_SIZE = 16
HINT_FONT_SIZE = 12

SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24
RADIUS_BUTTON = 4
RADIUS_CARD = 8
RADIUS_INPUT = 5
CONTROL_HEIGHT = 40
CAPTION_SIZE = -11

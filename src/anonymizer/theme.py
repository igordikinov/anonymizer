"""Offline In.Plan ttk theme. Native widgets retain editing and keyboard behavior."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import ds_tokens as ds
from .fonts import resolve_fonts


def _rounded(root, size, radius, fill, border, stroke=1, matte=None,
             outer_border=None, outer_stroke=1):
    """Small transparent nine-slice tile; no raster package or runtime files."""
    image = tk.PhotoImage(master=root, width=size, height=size)
    if matte is not None:
        image.put(matte, to=(0, 0, size, size))

    def inside(x, y, inset):
        r = max(0, radius - inset)
        if x < inset or y < inset or x >= size - inset or y >= size - inset:
            return False
        cx = min(max(x + .5, inset + r), size - inset - r)
        cy = min(max(y + .5, inset + r), size - inset - r)
        return (x + .5 - cx) ** 2 + (y + .5 - cy) ** 2 <= r ** 2

    for y in range(size):
        for x in range(size):
            if inside(x, y, 0):
                color = fill if inside(x, y, stroke) else border
                if outer_border is not None and not inside(x, y, outer_stroke):
                    color = outer_border
                image.put(color, (x, y))
    return image


def install(root):
    """Install once per interpreter and retain every PhotoImage on the root."""
    if hasattr(root, "_ds_theme"):
        return root._ds_theme
    scale = root.winfo_fpixels("1i") / 96
    px = lambda value: max(1, round(value * scale))
    faces = resolve_fonts(root)
    body = (faces["body"], -px(ds.BODY_FONT_SIZE))
    field = (faces["body"], -px(ds.FIELD_FONT_SIZE))
    fonts = {"body": body, "field": field, "caption": (faces["bold"], -px(11), "bold"),
             "heading": (faces["semibold"], -px(ds.HEADING_FONT_SIZE)),
             "secondary": (faces["body"], -px(ds.HINT_FONT_SIZE)),
             "button": (faces["semibold"], -px(ds.BUTTON_FONT_SIZE))}
    style = ttk.Style(root)
    style.theme_use("clam")
    images = []
    image_states = {}

    def element(name, radius, states, height=ds.CONTROL_HEIGHT):
        tiles = []
        for state, fill, border, stroke in states:
            # Primary focus keeps a blue outer edge, then a white inset ring;
            # this remains a distinct outline on the white surrounding surface.
            outer = fill if name == "DS.Primary.TButton" and "focus" in state else None
            tile = _rounded(root, px(height), px(radius), fill, border, px(stroke),
                            outer_border=outer, outer_stroke=px(1))
            images.append(tile)
            tiles.append((state, tile))
        default = tiles[-1][1]
        specs = [(state, tile) for state, tile in tiles[:-1]]
        image_states[name] = tiles
        style.element_create(name, "image", default, *specs,
                             border=(px(radius + 2),) * 4, padding=0, sticky="nswe")

    style.configure("TFrame", background=ds.BG_CANVAS)
    style.configure("Card.TFrame", background=ds.BG_SURFACE)
    # WinTk continually redraws a stretched image frame larger than a Canvas
    # viewport. Keep the white centre native; render the four fixed corner
    # slices with ttk image elements, so scrolling never stretches an image.
    style.configure("RoundedCard.TFrame", background=ds.BG_SURFACE)
    radius = px(ds.RADIUS_CARD)
    tile = _rounded(root, radius * 2, radius, ds.BG_SURFACE, ds.BG_SURFACE, 0, ds.BG_CANVAS)
    images.append(tile)
    for corner, x, y in [("nw", 0, 0), ("ne", radius, 0), ("sw", 0, radius), ("se", radius, radius)]:
        image = tk.PhotoImage(master=root, width=radius, height=radius)
        root.tk.call(str(image), "copy", str(tile), "-from", x, y, x + radius, y + radius)
        images.append(image)
        name = "DS.card." + corner
        style.element_create(name, "image", image)
        style.layout("CardCorner" + corner + ".TLabel", [(name, {"sticky": "nswe"})])
        style.configure("CardCorner" + corner + ".TLabel", background=ds.BG_CANVAS)
    for name, bg, fg, font in [
        ("TLabel", ds.BG_CANVAS, ds.TEXT_PRIMARY, body),
        ("Card.TLabel", ds.BG_SURFACE, ds.TEXT_PRIMARY, body),
        ("Caption.TLabel", ds.RAIL, ds.TEXT_ON_BRAND, fonts["caption"]),
        ("Sub.TLabel", ds.BG_CANVAS, ds.TEXT_SECONDARY, fonts["secondary"]),
        ("Hint.TLabel", ds.BG_SURFACE, ds.TEXT_SUB, fonts["secondary"]),
        ("Heading.TLabel", ds.BG_SURFACE, ds.TEXT_PRIMARY, fonts["heading"]),
    ]:
        style.configure(name, background=bg, foreground=fg, font=font)
    for name, primary in [("TButton", False), ("Secondary.TButton", False), ("Primary.TButton", True)]:
        default = ds.BRAND if primary else ds.BG_SURFACE
        active = ds.BRAND_HOVER if primary else ds.BG_HOVER
        pressed = ds.BRAND_ACTIVE if primary else ds.BG_PRESSED
        border = ds.BRAND if primary else ds.BORDER_NEUTRAL
        image_name = "DS." + name
        element(image_name, ds.RADIUS_BUTTON, [
            ("disabled", ds.BG_DISABLED if primary else ds.BG_SURFACE, ds.BORDER_TERTIARY, 1),
            ("pressed focus", pressed, ds.TEXT_ON_BRAND if primary else ds.BORDER_BRAND, 3 if primary else 2),
            ("active focus", active, ds.TEXT_ON_BRAND if primary else ds.BORDER_BRAND, 3 if primary else 2),
            ("focus", default, ds.TEXT_ON_BRAND if primary else ds.BORDER_BRAND, 3 if primary else 2),
            ("pressed", pressed, pressed if primary else border, 1),
            ("active", active, active if primary else border, 1),
            ("", default, border, 0 if primary else 1)])
        style.layout(name, [(image_name, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"})]})]})])
        style.configure(name, background=default, foreground=ds.TEXT_ON_BRAND if primary else ds.TEXT_HEADING,
                        font=fonts["button"], padding=(px(ds.SPACE_L), px(ds.SPACE_XS)), bordercolor=border)
        style.map(name, background=[("disabled", ds.BG_DISABLED if primary else ds.BG_SURFACE),
                                    ("pressed", pressed), ("active", active)],
                  foreground=[("disabled", ds.BUTTON_TEXT_DISABLED)],
                  bordercolor=[("disabled", ds.BORDER_TERTIARY),
                               ("pressed focus", pressed if primary else ds.BORDER_BRAND),
                               ("active focus", active if primary else ds.BORDER_BRAND),
                               ("focus", ds.BRAND if primary else ds.BORDER_BRAND),
                               ("pressed", pressed if primary else border),
                               ("active", active if primary else border)])
    field_states = [("disabled", ds.BG_DISABLED, ds.BORDER_TERTIARY, 1),
                    ("focus", ds.BG_SURFACE, ds.BORDER_BRAND, 2),
                    ("hover", ds.BG_SURFACE, ds.BORDER_HOVER, 1),
                    ("", ds.BG_SURFACE, ds.BORDER_NEUTRAL, 1)]
    for name, prefix in [("TEntry", "Entry"), ("TCombobox", "Combobox")]:
        image_name = "DS." + name
        element(image_name, ds.RADIUS_INPUT, field_states)
        children = [(prefix + ".padding", {"sticky": "nswe", "children": [
            (prefix + ".textarea", {"sticky": "nswe"})]})]
        if prefix == "Combobox":
            # Keep the native arrow inside the field padding, clear of the
            # rounded border; its clam rectangle must not cover the corners.
            children[0][1]["children"].insert(0, ("Combobox.downarrow", {"side": "right", "sticky": "ns"}))
        style.layout(name, [(image_name, {"sticky": "nswe", "children": children})])
        style.configure(name, font=field, padding=(px(ds.SPACE_S), px(ds.SPACE_XS)),
                        foreground=ds.TEXT_PRIMARY, fieldbackground=ds.BG_SURFACE,
                        background=ds.BG_SURFACE, lightcolor=ds.BG_SURFACE, darkcolor=ds.BG_SURFACE,
                        borderwidth=0, relief="flat",
                        bordercolor=ds.BORDER_NEUTRAL, arrowcolor=ds.TEXT_SUB,
                        arrowsize=px(14), selectbackground=ds.BRAND, selectforeground=ds.TEXT_ON_BRAND)
        style.map(name, bordercolor=[("disabled", ds.BORDER_TERTIARY), ("focus", ds.BORDER_BRAND),
                                    ("hover", ds.BORDER_HOVER)],
                  background=[("disabled", ds.BG_DISABLED), ("pressed", ds.BG_SURFACE),
                              ("active", ds.BG_SURFACE), ("readonly", ds.BG_SURFACE)],
                  lightcolor=[("disabled", ds.BG_DISABLED)], darkcolor=[("disabled", ds.BG_DISABLED)],
                  fieldbackground=[("disabled", ds.BG_DISABLED), ("readonly", ds.BG_SURFACE)],
                  foreground=[("disabled", ds.TEXT_DISABLED)])
    root.option_add("*TCombobox*Listbox.font", field)
    root.option_add("*TCombobox*Listbox.background", ds.BG_SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", ds.TEXT_PRIMARY)
    root.option_add("*TCombobox*Listbox.selectBackground", ds.BRAND)
    root.option_add("*TCombobox*Listbox.selectForeground", ds.TEXT_ON_BRAND)
    root.option_add("*TCombobox*Listbox.highlightColor", ds.BORDER_BRAND)
    root.option_add("*TCombobox*Listbox.highlightBackground", ds.BG_SURFACE)
    style.configure("TCheckbutton", background=ds.BG_SURFACE, foreground=ds.TEXT_PRIMARY, font=body,
                    indicatorbackground=ds.BG_SURFACE, indicatorforeground=ds.BRAND,
                    upperbordercolor=ds.BORDER_NEUTRAL, lowerbordercolor=ds.BORDER_NEUTRAL)
    indicator_border = [("disabled", ds.BORDER_TERTIARY), ("focus", ds.BORDER_BRAND),
                        ("pressed", ds.BRAND_ACTIVE), ("active", ds.BRAND_HOVER), ("selected", ds.BRAND)]
    style.map("TCheckbutton", background=[("active", ds.BG_SURFACE)],
              indicatorbackground=[("disabled", ds.BG_DISABLED), ("pressed", ds.BG_CANVAS),
                                   ("alternate", ds.BRAND_100)],
              indicatorforeground=[("disabled", ds.BUTTON_TEXT_DISABLED), ("pressed", ds.BRAND_ACTIVE),
                                   ("active", ds.BRAND_HOVER), ("selected", ds.BRAND)],
              upperbordercolor=indicator_border, lowerbordercolor=indicator_border)
    style.configure("Horizontal.TProgressbar", background=ds.BRAND, troughcolor=ds.BRAND_300,
                    bordercolor=ds.BORDER_TERTIARY, lightcolor=ds.BRAND, darkcolor=ds.BRAND)
    style.configure("Vertical.TScrollbar", background=ds.BG_CANVAS, troughcolor=ds.BG_SURFACE,
                    bordercolor=ds.BG_SURFACE, arrowcolor=ds.TEXT_SECONDARY)
    root._ds_theme = {"scale": scale, "px": px, "fonts": fonts, "faces": faces,
                      "images": images, "image_states": image_states, "style": style}
    return root._ds_theme

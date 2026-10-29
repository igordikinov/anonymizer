"""Widget-level DS checks, including bounded WinTk clipping regression."""
import json
import os
import subprocess
import sys
import tkinter.font as tkfont
from tkinter import ttk

import pytest

from anonymizer import ds_tokens as ds, theme
from anonymizer.gui import Application


def fresh_app(root):
    for child in root.winfo_children():
        child.destroy()
    app = Application(root)
    root.update()
    return app


def test_native_fonts_heights_and_popdown(tk_root):
    app = fresh_app(tk_root)
    assert theme.install(tk_root) is app.theme
    assert app.theme["faces"]["bundled"]
    for widget in app.controls:
        if isinstance(widget, (ttk.Entry, ttk.Combobox)):
            font = tkfont.Font(root=tk_root, font=widget.cget("font"))
            assert font.actual("family") == "InPlan Desktop Manrope"
            assert tk_root.tk.splitlist(widget.cget("font"))[-1] == str(-app.px(12))
            assert widget.winfo_reqheight() == app.px(40)
            assert widget.winfo_height() == app.px(40) or not widget.winfo_ismapped()
        if isinstance(widget, ttk.Combobox):
            popup = tk_root.tk.call("ttk::combobox::PopdownWindow", str(widget))
            font = tk_root.tk.call(popup + ".f.l", "cget", "-font")
            assert tkfont.Font(root=tk_root, font=font).actual("family") == "InPlan Desktop Manrope"
            assert tk_root.tk.splitlist(font)[-1] == str(-app.px(12))
            assert tk_root.tk.call(popup + ".f.l", "cget", "-background") == ds.BG_SURFACE
            assert tk_root.tk.call(popup + ".f.l", "cget", "-foreground") == ds.TEXT_PRIMARY
            assert tk_root.tk.call(popup + ".f.l", "cget", "-selectbackground") == ds.BRAND
            assert tk_root.tk.call(popup + ".f.l", "cget", "-selectforeground") == ds.TEXT_ON_BRAND
    assert app.start_button.winfo_height() == app.px(40)
    assert app.font_body[1] == -app.px(14)
    assert app.font_field[1] == -app.px(12)
    assert app.font_button[1] == -app.px(14)
    assert app.font_heading[1] == -app.px(16)
    assert app.font_secondary[1] == -app.px(12)
    report = app.diagnostics()["fonts"]
    assert report["configured_pixels"]["field"] == app.px(12)
    assert report["configured_pixels"]["body"] == app.px(14)
    fields = [control for control in app.controls if isinstance(control, (ttk.Entry, ttk.Combobox))]
    assert len(report["widgets"]) == len(fields)
    assert len(report["popdowns"]) == sum(isinstance(control, ttk.Combobox) for control in fields)
    assert all(font == report["actual"]["field"] for font in report["widgets"].values())
    assert all(font == report["actual"]["field"] for font in report["popdowns"].values())


def test_combined_button_states_and_focus_images(tk_root):
    app = fresh_app(tk_root)
    style = app.theme["style"]
    for name in ("Primary.TButton", "Secondary.TButton"):
        assert style.lookup(name, "foreground", ("disabled", "pressed", "active", "focus")) == ds.BUTTON_TEXT_DISABLED
        assert style.lookup(name, "background", ("pressed", "active")) == (ds.BRAND_ACTIVE if name.startswith("Primary") else ds.BG_PRESSED)
        # Inspect the raster actually supplied to the native image element:
        # focus remains visible while hovered/pressed; disabled is first.
        states = app.theme["image_states"]["DS." + name]
        assert states[0][0] == "disabled"
        for state, fill in [("focus", ds.BRAND), ("active focus", ds.BRAND_HOVER), ("pressed focus", ds.BRAND_ACTIVE)]:
            image = dict(states)[state]
            mid = image.width() // 2
            if name.startswith("Primary"):
                rgb = tuple(value // 257 for value in tk_root.winfo_rgb(fill))
                assert image.get(mid, 0) == rgb  # visible outer edge
                assert image.get(mid, app.px(1)) == (255, 255, 255)
                assert image.get(mid, app.px(2)) == (255, 255, 255)
                assert image.get(mid, app.px(3)) == rgb  # inner fill
                assert image.get(mid, image.height() // 2) == rgb
            else:
                assert image.get(mid, 0) == (71, 105, 255)
        disabled = states[0][1]
        if name.startswith("Primary"):
            assert disabled.get(disabled.width() // 2, app.px(1)) == (244, 246, 250)


def test_hint_and_subtitle_remain_readable_on_light_surfaces(tk_root):
    app = fresh_app(tk_root)
    style = app.theme["style"]
    def luminance(color):
        values = [value / 65535 for value in tk_root.winfo_rgb(color)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    for name in ("TLabel", "Sub.TLabel", "Hint.TLabel"):
        foreground = luminance(style.lookup(name, "foreground"))
        background = luminance(style.lookup(name, "background"))
        assert (max(foreground, background) + .05) / (min(foreground, background) + .05) >= 7


def test_native_checkbutton_uses_brand_and_retains_toggle_behavior(tk_root):
    app = fresh_app(tk_root)
    style = app.theme["style"]
    assert "indicatorforeground" in style.element_options("Checkbutton.indicator")
    assert style.lookup("TCheckbutton", "indicatorforeground", ("selected",)) == ds.BRAND
    assert style.lookup("TCheckbutton", "indicatorforeground", ("pressed", "active", "selected")) == ds.BRAND_ACTIVE
    assert style.lookup("TCheckbutton", "indicatorforeground", ("disabled", "pressed", "active", "selected")) == ds.BUTTON_TEXT_DISABLED
    assert style.lookup("TCheckbutton", "upperbordercolor", ("focus",)) == ds.BORDER_BRAND
    assert style.lookup("TCheckbutton", "indicatorbackground", ("disabled", "alternate")) == ds.BG_DISABLED
    check = next(control for control in app.controls if isinstance(control, ttk.Checkbutton))
    before = check.instate(["selected"])
    check.invoke()
    assert check.instate(["selected"]) != before
    check.invoke()
    assert check.instate(["selected"]) == before


def test_button_keyboard_tab_space_and_enter_are_scoped_to_focus(tk_root):
    app = fresh_app(tk_root)
    calls = []
    app.start_button.configure(command=lambda: calls.append("start"))
    entry = next(control for control in app.controls if isinstance(control, ttk.Entry))
    entry.focus_force()
    tk_root.update()
    expected_next = tk_root.tk.call("tk_focusNext", str(entry))
    entry.event_generate("<Tab>")
    tk_root.update()
    assert str(tk_root.focus_get()) == str(expected_next)

    # Return in an editable field must not trigger the application's command.
    entry.focus_force()
    tk_root.update()
    entry.event_generate("<Return>")
    tk_root.update()
    assert calls == []

    app.start_button.focus_force()
    tk_root.update()
    for sequence in ("<space>", "<Return>"):
        before = len(calls)
        app.start_button.event_generate(sequence)
        tk_root.update()
        assert len(calls) == before + 1

    app.start_button.state(["disabled"])
    for sequence in ("<space>", "<Return>"):
        before = len(calls)
        app.start_button.event_generate(sequence)
        tk_root.update()
        assert len(calls) == before

    check = next(control for control in app.controls if isinstance(control, ttk.Checkbutton))
    check.focus_force()
    tk_root.update()
    before = check.instate(["selected"])
    check.event_generate("<space>")
    tk_root.update()
    assert check.instate(["selected"]) != before


def test_help_surface_and_close_keyboard_use_the_same_brand(tk_root):
    from anonymizer.help_text import show_help

    fresh_app(tk_root)
    window = show_help(tk_root, "synthetic")
    tk_root.update()
    assert window.cget("background") == ds.BG_CANVAS
    close = next(child for child in window.winfo_children() if isinstance(child, ttk.Button))
    close.focus_force()
    tk_root.update()
    close.event_generate("<Return>")
    tk_root.update()
    assert not window.winfo_exists()


def test_entry_and_combo_native_hover_and_focus(tk_root):
    app = fresh_app(tk_root)
    style = app.theme["style"]
    for cls in (ttk.Entry, ttk.Combobox):
        widget = next(control for control in app.controls if isinstance(control, cls))
        widget.event_generate("<Enter>")
        tk_root.update()
        assert widget.instate(["hover"])
        assert style.lookup(widget.winfo_class(), "bordercolor", widget.state()) == ds.BORDER_HOVER
        widget.focus_force()
        tk_root.update()
        assert widget.instate(["focus"])
        assert style.lookup(widget.winfo_class(), "bordercolor", widget.state()) == ds.BORDER_BRAND
        widget.state(["disabled"])
        assert style.lookup(widget.winfo_class(), "bordercolor", widget.state()) == ds.BORDER_TERTIARY
        widget.state(["!disabled", "!focus", "!hover"])
        tk_root.focus_force()


def test_native_combo_arrow_uses_surface_in_all_interactive_states(tk_root):
    app = fresh_app(tk_root)
    style = app.theme["style"]
    # Combobox.downarrow paints background/lightcolor/darkcolor itself;
    # fieldbackground alone leaves the clam grey rectangle visible.
    for states in [(), ("readonly",), ("readonly", "hover"), ("readonly", "active"),
                   ("readonly", "pressed"), ("focus", "active", "readonly")]:
        for option in ("background", "lightcolor", "darkcolor"):
            assert style.lookup("TCombobox", option, states) == ds.BG_SURFACE
    for option in ("background", "lightcolor", "darkcolor"):
        assert style.lookup("TCombobox", option, ("disabled", "active", "pressed")) == ds.BG_DISABLED
    assert style.lookup("TCombobox", "borderwidth") == 0


@pytest.mark.parametrize("scale,screen", [(1, None), (1.25, None), (1.5, None), (1.5, (1366, 768))])
def test_dpi_layout_and_clipped_cards_finish_in_bounded_process(scale, screen):
    # Independent interpreter: Tk caches font/theme metrics per interpreter.
    # Timeout is essential: WinTk image-backed frames used to loop forever
    # when a card was taller than its Canvas viewport.
    code = r'''
import json
from anonymizer.fonts import prepare_gui
prepare_gui()
import tkinter as tk
from tkinter import ttk
from anonymizer.gui import Application
r = tk.Tk()
r.tk.call("tk", "scaling", SCALE * 96 / 72)
screen = SCREEN
if screen is not None:
    r.winfo_screenwidth = lambda: screen[0]
    r.winfo_screenheight = lambda: screen[1]
a = Application(r)
r.update_idletasks(); r.update()
available_width = r.winfo_screenwidth()-a.px(48)
available_height = r.winfo_screenheight()-a.px(80)
assert r.minsize() == (min(a.px(870),available_width),min(a.px(640),available_height))
expected_width = min(a.px(1030),available_width)
expected_height = min(a.px(820),available_height)
assert (r.winfo_width(),r.winfo_height()) == (expected_width,expected_height)
# First case validates the untouched real startup size, including PG expanded.
for size in [None,(1030,820),(870,640)]:
    if size is not None:
        width,height = size
        r.geometry(f"{a.px(width)}x{a.px(height)}")
    for pg in [False,True]:
        a.pg_enabled.set(pg); a.pg_toggle()
        a.source.set("C:/synthetic/" + "long-directory/"*50 + "input.sql")
        a.status.set("Синтетическая проверка длинного сообщения. "*4)
        a.result.set("Синтетический результат проверки. "*5)
        r.update_idletasks(); r.update()
        assert a.canvas.winfo_height() >= a.px(40)
        for button in a._footer_buttons:
            assert button.winfo_ismapped()
            assert button.winfo_rootx() >= r.winfo_rootx()
            assert button.winfo_rootx()+button.winfo_width() <= r.winfo_rootx()+r.winfo_width()
            assert button.winfo_rooty()+button.winfo_height() <= r.winfo_rooty()+r.winfo_height()
        for control in a.controls:
            if isinstance(control,(ttk.Entry,ttk.Combobox)) and control.winfo_ismapped():
                assert control.winfo_height() == a.px(40)
                assert control.winfo_width() >= a.px(40)
                assert control.winfo_rootx()+control.winfo_width() <= a.canvas.winfo_rootx()+a.canvas.winfo_width()
        for card in a.cards:
            assert card.winfo_x() == a.px(16)
            assert card.winfo_y() == a.px(16)
        if size == (870,640): assert a._footer_two_rows
        assert r.tk.call("tk_focusNext",str(a.start_button))
a.cards[0].configure(height=a.px(1200)); a.cards[0].grid_propagate(False)
r.update_idletasks();r.update()
assert a.cards[0].winfo_height() > a.canvas.winfo_height()
print(json.dumps(a.diagnostics()))
r.destroy()
'''.replace("SCALE", str(scale)).replace("SCREEN", repr(screen))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            timeout=20, env={**os.environ, "PYTHONPATH": "src"})
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["fonts"]["resolved"]["bundled"]
    assert set(report["actual_heights"].values()) == {round(40 * report["scale"])}

"""GUI layout regression: the launch buttons stay visible at any window size.

The footer is packed before the cards (bottom reserved); when the PostgreSQL
fields are enabled the launch button must still be inside the visible window.
Both tests attach to the shared session root (see conftest).
"""
import tkinter as tk
from tkinter import ttk

import pytest

from anonymizer.gui import Application


def fresh_surface(root):
    """Previous tests' Applications still hold widgets on the shared root."""
    for child in root.winfo_children():
        child.destroy()


def test_start_button_visible_at_default_and_min_size(tk_root):
    fresh_surface(tk_root)
    app = Application(tk_root)
    tk_root.update()
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= tk_root.winfo_rooty() + tk_root.winfo_height()
    tk_root.geometry("870x640")
    tk_root.update()
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= tk_root.winfo_rooty() + tk_root.winfo_height()


def test_start_button_visible_with_postgresql_enabled(tk_root):
    fresh_surface(tk_root)
    app = Application(tk_root)
    app.pg_enabled.set(True)
    app.pg_toggle()
    tk_root.update()
    assert app.pg_frame.winfo_ismapped()  # поля появились
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= tk_root.winfo_rooty() + tk_root.winfo_height()
    assert app.help_button.winfo_ismapped()


def test_dark_rail_with_in_icon(tk_root):
    fresh_surface(tk_root)
    app = Application(tk_root)
    tk_root.update()
    rail = app.rail
    assert rail.winfo_ismapped() and rail.winfo_width() == app.px(69)
    assert rail.cget("background") == "#04143a"
    assert app.rail_logo.winfo_ismapped()  # только In., белый на тёмно-синем


def test_help_button_lives_in_dark_topbar(tk_root):
    fresh_surface(tk_root)
    app = Application(tk_root)
    tk_root.update()
    assert app.help_button.winfo_ismapped()
    assert app.help_button.cget("background") == "#04143a"
    assert app.help_button.cget("foreground") == "#ffffff"  # белая на тёмно-синей полосе — сразу видно
    assert app.help_button.cget("highlightcolor") == "#ffffff"
    assert int(app.help_button.cget("highlightthickness")) == app.px(2)
    assert "Справка" in app.help_button.cget("text") and "F1" in app.help_button.cget("text")


def test_buttons_follow_scp_button_canon(tk_root):
    style = ttk.Style(tk_root)
    # stroked: белый фон + обводка #949598 (видимый контур на любом фоне)
    assert style.lookup("TButton", "background") == "#ffffff"
    assert style.lookup("TButton", "bordercolor") == "#949598"
    # primary: заливка #4769ff
    assert style.lookup("Primary.TButton", "background") == "#4769ff"


def test_journal_export_button(tmp_path, tk_root, monkeypatch):
    fresh_surface(tk_root)
    app = Application(tk_root)
    tk_root.update()
    assert app.export_button.instate(["disabled"])  # до прогона журнал выгружать нечего
    app.runlog.add("info", "import", "start", "", "synthetic entry")
    target = tmp_path / "exported.csv"
    from anonymizer.gui import filedialog

    monkeypatch.setattr("anonymizer.gui.messagebox.showinfo", lambda *args: None)
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kwargs: str(target))
    app.export_log()
    assert target.is_file()
    rows = target.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("время;") and rows[1].endswith("synthetic entry")
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kwargs: "")  # диалог отменён
    app.export_log()


def test_uncertain_commit_keeps_result_accessible(tk_root, monkeypatch):
    fresh_surface(tk_root)
    app = Application(tk_root)
    warnings = []
    monkeypatch.setattr("anonymizer.gui.messagebox.showwarning", lambda *args: warnings.append(args))
    app.running = True
    app.events.put(("uncertain", "Пакет, проект и карта замен сохранены; проверьте PostgreSQL."))
    app._poll_once()
    assert not app.running
    assert "не подтверждён" in app.status.get()
    assert "сохранены" in app.result.get()
    assert "не создан" not in app.result.get()
    assert app.open_button.instate(["!disabled"])
    assert warnings

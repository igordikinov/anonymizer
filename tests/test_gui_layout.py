"""GUI layout regression: the launch buttons stay visible at any window size.

The footer is packed before the cards (bottom reserved); when the PostgreSQL
fields are enabled the launch button must still be inside the visible window.
Both tests attach to the shared session root (see conftest).
"""
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
    assert rail.winfo_ismapped() and rail.winfo_width() == 69
    assert rail.cget("background") == "#1b1b1b"
    assert app.rail_logo.winfo_ismapped()  # только In., белый на чёрном

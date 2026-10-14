"""GUI layout regression: the launch buttons stay visible at any window size.

The footer is packed before the cards (bottom reserved); when the PostgreSQL
fields are enabled the launch button must still be inside the visible window.
Both tests attach to the shared session root (see conftest).
"""
import pytest

from anonymizer.gui import Application


def test_start_button_visible_at_default_and_min_size(tk_root):
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
    app = Application(tk_root)
    app.pg_enabled.set(True)
    app.pg_toggle()
    tk_root.update()
    assert app.pg_frame.winfo_ismapped()  # поля появились
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= tk_root.winfo_rooty() + tk_root.winfo_height()
    assert app.help_button.winfo_ismapped()

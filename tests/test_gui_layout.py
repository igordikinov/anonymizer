"""GUI layout regression: the launch buttons stay visible at any window size.

The footer is packed before the cards (bottom reserved); when the PostgreSQL
fields are enabled the launch button must still be inside the visible window.
"""
import tkinter as tk

import pytest

from anonymizer.gui import Application


@pytest.fixture
def root():
    try:
        root = tk.Tk()
        root.update()  # previous test's pending after-callback must not hit a dead interp
    except tk.TclError as error:
        pytest.skip(f"нет дисплея для Tk ({error})")
    root.geometry("1030x820")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_start_button_visible_at_default_and_min_size(root):
    app = Application(root)
    root.update()
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= root.winfo_rooty() + root.winfo_height()
    root.geometry("870x720")
    root.update()
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= root.winfo_rooty() + root.winfo_height()


def test_start_button_visible_with_postgresql_enabled(root):
    app = Application(root)
    app.pg_enabled.set(True)
    app.pg_toggle()
    root.update()
    assert app.pg_frame.winfo_ismapped()  # поля появились
    assert app.start_button.winfo_ismapped()
    bottom = app.start_button.winfo_rooty() + app.start_button.winfo_height()
    assert bottom <= root.winfo_rooty() + root.winfo_height()

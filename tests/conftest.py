"""Shared Tk display for GUI tests.

One session-scoped root for the whole pytest process: repeated create/destroy
of Tk roots in one interpreter is racy (pending after-callbacks hit the dead
interp and poison the next Tk()), so tests attach to a single never-destroyed
root instead.
"""
import tkinter as tk

import pytest


@pytest.fixture(scope="session")
def tk_root():
    from anonymizer.fonts import prepare_gui

    prepare_gui()
    try:
        root = tk.Tk()
        root.update()
    except tk.TclError as error:
        pytest.skip(f"нет дисплея для Tk ({error})")
    root.geometry("1030x820")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass

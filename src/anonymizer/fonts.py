"""Bundled Open Sans, registered only for this process on Windows.

No installation, registry changes or network access. Resources stay alive until
GDI releases the private font handles, including when imported from a wheel.
"""
from __future__ import annotations

import atexit
import ctypes
from contextlib import ExitStack
from importlib.resources import as_file, files
import sys
import tkinter.font as tkfont

FONT_FILES = ("OpenSans-Regular.ttf", "OpenSans-SemiBold.ttf", "OpenSans-Bold.ttf")
_FR_PRIVATE = 0x10
_resources = ExitStack()
_registered: list[str] = []
_attempted = False
_dpi_prepared = False


def register_private_fonts() -> bool:
    """Best effort; the release smoke rejects fallback instead of hiding it."""
    global _attempted
    if _attempted:
        return len(_registered) == len(FONT_FILES)
    _attempted = True
    if sys.platform != "win32":
        return False
    try:
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        add = gdi.AddFontResourceExW
        add.argtypes = (ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
        add.restype = ctypes.c_int
        for name in FONT_FILES:
            path = str(_resources.enter_context(as_file(files("anonymizer.resources").joinpath("fonts", name))))
            if add(path, _FR_PRIVATE, None) > 0:
                _registered.append(path)
    except (OSError, AttributeError):
        return False
    return len(_registered) == len(FONT_FILES)


def prepare_gui() -> bool:
    """Set only this process's DPI policy, before creating its first Tk root."""
    global _dpi_prepared
    if not _dpi_prepared:
        _dpi_prepared = True
        if sys.platform == "win32":
            try:
                # E_ACCESSDENIED means a manifest/host already selected a policy.
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            except (OSError, AttributeError):
                try:
                    ctypes.WinDLL("user32").SetProcessDPIAware()
                except (OSError, AttributeError):
                    pass
    return register_private_fonts()


def resolve_fonts(root) -> dict:
    """Return actual GDI family names; ttk fields must set these explicitly."""
    loaded = register_private_fonts()
    families = {name.casefold(): name for name in tkfont.families(root)}
    fallback = families.get("segoe ui", tkfont.nametofont("TkDefaultFont", root=root).actual("family"))
    if loaded and "open sans" in families and "open sans semibold" in families:
        return {"body": families["open sans"], "semibold": families["open sans semibold"],
                "bold": families["open sans"], "bundled": True}
    # Do not mix a partially registered family with a system-installed face.
    return {"body": fallback, "semibold": fallback, "bold": fallback, "bundled": False}


def _release_fonts():
    if sys.platform == "win32" and _registered:
        try:
            remove = ctypes.WinDLL("gdi32").RemoveFontResourceExW
            remove.argtypes = (ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
            remove.restype = ctypes.c_bool
            for path in reversed(_registered):
                remove(path, _FR_PRIVATE, None)
        except (OSError, AttributeError):
            pass
    _registered.clear()
    _resources.close()


atexit.register(_release_fonts)

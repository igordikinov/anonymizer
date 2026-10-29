"""Bundled Manrope and Open Sans, registered privately for this process.

No installation, registry changes or network access. Resources stay alive until
GDI releases the private font handles, including when imported from a wheel.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
from contextlib import ExitStack
from importlib.resources import as_file, files
import sys
from pathlib import Path
import tkinter.font as tkfont

FONT_FILES = ("Manrope-Regular.ttf", "Manrope-SemiBold.ttf", "Manrope-Bold.ttf")
FALLBACK_FONT_FILES = ("OpenSans-Regular.ttf", "OpenSans-SemiBold.ttf", "OpenSans-Bold.ttf")
# App-private aliases prevent the Windows mapper from substituting an installed
# variable Manrope instance when Tk requests normal weight for the 600 face.
GDI_FAMILY = "InPlan Desktop Manrope"
GDI_SEMIBOLD_FAMILY = GDI_FAMILY + " SemiBold"
_FR_PRIVATE = 0x10
_resources = ExitStack()
_registered: list[str] = []
_attempted = False
_dpi_prepared = False


def _complete(names):
    return set(names).issubset({Path(path).name for path in _registered})


def register_private_fonts() -> bool:
    """Best effort; the release smoke rejects fallback instead of hiding it."""
    global _attempted
    if _attempted:
        return _complete(FONT_FILES)
    _attempted = True
    if sys.platform != "win32":
        return False
    try:
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        add = gdi.AddFontResourceExW
        add.argtypes = (ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
        add.restype = ctypes.c_int
        for name in (*FONT_FILES, *FALLBACK_FONT_FILES):
            try:
                path = str(_resources.enter_context(as_file(files("anonymizer.resources").joinpath("fonts", name))))
                if add(path, _FR_PRIVATE, None) > 0:
                    _registered.append(path)
            except (OSError, AttributeError):
                # One missing primary face must not prevent registering the
                # complete bundled fallback. Partial primary families are unused.
                continue
    except (OSError, AttributeError):
        return False
    return _complete(FONT_FILES)


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
    if loaded and GDI_FAMILY.casefold() in families and GDI_SEMIBOLD_FAMILY.casefold() in families:
        return {"body": families[GDI_FAMILY.casefold()], "semibold": families[GDI_SEMIBOLD_FAMILY.casefold()],
                "bold": families[GDI_FAMILY.casefold()], "bundled": True}
    if _complete(FALLBACK_FONT_FILES) and "open sans" in families and "open sans semibold" in families:
        return {"body": families["open sans"], "semibold": families["open sans semibold"],
                "bold": families["open sans"], "bundled": False}
    # Do not mix a partially registered family with a system-installed face.
    return {"body": fallback, "semibold": fallback, "bold": fallback, "bundled": False}


def selected_font_evidence() -> dict:
    """Smoke-only proof that GDI selects bundled static files, not system aliases."""
    if sys.platform != "win32" or not _complete(FONT_FILES):
        return {}
    from ctypes import wintypes as w

    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = (w.HDC,), w.HDC
    gdi.CreateFontW.argtypes = (ctypes.c_int,) * 5 + (w.DWORD,) * 8 + (w.LPCWSTR,)
    gdi.CreateFontW.restype = w.HANDLE
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = (w.HDC, w.HANDLE), w.HANDLE
    gdi.GetFontData.argtypes = (w.HDC, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD)
    gdi.GetFontData.restype = w.DWORD
    gdi.DeleteObject.argtypes, gdi.DeleteObject.restype = (w.HANDLE,), w.BOOL
    gdi.DeleteDC.argtypes, gdi.DeleteDC.restype = (w.HDC,), w.BOOL
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        raise OSError("Cannot create font diagnostic DC")
    result = {}
    try:
        for role, family, requested, weight, name in [
            ("body", GDI_FAMILY, 400, 400, FONT_FILES[0]),
            ("semibold", GDI_SEMIBOLD_FAMILY, 400, 600, FONT_FILES[1]),
            ("bold", GDI_FAMILY, 700, 700, FONT_FILES[2]),
        ]:
            handle = gdi.CreateFontW(-16, 0, 0, 0, requested, 0, 0, 0, 1, 0, 0, 0, 0, family)
            if not handle:
                raise OSError("Cannot select diagnostic font")
            previous = gdi.SelectObject(dc, handle)
            try:
                count = gdi.GetFontData(dc, 0, 0, None, 0)
                if not 0 < count <= 2 * 1024**2:
                    raise OSError("Unexpected diagnostic font size")
                data = ctypes.create_string_buffer(count)
                if gdi.GetFontData(dc, 0, 0, data, count) != count:
                    raise OSError("Cannot read selected font")
                actual_weight = ctypes.create_string_buffer(2)
                if gdi.GetFontData(dc, int.from_bytes(b"OS/2", "little"), 4, actual_weight, 2) != 2:
                    raise OSError("Cannot read selected font weight")
                selected_sha = hashlib.sha256(data.raw).hexdigest()
                resource_sha = hashlib.sha256(files("anonymizer.resources").joinpath("fonts", name).read_bytes()).hexdigest()
                actual = int.from_bytes(actual_weight.raw, "big")
                result[role] = {"family": family, "weight": actual, "sha256": selected_sha,
                                "matches_bundled": selected_sha == resource_sha and actual == weight}
            finally:
                gdi.SelectObject(dc, previous)
                gdi.DeleteObject(handle)
    finally:
        gdi.DeleteDC(dc)
    return result


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

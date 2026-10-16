"""Font resources and private process registration; no installed fonts required."""
import hashlib
from importlib.resources import files
import json
import sys

import pytest

from anonymizer import fonts


def test_bundled_fonts_match_recorded_upstream_and_license():
    resources = files("anonymizer.resources").joinpath("fonts")
    manifest = json.loads(resources.joinpath("provenance.json").read_text(encoding="utf-8"))
    assert len(manifest["revision"]) == 40
    assert manifest["source"] == "https://github.com/googlefonts/opensans"
    assert {row["name"] for row in manifest["files"]} == {*fonts.FONT_FILES, "OFL.txt"}
    for entry in manifest["files"]:
        content = resources.joinpath(entry["name"]).read_bytes()
        assert len(content) == entry["bytes"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert f"/{manifest['revision']}/" in entry["source_url"]
        if entry["name"].endswith(".ttf"):
            assert content[:4] == b"\x00\x01\x00\x00"
    assert "SIL OPEN FONT LICENSE Version 1.1" in resources.joinpath("OFL.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="private GDI registration is Windows-only")
def test_open_sans_is_available_without_system_installation(tk_root):
    resolved = fonts.resolve_fonts(tk_root)
    assert fonts.register_private_fonts()
    assert resolved == {"body": "Open Sans", "semibold": "Open Sans SemiBold", "bold": "Open Sans", "bundled": True}
    assert len(fonts._registered) == len(fonts.FONT_FILES)
    before = list(fonts._registered)
    assert fonts.prepare_gui()
    assert fonts._registered == before  # repeated Applications cannot leak GDI registrations


def test_unavailable_font_falls_back_without_a_network_request(tk_root, monkeypatch):
    monkeypatch.setattr(fonts, "register_private_fonts", lambda: False)
    monkeypatch.setattr(fonts.tkfont, "families", lambda root: ("Segoe UI", "Open Sans", "Open Sans SemiBold"))
    assert fonts.resolve_fonts(tk_root) == {"body": "Segoe UI", "semibold": "Segoe UI", "bold": "Segoe UI", "bundled": False}

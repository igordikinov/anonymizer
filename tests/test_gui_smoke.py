"""The windowed release smoke must fail cleanly when DS acceptance fails."""
import pytest

from anonymizer.cli import main
from anonymizer import gui


def test_smoke_failure_is_nonzero_without_unhandled_dialog(monkeypatch, tmp_path):
    def fail(**kwargs):
        raise RuntimeError("synthetic missing bundled font")

    monkeypatch.setattr(gui, "launch", fail)
    assert main(["--smoke-gui", "--smoke-gui-report", str(tmp_path / "gui.json")]) == 1


def test_report_flag_requires_smoke():
    with pytest.raises(SystemExit) as error:
        main(["--smoke-gui-report", "unused.json"])
    assert error.value.code == 2


def test_interactive_gui_errors_are_not_silenced(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(gui, "launch", fail)
    with pytest.raises(RuntimeError, match="startup failure"):
        main([])

"""New-project workflow: choose a folder; the pipeline owns file creation."""
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from anonymizer import gui
from anonymizer.pipeline import Options, run


def surface(tk_root, current="previous.snpanon"):
    return SimpleNamespace(root=tk_root, project=tk.StringVar(tk_root, value=current))


def test_new_project_selects_folder_and_pipeline_creates_file(tmp_path, tk_root, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    app = surface(tk_root)
    dialogs = []

    def choose_folder(**kwargs):
        dialogs.append(kwargs)
        return str(private)

    monkeypatch.setattr(gui.filedialog, "askdirectory", choose_folder)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: pytest.fail("Save dialog opened"))
    gui.Application.new_project(app)
    first = Path(app.project.get())
    assert first.parent == private and first.suffix == ".snpanon"
    assert not first.exists() and not list(private.iterdir())
    assert dialogs[0]["mustexist"] and dialogs[0]["parent"] is tk_root

    # A second click is a genuinely fresh choice even before a run creates it.
    gui.Application.new_project(app)
    project = Path(app.project.get())
    assert project != first and not project.exists()
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.csv").write_text("note,quantity\nSYNTHETIC,12.340\n", encoding="utf-8")
    report = run(Options(source, tmp_path / "out", project, bypass_model=True))
    assert project.is_file() and report["rows"] == 1
    assert not first.exists()
    assert not list((tmp_path / "out").rglob("*.snpanon"))


def test_cancel_new_project_preserves_current_path(tmp_path, tk_root, monkeypatch):
    app = surface(tk_root)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: "")
    gui.Application.new_project(app)
    assert app.project.get() == "previous.snpanon"
    assert not list(tmp_path.iterdir())


def test_name_collision_preserves_existing_file(tmp_path, tk_root, monkeypatch):
    existing = tmp_path / "client_existing.snpanon"
    existing.write_bytes(b"SYNTHETIC_SENTINEL")
    ids = iter(["existing", "fresh"])
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(gui, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    app = surface(tk_root)
    gui.Application.new_project(app)
    assert Path(app.project.get()) == tmp_path / "client_fresh.snpanon"
    assert existing.read_bytes() == b"SYNTHETIC_SENTINEL"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("access_error", [False, True])
def test_name_failure_is_bounded_and_preserves_selection(tmp_path, tk_root, monkeypatch, access_error):
    (tmp_path / "client_existing.snpanon").mkdir()
    calls, errors = [], []
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: str(tmp_path))

    def candidate():
        calls.append(1)
        if access_error:
            raise PermissionError("SYNTHETIC_PRIVATE_DETAIL")
        return SimpleNamespace(hex="existing")

    monkeypatch.setattr(gui, "uuid4", candidate)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kw: errors.append(args))
    app = surface(tk_root)
    gui.Application.new_project(app)
    assert app.project.get() == "previous.snpanon"
    assert len(calls) == (1 if access_error else 10)
    assert len(errors) == 1 and "SYNTHETIC_PRIVATE_DETAIL" not in str(errors)

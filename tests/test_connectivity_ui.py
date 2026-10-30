"""Profile selection and required-integrity behavior at the GUI/CLI boundary."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anonymizer import cli, gui
from anonymizer.errors import AnonymizerError


def report(status="PASSED", source="PASSED"):
    return {"tables": 1, "rows": 2, "integrity_status": status,
            "source_integrity_status": source,
            "publication_mode": "VERIFIED_INTEGRITY" if status == "PASSED" else "UNVERIFIED_INTEGRITY",
            "validation": {"foreign_keys": []}}


@pytest.mark.parametrize("profile,required", [(None, False), ("builtin:optimizer-v1", True), ("custom profile.json", False)])
def test_cli_routes_profile_and_requirement_without_changing_legacy_defaults(tmp_path, monkeypatch, capsys, profile, required):
    received = []
    monkeypatch.setattr(cli, "run", lambda options: received.append(options) or report())
    args = ["--input", str(tmp_path / "source.sql"), "--output", str(tmp_path / "out"),
            "--project", str(tmp_path / "private.snpanon"), "--bypass-model"]
    if profile:
        args += ["--connectivity-profile", profile]
    if required:
        args += ["--require-integrity"]
    assert cli.main(args) == 0
    options, = received
    assert options.connectivity_profile == profile and options.require_integrity is required
    assert options.bypass_model and options.industry == "food"
    summary = json.loads(capsys.readouterr().out)
    assert summary["integrity_status"] == "PASSED"
    assert summary["publication_mode"] == "VERIFIED_INTEGRITY"


def test_cli_diagnostic_failure_is_nonzero_and_does_not_print_success(tmp_path, monkeypatch, capsys):
    def fail(options):
        error = AnonymizerError("RELATION_CHANGED: изменение связей; диагностический отчёт создан.")
        error.report_path = options.output / "report.json"
        raise error

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["--input", str(tmp_path / "source.sql"), "--output", str(tmp_path / "out"),
                     "--project", str(tmp_path / "private.snpanon"), "--require-integrity"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "RELATION_CHANGED" in captured.err


def app_surface(tk_root):
    for child in tk_root.winfo_children():
        child.destroy()
    app = gui.Application(tk_root)
    tk_root.update()
    return app


def test_gui_profile_selection_requires_integrity_but_allows_explicit_unverified(tk_root):
    app = app_surface(tk_root)
    assert app.connectivity_options() is None and not app.require_integrity.get()
    assert app.profile_file_entry.instate(["disabled"])
    app.connectivity_choice.set("Optimizer v1")
    app.connectivity_selected()
    assert app.connectivity_options() == "builtin:optimizer-v1" and app.require_integrity.get()
    app.require_integrity.set(False)
    assert app.connectivity_options() == "builtin:optimizer-v1" and not app.require_integrity.get()
    app.connectivity_choice.set("Без профиля")
    app.connectivity_selected()
    assert app.connectivity_options() is None and not app.require_integrity.get()


def test_gui_json_picker_cancel_preserves_previous_profile_and_requirement(tk_root, monkeypatch):
    app = app_surface(tk_root)
    app.connectivity_choice.set("Optimizer v1")
    app.connectivity_selected()
    app.require_integrity.set(False)
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: "")
    app.connectivity_choice.set("JSON-файл…")
    app.connectivity_selected()
    assert app.connectivity_choice.get() == "Optimizer v1"
    assert not app.require_integrity.get()
    assert app.profile_file_entry.instate(["disabled"])


def test_gui_json_profile_reaches_pipeline_and_controls_restore(tmp_path, tk_root, monkeypatch):
    app = app_surface(tk_root)
    path = str(tmp_path / "profile with spaces.json")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: path)
    app.connectivity_choice.set("JSON-файл…")
    app.connectivity_selected()
    assert app.connectivity_options() == path and app.require_integrity.get()
    assert app.profile_file_frame.winfo_manager() == "grid"
    received = []
    monkeypatch.setattr(gui, "run", lambda options, *args: received.append(options) or report(source="WARNINGS"))
    monkeypatch.setattr(gui.threading, "Thread", lambda **kwargs: SimpleNamespace(start=kwargs["target"]))
    app.source.set(str(tmp_path / "source.sql"))
    app.output.set(str(tmp_path / "out"))
    app.project.set(str(tmp_path / "private.snpanon"))
    app.bypass_model.set(True)
    app.start()
    options, = received
    assert options.connectivity_profile == path and options.require_integrity and options.bypass_model
    app._poll_once()
    assert app.connectivity_selector.instate(["readonly"])
    assert app.profile_file_entry.instate(["!disabled"])
    assert "Обязательные проверки" in app.result.get() and "исходные дефекты" in app.result.get()


def test_gui_unverified_success_and_diagnostic_failure_are_not_reported_as_pass(tk_root, monkeypatch):
    app = app_surface(tk_root)
    app.events.put(("done", report(status="NOT_CHECKED")))
    app._poll_once()
    assert "Связность не подтверждена" in app.result.get()
    messages = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: messages.append(args))
    app.events.put(("diagnostic", ("CHECK_INCOMPLETE", "report.json")))
    app._poll_once()
    assert "набор данных не опубликован" in app.status.get()
    assert app.open_button.instate(["!disabled"])
    assert messages and "CHECK_INCOMPLETE" in messages[0][1]


def test_gui_empty_json_path_blocks_launch(tk_root, monkeypatch):
    app = app_surface(tk_root)
    app.connectivity_choice.set("JSON-файл…")
    messages = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: messages.append(args))
    with pytest.raises(ValueError, match="connectivity_profile"):
        app.connectivity_options()
    assert messages


def test_profile_keyboard_focus_is_visible_in_small_window_with_postgresql(tk_root, tmp_path, monkeypatch):
    import tkinter.font as tkfont

    app = app_surface(tk_root)
    tk_root.geometry("870x640")
    app.pg_enabled.set(True)
    app.pg_toggle()
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(tmp_path / "profile.json"))
    app.connectivity_choice.set("JSON-файл…")
    app.connectivity_selected()
    tk_root.update()
    app.canvas.yview_moveto(0)
    app.profile_file_entry.focus_force()
    tk_root.update()
    top = app.canvas.winfo_rooty()
    assert app.profile_file_entry.winfo_rooty() >= top
    assert app.profile_file_entry.winfo_rooty() + app.profile_file_entry.winfo_height() <= top + app.canvas.winfo_height()
    actual = tkfont.Font(root=tk_root, font=app.connectivity_selector.cget("font")).actual()
    assert actual == tkfont.Font(root=tk_root, font=app.font_field).actual()
    assert app.start_button.winfo_rooty() + app.start_button.winfo_height() <= tk_root.winfo_rooty() + tk_root.winfo_height()

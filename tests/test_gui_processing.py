"""Real worker/Tk event-loop regressions for the processing progress contract."""
import threading
import time

import pytest

from anonymizer import gui
from anonymizer.errors import AnonymizerError, Cancelled


def surface(root):
    for child in root.winfo_children():
        child.destroy()
    app = gui.Application(root)
    root.update()
    return app


def setup_paths(app, tmp_path):
    app.source.set(str(tmp_path / "source.sql"))
    app.output.set(str(tmp_path / "out"))
    app.project.set(str(tmp_path / "private.snpanon"))


def pump(root, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(.005)
    assert predicate(), "GUI did not receive the worker's terminal event"


def result():
    return {"tables": 1, "rows": 1, "integrity_status": "PASSED",
            "source_integrity_status": "PASSED", "validation": {"foreign_keys": []}}


def test_real_pipeline_worker_completes_new_stages_without_stopping_tk(tmp_path, tk_root, monkeypatch):
    app = surface(tk_root)
    setup_paths(app, tmp_path)
    tables = {
        "optimizer_periods": ("datasetid numeric,period numeric", "1,0"),
        "optimizer_demand": ("datasetid numeric,period numeric,location text,product text,client text,quantity numeric", "1,0,'A','P','C',10.25"),
        "optimizer_production": ("datasetid numeric,period numeric,location text,product text,bomnum text,resource text", "1,0,'A','P','B','R'"),
        "optimizer_bom": ("datasetid numeric,period numeric,location text,product text,bomnum text,input_output numeric", "1,0,'A','P','B',1"),
        "optimizer_storage": ("datasetid numeric,period numeric,location text,product text,resource text", "1,0,'A','P','R'"),
        "optimizer_procurement": ("datasetid numeric,period numeric,location text,product text,supplier text", "1,0,'A','P','A'"),
        "optimizer_transportation": ("datasetid numeric,period numeric,loc_from text,loc_to text,product text,transport_type text,resource text", "1,0,'A','A','P','T','R'"),
    }
    (tmp_path / "source.sql").write_text("\n".join(
        f"CREATE TABLE public.{name} ({columns}); INSERT INTO public.{name} VALUES ({values});"
        for name, (columns, values) in tables.items()), encoding="utf-8")
    app.bypass_model.set(True)
    app.connectivity_choice.set("Optimizer v1")
    app.connectivity_selected()
    stages, threads, errors, beats = [], [], [], []
    original_run = gui.run

    def observed(options, progress, cancel):
        def event(stage, amount, message):
            stages.append(stage)
            threads.append(threading.get_ident())
            progress(stage, amount, message)
        return original_run(options, event, cancel)

    monkeypatch.setattr(gui, "run", observed)
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(args))

    def heartbeat():
        beats.append(1)
        if app.running:
            tk_root.after(5, heartbeat)

    app.bar["value"] = 100
    app.start()
    assert float(app.bar["value"]) == 0
    tk_root.after(5, heartbeat)
    try:
        pump(tk_root, lambda: not app.running)
    finally:
        if app.running:
            app.cancel.set()
            pump(tk_root, lambda: not app.running)
    assert not errors
    assert {"connectivity", "verify_exports", "done"} <= set(stages)
    assert threads and all(identity != threading.get_ident() for identity in threads)
    assert len(beats) >= 2
    assert app.start_button.instate(["!disabled"]) and app.cancel_button.instate(["disabled"])
    assert float(app.bar["value"]) == 100
    assert "Обязательные проверки связности пройдены" in app.result.get()
    assert (tmp_path / "out/report.json").is_file()


@pytest.mark.parametrize("stage", ["connectivity", "verify_exports"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_terminal_error_and_cancel_after_new_stage_are_received(tmp_path, tk_root, monkeypatch, stage, failure):
    app = surface(tk_root)
    setup_paths(app, tmp_path)
    entered, messages, errors = threading.Event(), [], []

    def run(options, progress, cancel):
        progress(stage, .5, "Контрольная стадия")
        entered.set()
        if failure == "cancel":
            assert cancel.wait(5), "cancel button did not reach the worker"
            raise Cancelled("Обработка отменена.")
        raise AnonymizerError("Контрольная ошибка.")

    monkeypatch.setattr(gui, "run", run)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: messages.append(args))
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    app.start()
    pump(tk_root, entered.is_set)
    if failure == "cancel":
        app.cancel_button.invoke()
    pump(tk_root, lambda: not app.running)
    assert not errors and messages
    assert app.start_button.instate(["!disabled"])
    assert "Итоговый пакет не создан" == app.status.get()
    assert float(app.bar["value"]) < 100


def test_unknown_progress_does_not_block_terminal_event_and_known_progress_is_clamped(tk_root):
    app = surface(tk_root)
    for stage in ("connectivity", "verify_exports"):
        app.events.put(("progress", (stage, -3, "Стадия")))
        app._poll_once()
        assert float(app.bar["value"]) == gui.PROGRESS_RANGES[stage][0]
        app.events.put(("progress", (stage, 5, "Стадия")))
        app._poll_once()
        assert float(app.bar["value"]) == sum(gui.PROGRESS_RANGES[stage])
    saved = float(app.bar["value"])
    app.events.put(("progress", ("future-stage", .5, "Дополнительная проверка")))
    app._poll_once()
    assert float(app.bar["value"]) == saved and app.status.get() == "Дополнительная проверка"
    app.events.put(("done", result()))
    app._poll_once()
    assert float(app.bar["value"]) == 100 and app.status.get().startswith("Готово")


def test_callback_error_is_reported_but_poll_continues(tk_root, monkeypatch):
    app = surface(tk_root)
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    app.events.put(("progress", ("connectivity", None, "Некорректное событие")))
    app.events.put(("done", result()))
    pump(tk_root, lambda: app.status.get().startswith("Готово"))
    assert len(errors) == 1 and errors[0][0] is TypeError
    assert app._poll_after is not None


def test_pending_close_destroys_window_and_does_not_reschedule(tk_root, monkeypatch):
    app = surface(tk_root)
    app.running = True
    app.on_close()
    assert app.cancel.is_set() and app.pending_close
    destroyed = []
    original_destroy = tk_root.destroy
    # Keep the session root alive while exercising the close completion path.
    monkeypatch.setattr(tk_root, "destroy", lambda: (destroyed.append(True), app.canvas.destroy()))
    app.events.put(("error", "Отменено"))
    app.poll()
    assert destroyed == [True] and app._poll_after is None
    assert not app._window_alive()
    monkeypatch.setattr(tk_root, "destroy", original_destroy)

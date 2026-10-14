"""Run journal: all checks and errors, CSV export on demand. Synthetic data only."""
import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from anonymizer.errors import AnonymizerError
from anonymizer.pipeline import Options, run
from anonymizer.runlog import HEADER, RunLog

SOURCE = """
      CREATE TABLE public.parent(a text PRIMARY KEY, note text);
      CREATE TABLE public.child(x text REFERENCES public.parent(a), qty numeric);
      INSERT INTO public.parent VALUES ('SYN_A','SECRET_ONE'),('SYN_B','SECRET_TWO');
      INSERT INTO public.child VALUES ('SYN_A',1),('SYN_X',2);
"""


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream, delimiter=";"))


def test_success_run_logs_checks_and_export(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text(SOURCE, encoding="utf-8")
    log = RunLog()
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log))
    events = [entry[3] for entry in log.entries]
    assert events[0] == "start" and "published" in events
    (fk,) = [entry for entry in log.entries if entry[3] == "fk"]
    # Входная битая ссылка (SYN_X нет в parent) показывается до->после и не исправляется.
    assert "public.child: x" in fk[4] and "1 -> 1" in fk[5] and fk[1] == "warning"
    assert [entry for entry in log.entries if entry[3] == "key_group"]
    assert not any(entry[1] == "error" for entry in log.entries)
    # Journal is exported by explicit demand and never sits inside the package.
    target = tmp_path / "journal.csv"
    assert log.write_csv(target) == len(log.entries)
    rows = read_rows(target)
    assert rows[0] == list(HEADER) and len(rows) - 1 == len(log.entries)
    assert not (tmp_path / "out" / "journal.csv").exists()
    assert report["validation"]["foreign_keys"][0]["missing_before"] == 1


def test_error_run_logs_safe_error_and_writes_journal(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text('CREATE TABLE public.t(p jsonb); INSERT INTO public.t VALUES (\'{"SYN_BROKEN\'\);', encoding="utf-8")
    log = RunLog()
    with pytest.raises(AnonymizerError):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log))
    assert not (tmp_path / "out").exists()
    errors = [entry for entry in log.entries if entry[1] == "error"]
    assert errors and "SYN_BROKEN" not in errors[-1][5]
    target = tmp_path / "journal.csv"
    log.write_csv(target)
    rows = read_rows(target)
    assert any(row[1] == "error" for row in rows[1:])
    assert not (tmp_path / "out").exists()


def test_cancel_is_logged(tmp_path):
    from threading import Event

    from anonymizer.errors import Cancelled
    src = tmp_path / "source.sql"
    src.write_text(SOURCE, encoding="utf-8")
    log = RunLog()
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log), cancel=cancel)
    cancelled = [entry for entry in log.entries if entry[3] == "cancelled"]
    assert cancelled and cancelled[0][1] == "warning"


def test_broken_csv_log_path_does_not_affect_run(tmp_path):
    src = tmp_path / "source.sql"
    src.write_text(SOURCE, encoding="utf-8")
    log = RunLog()
    report = run(Options(src, tmp_path / "out", tmp_path / "synthetic.snpanon", log=log))
    assert report["rows"] == 4
    blocked = tmp_path / "no_such_dir" / "journal.csv"  # parent missing -> OSError to the caller (GUI shows it)
    with pytest.raises(OSError):
        log.write_csv(blocked)  # the finished run itself is unaffected


def test_header_and_delimiter_stable(tmp_path):
    log = RunLog()
    log.add("info", "import", "start")
    target = tmp_path / "journal.csv"
    assert log.write_csv(target) == 1
    rows = read_rows(target)
    assert rows[0] == list(HEADER) and rows[1][2] == "import"

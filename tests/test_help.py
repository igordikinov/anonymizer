"""Help window content: complete sections, user-safe wording, no placeholders."""
from anonymizer import __version__
from anonymizer.help_text import HELP_TEXT, show_help


def test_help_covers_all_product_areas():
    for marker in ["НАЗНАЧЕНИЕ", "КАК ПОЛЬЗОВАТЬСЯ", "ПРОЕКТ .SNPANON", "ЧТО ЗАМЕНЯЕТСЯ",
                   "ПРОВЕРКИ", "РЕЗУЛЬТАТ", "ЗАГРУЗКА В POSTGRESQL", "БЕЗОПАСНОСТЬ",
                   "dump.sql", "inserts.sql", "manifest.json", "report.json", "sys_id",
                   "TRUNCATE", "F1"]:
        assert marker in HELP_TEXT, marker


def test_help_has_no_placeholder_or_sample_values():
    for forbidden in ["TODO", "FIXME", "SYN_", "XXX", "SECRET", "localhost", "54329"]:
        assert forbidden not in HELP_TEXT, forbidden


def test_window_title_carries_version(tk_root):
    window = show_help(tk_root, __version__)
    try:
        assert window.title() == f"Справка — SNP Anonymizer {__version__}"
    finally:
        window.destroy()

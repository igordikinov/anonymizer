class AnonymizerError(Exception):
    """Сообщение без значений клиентских данных."""


class Cancelled(AnonymizerError):
    pass


class IntegrityError(AnonymizerError):
    """A failed integrity gate with an explicitly published diagnostics file."""

    def __init__(self, code, report_path, report):
        detail = report.get("error_message")
        message = f"{code}: " + (detail + " " if detail else "")
        super().__init__(message + "Набор данных не опубликован; создан диагностический отчёт.")
        self.code, self.report_path, self.report = code, report_path, report


class PgCommitUncertain(AnonymizerError):
    """COMMIT was attempted; the server outcome cannot safely be inferred."""

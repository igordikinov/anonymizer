class AnonymizerError(Exception):
    """Сообщение без значений клиентских данных."""


class Cancelled(AnonymizerError):
    pass


class IntegrityError(AnonymizerError):
    """A failed integrity gate with an explicitly published diagnostics file."""

    def __init__(self, code, report_path, report):
        super().__init__(f"{code}: набор данных не опубликован; создан диагностический отчёт.")
        self.code, self.report_path, self.report = code, report_path, report


class PgCommitUncertain(AnonymizerError):
    """COMMIT was attempted; the server outcome cannot safely be inferred."""

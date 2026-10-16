class AnonymizerError(Exception):
    """Сообщение без значений клиентских данных."""


class Cancelled(AnonymizerError):
    pass


class PgCommitUncertain(AnonymizerError):
    """COMMIT was attempted; the server outcome cannot safely be inferred."""

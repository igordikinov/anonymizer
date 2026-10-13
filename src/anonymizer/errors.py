class AnonymizerError(Exception):
    """Сообщение без значений клиентских данных."""


class Cancelled(AnonymizerError):
    pass

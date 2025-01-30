import logging
from typing import Any

# Suppress Kafka logs
logging.getLogger("kafka").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get the logger.

    Args:
        name (str): Logger name (usually __name__).

    Returns:
        logging.Logger: Logger instance.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)-8s [%(filename)s]  %(message)s'
    )
    return logging.getLogger(name)


def limit(message: Any, limit: int = 200) -> str:
    """Util to truncate a message to a certain limit.

    Args:
        message (Any): The message to truncate.
        limit (int, optional): Max message length. Defaults to 200.

    Returns:
        str: _description_
    """
    if isinstance(message, str) is False:
        message = str(message)
    if len(message) > limit:
        return message[:(limit - 3)] + '...'
    return message

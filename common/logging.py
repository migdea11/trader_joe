import logging


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s:   [%(filename)s]  %(message)s'
    )
    return logging.getLogger(name)
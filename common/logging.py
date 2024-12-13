import logging


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)-8s [%(filename)s]  %(message)s'
    )
    return logging.getLogger(name)

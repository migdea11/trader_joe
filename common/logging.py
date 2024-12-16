import logging

# Suppress Kafka logs
logging.getLogger("kafka").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)-8s [%(filename)s]  %(message)s'
    )
    return logging.getLogger(name)

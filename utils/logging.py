import logging
import os

def get_logger(name: str):
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    return logging.getLogger(name)

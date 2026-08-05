import logging

def get_logger(name="printiq"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return logging.getLogger(name)

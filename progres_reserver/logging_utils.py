import logging
import sys
from pathlib import Path

from .config import Config

LOG = logging.getLogger("progres")


def setup_logging(cfg: Config) -> None:
    level = logging.DEBUG if cfg.verbose else logging.INFO
    LOG.setLevel(level)
    LOG.propagate = False
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    if cfg.log_file:
        try:
            path = Path(cfg.log_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            LOG.addHandler(file_handler)
        except OSError:
            LOG.warning("Cannot open log file %r", cfg.log_file)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

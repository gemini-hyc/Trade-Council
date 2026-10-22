"""Small, dependency-free logging configuration."""

import logging
from pathlib import Path


def configure_logging(
    level: str = "INFO",
    *,
    log_path: Path | None = None,
    force: bool = False,
) -> None:
    """Configure console logging and an optional per-run log file."""

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=force,
    )

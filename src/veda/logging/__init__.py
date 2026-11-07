import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    """JSON logs to stdout; call once at process start (API, worker, CLI)."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # request_id etc.
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


get_logger = structlog.get_logger

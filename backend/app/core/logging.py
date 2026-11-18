import json
import logging
import re
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Formats log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        if record.exc_info:
            # Exception messages and SQL parameters can contain credentials or caller data.
            # Keep the type for diagnosis without serializing the exception payload.
            log_obj["exception_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
        return json.dumps(log_obj)


def _redact(message: str) -> str:
    """Remove common contact details and credential-bearing URLs from log messages."""
    message = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED]", message, flags=re.I
    )
    message = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s]+", "[REDACTED URL]", message, flags=re.I)
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[REDACTED]", message)


def setup_logging(level: str = "INFO") -> None:
    """Configures root logger with JSON formatting."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)

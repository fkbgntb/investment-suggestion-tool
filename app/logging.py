"""Application logging with basic secret redaction."""

import logging
import re

_LABELED_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|api[_-]?key|password|secret|token)\b"
    r"\s*[:=]\s*[^\r\n,;]+"
)
_TOKEN_LIKE_SECRET = re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b")


def redact_sensitive(value: object) -> str:
    """Redact common credential formats from a value before it is logged."""

    text = str(value)
    text = _LABELED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return _TOKEN_LIKE_SECRET.sub("[REDACTED]", text)


class SensitiveDataFilter(logging.Filter):
    """Ensure formatted log messages do not expose common secret patterns."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    """Configure process logging and attach the redaction filter."""

    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    root_logger = logging.getLogger()
    redaction_filter = SensitiveDataFilter()
    root_logger.addFilter(redaction_filter)
    for handler in root_logger.handlers:
        handler.addFilter(redaction_filter)

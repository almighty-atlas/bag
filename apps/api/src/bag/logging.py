import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Deliberate allowlist: never serialize request bodies, tokens or exception text.
        fields = {"level": record.levelname, "event": record.getMessage()}
        for key in ("item_id", "owner_id", "client_capture_id", "job_id", "processor", "attempt"):
            value = getattr(record, key, None)
            if value is not None:
                fields[key] = str(value)
        return json.dumps(fields)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("bag")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

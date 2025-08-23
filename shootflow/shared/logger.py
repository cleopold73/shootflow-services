import logging
import sys
from shootflow.shared.config import settings



def _env_log_level() -> int:
    name = settings.LOG_LEVEL
    return getattr(logging, name, logging.INFO)


def setup_basic_logging(level: int | None = None) -> None:
    """
    Configure root logger once with a stdout handler and a concise formatter.
    Respects LOG_LEVEL env var (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    Safe to call multiple times.
    """
    root = logging.getLogger()
    chosen_level = level or _env_log_level()

    if not root.handlers:
        root.setLevel(chosen_level)
        handler = logging.StreamHandler(stream=sys.stdout)
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        root.setLevel(chosen_level)

    # Optionally quiet common noisy libraries (tune as needed)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("flickr_api").setLevel(logging.WARNING)


# Usage:
# Call setup_basic_logging() once at app startup (e.g., in main or Lambda init).
# Then in any module:
#   import logging
#   logger = logging.getLogger(__name__)
#   logger.info("hello")
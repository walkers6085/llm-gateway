"""Ограниченный backoff временных ошибок модели и заголовок Retry-After."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random

TRANSIENT = {408, 429, 500, 502, 503, 504}


def retry_after(value):
    if not value:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            result = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, result) if math.isfinite(result) else None


def delay_for(error, attempt, elapsed, config):
    """None означает остановку повторов; attempt — число уже сделанных повторов."""
    if not getattr(error, 'retryable', False) or attempt >= config['max_retries']:
        return None
    base = min(config['max_delay'], config['initial_delay'] * 2 ** min(attempt, 30))
    delay = min(config['max_delay'], base + random.uniform(0, base * .2))
    if error.retry_after is not None:
        delay = max(delay, error.retry_after)
    if delay + elapsed > config['max_elapsed']:
        return None
    return delay

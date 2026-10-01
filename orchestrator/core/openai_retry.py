import logging
import re
import time
from typing import Callable, Optional, TypeVar

import openai

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Il messaggio del 429 dice quanto aspettare: "Please try again in 371ms" / "in 1.2s" / "in 6m0s".
_SUGGESTED_WAIT = re.compile(r"try again in (?:(\d+(?:\.\d+)?)m(?!s))?\s*(\d+(?:\.\d+)?)\s*(ms|s)\b", re.IGNORECASE)
_DEFAULT_WAIT_SECONDS = 2.0
_MARGIN_SECONDS = 0.5
_MAX_WAIT_SECONDS = 20.0
# Con la finestra al minuto piena un'attesa di frazioni di secondo non basta: almeno 5 s per tentativo.
_MIN_WAIT_PER_ATTEMPT_SECONDS = 5.0


def suggested_wait_seconds(message: str) -> float:
    """Attesa suggerita da OpenAI nel testo dell'errore (+ un margine), con un tetto."""
    match = _SUGGESTED_WAIT.search(message or "")
    if not match:
        return _DEFAULT_WAIT_SECONDS
    minutes, amount, unit = match.groups()
    seconds = float(amount) / 1000.0 if unit.lower() == "ms" else float(amount)
    if minutes:
        seconds += float(minutes) * 60.0
    return min(seconds + _MARGIN_SECONDS, _MAX_WAIT_SECONDS)


def _is_quota_exhausted(exc: openai.RateLimitError) -> bool:
    return "insufficient_quota" in str(exc) or getattr(exc, "code", None) == "insufficient_quota"


def call_with_rate_limit_retry(
    call: Callable[[], T], sleep: Optional[Callable[[float], None]] = None, max_retries: int = 4,
) -> T:
    """Esegue `call()`; su un 429 (limite di richieste/token al minuto) aspetta il tempo suggerito da
    OpenAI e riprova, fino a `max_retries` volte: e' un limite passeggero, non un elemento "non
    trovato". Il credito finito (insufficient_quota) non passa da solo: niente riprove.
    Ogni altro errore, e il 429 che persiste, vengono rilanciati a chi chiama."""
    sleep = sleep or time.sleep
    attempt = 0
    while True:
        try:
            return call()
        except openai.RateLimitError as exc:
            if _is_quota_exhausted(exc):
                logger.error("Credito OpenAI esaurito (insufficient_quota): non riprovo.")
                raise
            if attempt >= max_retries:
                raise
            attempt += 1
            wait = min(max(suggested_wait_seconds(str(exc)), _MIN_WAIT_PER_ATTEMPT_SECONDS * attempt), _MAX_WAIT_SECONDS)
            logger.warning(
                "Limite di richieste OpenAI raggiunto: aspetto %.1f s e riprovo (%d/%d).",
                wait, attempt, max_retries,
            )
            sleep(wait)

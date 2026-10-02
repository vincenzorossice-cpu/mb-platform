import logging
from typing import Callable

import openai

logger = logging.getLogger(__name__)

# Per modello: parametri che l'API ha rifiutato (scoperti alla prima chiamata e ricordati).
_REJECTED = {}


def _apply_known(model: str, kwargs: dict) -> None:
    rejected = _REJECTED.get(model, set())
    if "max_tokens" in rejected and "max_tokens" in kwargs:
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
    if "temperature" in rejected:
        kwargs.pop("temperature", None)


def create_with_param_fallback(create: Callable, kwargs: dict):
    """Chiama `create(**kwargs)` (es. client.chat.completions.create). I modelli piu' recenti spesso
    rifiutano `max_tokens` (vogliono `max_completion_tokens`) o un valore di `temperature` diverso dal
    predefinito: su un 400 che cita il parametro si riprova una volta senza quel problema e si ricorda
    la scelta per quel modello. Ogni altro errore passa invariato."""
    kwargs = dict(kwargs)
    model = str(kwargs.get("model", ""))
    _apply_known(model, kwargs)
    for _ in range(3):
        try:
            return create(**kwargs)
        except openai.BadRequestError as exc:
            message = str(exc).lower()
            if "max_tokens" in message and "max_tokens" in kwargs:
                _REJECTED.setdefault(model, set()).add("max_tokens")
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                logger.warning("Il modello '%s' non accetta max_tokens: uso max_completion_tokens.", model)
            elif "temperature" in message and "temperature" in kwargs:
                _REJECTED.setdefault(model, set()).add("temperature")
                kwargs.pop("temperature")
                logger.warning("Il modello '%s' non accetta questa temperature: uso quella predefinita.", model)
            else:
                raise
    return create(**kwargs)


def reset_known_rejections() -> None:
    """Solo per i test."""
    _REJECTED.clear()

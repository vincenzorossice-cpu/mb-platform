import os

import requests

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.interface import DecisionEngineClientInterface


class DecisionEngineClient(DecisionEngineClientInterface):
    """Client sincrono per il Decision Engine (API compatibile TypeSafe/Open-Jev)."""

    DECISION_ENGINE_UNREACHABLE = "DECISION_ENGINE_UNREACHABLE"
    DECISION_ENGINE_TIMEOUT = "DECISION_ENGINE_TIMEOUT"
    DECISION_ENGINE_HTTP_ERROR = "DECISION_ENGINE_HTTP_ERROR"
    DECISION_ENGINE_PARSE_ERROR = "DECISION_ENGINE_PARSE_ERROR"

    def __init__(self, base_url: str = None, timeout: int = 10):
        self.base_url = (base_url or os.environ.get("DECISION_ENGINE_URL", "http://localhost:8001")).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def ask(self, state: str, questions: dict) -> Result[dict]:
        """POST /v1/systemone → {"answers": {question_key: {choice, confidence, probabilities}}}."""
        url = f"{self.base_url}/v1/systemone"
        try:
            resp = self.session.post(
                url,
                json={"state": state, "questions": questions},
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            return Result.fail(f"Decision engine non raggiungibile: {exc}", self.DECISION_ENGINE_UNREACHABLE)
        except requests.Timeout as exc:
            return Result.fail(f"Timeout su {url}: {exc}", self.DECISION_ENGINE_TIMEOUT)
        except requests.RequestException as exc:
            return Result.fail(f"Errore di rete: {exc}", self.DECISION_ENGINE_UNREACHABLE)

        if not resp.ok:
            return Result.fail(f"HTTP {resp.status_code} da {url}", self.DECISION_ENGINE_HTTP_ERROR)

        try:
            return Result.ok(resp.json())
        except ValueError as exc:
            return Result.fail(f"JSON non valido: {exc}", self.DECISION_ENGINE_PARSE_ERROR)

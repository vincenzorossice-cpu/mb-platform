from abc import ABC, abstractmethod
from orchestrator.core.result import Result


class DecisionEngineClientInterface(ABC):
    @abstractmethod
    def ask(self, state: str, questions: dict) -> Result[dict]:
        """POST /v1/systemone. Ritorna Result.ok({"answers": {...}})."""


def choice_question(instructions: str, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul_question(instructions: str) -> dict:
    return {"type": "noul", "instructions": instructions}


def score_question(instructions: str, levels: list) -> dict:
    return {"type": "score", "instructions": instructions, "levels": levels}

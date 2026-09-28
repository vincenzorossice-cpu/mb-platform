import dataclasses
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from orchestrator.core.result import Result
from orchestrator.modules.step_logger.interface import (
    EpisodeSummary,
    StepLoggerInterface,
    StepRecord,
)

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())
    return safe or "unknown"


class JSONLStepLogger(StepLoggerInterface):
    """Un file .jsonl per episodio, append-only: un crash non perde le righe
    gia' scritte. Un file .summary.json separato a fine episodio."""

    def __init__(self, dataset_dir: str = "dataset"):
        self.dataset_dir = dataset_dir
        self._open_episodes = {}  # episode_id -> {"path": str, "slot_name": str, "started_at": str}

    def start_episode(self, slot_name: str, device_id: str) -> Result[str]:
        slug = _slugify(slot_name)
        started_at = datetime.now(timezone.utc).isoformat()
        timestamp_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        episode_id = f"{slug}_{timestamp_compact}_{uuid.uuid4().hex[:8]}"
        episode_dir = os.path.join(self.dataset_dir, slug)
        try:
            os.makedirs(episode_dir, exist_ok=True)
            path = os.path.join(episode_dir, f"{episode_id}.jsonl")
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError as exc:
            return Result.fail(f"Cannot start episode: {exc}", "EPISODE_START_IO_ERROR")

        self._open_episodes[episode_id] = {
            "path": path, "slot_name": slot_name, "device_id": device_id,
            "started_at": started_at,
        }
        return Result.ok(episode_id)

    def log_step(self, episode_id: str, record: StepRecord) -> Result[bool]:
        episode = self._open_episodes.get(episode_id)
        if episode is None:
            return Result.fail(f"Episode not started: {episode_id}", "EPISODE_NOT_STARTED")
        try:
            line = json.dumps(dataclasses.asdict(record), default=str)
            with open(episode["path"], "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, TypeError) as exc:
            return Result.fail(f"Cannot write step: {exc}", "STEP_LOG_WRITE_ERROR")
        return Result.ok(True)

    def end_episode(self, episode_id: str, summary: EpisodeSummary) -> Result[bool]:
        episode = self._open_episodes.get(episode_id)
        if episode is None:
            return Result.fail(f"Episode not started: {episode_id}", "EPISODE_NOT_STARTED")
        slug = _slugify(episode["slot_name"])
        summary_path = os.path.join(self.dataset_dir, slug, f"{episode_id}.summary.json")
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(summary), f, default=str, indent=2)
        except OSError as exc:
            return Result.fail(f"Cannot write summary: {exc}", "EPISODE_START_IO_ERROR")
        del self._open_episodes[episode_id]
        return Result.ok(True)

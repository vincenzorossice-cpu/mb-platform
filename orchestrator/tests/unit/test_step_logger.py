import json
import os
from unittest.mock import mock_open, patch

from orchestrator.modules.step_logger.interface import EpisodeSummary, StepRecord
from orchestrator.modules.step_logger.logger import JSONLStepLogger


def _step_record(episode_id: str, step_index: int = 1, **overrides) -> StepRecord:
    defaults = dict(
        episode_id=episode_id, step_index=step_index, timestamp="2026-09-28T10:00:00+00:00",
        slot_name="pirots5", device_id="ZT322ZSJVS", phase="bet_setup", action_type="tap",
        element_name="bet_minus", strategy_used="profile", requested_coords=(540, 2160),
        balance_before="10.00", balance_after="10.00", bet_before="1.00", bet_after="0.50",
        win_amount=None, verified=True, diff_score=0.5, outcome="success",
        error_code=None, notes=None,
    )
    defaults.update(overrides)
    return StepRecord(**defaults)


def _summary(episode_id: str, slot_name: str = "pirots5", **overrides) -> EpisodeSummary:
    defaults = dict(
        episode_id=episode_id, slot_name=slot_name, device_id="ZT322ZSJVS",
        started_at="2026-09-28T10:00:00+00:00", ended_at="2026-09-28T10:05:00+00:00",
        total_steps=3, total_spins=1, total_played="0.50", final_outcome="target_reached",
    )
    defaults.update(overrides)
    return EpisodeSummary(**defaults)


class TestStartEpisode:
    def test_creates_empty_jsonl_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        result = logger.start_episode("pirots5", "ZT322ZSJVS")
        assert result.success
        assert result.data
        expected_path = os.path.join("dataset", "pirots5", f"{result.data}.jsonl")
        assert os.path.exists(expected_path)
        assert os.path.getsize(expected_path) == 0

    def test_two_calls_produce_distinct_episode_ids(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        r1 = logger.start_episode("pirots5", "ZT322ZSJVS")
        r2 = logger.start_episode("pirots5", "ZT322ZSJVS")
        assert r1.data != r2.data
        assert os.path.exists(os.path.join("dataset", "pirots5", f"{r1.data}.jsonl"))
        assert os.path.exists(os.path.join("dataset", "pirots5", f"{r2.data}.jsonl"))


class TestLogStep:
    def test_three_steps_produce_three_parseable_lines(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        episode_id = logger.start_episode("pirots5", "ZT322ZSJVS").data
        for i in range(1, 4):
            result = logger.log_step(episode_id, _step_record(episode_id, step_index=i))
            assert result.success

        path = os.path.join("dataset", "pirots5", f"{episode_id}.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 3
        assert [row["step_index"] for row in lines] == [1, 2, 3]

    def test_never_started_episode_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        result = logger.log_step("ghost_episode", _step_record("ghost_episode"))
        assert not result.success
        assert result.error_code == "EPISODE_NOT_STARTED"
        assert not os.path.exists("dataset")

    def test_decimal_string_fields_serialized_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        episode_id = logger.start_episode("pirots5", "ZT322ZSJVS").data
        logger.log_step(episode_id, _step_record(episode_id, balance_before="12.34"))
        path = os.path.join("dataset", "pirots5", f"{episode_id}.jsonl")
        with open(path, encoding="utf-8") as f:
            row = json.loads(f.readline())
        assert row["balance_before"] == "12.34"

    def test_write_error_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        episode_id = logger.start_episode("pirots5", "ZT322ZSJVS").data
        with patch("orchestrator.modules.step_logger.logger.open", mock_open()) as mocked:
            mocked.side_effect = OSError("disk full")
            result = logger.log_step(episode_id, _step_record(episode_id))
        assert not result.success
        assert result.error_code == "STEP_LOG_WRITE_ERROR"

    def test_two_episodes_do_not_cross_contaminate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        ep_a = logger.start_episode("pirots5", "ZT322ZSJVS").data
        ep_b = logger.start_episode("bigbass", "ZT322ZSJVS").data
        logger.log_step(ep_a, _step_record(ep_a, slot_name="pirots5"))
        logger.log_step(ep_b, _step_record(ep_b, slot_name="bigbass"))
        logger.log_step(ep_a, _step_record(ep_a, step_index=2, slot_name="pirots5"))

        path_a = os.path.join("dataset", "pirots5", f"{ep_a}.jsonl")
        path_b = os.path.join("dataset", "bigbass", f"{ep_b}.jsonl")
        with open(path_a, encoding="utf-8") as f:
            rows_a = [json.loads(line) for line in f]
        with open(path_b, encoding="utf-8") as f:
            rows_b = [json.loads(line) for line in f]
        assert len(rows_a) == 2
        assert len(rows_b) == 1
        assert all(row["slot_name"] == "pirots5" for row in rows_a)
        assert all(row["slot_name"] == "bigbass" for row in rows_b)

    def test_append_only_previous_lines_untouched(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        episode_id = logger.start_episode("pirots5", "ZT322ZSJVS").data
        path = os.path.join("dataset", "pirots5", f"{episode_id}.jsonl")
        seen_lines = []
        for i in range(1, 5):
            logger.log_step(episode_id, _step_record(episode_id, step_index=i))
            with open(path, encoding="utf-8") as f:
                current_lines = f.readlines()
            assert current_lines[:len(seen_lines)] == seen_lines
            seen_lines = current_lines


class TestEndEpisode:
    def test_writes_summary_and_closes_episode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        episode_id = logger.start_episode("pirots5", "ZT322ZSJVS").data
        result = logger.end_episode(episode_id, _summary(episode_id))
        assert result.success
        summary_path = os.path.join("dataset", "pirots5", f"{episode_id}.summary.json")
        assert os.path.exists(summary_path)

        follow_up = logger.log_step(episode_id, _step_record(episode_id))
        assert not follow_up.success
        assert follow_up.error_code == "EPISODE_NOT_STARTED"

    def test_unknown_episode_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = JSONLStepLogger(dataset_dir="dataset")
        result = logger.end_episode("ghost", _summary("ghost"))
        assert not result.success
        assert result.error_code == "EPISODE_NOT_STARTED"

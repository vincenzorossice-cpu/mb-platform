import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest

from orchestrator.core import model_metrics
from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
from orchestrator.scripts import model_report


def _lines(tmp_path):
    path = tmp_path / "model_metrics.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def sol():
    with patch("orchestrator.modules.decision_engine.gpt_client.openai.OpenAI"):
        return GPTDecisionClient(api_key="k", model="gpt-6.1-sol")


def _response(tokens_in=1000, tokens_out=50):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out),
                           choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))])


class TestWriting:
    def test_a_call_is_written_with_model_purpose_seconds_and_tokens(self, tmp_path, sol):
        sol.set_run("navigazione_casino", "SISAL")
        sol.record_usage(_response(), "visione", seconds=3.21)

        record = _lines(tmp_path)[0]
        assert record["type"] == "call" and record["model"] == "gpt-6.1-sol" and record["purpose"] == "visione"
        assert record["seconds"] == 3.21 and record["tokens_in"] == 1000 and record["tokens_out"] == 50
        assert record["task"] == "navigazione_casino" and record["run_id"].endswith("navigazione_casino_SISAL")

    def test_an_unwritable_file_never_breaks_the_caller(self, monkeypatch, sol):
        monkeypatch.setenv("MODEL_METRICS_FILE", "/proc/nonexistent/dir/m.jsonl")

        sol.record_usage(_response(), "visione", seconds=1.0)      # nessuna eccezione
        model_metrics.log_run(type_of_run="slot", model="x")

    def test_events_and_runs_are_written(self, tmp_path, sol):
        sol.record_event("cella_non_valida", "griglia icone")
        model_metrics.log_run(type_of_run="navigazione", model="gpt-6.1-sol", steps=13)

        kinds = [(r["type"], r.get("kind")) for r in _lines(tmp_path)]
        assert kinds == [("event", "cella_non_valida"), ("run", None)]

    def test_an_empty_answer_is_counted_as_an_event(self, tmp_path, sol):
        empty = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=0),
                                choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        with patch.object(sol.client.chat.completions, "create", return_value=empty):
            sol._create_completion_once([{"role": "user", "content": "x"}], 100, "visione")

        assert any(r.get("kind") == "risposta_non_valida" for r in _lines(tmp_path))

    def test_a_timeout_is_recorded_with_its_seconds(self, tmp_path, sol):
        with patch.object(sol.client.chat.completions, "create", side_effect=openai.APITimeoutError(request=MagicMock())):
            result = sol._create_completion_once([{"role": "user", "content": "x"}], 100, "visione")

        assert not result.success
        failed = [r for r in _lines(tmp_path) if r["type"] == "call"][0]
        assert failed["outcome"] == "timeout" and failed["seconds"] is not None


class TestPrices:
    def test_cost_from_the_price_file(self):
        assert model_metrics.compute_cost("gpt-6.1-sol", 1_000_000, 100_000) == pytest.approx(3.0)

    def test_cached_tokens_cost_less(self):
        assert model_metrics.compute_cost("gpt-6.1-sol", 1_000_000, 0, cached_tokens=500_000) == pytest.approx(1.05)

    def test_unknown_model_without_env_has_no_cost(self, monkeypatch):
        monkeypatch.delenv("GPT_PRICE_INPUT_PER_M", raising=False)
        assert model_metrics.compute_cost("gpt-x", 1000, 10) is None

    def test_a_missing_or_broken_price_file_is_tolerated(self, tmp_path):
        broken = tmp_path / "p.json"
        broken.write_text("{nope")
        assert model_metrics.load_prices(str(broken)) == {} and model_metrics.load_prices(str(tmp_path / "x")) == {}

    def test_the_price_file_is_complete_for_sol_and_mini(self):
        prices = model_metrics.load_prices()
        assert prices["gpt-6.1-sol"] == {"input": 2.0, "cached_input": 0.10, "output": 10.0}
        assert prices["gpt-4o-mini"]["output"] == 0.60


def _write(tmp_path, records):
    path = tmp_path / "m.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8")
    return str(path)


def _call(run_id, model="gpt-6.1-sol", purpose="visione", seconds=4.0, tokens_in=1000, tokens_out=100, outcome="ok",
          task="navigazione_casino"):
    return {"type": "call", "ts": "2026-10-02T17:00:00", "run_id": run_id, "task": task, "model": model,
            "purpose": purpose, "seconds": seconds, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cached_tokens": 0, "outcome": outcome}


class TestReport:
    def _records(self):
        return [
            _call("r1", seconds=2.0), _call("r1", seconds=4.0), _call("r1", seconds=10.0),
            _call("r1", purpose="griglia icone", seconds=3.0),
            _call("r1", model="gpt-4o-mini", purpose="importi", seconds=1.0),
            _call("r1", seconds=30.0, outcome="timeout", tokens_in=0, tokens_out=0),
            {"type": "event", "ts": "2026-10-02T17:00:00", "run_id": "r1", "task": "navigazione_casino",
             "model": "gpt-6.1-sol", "kind": "cella_non_valida"},
            {"type": "run", "ts": "2026-10-02T17:05:00", "run_id": "r1", "task": "navigazione_casino",
             "type_of_run": "navigazione", "model": "gpt-6.1-sol", "done_declared": True, "verified_by_user": True,
             "steps": 13, "seconds": 280.0, "operator_interventions": 0, "supervisor_calls": 0},
            {"type": "run", "ts": "2026-10-02T17:20:00", "run_id": "r2", "task": "navigazione_casino",
             "type_of_run": "navigazione", "model": "gpt-6.1-sol", "done_declared": True, "verified_by_user": False,
             "steps": 25, "seconds": 535.0, "operator_interventions": 0, "supervisor_calls": 0},
        ]

    def test_latency_stats_exclude_failed_calls_and_report_them(self):
        result = model_report.aggregate(self._records(), model="gpt-6.1-sol")

        visione = result["calls"][("gpt-6.1-sol", "visione")]
        assert visione["calls"] == 4 and visione["failures"] == 1
        assert visione["sec_avg"] == pytest.approx(5.333, abs=0.01) and visione["sec_median"] == 4.0
        assert visione["sec_p95"] == 10.0

    def test_cost_is_computed_from_tokens_and_prices(self):
        result = model_report.aggregate(self._records(), model="gpt-6.1-sol")

        # 4 chiamate riuscite: 4000 token in a $2/M + 400 out a $10/M
        assert result["models"]["gpt-6.1-sol"]["cost"] == pytest.approx(4000 * 2 / 1e6 + 400 * 10 / 1e6)
        assert "gpt-4o-mini" not in result["models"]

    def test_the_model_filter_also_filters_runs_and_events(self):
        result = model_report.aggregate(self._records(), model="gpt-4o-mini")

        assert result["runs"] == {} and result["events"] == {}

    def test_run_success_uses_the_users_verdict_and_run_cost_covers_all_models(self):
        result = model_report.aggregate(self._records())

        run = result["runs"][("navigazione_casino", "gpt-6.1-sol")]
        assert run["runs"] == 2 and run["done_declared"] == 2
        assert (run["verified_ok"], run["verified_total"]) == (1, 2)
        assert run["steps_avg"] == 19 and run["seconds_avg"] == pytest.approx(407.5)
        # r1 = 4 chiamate Sol + 1 mini; r2 senza chiamate => media = meta' del costo di r1
        assert run["cost_avg"] is not None and run["cost_avg"] > 0

    def test_missing_price_gives_unknown_cost_not_zero(self):
        result = model_report.aggregate([_call("r1", model="gpt-x")], prices={})

        assert result["models"]["gpt-x"]["cost"] is None
        assert "n/d" in model_report.format_report(result)

    def test_slot_runs_are_summarised(self):
        records = [{"type": "run", "ts": "2026-10-02T18:00:00", "run_id": "s1", "task": "slot_autopilot",
                    "type_of_run": "slot", "model": "gpt-6.1-sol", "failed": False, "spins": 30,
                    "seconds_per_spin": 9.5, "seconds": 300.0, "operator_interventions": 2, "supervisor_calls": 3}]

        text = model_report.format_report(model_report.aggregate(records))

        assert "spin medi 30.0" in text and "secondi per spin 9.5" in text and "interventi operatore 2" in text

    def test_the_report_has_the_three_sections_and_a_csv(self, tmp_path):
        result = model_report.aggregate(self._records())
        text = model_report.format_report(result)
        csv_path = tmp_path / "r.csv"
        model_report.write_csv(result, str(csv_path))

        assert "PERFORMANCE" in text and "COSTO" in text and "TEMPO" in text
        assert "95o percentile" in text and "FALLITE 1" in text
        assert "gpt-6.1-sol,visione,4,1" in csv_path.read_text(encoding="utf-8")

    def test_main_reads_the_file_and_filters_by_date(self, tmp_path, capsys):
        path = _write(tmp_path, self._records())

        assert model_report.main(["--file", path, "--since", "2026-10-03"]) == 1
        assert model_report.main(["--file", path, "--model", "gpt-6.1-sol"]) == 0
        assert "gpt-6.1-sol" in capsys.readouterr().out

    def test_percentile_is_nearest_rank(self):
        assert model_report.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
        assert model_report.percentile([], 0.95) is None

import pytest
from orchestrator.core.result import Result
from orchestrator.modules.multiplier_engine.calculator import MultiplierEngine


@pytest.fixture
def engine():
    return MultiplierEngine()


class TestCalculateCascade:

    def test_equal_profit_triple(self, engine):
        """Verifica che la strategia equal-profit produca stesso profitto in ogni scenario"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95],
            commission=0.045
        )
        assert result.success
        profits = [e.profit_if_won for e in result.data.events]
        assert max(profits) - min(profits) < 0.01
        assert all(p > 0 for p in profits)

    def test_negative_profit_allowed(self, engine):
        """Quote lay molto alte su quote back basse → profitto negativo (qualifying loss)"""
        result = engine.calculate_cascade(
            back_stake=10.0,
            back_odds=[1.1, 1.1, 1.1],
            lay_odds=[5.0, 5.0, 5.0],
            commission=0.045
        )
        assert result.success
        assert all(p < 0 for p in result.data.profit_per_scenario.values())
        assert any("qualifying loss" in w.lower() for w in result.data.warnings)

    def test_invalid_odds(self, engine):
        """Quote <= 1.0 devono essere rifiutate"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[0.9, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95]
        )
        assert not result.success
        assert result.error_code == "INVALID_ODDS"

    def test_mismatched_odds_length(self, engine):
        """Numero quote back != lay deve fallire"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95]
        )
        assert not result.success
        assert result.error_code == "INVALID_ODDS"

    def test_profit_per_scenario_keys(self, engine):
        """Verifica che profit_per_scenario abbia le chiavi corrette"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95]
        )
        assert "event_1_won" in result.data.profit_per_scenario
        assert "event_2_won" in result.data.profit_per_scenario
        assert "event_3_won" in result.data.profit_per_scenario
        assert "all_lost" in result.data.profit_per_scenario

    def test_total_liability_is_max(self, engine):
        """Total liability deve essere il massimo tra gli eventi"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95]
        )
        max_liability = max(e.liability for e in result.data.events)
        assert result.data.total_liability == max_liability

    def test_double_triple_same_result(self, engine):
        """Due chiamate con stessi input devono dare stesso output (determinismo)"""
        r1 = engine.calculate_cascade(100.0, [2.0, 2.0, 2.0], [1.95, 1.95, 1.95])
        r2 = engine.calculate_cascade(100.0, [2.0, 2.0, 2.0], [1.95, 1.95, 1.95])
        assert r1.data.events[0].lay_stake == r2.data.events[0].lay_stake
        assert r1.data.events[2].profit_if_won == r2.data.events[2].profit_if_won

    def test_different_odds_per_event(self, engine):
        """Quote diverse per ogni evento con margine sufficiente"""
        result = engine.calculate_cascade(
            back_stake=100.0,
            back_odds=[2.0, 3.0, 4.0],
            lay_odds=[1.90, 2.85, 3.80],
            commission=0.045
        )
        assert result.success
        assert result.data.events[0].lay_odds == 1.90
        assert result.data.events[2].lay_odds == 3.80


class TestCalculateSingle:

    def test_single_profit(self, engine):
        """Calcolo singola scommessa"""
        result = engine.calculate_single(
            back_stake=100.0,
            back_odds=2.0,
            lay_odds=1.95,
            commission=0.045
        )
        assert result.success
        assert result.data.lay_stake > 0
        assert result.data.liability > 0
        assert result.data.profit_if_won > 0

    def test_single_negative_profit(self, engine):
        """Singola con profitto negativo (qualifying loss)"""
        result = engine.calculate_single(
            back_stake=10.0,
            back_odds=1.1,
            lay_odds=5.0
        )
        assert result.success
        assert result.data.profit_if_won <= 0


class TestRecalculateFromIndex:

    def test_recalc_from_index_1(self, engine):
        """Ricalcolo dal secondo evento"""
        result = engine.recalculate_from_index(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95],
            commission=0.045,
            from_index=1
        )
        assert result.success
        assert len(result.data.events) == 2

    def test_recalc_negative_profit(self, engine):
        """Ricalcolo con quote che producono profitto negativo (qualifying loss)"""
        result = engine.recalculate_from_index(
            back_stake=100.0,
            back_odds=[2.0, 1.1, 1.1],
            lay_odds=[1.95, 5.0, 5.0],
            commission=0.045,
            from_index=1
        )
        assert result.success
        assert all(p < 0 for p in result.data.profit_per_scenario.values())

    def test_recalc_invalid_index(self, engine):
        """Indice non valido"""
        result = engine.recalculate_from_index(
            back_stake=100.0,
            back_odds=[2.0, 2.0, 2.0],
            lay_odds=[1.95, 1.95, 1.95],
            commission=0.045,
            from_index=5
        )
        assert not result.success
        assert result.error_code == "INVALID_ODDS"

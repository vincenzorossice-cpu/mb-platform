import pytest

from orchestrator.modules.screen_reader.interface import BalanceReading
from orchestrator.modules.rules_engine.interface import Account
from orchestrator.modules.rules_engine.engine import RulesEngine


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

def _make_balance(
    raw: float = 100.0,
    open_bets: float = 0.0,
    real_bonus: float = 0.0,
    wagering: float | None = None,
    confidence: float = 0.95,
) -> BalanceReading:
    return BalanceReading(
        raw_balance=raw,
        open_bets=open_bets,
        real_bonus=real_bonus,
        wagering_bonus=wagering,
        read_method="ui_dump",
        confidence=confidence,
    )


def _make_account(
    book: str = "Sisal",
    is_betfair: bool = False,
) -> Account:
    return Account(
        account_id="acc-001",
        identity_id="id-001",
        book=book,
        is_betfair=is_betfair,
    )


@pytest.fixture
def engine() -> RulesEngine:
    return RulesEngine()


# ------------------------------------------------------------------
# tests
# ------------------------------------------------------------------

class TestReconcileNoAdjustment:
    def test_small_difference_below_threshold(self, engine: RulesEngine):
        """Differenza < 0.50 → no_adjustment."""
        br = _make_balance(raw=100.30)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.action == "no_adjustment"
        assert data.rectification is None
        assert "below_materiality_threshold" in data.rules_applied


class TestReconcileAdjustBook:
    def test_difference_above_threshold_non_betfair(self, engine: RulesEngine):
        """Differenza >= 0.50, non Betfair → adjust, column BOOK."""
        br = _make_balance(raw=105.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.action == "adjust"
        assert data.column == "BOOK"
        assert data.rectification is not None
        assert data.rectification.column == "BOOK"
        assert data.rectification.amount == pytest.approx(5.0)


class TestReconcileAdjustBanco:
    def test_difference_betfair_uses_book(self, engine: RulesEngine):
        """Differenza >= 0.50, Betfair → adjust, column BOOK (non BANCO per evitare fee Netcap)."""
        br = _make_balance(raw=110.0)
        account = _make_account(book="Betfair", is_betfair=True)
        result = engine.reconcile(br, expected_balance=100.0, account=account)
        assert result.success
        data = result.data
        assert data.action == "adjust"
        assert data.column == "BOOK"
        assert data.rectification.column == "BOOK"


class TestReconcileAnomalousDifference:
    def test_difference_above_500_triggers_hitl(self, engine: RulesEngine):
        """Differenza > 500 → hitl_required."""
        br = _make_balance(raw=700.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.action == "hitl_required"
        assert data.rectification is None
        assert "anomalous_difference_hitl" in data.rules_applied


class TestReconcileLowConfidence:
    def test_low_confidence_triggers_hitl(self, engine: RulesEngine):
        """Confidence < 0.85 → hitl_required prima di qualsiasi calcolo."""
        br = _make_balance(raw=200.0, confidence=0.70)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.action == "hitl_required"
        assert "low_confidence_hitl" in data.rules_applied


class TestReconcileWageringBonus:
    def test_wagering_bonus_not_summed_but_noted(self, engine: RulesEngine):
        """wagering_bonus NON va nel adjusted_balance, ma genera bonus_note."""
        br = _make_balance(raw=100.0, wagering=30.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        # adjusted = 100 + 0 + 0 = 100, non 130
        assert data.adjusted_balance == pytest.approx(100.0)
        assert data.bonus_note == "BONUS: 30.00"
        assert "wagering_bonus_noted" in data.rules_applied


class TestReconcileRealBonus:
    def test_real_bonus_summed_to_adjusted(self, engine: RulesEngine):
        """real_bonus va sommato al adjusted_balance."""
        br = _make_balance(raw=80.0, real_bonus=20.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.adjusted_balance == pytest.approx(100.0)


class TestReconcileOpenBets:
    def test_open_bets_not_summed_to_adjusted_but_used_for_crosscheck(self, engine: RulesEngine):
        """open_bets NON va piu' sommato al adjusted_balance (solo raw + real_bonus).
        Viene usato solo come cross-check contro impegnato_netcap: se le due fonti
        coincidono (entro soglia), la riconciliazione procede normalmente."""
        br = _make_balance(raw=90.0, open_bets=15.0)
        result = engine.reconcile(
            br, expected_balance=100.0, account=_make_account(), impegnato_netcap=15.0,
        )
        assert result.success
        data = result.data
        assert data.adjusted_balance == pytest.approx(90.0)
        assert data.difference == pytest.approx(5.0)
        assert data.action == "adjust"


class TestReconcileBetfairColumnBanco:
    def test_betfair_always_book(self, engine: RulesEngine):
        """Betfair → colonna BOOK in riconciliazione (non BANCO per evitare fee Netcap)."""
        br = _make_balance(raw=100.0)
        account = _make_account(book="Betfair", is_betfair=True)
        result = engine.reconcile(br, expected_balance=100.0, account=account)
        assert result.success
        assert result.data.column == "BOOK"
        assert "betfair_banco_column" in result.data.rules_applied


class TestReconcileRulesAppliedTracked:
    def test_rules_list_not_empty(self, engine: RulesEngine):
        """rules_applied contiene almeno le regole base."""
        br = _make_balance(raw=105.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        assert len(result.data.rules_applied) > 0
        assert "adjusted_balance_calc" in result.data.rules_applied
        assert "adjustment_required" in result.data.rules_applied


class TestReconcileExactZeroDifference:
    def test_zero_difference(self, engine: RulesEngine):
        """Differenza = 0.0 → no_adjustment."""
        br = _make_balance(raw=100.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.difference == pytest.approx(0.0)
        assert data.action == "no_adjustment"
        assert data.rectification is None


class TestReconcileNegativeDifference:
    def test_negative_difference_adjusts(self, engine: RulesEngine):
        """Saldo reale < atteso → adjust, amount negativo."""
        br = _make_balance(raw=95.0)
        result = engine.reconcile(br, expected_balance=100.0, account=_make_account())
        assert result.success
        data = result.data
        assert data.difference == pytest.approx(-5.0)
        assert data.action == "adjust"
        assert data.rectification.amount == pytest.approx(-5.0)


# ------------------------------------------------------------------
# P6 fix — matrix (totale) vs disponibile Netcap (matrix - impegnato)
# ------------------------------------------------------------------

class TestReconcileWithImpegnatoMatch:
    def test_reconcile_with_impegnato_match(self, engine: RulesEngine):
        """Caso reale ID 12/BET365: book=12.92, matrix=212.92, impegnato=200 →
        disponibile Netcap = 12.92 = disponibile telefono → diff 0, no_adjustment
        (prima del fix: 12.92 vs 212.92 avrebbe generato una rettifica di -200€)."""
        br = _make_balance(raw=12.92)
        result = engine.reconcile(
            br, expected_balance=212.92, account=_make_account(), impegnato_netcap=200.0,
        )
        assert result.success
        data = result.data
        assert data.difference == pytest.approx(0.0)
        assert data.action == "no_adjustment"
        assert data.rectification is None


class TestReconcileWithImpegnatoMismatch:
    def test_reconcile_with_impegnato_mismatch(self, engine: RulesEngine):
        """book=5.92, matrix=212.92, impegnato=200 → disponibile Netcap=12.92,
        discrepanza reale di -7€ → adjust."""
        br = _make_balance(raw=5.92)
        result = engine.reconcile(
            br, expected_balance=212.92, account=_make_account(), impegnato_netcap=200.0,
        )
        assert result.success
        data = result.data
        assert data.difference == pytest.approx(-7.0)
        assert data.action == "adjust"
        assert data.rectification.amount == pytest.approx(-7.0)


class TestReconcileImpegnatoZero:
    def test_reconcile_impegnato_zero(self, engine: RulesEngine):
        """Nessuna scommessa aperta (impegnato=0): book=1194.57, matrix=897.39 →
        disponibile Netcap=897.39, diff=+297.18 → adjust."""
        br = _make_balance(raw=1194.57)
        result = engine.reconcile(
            br, expected_balance=897.39, account=_make_account(), impegnato_netcap=0.0,
        )
        assert result.success
        data = result.data
        assert data.difference == pytest.approx(297.18)
        assert data.action == "adjust"
        assert data.rectification.amount == pytest.approx(297.18)


class TestReconcileOpenBetsMismatchHitl:
    def test_reconcile_open_bets_mismatch_hitl(self, engine: RulesEngine):
        """open_bets (telefono) e impegnato_netcap divergono di piu' di 5€ →
        hitl_required, nessuna rettifica automatica."""
        br = _make_balance(raw=100.0, open_bets=200.0)
        result = engine.reconcile(
            br, expected_balance=100.0, account=_make_account(), impegnato_netcap=0.0,
        )
        assert result.success
        data = result.data
        assert data.action == "hitl_required"
        assert data.rectification is None
        assert "open_bets_netcap_mismatch" in data.rules_applied


class TestReconcileOpenBetsMatch:
    def test_reconcile_open_bets_match(self, engine: RulesEngine):
        """open_bets e impegnato_netcap coincidono (entro soglia) → il cross-check
        passa e la riconciliazione procede normalmente sul disponibile."""
        br = _make_balance(raw=100.0, open_bets=200.0)
        result = engine.reconcile(
            br, expected_balance=300.0, account=_make_account(), impegnato_netcap=200.0,
        )
        assert result.success
        data = result.data
        assert "open_bets_netcap_mismatch" not in data.rules_applied
        assert data.difference == pytest.approx(0.0)
        assert data.action == "no_adjustment"

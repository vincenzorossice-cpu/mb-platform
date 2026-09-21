from datetime import datetime, timedelta

import pytest

from orchestrator.modules.signal_parser.interface import Signal
from orchestrator.modules.signal_parser.promo_filter import PromoFilter


def _signal(deadline: str = None, priority: str = "media") -> Signal:
    if deadline is None:
        deadline = (datetime.now() + timedelta(hours=6)).isoformat()
    return Signal(
        telegram_message_id="tm1",
        book="SISAL",
        category="sport",
        bonus_type="ricorrente",
        difficulty="facile",
        strategy={},
        bonus={},
        deadline=deadline,
        priority=priority,
        expected_profit="alto",
    )


@pytest.fixture
def promo_filter() -> PromoFilter:
    return PromoFilter()


class TestFilterPromo:
    def test_promo_filter_skip_low_va(self, promo_filter):
        result = promo_filter.filter_promo(
            _signal(), expected_value_eur=0.5, capital_required=10.0, available_capital=1000.0,
        )
        assert result.success
        assert result.data.action == "skip"
        assert result.data.reason == "Valore atteso troppo basso"

    def test_promo_filter_skip_low_capital(self, promo_filter):
        result = promo_filter.filter_promo(
            _signal(), expected_value_eur=5.0, capital_required=100.0, available_capital=50.0,
        )
        assert result.success
        assert result.data.action == "skip"
        assert result.data.reason == "Capitale insufficiente per VA basso"

    def test_promo_filter_warning(self, promo_filter):
        result = promo_filter.filter_promo(
            _signal(), expected_value_eur=5.0, capital_required=50.0, available_capital=100.0,
        )
        assert result.success
        assert result.data.action == "warning"
        assert result.data.reason == "VA basso ma possibile"

    def test_promo_filter_proceed(self, promo_filter):
        result = promo_filter.filter_promo(
            _signal(priority="alta"), expected_value_eur=25.0, capital_required=50.0, available_capital=100.0,
        )
        assert result.success
        assert result.data.action == "proceed"
        assert result.data.priority == "alta"

    def test_promo_filter_flash(self, promo_filter):
        near_deadline = (datetime.now() + timedelta(hours=1)).isoformat()
        result = promo_filter.filter_promo(
            _signal(deadline=near_deadline), expected_value_eur=25.0,
            capital_required=50.0, available_capital=100.0,
        )
        assert result.success
        assert result.data.priority == "flash"


class TestDeterminePriority:
    def test_flash_beats_high_va(self, promo_filter):
        near_deadline = (datetime.now() + timedelta(minutes=30)).isoformat()
        assert promo_filter.determine_priority(_signal(deadline=near_deadline), 50.0) == "flash"

    def test_bassa_for_low_va(self, promo_filter):
        assert promo_filter.determine_priority(_signal(priority="bassa"), 3.0) == "bassa"

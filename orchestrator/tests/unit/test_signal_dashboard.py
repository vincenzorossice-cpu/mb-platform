from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from orchestrator.core.result import Result
from orchestrator.dashboard.app import _find_placeholders, app
from orchestrator.flows.sport_flow import SignalFlowResult
from orchestrator.modules.rag_system.interface import TCVerification
from orchestrator.modules.signal_parser.interface import Signal
from orchestrator.modules.signal_parser.promo_filter import PromoFilterResult
from orchestrator.modules.telegram_acquirer.interface import AcquiredSignal


@pytest.fixture(autouse=True)
def reset_rag_singleton():
    import orchestrator.dashboard.app as app_module
    app_module._rag_system = None
    yield
    app_module._rag_system = None


def _signal(**overrides):
    defaults = dict(
        telegram_message_id="acq_dev1_123",
        book="Stanleybet",
        category="sport",
        bonus_type="ricorrente",
        difficulty="facile",
        strategy={},
        bonus={},
        deadline="2026-12-31T23:59:00",
        priority="alta",
        expected_profit="12.50",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _acquired_signal(**overrides):
    defaults = dict(
        raw_text="ricarica 🔡🔡€",
        ocr_text="ricarica 🔡🔡€",
        emoji_values={},
        source="telegram_telethon",
        device_id="pc",
        detected_at="2026-09-21T10:00:00+00:00",
        channel_name="MB Evo Pro",
        message_id="100",
    )
    defaults.update(overrides)
    return AcquiredSignal(**defaults)


def _flow_result(**overrides):
    defaults = dict(
        signal=_signal(),
        tc_verification=TCVerification(verified=True, discrepancies=[], matched_docs=[]),
        cascade_calc=None,
        identity_ids_available=["01"],
        status="pending_hitl",
    )
    defaults.update(overrides)
    return SignalFlowResult(**defaults)


# --------------------------------------------------------------------------
# _find_placeholders (pure logic)
# --------------------------------------------------------------------------

def test_find_placeholders_detects_importo_ricarica():
    placeholders = _find_placeholders("Effettua una ricarica 🔡🔡€ entro oggi")
    keys = [p["key"] for p in placeholders]
    assert "importo_ricarica" in keys


def test_find_placeholders_detects_profitto_atteso():
    placeholders = _find_placeholders("PROFITTO MEDIO:🔡🔡€ garantito")
    keys = [p["key"] for p in placeholders]
    assert "profitto_atteso" in keys


def test_find_placeholders_none_when_clean():
    assert _find_placeholders("Nessun valore nascosto qui, 100€ ricarica") == []


def test_find_placeholders_detects_all_three():
    text = "ricarica 🔡🔡€ bonus 🔡🔡€ PROFITTO:🔡🔡€"
    placeholders = _find_placeholders(text)
    keys = {p["key"] for p in placeholders}
    assert keys == {"importo_ricarica", "importo_bonus", "profitto_atteso"}


# --------------------------------------------------------------------------
# POST /acquire_signal
# --------------------------------------------------------------------------

def test_acquire_signal_success():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.capture_signal.return_value = Result.ok(_acquired_signal())
        mock_cls.return_value = mock_instance

        client = TestClient(app)
        response = client.post("/acquire_signal")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["telegram_message_id"] == "100"
    keys = [p["key"] for p in body["placeholders"]]
    assert "importo_ricarica" in keys


def test_acquire_signal_no_signal_found():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.capture_signal.return_value = Result.fail(
            "Nessun segnale trovato negli ultimi 50 messaggi", "NO_SIGNAL_FOUND",
        )
        mock_cls.return_value = mock_instance

        client = TestClient(app)
        response = client.post("/acquire_signal")

    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "NO_SIGNAL_FOUND"


# --------------------------------------------------------------------------
# POST /submit_signal
# --------------------------------------------------------------------------

def _submit_payload(**overrides):
    payload = dict(
        ocr_text="ricarica 100€",
        manual_values={},
        telegram_message_id="acq_dev1_123",
        expected_value_eur=15.0,
        capital_required=50.0,
        available_capital=200.0,
    )
    payload.update(overrides)
    return payload


def test_submit_signal_proceed_path():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_acquirer_cls, \
         patch("orchestrator.dashboard.app.SignalParser") as mock_parser_cls, \
         patch("orchestrator.dashboard.app.PromoFilter") as mock_filter_cls, \
         patch("orchestrator.dashboard.app.SportFlow") as mock_flow_cls, \
         patch("orchestrator.dashboard.app.SportFlowHandler") as mock_handler_cls:

        mock_acquirer_cls.return_value.merge_text_and_values.return_value = "ricarica 100€"
        mock_parser_cls.return_value.parse.return_value = Result.ok(_signal())
        mock_filter_cls.return_value.filter_promo.return_value = Result.ok(PromoFilterResult(
            action="proceed", priority="alta", reason="VA sufficiente e capitale disponibile",
            capital_required=50.0, capital_available=200.0,
        ))
        mock_flow_cls.return_value.process_signal.return_value = Result.ok(_flow_result())
        mock_handler_cls.return_value.save_pending_flow.return_value = Result.ok("sport_acq_dev1_123_999")

        client = TestClient(app)
        response = client.post("/submit_signal", json=_submit_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["filter_action"] == "proceed"
    assert body["hitl_item_id"] == "sport_acq_dev1_123_999"
    assert body["flow_status"] == "pending_hitl"


def test_submit_signal_skip_path():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_acquirer_cls, \
         patch("orchestrator.dashboard.app.SignalParser") as mock_parser_cls, \
         patch("orchestrator.dashboard.app.PromoFilter") as mock_filter_cls, \
         patch("orchestrator.dashboard.app.SportFlow") as mock_flow_cls, \
         patch("orchestrator.dashboard.app.SportFlowHandler") as mock_handler_cls:

        mock_acquirer_cls.return_value.merge_text_and_values.return_value = "ricarica 100€"
        mock_parser_cls.return_value.parse.return_value = Result.ok(_signal())
        mock_filter_cls.return_value.filter_promo.return_value = Result.ok(PromoFilterResult(
            action="skip", priority="bassa", reason="Valore atteso troppo basso",
            capital_required=50.0, capital_available=200.0,
        ))

        client = TestClient(app)
        response = client.post("/submit_signal", json=_submit_payload(expected_value_eur=0.5))

        mock_flow_cls.return_value.process_signal.assert_not_called()
        mock_handler_cls.return_value.save_pending_flow.assert_not_called()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["filter_action"] == "skip"


def test_submit_signal_warning_path():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_acquirer_cls, \
         patch("orchestrator.dashboard.app.SignalParser") as mock_parser_cls, \
         patch("orchestrator.dashboard.app.PromoFilter") as mock_filter_cls, \
         patch("orchestrator.dashboard.app.SportFlow") as mock_flow_cls, \
         patch("orchestrator.dashboard.app.SportFlowHandler") as mock_handler_cls:

        mock_acquirer_cls.return_value.merge_text_and_values.return_value = "ricarica 100€"
        mock_parser_cls.return_value.parse.return_value = Result.ok(_signal())
        mock_filter_cls.return_value.filter_promo.return_value = Result.ok(PromoFilterResult(
            action="warning", priority="bassa", reason="VA basso ma possibile",
            capital_required=50.0, capital_available=200.0,
        ))
        mock_flow_cls.return_value.process_signal.return_value = Result.ok(_flow_result())
        mock_handler_cls.return_value.save_pending_flow.return_value = Result.ok("sport_acq_dev1_123_999")

        client = TestClient(app)
        response = client.post("/submit_signal", json=_submit_payload(expected_value_eur=5.0))

        mock_handler_cls.return_value.save_pending_flow.assert_called_once()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["filter_action"] == "warning"


def test_submit_signal_obscured_values():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_acquirer_cls, \
         patch("orchestrator.dashboard.app.SignalParser") as mock_parser_cls, \
         patch("orchestrator.dashboard.app.PromoFilter") as mock_filter_cls, \
         patch("orchestrator.dashboard.app.SportFlow") as mock_flow_cls, \
         patch("orchestrator.dashboard.app.SportFlowHandler") as mock_handler_cls:

        mock_acquirer_cls.return_value.merge_text_and_values.return_value = "ricarica 🔡🔡€"
        mock_parser_cls.return_value.parse.return_value = Result.fail(
            "Obscured animated values in signal", "OBSCURED_VALUES",
        )

        client = TestClient(app)
        response = client.post("/submit_signal", json=_submit_payload())

        mock_filter_cls.return_value.filter_promo.assert_not_called()
        mock_flow_cls.return_value.process_signal.assert_not_called()
        mock_handler_cls.return_value.save_pending_flow.assert_not_called()

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"] == "Alcuni valori non sono stati risolti"
    assert body["error_code"] == "OBSCURED_VALUES"


def test_submit_signal_telegram_message_id_roundtrip():
    with patch("orchestrator.dashboard.app.TelegramAcquirer") as mock_acquirer_cls, \
         patch("orchestrator.dashboard.app.SignalParser") as mock_parser_cls, \
         patch("orchestrator.dashboard.app.PromoFilter") as mock_filter_cls, \
         patch("orchestrator.dashboard.app.SportFlow") as mock_flow_cls, \
         patch("orchestrator.dashboard.app.SportFlowHandler") as mock_handler_cls:

        mock_acquirer_cls.return_value.merge_text_and_values.return_value = "ricarica 100€"
        mock_parser_cls.return_value.parse.return_value = Result.ok(_signal())
        mock_filter_cls.return_value.filter_promo.return_value = Result.ok(PromoFilterResult(
            action="proceed", priority="alta", reason="VA sufficiente e capitale disponibile",
            capital_required=50.0, capital_available=200.0,
        ))
        mock_flow_cls.return_value.process_signal.return_value = Result.ok(_flow_result())
        mock_handler_cls.return_value.save_pending_flow.return_value = Result.ok("sport_custom_id_999")

        client = TestClient(app)
        client.post("/submit_signal", json=_submit_payload(telegram_message_id="custom_id"))

        parse_call_args = mock_parser_cls.return_value.parse.call_args
        assert parse_call_args.args[1] == "custom_id"
        process_call_args = mock_flow_cls.return_value.process_signal.call_args
        assert process_call_args.args[1] == "custom_id"


def test_submit_signal_missing_fields():
    client = TestClient(app)
    payload = _submit_payload()
    del payload["expected_value_eur"]
    response = client.post("/submit_signal", json=payload)
    assert response.status_code == 400
    assert response.json()["success"] is False


# --------------------------------------------------------------------------
# RAGSystem lazy singleton
# --------------------------------------------------------------------------

def test_get_rag_system_is_cached():
    from orchestrator.dashboard.app import _get_rag_system

    with patch("orchestrator.dashboard.app.RAGSystem") as mock_rag_cls:
        mock_rag_cls.return_value = MagicMock()

        first = _get_rag_system()
        second = _get_rag_system()

        mock_rag_cls.assert_called_once()
        assert first is second

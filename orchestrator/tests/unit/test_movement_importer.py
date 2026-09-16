import pytest
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.movement_importer.importer import MovementImporter
from orchestrator.modules.movement_importer.interface import Movement
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement
from orchestrator.modules.screen_reader.reader import ScreenReader


# ------------------------------------------------------------------
# fixtures / mock factories
# ------------------------------------------------------------------

def _mock_app_api_client():
    client = MagicMock()
    client._get_json.return_value = Result.ok([])
    client._post_json.return_value = Result.ok({"id": 501})
    return client


def _mock_screen_reader():
    reader = MagicMock()
    # delega alla vera logica _clean_italian_amount/_extract_amount, non un MagicMock
    # generico, per non duplicare il parsing italiano nei test
    real = ScreenReader()
    reader._clean_italian_amount.side_effect = real._clean_italian_amount
    reader._extract_amount.side_effect = real._extract_amount
    return reader


@pytest.fixture
def app_api_client():
    return _mock_app_api_client()


@pytest.fixture
def screen_reader():
    return _mock_screen_reader()


HYPE_DEPOSITO_EMAIL = "Pagamento di EUR 50,00 effettuato da conto verso Bookmaker XYZ in data 13/09/2026"
HYPE_PRELIEVO_EMAIL = "Ti abbiamo addebitato EUR 30,00 sul tuo conto. Data operazione: 10/09/2026"
HYPE_MIGLIAIA_EMAIL = "Pagamento di EUR 1.234,56 effettuato da conto verso Bookmaker XYZ in data 01/09/2026"


class TestImportHype:

    def test_import_hype_email_parsing(self, app_api_client, screen_reader):
        importer = MovementImporter(app_api_client, screen_reader, email_bodies=[HYPE_DEPOSITO_EMAIL])
        result = importer.import_hype("03")
        assert result.success
        assert len(result.data) == 1
        m = result.data[0]
        assert m.importo == 50.00
        assert m.data == "2026-09-13"
        assert m.fonte == "hype_email"
        assert m.confidence == 0.95

    def test_import_hype_deposito(self, app_api_client, screen_reader):
        importer = MovementImporter(app_api_client, screen_reader, email_bodies=[HYPE_DEPOSITO_EMAIL])
        result = importer.import_hype("03")
        assert result.success
        assert result.data[0].tipo == "deposito"

    def test_import_hype_prelievo(self, app_api_client, screen_reader):
        importer = MovementImporter(app_api_client, screen_reader, email_bodies=[HYPE_PRELIEVO_EMAIL])
        result = importer.import_hype("03")
        assert result.success
        assert result.data[0].tipo == "prelievo"

    def test_import_hype_separatori_migliaia(self, app_api_client, screen_reader):
        importer = MovementImporter(app_api_client, screen_reader, email_bodies=[HYPE_MIGLIAIA_EMAIL])
        result = importer.import_hype("03")
        assert result.success
        assert result.data[0].importo == 1234.56

    def test_import_hype_empty_folder(self, app_api_client, screen_reader, capsys):
        importer = MovementImporter(app_api_client, screen_reader, email_folder="/nonexistent/path")
        result = importer.import_hype("03")
        assert result.success
        assert result.data == []
        captured = capsys.readouterr()
        assert "Nessuna email trovata" in captured.out


class TestImportPaypal:

    def _screen_content(self, texts):
        elements = [UIElement(text=t, bounds=[0, 0, 0, 0]) for t in texts]
        return ScreenContent(elements=elements, screen_type="movements", confidence=0.9, method="uiautomator")

    def test_import_paypal_ocr_success(self, app_api_client, screen_reader):
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(["Inviato 25,00"]))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert len(result.data) == 1
        assert result.data[0].fonte == "paypal_ocr"

    def test_import_paypal_deposito(self, app_api_client, screen_reader):
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(["Inviato 25,00"]))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].tipo == "deposito"

    def test_import_paypal_prelievo(self, app_api_client, screen_reader):
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(["Ricevuto 40,00"]))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].tipo == "prelievo"

    def test_import_paypal_ocr_failed(self, app_api_client, screen_reader):
        screen_reader.read_screen.return_value = Result.fail("device offline", "DEVICE_OFFLINE")
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert not result.success
        assert result.error_code == MovementImporter.PAYPAL_OCR_FAILED


class TestWriteMovementsToNetcap:

    def _movement(self, **overrides):
        base = dict(
            tipo="deposito", importo=30.0, conto_id=6, bookmaker_id=38,
            bookmaker_nome=None, data="2026-09-13", collaboratore_id="03",
            descrizione="Import automatico Hype", fonte="hype_email", confidence=0.95,
        )
        base.update(overrides)
        return Movement(**base)

    def test_write_movements_deduplicazione(self, app_api_client, screen_reader):
        existing = [{"collaboratore_id": "03", "conto_id": 6, "data": "2026-09-13", "importo": 30.0}]
        app_api_client._get_json.return_value = Result.ok(existing)
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.write_movements_to_netcap([self._movement()])
        assert result.success
        assert result.data[0].message == "Duplicato, skip"
        app_api_client._post_json.assert_not_called()

    def test_write_movements_success(self, app_api_client, screen_reader):
        app_api_client._get_json.return_value = Result.ok([])
        app_api_client._post_json.return_value = Result.ok({"id": 501})
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.write_movements_to_netcap([self._movement()])
        assert result.success
        assert result.data[0].success
        assert result.data[0].netcap_id == 501
        called_body = app_api_client._post_json.call_args[0][1]
        assert called_body["tag"] == "auto_import"
        assert called_body["conto_id"] == 6

    def test_write_movements_partial_failure(self, app_api_client, screen_reader):
        app_api_client._get_json.return_value = Result.ok([])
        app_api_client._post_json.side_effect = [
            Result.ok({"id": 1}),
            Result.fail("HTTP 500", "NETCAP_HTTP_ERROR"),
            Result.ok({"id": 3}),
        ]
        movements = [self._movement(importo=10), self._movement(importo=20), self._movement(importo=30)]
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.write_movements_to_netcap(movements)
        assert result.success
        assert len(result.data) == 3
        assert result.data[0].success and result.data[2].success
        assert not result.data[1].success


class TestCheckConsentExpiry:

    def test_check_consent_stub(self, app_api_client, screen_reader, capsys):
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.check_consent_expiry("03")
        assert result.success
        assert result.data.is_valid is True
        assert result.data.collaboratore_id == "03"
        captured = capsys.readouterr()
        assert "GoCardless non configurato" in captured.out

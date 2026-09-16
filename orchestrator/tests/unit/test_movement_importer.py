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

# FIX-PAYPAL-PARSER: struttura reale osservata via debug — ogni movimento è un blocco di 4
# elementi consecutivi [descrizione][importo][data][tipo], intervallato da elementi di UI
# (menu, filtri) che non matchano il pattern importo e vengono naturalmente saltati.
MOCK_PAYPAL_ELEMENTS = [
    UIElement(text="Cronologia", bounds=[0, 0, 0, 0]),
    UIElement(text="Tutto", bounds=[0, 0, 0, 0]),
    UIElement(text="Questa settimana", bounds=[0, 0, 0, 0]),
    UIElement(text="TSG Italy S.R.L", bounds=[0, 0, 0, 0]),
    UIElement(text="-40 â‚¬", bounds=[0, 0, 0, 0]),
    UIElement(text="13 set 2026", bounds=[0, 0, 0, 0]),
    UIElement(text="Pagamento", bounds=[0, 0, 0, 0]),
    UIElement(text="Marathonbet Italy S.r.l", bounds=[0, 0, 0, 0]),
    UIElement(text="+87,50 â‚¬", bounds=[0, 0, 0, 0]),
    UIElement(text="13 set 2026", bounds=[0, 0, 0, 0]),
    UIElement(text="Pagamento ricevuto", bounds=[0, 0, 0, 0]),
    UIElement(text="Settimana scorsa", bounds=[0, 0, 0, 0]),
    UIElement(text="BPG SRL", bounds=[0, 0, 0, 0]),
    UIElement(text="-10 â‚¬", bounds=[0, 0, 0, 0]),
    UIElement(text="8 set 2026", bounds=[0, 0, 0, 0]),
    UIElement(text="Pagamento", bounds=[0, 0, 0, 0]),
    UIElement(text="Sisal", bounds=[0, 0, 0, 0]),
    UIElement(text="-50 â‚¬", bounds=[0, 0, 0, 0]),
    UIElement(text="8 set 2026", bounds=[0, 0, 0, 0]),
    UIElement(text="Pagamento", bounds=[0, 0, 0, 0]),
    UIElement(text="Vincenzo Gallicola", bounds=[0, 0, 0, 0]),
    UIElement(text="+50 â‚¬", bounds=[0, 0, 0, 0]),
    UIElement(text="8 set 2026", bounds=[0, 0, 0, 0]),
    UIElement(text="Denaro ricevuto", bounds=[0, 0, 0, 0]),
]

# FIX-PAYPAL-PARSER: struttura reale osservata via debug — ogni movimento è un blocco di 4
# elementi consecutivi [descrizione][importo][data][tipo], intervallato da elementi di UI
# (menu, filtri) che non matchano il pattern importo e vengono naturalmente saltati.
MOCK_PAYPAL_ELEMENTS = [
    UIElement(text="Cronologia", bounds=[0, 0, 100, 50]),
    UIElement(text="Tutto", bounds=[0, 0, 100, 50]),
    UIElement(text="Questa settimana", bounds=[0, 0, 100, 50]),
    UIElement(text="TSG Italy S.R.L", bounds=[0, 0, 100, 50]),
    UIElement(text="-40 â‚¬", bounds=[0, 0, 100, 50]),
    UIElement(text="13 set 2026", bounds=[0, 0, 100, 50]),
    UIElement(text="Pagamento", bounds=[0, 0, 100, 50]),
    UIElement(text="Marathonbet Italy S.r.l", bounds=[0, 0, 100, 50]),
    UIElement(text="+87,50 â‚¬", bounds=[0, 0, 100, 50]),
    UIElement(text="13 set 2026", bounds=[0, 0, 100, 50]),
    UIElement(text="Pagamento ricevuto", bounds=[0, 0, 100, 50]),
    UIElement(text="Settimana scorsa", bounds=[0, 0, 100, 50]),
    UIElement(text="BPG SRL", bounds=[0, 0, 100, 50]),
    UIElement(text="-10 â‚¬", bounds=[0, 0, 100, 50]),
    UIElement(text="8 set 2026", bounds=[0, 0, 100, 50]),
    UIElement(text="Pagamento", bounds=[0, 0, 100, 50]),
    UIElement(text="Sisal", bounds=[0, 0, 100, 50]),
    UIElement(text="-50 â‚¬", bounds=[0, 0, 100, 50]),
    UIElement(text="8 set 2026", bounds=[0, 0, 100, 50]),
    UIElement(text="Pagamento", bounds=[0, 0, 100, 50]),
    UIElement(text="Vincenzo Gallicola", bounds=[0, 0, 100, 50]),
    UIElement(text="+50 â‚¬", bounds=[0, 0, 100, 50]),
    UIElement(text="8 set 2026", bounds=[0, 0, 100, 50]),
    UIElement(text="Denaro ricevuto", bounds=[0, 0, 100, 50]),
]


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

    def test_import_paypal_multiple_movements(self, app_api_client, screen_reader):
        screen = ScreenContent(elements=MOCK_PAYPAL_ELEMENTS, screen_type="movements", confidence=0.9, method="uiautomator")
        screen_reader.read_screen.return_value = Result.ok(screen)
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert len(result.data) == 5

        tsg = next(m for m in result.data if m.descrizione == "TSG Italy S.R.L")
        assert tsg.importo == 40.0
        assert tsg.tipo == "deposito"
        assert tsg.data == "2026-09-13"

        marathonbet = next(m for m in result.data if m.descrizione == "Marathonbet Italy S.r.l")
        assert marathonbet.importo == 87.50
        assert marathonbet.tipo == "prelievo"

        gallicola = next(m for m in result.data if m.descrizione == "Vincenzo Gallicola")
        assert gallicola.importo == 50.0
        assert gallicola.tipo == "prelievo"  # "Denaro ricevuto"

    def test_import_paypal_negative_amount(self, app_api_client, screen_reader):
        elements = ["TSG Italy S.R.L", "-40 â‚¬", "13 set 2026", "Pagamento"]
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(elements))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].importo == 40.0
        assert result.data[0].tipo == "deposito"

    def test_import_paypal_positive_amount(self, app_api_client, screen_reader):
        elements = ["Marathonbet Italy S.r.l", "+87,50 â‚¬", "13 set 2026", "Pagamento ricevuto"]
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(elements))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].importo == 87.50
        assert result.data[0].tipo == "prelievo"

    def test_import_paypal_italian_date(self, app_api_client, screen_reader):
        importer = MovementImporter(app_api_client, screen_reader)
        assert importer._parse_italian_date("13 set 2026") == "2026-09-13"
        assert importer._parse_italian_date("8 set 2026") == "2026-09-08"

    def test_import_paypal_missing_data(self, app_api_client, screen_reader):
        # l'importo è l'ultimo elemento della lista: nessun elemento data/tipo successivo
        elements = ["TSG Italy S.R.L", "-40 â‚¬"]
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(elements))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].confidence == 0.7

    def test_import_paypal_corrupted_euro(self, app_api_client, screen_reader):
        elements = ["Sisal", "-50 â‚¬", "8 set 2026", "Pagamento"]
        screen_reader.read_screen.return_value = Result.ok(self._screen_content(elements))
        importer = MovementImporter(app_api_client, screen_reader)
        result = importer.import_paypal("device1", "03")
        assert result.success
        assert result.data[0].importo == 50.0
        assert result.data[0].tipo == "deposito"

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

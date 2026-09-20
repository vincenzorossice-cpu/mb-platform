import pytest
from unittest.mock import patch, MagicMock
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.screen_reader.interface import UIElement


@pytest.fixture
def reader():
    return ScreenReader(ocr_confidence_threshold=0.85, adb_timeout=5)


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout" resource-id="" text="">
    <node bounds="[100,200][800,300]" class="android.widget.TextView" resource-id="com.book.app:id/balance_label" text="Saldo: € 123.45" />
    <node bounds="[100,350][800,450]" class="android.widget.Button" resource-id="com.book.app:id/deposit_btn" text="Deposita" />
    <node bounds="[100,500][800,600]" class="android.widget.TextView" resource-id="com.book.app:id/bonus_text" text="Bonus: € 30.00" />
  </node>
</hierarchy>"""

SAMPLE_XML_MYBETS = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout" text="">
    <node bounds="[100,200][800,300]" class="android.widget.TextView" resource-id="" text="Le mie scommesse" />
    <node bounds="[100,350][800,450]" class="android.widget.TextView" resource-id="" text="matched" />
    <node bounds="[100,500][800,600]" class="android.widget.TextView" resource-id="" text="2.45" />
  </node>
</hierarchy>"""

EMPTY_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout" text="" />
</hierarchy>"""

SAMPLE_XML_MYBETS_UNKNOWN = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout" text="">
    <node bounds="[100,200][800,300]" class="android.widget.TextView" resource-id="" text="Le mie scommesse" />
    <node bounds="[100,350][800,450]" class="android.widget.TextView" resource-id="" text="Schermata inattesa, nessuna informazione disponibile" />
  </node>
</hierarchy>"""

SAMPLE_XML_MYBETS_NO_BETS = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout" text="">
    <node bounds="[100,200][800,300]" class="android.widget.TextView" resource-id="" text="Le mie scommesse" />
    <node bounds="[100,350][800,450]" class="android.widget.TextView" resource-id="" text="Non sono presenti scommesse aperte" />
  </node>
</hierarchy>"""


class TestUiautomatorDump:

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_uiautomator_success(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader._uiautomator_dump("device_001")
        assert result.success
        assert "<hierarchy" in result.data

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_uiautomator_device_offline(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="device offline")
        result = reader._uiautomator_dump("device_001")
        assert not result.success
        assert result.error_code == "DEVICE_OFFLINE"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_uiautomator_timeout(self, mock_run, reader):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="adb", timeout=5)
        result = reader._uiautomator_dump("device_001")
        assert not result.success
        assert result.error_code == "ADB_TIMEOUT"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_uiautomator_flag_secure(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = reader._uiautomator_dump("device_001")
        assert not result.success
        assert result.error_code == "UIAUTOMATOR_BLOCKED"


class TestParseXml:

    def test_parse_valid_xml(self, reader):
        elements = reader._parse_xml(SAMPLE_XML)
        assert len(elements) >= 2
        texts = [e.text for e in elements]
        assert "Saldo: € 123.45" in texts
        assert "Deposita" in texts

    def test_parse_empty_xml(self, reader):
        elements = reader._parse_xml(EMPTY_XML)
        assert len(elements) == 0

    def test_parse_bounds(self, reader):
        elements = reader._parse_xml(SAMPLE_XML)
        balance_elem = [e for e in elements if "Saldo" in e.text][0]
        assert balance_elem.bounds == [100, 200, 800, 300]

    def test_parse_resource_id(self, reader):
        elements = reader._parse_xml(SAMPLE_XML)
        balance_elem = [e for e in elements if "Saldo" in e.text][0]
        assert balance_elem.resource_id == "com.book.app:id/balance_label"


class TestReadScreen:

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_uiautomator_success(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader.read_screen("device_001")
        assert result.success
        assert result.data.method == "uiautomator"
        assert result.data.confidence == 1.0
        assert result.data.screen_type == "balance_page"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_device_offline(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="device offline")
        result = reader.read_screen("device_001")
        assert not result.success
        assert result.error_code == "DEVICE_OFFLINE"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_adb_timeout(self, mock_run, reader):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="adb", timeout=5)
        result = reader.read_screen("device_001")
        assert not result.success
        assert result.error_code == "ADB_TIMEOUT"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_cache_hit(self, mock_run, reader):
        """FIX-B: seconda lettura entro la TTL non deve rifare il round-trip ADB."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        first = reader.read_screen("device_001", screen_type="balance_page", use_cache=True)
        calls_after_first = mock_run.call_count
        second = reader.read_screen("device_001", screen_type="balance_page", use_cache=True)
        assert mock_run.call_count == calls_after_first
        assert second.success
        assert second.data == first.data

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_cache_expired(self, mock_run, reader):
        """FIX-B: con cache_ttl=0 la entry e' sempre scaduta -> rilegge da ADB."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        reader.read_screen("device_001", screen_type="balance_page", use_cache=True)
        calls_after_first = mock_run.call_count
        reader.read_screen("device_001", screen_type="balance_page", use_cache=True, cache_ttl=0)
        assert mock_run.call_count > calls_after_first

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_screen_force_refresh_bypasses_cache(self, mock_run, reader):
        """FIX-22: force_refresh=True salta la cache anche entro la TTL, ma
        ricache il risultato fresco per le letture successive non forzate."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        reader.read_screen("device_001", screen_type="balance_page", use_cache=True)
        calls_after_first = mock_run.call_count

        reader.read_screen("device_001", screen_type="balance_page", use_cache=True, force_refresh=True)
        assert mock_run.call_count > calls_after_first

        calls_after_forced = mock_run.call_count
        reader.read_screen("device_001", screen_type="balance_page", use_cache=True)
        assert mock_run.call_count == calls_after_forced  # cache ripopolata dal forced read


class TestFindElement:

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_find_element_by_text(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader.find_element("device_001", "Deposita")
        assert result.success
        assert "Deposita" in result.data.text

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_find_element_by_resource_id(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader.find_element("device_001", "balance_label", field="resource_id")
        assert result.success
        assert "Saldo" in result.data.text

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_find_element_not_found(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader.find_element("device_001", "Prelievo")
        assert not result.success
        assert result.error_code == "NO_ELEMENTS_FOUND"


class TestReadBalance:

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_balance_success(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML, stderr="")
        result = reader.read_balance("device_001", "Stanleybet")
        assert result.success
        assert result.data.raw_balance == 123.45
        assert result.data.read_method == "uiautomator"


class TestReadBetStatus:

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_bet_status_matched(self, mock_run, reader):
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML_MYBETS, stderr="")
        result = reader.read_bet_status("device_001", betfair=True)
        assert result.success
        assert result.data.status == "matched"
        assert result.data.lay_odds == 2.45

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_bet_status_unknown_screen(self, mock_run, reader):
        """FIX-5: schermata non riconosciuta -> status='unknown', mai 'unsettled' di default."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML_MYBETS_UNKNOWN, stderr="")
        result = reader.read_bet_status("device_001", betfair=True)
        assert result.success
        assert result.data.status == "unknown"

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_bet_status_no_bets(self, mock_run, reader):
        """FIX-A: 'nessuna scommessa aperta' -> not_found, non 'unknown'."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML_MYBETS_NO_BETS, stderr="")
        result = reader.read_bet_status("device_001", betfair=True)
        assert result.success
        assert result.data.status == "not_found"
        assert result.data.lay_odds is None
        assert result.data.confidence == 1.0

    @patch("orchestrator.modules.screen_reader.reader.subprocess.run")
    def test_read_bet_status_force_refresh_passed_through(self, mock_run, reader):
        """FIX-22: force_refresh su read_bet_status deve propagarsi a read_screen,
        cosi' ogni chiamata forzata rilegge invece di servire la cache 5s."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_XML_MYBETS, stderr="")
        reader.read_bet_status("device_001", betfair=True, force_refresh=True)
        calls_after_first = mock_run.call_count
        reader.read_bet_status("device_001", betfair=True, force_refresh=True)
        assert mock_run.call_count > calls_after_first


class TestExtractBalance:

    def test_extract_balance_betfair_standalone(self, reader):
        """FIX-BETFAIR-STANDALONE: saldo senza etichetta, solo importo con simbolo euro."""
        texts = ["â‚¬1,194.57", "Vincenzo Carfora - C.F. CRFVCN00H15E791G"]
        result = reader._extract_balance(texts, "betfair")
        assert result is not None
        assert result["raw"] == 1194.57

    def test_extract_balance_standalone_with_migliaia(self, reader):
        texts = ["â‚¬2,500.00"]
        result = reader._extract_balance(texts, "betfair")
        assert result is not None
        assert result["raw"] == 2500.00

    def test_extract_balance_standalone_not_quota(self, reader):
        """Un numero decimale senza simbolo euro e' una quota, non un saldo."""
        texts = ["1.80"]
        result = reader._extract_balance(texts, "betfair")
        assert result is None


class TestClassifyScreen:

    def test_classify_balance_page(self, reader):
        elements = [UIElement(text="Saldo disponibile: € 500", bounds=[0,0,100,100])]
        assert reader._classify_screen(elements) == "balance_page"

    def test_classify_my_bets(self, reader):
        elements = [UIElement(text="Le mie scommesse", bounds=[0,0,100,100])]
        assert reader._classify_screen(elements) == "my_bets"

    def test_classify_market_page(self, reader):
        elements = [UIElement(text="Mercati disponibili 1X2", bounds=[0,0,100,100])]
        assert reader._classify_screen(elements) == "market_page"

    def test_classify_unknown(self, reader):
        elements = [UIElement(text="Qualcos'altro", bounds=[0,0,100,100])]
        assert reader._classify_screen(elements) == "unknown"

    def test_classify_betfair_standalone_euro(self, reader):
        """FIX-CLASSIFY-STANDALONE: saldo senza etichetta, solo importo standalone."""
        elements = [UIElement(text="â‚¬1,194.57", bounds=[0, 0, 100, 100])]
        assert reader._classify_screen(elements) == "balance_page"

    def test_classify_standalone_euro_with_other_elements(self, reader):
        """L'importo standalone deve vincere su 'unknown' anche con altri elementi
        della UI Betfair che non fanno match con nessuna keyword esistente."""
        elements = [
            UIElement(text="â‚¬1,194.57", bounds=[0, 0, 100, 100]),
            UIElement(text="Vincenzo Carfora - C.F. CRFVCN00H15E791G", bounds=[0, 0, 100, 100]),
            UIElement(text="Scommesse", bounds=[0, 0, 100, 100]),
            UIElement(text="Home", bounds=[0, 0, 100, 100]),
            UIElement(text="Menu", bounds=[0, 0, 100, 100]),
        ]
        assert reader._classify_screen(elements) == "balance_page"


class TestValidateScreenConfidence:

    def test_validate_confidence_standalone_euro(self, reader):
        """FIX-CONFIDENCE-STANDALONE: saldo Betfair senza etichetta ma con importo
        standalone -> confidence sufficiente a superare il gate MIN_CONFIDENCE (0.85)."""
        elements = [
            UIElement(text="â‚¬1,194.57", bounds=[0, 0, 100, 100]),
            UIElement(text="Vincenzo Carfora - C.F. CRFVCN00H15E791G", bounds=[0, 0, 100, 100]),
            UIElement(text="Scommesse", bounds=[0, 0, 100, 100]),
            UIElement(text="Home", bounds=[0, 0, 100, 100]),
            UIElement(text="Menu", bounds=[0, 0, 100, 100]),
        ]
        assert reader._validate_screen_confidence(elements, "balance_page") == 0.85

    def test_validate_confidence_with_label(self, reader):
        """Regressione: etichetta 'Saldo' presente con importo -> confidence invariata a 1.0."""
        elements = [UIElement(text="Saldo: € 500,00", bounds=[0, 0, 100, 100])]
        assert reader._validate_screen_confidence(elements, "balance_page") == 1.0

    def test_validate_confidence_no_label_no_amount(self, reader):
        """Né etichetta né importo standalone -> confidence bassa (0.3, non più 0.5).
        Regression guard per il cambio di fallback richiesto dal fix."""
        elements = [UIElement(text="Qualcos'altro", bounds=[0, 0, 100, 100])]
        assert reader._validate_screen_confidence(elements, "balance_page") == 0.3

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

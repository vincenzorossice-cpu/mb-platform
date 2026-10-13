import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from orchestrator.modules.screen_reader.reader import ScreenReader, parse_raw_screencap
from orchestrator.modules.slot_autopilot import session as session_module
from orchestrator.modules.slot_autopilot.frame_diff import diff_score, wait_until_stable
from orchestrator.core.result import Result
from orchestrator.scripts import benchmark_capture

RUN = "orchestrator.modules.screen_reader.reader.subprocess.run"


def _raw(width=4, height=3, fmt=1, header=12, color=(10, 200, 30)):
    pixels = bytes(color + (255,)) * (width * height)
    head = struct.pack("<III", width, height, fmt) + (b"\x00" * 4 if header == 16 else b"")
    return head + pixels


class TestParseRaw:
    @pytest.mark.parametrize("header", [12, 16])
    def test_both_header_sizes_are_understood(self, header):
        width, height, pixels = parse_raw_screencap(_raw(header=header))

        assert (width, height, len(pixels)) == (4, 3, 4 * 3 * 4)

    @pytest.mark.parametrize("data", [b"", b"x" * 10, _raw()[:-5], _raw() + b"jj", _raw(fmt=4)])
    def test_unexpected_data_gives_none(self, data):
        assert parse_raw_screencap(data) is None


class TestRawCapture:
    def test_default_mode_is_the_png_command_unchanged(self, tmp_path):
        assert ScreenReader(screenshot_dir=str(tmp_path)).capture_mode == "png"

    def test_the_mode_comes_from_the_environment_and_unknown_values_fall_back(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ADB_CAPTURE_MODE", "RAW")
        assert ScreenReader(screenshot_dir=str(tmp_path)).capture_mode == "raw"
        monkeypatch.setenv("ADB_CAPTURE_MODE", "boh")
        assert ScreenReader(screenshot_dir=str(tmp_path)).capture_mode == "png"

    def test_raw_mode_runs_plain_screencap_and_saves_a_valid_png(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="raw")

        with patch(RUN, return_value=SimpleNamespace(stdout=_raw(width=6, height=5, color=(10, 200, 30)), returncode=0)) as run:
            result = reader._screenshot("dev1")

        assert run.call_args[0][0] == ["adb", "-s", "dev1", "exec-out", "screencap"]      # senza -p
        assert result.success
        with Image.open(result.data) as image:
            assert image.size == (6, 5) and image.convert("RGB").getpixel((0, 0)) == (10, 200, 30)

    def test_a_bad_raw_answer_falls_back_to_the_png_command(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="raw")
        calls = []

        def run(cmd, stdout=None, **kwargs):
            calls.append(cmd)
            if cmd[-1] == "-p":
                stdout.write(b"\x89PNG-bytes")
                return MagicMock(returncode=0)
            return SimpleNamespace(stdout=b"garbage", returncode=0)

        with patch(RUN, side_effect=run):
            result = reader._screenshot("dev1")

        assert result.success and calls[-1][-1] == "-p"
        assert reader.capture_stats["raw_failures"] == 1
        assert "raw" not in reader._disabled_methods          # in modo 'raw' si riprova al giro dopo

    def test_auto_mode_gives_up_on_raw_after_the_first_failure(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="auto")
        calls = []

        def run(cmd, stdout=None, **kwargs):
            calls.append(cmd)
            if cmd[-1] == "-p":
                stdout.write(b"\x89PNG-bytes")
                return MagicMock(returncode=0)
            return SimpleNamespace(stdout=b"garbage", returncode=0)

        with patch(RUN, side_effect=run):
            reader._screenshot("dev1")
            reader._screenshot("dev1")

        assert [c[-1] for c in calls] == ["screencap | gzip -1", "screencap", "-p", "-p"]   # i metodi grezzi solo la prima volta

    def test_captures_are_counted_with_their_seconds(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path))

        def run(cmd, stdout=None, **kwargs):
            stdout.write(b"\x89PNG-bytes")
            return MagicMock(returncode=0)

        with patch(RUN, side_effect=run):
            reader._screenshot("dev1")
            reader._screenshot("dev1")

        assert reader.capture_stats["calls"] == 2 and reader.capture_stats["seconds"] >= 0


class TestDecodeOnce:
    def _frames(self, tmp_path, count, same=True):
        paths = []
        for index in range(count):
            path = tmp_path / f"{'still' if same else 'moving'}{index}.png"
            Image.new("RGB", (60, 120), "white" if same else (index * 40, 0, 0)).save(path)
            paths.append(str(path))
        return paths

    def test_wait_until_stable_decodes_each_frame_once(self, tmp_path):
        paths = self._frames(tmp_path, 4, same=False) + self._frames(tmp_path, 2)
        frames = iter(paths)
        opened = []
        real_open = Image.open

        def counting_open(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        with patch("orchestrator.modules.slot_autopilot.frame_diff.Image.open", side_effect=counting_open):
            result = wait_until_stable(lambda: Result.ok(next(frames)), 5.0, 0.0, 0.02)

        assert result.success
        assert max(opened.count(p) for p in set(opened)) == 1       # nessun frame decodificato due volte

    def test_the_result_is_the_same_as_before_for_changing_and_still_screens(self, tmp_path):
        moving = self._frames(tmp_path, 2, same=False)
        still = self._frames(tmp_path, 2)

        assert diff_score(moving[0], moving[1]).data > 0.02
        assert diff_score(still[0], still[1]).data == 0.0
        assert not diff_score(moving[0], str(tmp_path / "nope.png")).success

    def test_an_unreadable_first_frame_is_an_error_as_before(self):
        result = wait_until_stable(lambda: Result.ok("/nonexistent.png"), 1.0, 0.0, 0.02)

        assert not result.success and result.error_code == "IMAGE_READ_ERROR"


class TestBenchmark:
    def test_recommendation_for_faster_raw_slower_raw_and_unsupported_raw(self):
        png = {"mean": 2.0, "raw_failures": 0}

        assert "ADB_CAPTURE_MODE" in benchmark_capture.recommendation(png, {"mean": 0.7, "raw_failures": 0})
        assert "resta ADB_CAPTURE_MODE=png" in benchmark_capture.recommendation(png, {"mean": 1.9, "raw_failures": 0})
        assert "non funziona" in benchmark_capture.recommendation(png, {"mean": None, "raw_failures": 2})
        assert "Nessuna cattura PNG" in benchmark_capture.recommendation({"mean": None, "raw_failures": 0}, png)

    def test_measure_capture_counts_failures_and_skips_the_warmup(self, tmp_path):
        path = tmp_path / "s.png"
        Image.new("RGB", (10, 10)).save(path)
        reader = MagicMock()
        reader.capture_stats = {"raw_failures": 0}
        reader._screenshot.side_effect = [Result.ok(str(path))] * 3 + [Result.fail("x", "ADB_TIMEOUT")]

        data = benchmark_capture.measure_capture(reader, "dev", 3)

        assert data["failures"] == 1 and data["mean"] is not None and data["mb"] > 0
        assert reader._screenshot.call_count == 4                       # 1 riscaldamento + 3 misure

    def test_the_session_reports_the_screenshot_cost(self, caplog):
        autopilot = SimpleNamespace(screen_reader=SimpleNamespace(
            capture_stats={"calls": 40, "seconds": 80.0}, capture_mode="png"))

        with caplog.at_level("INFO"):
            session_module._log_capture_stats(autopilot, session_module.logger)

        assert "Screenshot ADB (modo png): 40 in 80 s, 2.00 s a scatto." in caplog.text


class TestRawGzipCapture:
    def test_rawgz_runs_screencap_through_gzip_and_decodes_the_answer(self, tmp_path):
        import gzip
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="rawgz")

        with patch(RUN, return_value=SimpleNamespace(stdout=gzip.compress(_raw(width=6, height=5)), returncode=0)) as run:
            result = reader._screenshot("dev1")

        assert run.call_args[0][0] == ["adb", "-s", "dev1", "exec-out", "screencap | gzip -1"]
        assert result.success and reader.last_method == "rawgz"
        with Image.open(result.data) as image:
            assert image.size == (6, 5)

    def test_a_phone_without_gzip_falls_back_to_raw_then_png(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="rawgz")
        calls = []

        def run(cmd, stdout=None, **kwargs):
            calls.append(cmd[-1])
            if cmd[-1] == "screencap | gzip -1":
                return SimpleNamespace(stdout=b"/system/bin/sh: gzip: not found\n", returncode=127)
            return SimpleNamespace(stdout=_raw(), returncode=0)

        with patch(RUN, side_effect=run):
            first = reader._screenshot("dev1")
            second = reader._screenshot("dev1")

        assert first.success and second.success and reader.last_method == "raw"
        assert calls == ["screencap | gzip -1", "screencap", "screencap"]     # gzip spento dopo il primo fallimento

    def test_the_last_resort_is_still_the_png_command(self, tmp_path):
        reader = ScreenReader(screenshot_dir=str(tmp_path), capture_mode="auto")

        def run(cmd, stdout=None, **kwargs):
            if cmd[-1] == "-p":
                stdout.write(b"\x89PNG-bytes")
                return MagicMock(returncode=0)
            return SimpleNamespace(stdout=b"nope", returncode=1)

        with patch(RUN, side_effect=run):
            result = reader._screenshot("dev1")

        assert result.success and reader.last_method == "png"


# ---------------- SCREENSHOT_MODE / ADB_CAPTURE_MODE: da dove arriva il modo di cattura ----------------

class TestCaptureModeSelection:
    def _reader(self, tmp_path, **kwargs):
        return ScreenReader(screenshot_dir=str(tmp_path), **kwargs)

    def test_screenshot_mode_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCREENSHOT_MODE", "rawgz")
        reader = self._reader(tmp_path)
        assert reader.capture_mode == "rawgz" and reader.capture_mode_source == "SCREENSHOT_MODE"

    def test_adb_capture_mode_still_works_and_wins_over_the_alias(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("ADB_CAPTURE_MODE", "raw")
        monkeypatch.setenv("SCREENSHOT_MODE", "rawgz")
        with caplog.at_level("WARNING"):
            reader = self._reader(tmp_path)
        assert reader.capture_mode == "raw" and reader.capture_mode_source == "ADB_CAPTURE_MODE"
        assert "ADB_CAPTURE_MODE='raw'" in caplog.text and "SCREENSHOT_MODE='rawgz'" in caplog.text

    def test_the_same_value_in_both_variables_is_not_a_conflict(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("ADB_CAPTURE_MODE", "rawgz")
        monkeypatch.setenv("SCREENSHOT_MODE", " RawGz ")
        with caplog.at_level("WARNING"):
            assert self._reader(tmp_path).capture_mode == "rawgz"
        assert caplog.text == ""

    def test_the_constructor_argument_beats_both_variables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ADB_CAPTURE_MODE", "raw")
        monkeypatch.setenv("SCREENSHOT_MODE", "rawgz")
        reader = self._reader(tmp_path, capture_mode="png")
        assert reader.capture_mode == "png" and reader.capture_mode_source == "argomento"

    def test_case_and_spaces_are_ignored_and_empty_means_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCREENSHOT_MODE", "  RAWGZ ")
        assert self._reader(tmp_path).capture_mode == "rawgz"
        monkeypatch.setenv("SCREENSHOT_MODE", "   ")
        reader = self._reader(tmp_path)
        assert reader.capture_mode == "png" and reader.capture_mode_source == "predefinito"

    def test_an_invalid_value_falls_back_to_png_but_says_so(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SCREENSHOT_MODE", "raw-gz")
        with caplog.at_level("WARNING"):
            reader = self._reader(tmp_path)
        assert reader.capture_mode == "png" and "SCREENSHOT_MODE non valido" in reader.capture_mode_source
        assert "SCREENSHOT_MODE='raw-gz' non valido" in caplog.text and "rawgz" in caplog.text

    def test_without_variables_the_mode_is_png(self, tmp_path):
        reader = self._reader(tmp_path)
        assert (reader.capture_mode, reader.capture_mode_source) == ("png", "predefinito")

    def test_the_variable_really_changes_the_adb_command(self, monkeypatch, tmp_path):
        import gzip
        commands = []

        def run(cmd, stdout=None, **kwargs):
            commands.append(cmd[-1])
            if cmd[-1] == "-p":
                stdout.write(b"\x89PNG-bytes")
                return MagicMock(returncode=0)
            return SimpleNamespace(stdout=gzip.compress(_raw(width=6, height=5)), returncode=0)

        with patch(RUN, side_effect=run):
            png_reader = self._reader(tmp_path)
            assert png_reader._screenshot("dev1").success
            monkeypatch.setenv("SCREENSHOT_MODE", "rawgz")
            rawgz_reader = self._reader(tmp_path)
            assert rawgz_reader._screenshot("dev1").success

        assert commands == ["-p", "screencap | gzip -1"]
        assert png_reader.method_counts == {"png": 1, "raw": 0, "rawgz": 0}
        assert rawgz_reader.method_counts == {"png": 0, "raw": 0, "rawgz": 1} and rawgz_reader.last_method == "rawgz"

    def test_a_phone_that_cannot_do_rawgz_is_counted_as_png(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCREENSHOT_MODE", "rawgz")

        def run(cmd, stdout=None, **kwargs):
            if cmd[-1] == "-p":
                stdout.write(b"\x89PNG-bytes")
                return MagicMock(returncode=0)
            return SimpleNamespace(stdout=b"nessun gzip su questo telefono", returncode=0)

        reader = self._reader(tmp_path)
        with patch(RUN, side_effect=run):
            assert reader._screenshot("dev1").success
        assert reader.capture_mode == "rawgz"                                   # il modo richiesto non cambia...
        assert reader.method_counts == {"png": 1, "raw": 0, "rawgz": 0}          # ...ma i metodi usati lo dicono


class TestSessionLogsTheCaptureMode:
    def _autopilot(self, mode, counts, source="SCREENSHOT_MODE"):
        return SimpleNamespace(screen_reader=SimpleNamespace(
            capture_stats={"calls": 40, "seconds": 40.0}, capture_mode=mode, capture_mode_source=source,
            method_counts=counts))

    def test_the_start_of_the_session_names_the_mode_and_where_it_comes_from(self, caplog):
        with caplog.at_level("INFO"):
            session_module._log_capture_mode(self._autopilot("rawgz", {}), session_module.logger)
        assert "Screenshot ADB: modo rawgz (da SCREENSHOT_MODE)" in caplog.text

    def test_no_line_when_the_reader_does_not_say(self, caplog):
        with caplog.at_level("INFO"):
            session_module._log_capture_mode(SimpleNamespace(screen_reader=MagicMock()), session_module.logger)
        assert caplog.text == ""

    def test_the_summary_says_rawgz_and_stays_quiet_when_nothing_fell_back(self, caplog):
        with caplog.at_level("INFO"):
            session_module._log_capture_stats(
                self._autopilot("rawgz", {"png": 0, "raw": 0, "rawgz": 40}), session_module.logger)
        assert "Screenshot ADB (modo rawgz): 40 in 40 s, 1.00 s a scatto." in caplog.text
        assert "metodi usati" not in caplog.text

    def test_the_summary_reports_the_fallbacks(self, caplog):
        with caplog.at_level("INFO"):
            session_module._log_capture_stats(
                self._autopilot("rawgz", {"png": 2, "raw": 0, "rawgz": 38}), session_module.logger)
        assert "Screenshot ADB (modo rawgz): 40 in 40 s" in caplog.text
        assert "metodi usati: png 2, rawgz 38 (ripiego su PNG)" in caplog.text

    def test_png_mode_summary_is_unchanged(self, caplog):
        with caplog.at_level("INFO"):
            session_module._log_capture_stats(
                self._autopilot("png", {"png": 40, "raw": 0, "rawgz": 0}, "predefinito"), session_module.logger)
        assert "Screenshot ADB (modo png): 40 in 40 s, 1.00 s a scatto." in caplog.text
        assert "metodi usati" not in caplog.text


def test_multi_phone_lists_both_capture_variable_names():
    from orchestrator.scripts import multi_phone
    names = multi_phone.passed_env_names({"SCREENSHOT_MODE": "rawgz", "ADB_CAPTURE_MODE": "raw", "ALTRO": "x"})
    assert "SCREENSHOT_MODE" in names and "ADB_CAPTURE_MODE" in names and "ALTRO" not in names

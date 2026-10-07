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
        assert not reader._raw_disabled                      # in modo 'raw' si riprova al giro dopo

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

        assert [c[-1] for c in calls] == ["screencap", "-p", "-p"]        # il grezzo solo la prima volta

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

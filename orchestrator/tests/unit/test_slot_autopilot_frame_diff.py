from unittest.mock import MagicMock

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.frame_diff import diff_score, wait_until_stable


def _solid_image(path, size=(100, 100), color=(0, 0, 0)):
    Image.new("RGB", size, color=color).save(path)
    return str(path)


class TestDiffScore:
    def test_identical_image_is_zero(self, tmp_path):
        path = _solid_image(tmp_path / "a.png", color=(120, 120, 120))
        result = diff_score(path, path)
        assert result.success
        assert result.data == 0.0

    def test_white_vs_black_is_one(self, tmp_path):
        white = _solid_image(tmp_path / "white.png", color=(255, 255, 255))
        black = _solid_image(tmp_path / "black.png", color=(0, 0, 0))
        result = diff_score(white, black)
        assert result.success
        assert result.data == 1.0

    def test_region_isolates_comparison(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        img_a = Image.new("L", (100, 100), color=0)
        img_a.save(a)
        img_b = Image.new("L", (100, 100), color=0)
        for x in range(10):
            for y in range(10):
                img_b.putpixel((x, y), 255)
        img_b.save(b)

        whole_image = diff_score(str(a), str(b))
        cropped_region = diff_score(str(a), str(b), region=(0, 0, 10, 10))
        assert whole_image.success and cropped_region.success
        assert cropped_region.data == 1.0
        assert whole_image.data < cropped_region.data

    def test_size_mismatch(self, tmp_path):
        small = _solid_image(tmp_path / "small.png", size=(50, 50))
        big = _solid_image(tmp_path / "big.png", size=(100, 100))
        result = diff_score(small, big)
        assert not result.success
        assert result.error_code == "IMAGE_SIZE_MISMATCH"

    def test_nonexistent_path(self, tmp_path):
        existing = _solid_image(tmp_path / "a.png")
        result = diff_score(existing, str(tmp_path / "ghost.png"))
        assert not result.success
        assert result.error_code == "IMAGE_READ_ERROR"


class TestWaitUntilStable:
    def test_identical_frames_stabilize_quickly(self, tmp_path, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.frame_diff.time.sleep", lambda s: None)
        path = _solid_image(tmp_path / "same.png")
        capture_fn = MagicMock(return_value=Result.ok(path))
        result = wait_until_stable(
            capture_fn, max_wait_seconds=5.0, poll_interval=0.1, stable_threshold=0.02,
        )
        assert result.success
        assert result.data.stable is True

    def test_alternating_frames_time_out(self, tmp_path, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.frame_diff.time.sleep", lambda s: None)
        black = _solid_image(tmp_path / "black.png", color=(0, 0, 0))
        white = _solid_image(tmp_path / "white.png", color=(255, 255, 255))

        monotonic_values = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        monkeypatch.setattr(
            "orchestrator.modules.slot_autopilot.frame_diff.time.monotonic",
            lambda: next(monotonic_values),
        )
        capture_fn = MagicMock(side_effect=[Result.ok(black), Result.ok(white), Result.ok(black), Result.ok(white)])

        result = wait_until_stable(
            capture_fn, max_wait_seconds=0.25, poll_interval=0.05, stable_threshold=0.02,
        )
        assert result.success
        assert result.data.stable is False
        assert result.data.waited_seconds >= 0.25

    def test_capture_failure_propagates_immediately(self, tmp_path):
        capture_fn = MagicMock(return_value=Result.fail("adb dead", "ADB_TIMEOUT"))
        result = wait_until_stable(
            capture_fn, max_wait_seconds=1.0, poll_interval=0.1, stable_threshold=0.02,
        )
        assert not result.success
        assert result.error_code == "ADB_TIMEOUT"
        assert capture_fn.call_count == 1

    def test_call_count_for_a_known_timing_combo(self, tmp_path, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.frame_diff.time.sleep", lambda s: None)
        black = _solid_image(tmp_path / "black.png", color=(0, 0, 0))
        white = _solid_image(tmp_path / "white.png", color=(255, 255, 255))

        # start=0.0; loop1 top-check elapsed=0.1 (<0.25); after 2nd capture elapsed=0.2 (<0.25, not stable);
        # loop2 top-check elapsed=0.3 (>=0.25) -> stop. Exactly 2 capture_fn calls total.
        monotonic_values = iter([0.0, 0.1, 0.2, 0.3])
        monkeypatch.setattr(
            "orchestrator.modules.slot_autopilot.frame_diff.time.monotonic",
            lambda: next(monotonic_values),
        )
        capture_fn = MagicMock(side_effect=[Result.ok(black), Result.ok(white)])

        result = wait_until_stable(
            capture_fn, max_wait_seconds=0.25, poll_interval=0.05, stable_threshold=0.02,
        )
        assert result.success
        assert result.data.stable is False
        assert capture_fn.call_count == 2


class TestCaptureCount:
    def test_the_number_of_captures_is_reported(self, tmp_path):
        paths = [
            _solid_image(tmp_path / "a.png", color=(0, 0, 0)),
            _solid_image(tmp_path / "b.png", color=(255, 255, 255)),
            _solid_image(tmp_path / "c.png", color=(255, 255, 255)),
        ]
        capture = MagicMock(side_effect=[Result.ok(p) for p in paths])

        result = wait_until_stable(capture, max_wait_seconds=5, poll_interval=0, stable_threshold=0.02)

        assert result.success and result.data.stable and result.data.captures == 3

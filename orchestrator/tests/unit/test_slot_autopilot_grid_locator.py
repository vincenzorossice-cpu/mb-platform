import pytest
from PIL import Image

from orchestrator.modules.slot_autopilot.grid_locator import annotate_grid, cell_label, refinement_box


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (1080, 2400), (10, 20, 30)).save(path)
    return str(path)


class TestLabels:
    def test_labels_are_column_letter_and_row_number(self):
        assert cell_label(0, 0) == "A1"
        assert cell_label(3, 7) == "D8"


class TestAnnotateGrid:
    def test_full_screen_centers_are_absolute_and_cover_every_cell(self, screenshot, tmp_path):
        out = tmp_path / "out.png"
        centers = annotate_grid(screenshot, (0, 0, 1080, 2400), 4, 8, str(out))

        assert len(centers) == 32
        assert centers["A1"] == (135, 150)
        assert centers["D8"] == (945, 2250)
        assert out.exists()

    def test_sub_box_centers_keep_the_absolute_offset(self, screenshot, tmp_path):
        centers = annotate_grid(screenshot, (400, 1600, 700, 1900), 6, 6, str(tmp_path / "o.png"))

        assert centers["A1"] == (425, 1625)
        assert centers["F6"] == (675, 1875)
        assert all(400 <= x <= 700 and 1600 <= y <= 1900 for x, y in centers.values())

    def test_narrow_crop_is_upscaled_for_readable_labels(self, screenshot, tmp_path):
        out = tmp_path / "o.png"
        annotate_grid(screenshot, (400, 1600, 700, 1900), 6, 6, str(out))

        with Image.open(out) as img:
            assert img.width >= 800

    def test_box_is_clamped_to_the_screen(self, screenshot, tmp_path):
        centers = annotate_grid(screenshot, (-50, -50, 2000, 3000), 2, 2, str(tmp_path / "o.png"))

        assert centers["A1"] == (270, 600)

    def test_empty_box_is_rejected(self, screenshot, tmp_path):
        with pytest.raises(ValueError):
            annotate_grid(screenshot, (500, 500, 500, 900), 2, 2, str(tmp_path / "o.png"))


class TestRefinementBox:
    def test_box_extends_half_a_cell_each_side(self):
        assert refinement_box((540, 1200), 270, 300, 1080, 2400) == (270, 900, 810, 1500)

    def test_box_is_clamped_to_screen_edges(self):
        assert refinement_box((135, 150), 270, 300, 1080, 2400) == (0, 0, 405, 450)

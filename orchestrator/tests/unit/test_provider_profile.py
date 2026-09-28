import json

from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile


def _write_profile(tmp_path, filename, data):
    path = tmp_path / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _valid_profile_data():
    return {
        "slot_name": "pirots5",
        "provider": "elk",
        "screen_width": 1080,
        "screen_height": 2400,
        "elements": {
            "spin": {"norm_x": 0.5, "norm_y": 0.9},
            "bet_minus": {"ocr_labels": ["-"]},
        },
        "macros": {
            "lower_bet": {"steps": ["bet_minus"]},
        },
    }


class TestLoad:
    def test_load_valid_profile(self, tmp_path):
        _write_profile(tmp_path, "pirots5.json", _valid_profile_data())
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("pirots5")
        assert result.success
        assert isinstance(result.data, ProviderProfile)
        assert result.data.slot_name == "pirots5"
        assert "spin" in result.data.elements

    def test_load_missing_file(self, tmp_path):
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("nonexistent")
        assert not result.success
        assert result.error_code == "PROFILE_NOT_FOUND"

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not valid json", encoding="utf-8")
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("broken")
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_JSON"

    def test_load_slugifies_name_with_punctuation(self, tmp_path):
        data = _valid_profile_data()
        _write_profile(tmp_path, "big_bass_bonanza_.json", data)
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("Big Bass Bonanza!")
        assert result.success


class TestValidate:
    def test_element_with_no_locator_fails(self):
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin")},
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_ELEMENT"

    def test_norm_out_of_range_fails(self):
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", norm_x=1.5, norm_y=0.5)},
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_ELEMENT"

    def test_macro_referencing_unknown_element_fails(self):
        from orchestrator.modules.provider_profile.profile import MacroSpec
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", norm_x=0.5, norm_y=0.5)},
            macros={"m": MacroSpec(name="m", steps=["ghost_element"])},
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_MACRO"

    def test_fully_consistent_profile_is_valid(self):
        from orchestrator.modules.provider_profile.profile import MacroSpec
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", norm_x=0.5, norm_y=0.9)},
            macros={"m": MacroSpec(name="m", steps=["spin"])},
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert result.success


class TestToAbsToNorm:
    def test_to_abs_computes_pixel_coords(self):
        loader = ProviderProfileLoader()
        element = ElementSpec(name="spin", norm_x=0.5, norm_y=0.9)
        result = loader.to_abs(element, 1080, 2400)
        assert result.success
        assert result.data == (540, 2160)

    def test_to_abs_fails_without_norm_coords(self):
        loader = ProviderProfileLoader()
        element = ElementSpec(name="spin", ocr_labels=["SPIN"])
        result = loader.to_abs(element, 1080, 2400)
        assert not result.success
        assert result.error_code == "ELEMENT_HAS_NO_NORM_COORDS"

    def test_to_norm_fails_on_zero_width(self):
        loader = ProviderProfileLoader()
        result = loader.to_norm(540, 2160, 0, 2400)
        assert not result.success
        assert result.error_code == "INVALID_SCREEN_DIMENSIONS"

    def test_round_trip_within_rounding_tolerance(self):
        loader = ProviderProfileLoader()
        norm_result = loader.to_norm(540, 2160, 1080, 2400)
        assert norm_result.success
        norm_x, norm_y = norm_result.data
        element = ElementSpec(name="spin", norm_x=norm_x, norm_y=norm_y)
        abs_result = loader.to_abs(element, 1080, 2400)
        assert abs_result.success
        x, y = abs_result.data
        assert abs(x - 540) <= 1
        assert abs(y - 2160) <= 1


class TestExampleProfile:
    def test_example_profile_loads_and_validates(self):
        loader = ProviderProfileLoader(profiles_dir="provider_profiles")
        result = loader.load("_example")
        assert result.success
        assert result.data.verified is False


class TestCanvasBbox:
    def test_to_abs_applies_canvas_offset(self):
        loader = ProviderProfileLoader()
        element = ElementSpec(name="spin", norm_x=0.5, norm_y=0.814212)
        result = loader.to_abs(element, 1080, 2400, canvas_bbox=(0, 247, 1080, 2400))
        assert result.success
        assert result.data == (540, 2000)

    def test_to_norm_applies_canvas_offset(self):
        loader = ProviderProfileLoader()
        result = loader.to_norm(719, 2225, 1080, 2400, canvas_bbox=(0, 247, 1080, 2400))
        assert result.success
        norm_x, norm_y = result.data
        assert abs(norm_x - 0.665741) < 1e-4
        assert abs(norm_y - 0.918718) < 1e-4

    def test_to_abs_to_norm_round_trip_with_canvas(self):
        loader = ProviderProfileLoader()
        canvas_bbox = (0, 247, 1080, 2400)
        norm_result = loader.to_norm(942, 268, 1080, 2400, canvas_bbox=canvas_bbox)
        assert norm_result.success
        norm_x, norm_y = norm_result.data
        element = ElementSpec(name="exit", norm_x=norm_x, norm_y=norm_y)
        abs_result = loader.to_abs(element, 1080, 2400, canvas_bbox=canvas_bbox)
        assert abs_result.success
        x, y = abs_result.data
        assert abs(x - 942) <= 1
        assert abs(y - 268) <= 1

    def test_to_abs_without_canvas_bbox_uses_full_screen(self):
        """Nessuna regressione: canvas_bbox=None (default) si comporta come oggi."""
        loader = ProviderProfileLoader()
        element = ElementSpec(name="spin", norm_x=0.5, norm_y=0.9)
        result = loader.to_abs(element, 1080, 2400)
        assert result.success
        assert result.data == (540, 2160)

    def test_validate_rejects_inverted_canvas_bbox(self):
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            canvas_bbox=(1080, 247, 0, 2400),
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_CANVAS_BBOX"

    def test_validate_rejects_canvas_bbox_out_of_screen_bounds(self):
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            canvas_bbox=(0, 247, 1080, 3000),
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert not result.success
        assert result.error_code == "PROFILE_INVALID_CANVAS_BBOX"

    def test_validate_accepts_valid_canvas_bbox(self):
        profile = ProviderProfile(
            slot_name="x", provider="p", screen_width=1080, screen_height=2400,
            elements={"spin": ElementSpec(name="spin", norm_x=0.5, norm_y=0.81)},
            canvas_bbox=(0, 247, 1080, 2400),
        )
        loader = ProviderProfileLoader()
        result = loader.validate(profile)
        assert result.success

    def test_load_parses_canvas_bbox_from_json(self, tmp_path):
        data = _valid_profile_data()
        data["canvas_bbox"] = [0, 247, 1080, 2400]
        _write_profile(tmp_path, "pirots5.json", data)
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("pirots5")
        assert result.success
        assert result.data.canvas_bbox == (0, 247, 1080, 2400)

    def test_load_without_canvas_bbox_defaults_to_none(self, tmp_path):
        _write_profile(tmp_path, "pirots5.json", _valid_profile_data())
        loader = ProviderProfileLoader(profiles_dir=str(tmp_path))
        result = loader.load("pirots5")
        assert result.success
        assert result.data.canvas_bbox is None

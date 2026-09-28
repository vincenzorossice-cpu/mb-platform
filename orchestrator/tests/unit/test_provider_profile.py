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

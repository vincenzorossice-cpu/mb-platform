import json
from decimal import Decimal

import pytest

from orchestrator.modules.provider_profile import profile_builder as pb_module
from orchestrator.modules.provider_profile.autopilot_memory import save_autopilot_element
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile_builder import ProfileBuilder, SessionEvidence

SLOT = "slot_di_prova"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    profiles, memory = tmp_path / "profiles", tmp_path / "memory"
    legacy = {"known_spin_coords": None, "known_bet_menu_toggle": None, "learned_hints": [], "known_min_bet": None}
    monkeypatch.setattr(pb_module, "load_slot_memory", lambda slot_name: dict(legacy))
    return profiles, memory, legacy


def _builder(dirs, threshold=2):
    profiles, memory, _ = dirs
    return ProfileBuilder(profiles_dir=str(profiles), memory_dir=str(memory), promotion_threshold=threshold)


def _learn(dirs, name="bet_minus", x=540, y=1200):
    _, memory, _ = dirs
    save_autopilot_element(SLOT, name, x, y, x / 1080, y / 2400, memory_dir=str(memory))


def _evidence(**overrides):
    defaults = dict(
        slot_name=SLOT, screen_width=1080, screen_height=2400, min_bet_verified=True,
        final_bet=Decimal("0.20"), spins_ok=10, assisted=False, no_effect_spins=0,
    )
    defaults.update(overrides)
    return SessionEvidence(**defaults)


def _load(dirs):
    profiles, _, _ = dirs
    return ProviderProfileLoader(profiles_dir=str(profiles)).load(SLOT)


class TestDraft:
    def test_first_good_session_writes_an_unverified_draft(self, dirs):
        _learn(dirs)
        result = _builder(dirs).record_session(_evidence(assisted=True))

        assert result.data == "draft_written"
        profile = _load(dirs).data
        assert profile.auto_generated and not profile.verified and profile.auto_sessions_ok == 0
        assert profile.known_min_bet == 0.2
        assert profile.elements["bet_minus"].norm_x == pytest.approx(0.5)
        assert profile.elements["bet_minus"].norm_y == pytest.approx(0.5)

    def test_legacy_memory_elements_are_included(self, dirs):
        _, _, legacy = dirs
        legacy["known_spin_coords"] = (540, 2000)
        _learn(dirs)
        _builder(dirs).record_session(_evidence())

        assert "spin" in _load(dirs).data.elements

    def test_no_file_when_min_bet_was_not_verified(self, dirs):
        _learn(dirs)
        result = _builder(dirs).record_session(_evidence(min_bet_verified=False))

        assert result.data == "insufficient_evidence"
        assert not _load(dirs).success

    def test_no_file_when_nothing_was_learned(self, dirs):
        assert _builder(dirs).record_session(_evidence()).data == "insufficient_evidence"

    def test_menu_cell_is_omitted_when_the_slot_has_a_bet_menu(self, dirs):
        _learn(dirs, "bet_menu_toggle", 900, 2000)
        _learn(dirs, "bet_minus", 198, 1070)
        _builder(dirs).record_session(_evidence())

        elements = _load(dirs).data.elements
        assert "bet_menu_toggle" in elements
        assert "bet_minus" not in elements


class TestPromotion:
    def test_two_consecutive_clean_sessions_promote_the_profile(self, dirs):
        _learn(dirs)
        builder = _builder(dirs)
        builder.record_session(_evidence(assisted=True))
        assert builder.record_session(_evidence()).data == "draft_written"
        assert _load(dirs).data.auto_sessions_ok == 1

        assert builder.record_session(_evidence()).data == "promoted"
        assert _load(dirs).data.verified

    def test_assisted_session_resets_the_count(self, dirs):
        _learn(dirs)
        builder = _builder(dirs)
        builder.record_session(_evidence(assisted=True))
        builder.record_session(_evidence())
        builder.record_session(_evidence(assisted=True))

        assert _load(dirs).data.auto_sessions_ok == 0

    def test_no_effect_spin_resets_the_count(self, dirs):
        _learn(dirs)
        builder = _builder(dirs)
        builder.record_session(_evidence(assisted=True))
        builder.record_session(_evidence())
        builder.record_session(_evidence(no_effect_spins=1))

        assert _load(dirs).data.auto_sessions_ok == 0

    def test_changed_coordinates_reset_the_count(self, dirs):
        _learn(dirs)
        builder = _builder(dirs)
        builder.record_session(_evidence(assisted=True))
        builder.record_session(_evidence())
        _learn(dirs, x=700, y=1300)
        builder.record_session(_evidence())

        assert _load(dirs).data.auto_sessions_ok == 0

    def test_failed_session_resets_an_existing_draft(self, dirs):
        _learn(dirs)
        builder = _builder(dirs)
        builder.record_session(_evidence(assisted=True))
        builder.record_session(_evidence())
        result = builder.record_session(_evidence(min_bet_verified=False))

        assert result.data == "counter_reset"
        assert _load(dirs).data.auto_sessions_ok == 0

    def test_a_promoted_profile_is_left_alone(self, dirs):
        _learn(dirs)
        builder = _builder(dirs, threshold=1)
        builder.record_session(_evidence(assisted=True))
        builder.record_session(_evidence())

        assert builder.record_session(_evidence(assisted=True)).data == "already_verified"
        assert _load(dirs).data.verified


class TestCuratedProfilesAreUntouched:
    def test_hand_written_profile_is_never_overwritten(self, dirs):
        profiles, _, _ = dirs
        profiles.mkdir()
        curated = {
            "slot_name": SLOT, "provider": "x", "screen_width": 1080, "screen_height": 2400,
            "verified": True, "elements": {"spin": {"norm_x": 0.5, "norm_y": 0.8}},
        }
        path = profiles / f"{SLOT}.json"
        path.write_text(json.dumps(curated))
        _learn(dirs)

        result = _builder(dirs).record_session(_evidence())

        assert result.data == "skipped_curated"
        assert json.loads(path.read_text()) == curated

    def test_unreadable_profile_is_not_overwritten(self, dirs):
        profiles, _, _ = dirs
        profiles.mkdir()
        path = profiles / f"{SLOT}.json"
        path.write_text("{ not json")
        _learn(dirs)

        result = _builder(dirs).record_session(_evidence())

        assert not result.success
        assert path.read_text() == "{ not json"

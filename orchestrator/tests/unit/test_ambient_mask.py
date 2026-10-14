from decimal import Decimal
from unittest.mock import MagicMock

from PIL import Image, ImageDraw

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import ambient_mask
from orchestrator.modules.slot_autopilot.ambient_mask import AmbientMask, learn_mask, load_mask, save_mask
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier

W, H = 240, 480


def _frame(tmp_path, name, counter=0, reels=0, banner=0):
    """Schermo W x H: contatori jackpot in alto (cambiano da soli), rulli al centro, banner in basso."""
    image = Image.new("RGB", (W, H), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 230, 60), fill=(counter % 256,) * 3)
    draw.rectangle((20, 150, 220, 320), fill=(reels % 256,) * 3)
    draw.rectangle((10, 430, 230, 470), fill=(banner % 256,) * 3)
    path = str(tmp_path / name)
    image.save(path)
    return path


def _ambient_frames(tmp_path, n=5):
    return [_frame(tmp_path, f"a{i}.png", counter=40 * i) for i in range(n)]


def test_cells_that_change_by_themselves_are_masked_and_the_rest_is_not(tmp_path):
    mask = learn_mask(_ambient_frames(tmp_path))

    assert mask is not None and mask.size == (W, H)
    rows = {row for _, row in mask.cells}
    assert min(rows) == 0 and max(rows) < 6              # solo la fascia alta (+1 cella di margine)


def test_masked_diff_ignores_the_counters_but_sees_the_reels(tmp_path):
    mask = learn_mask(_ambient_frames(tmp_path))
    base = _frame(tmp_path, "base.png")
    counters_only = _frame(tmp_path, "c.png", counter=200)
    reels_moved = _frame(tmp_path, "r.png", reels=200)

    assert mask.masked_diff(base, counters_only).data < 0.001
    assert mask.masked_diff(base, reels_moved).data > 0.02


def test_nothing_ambient_means_no_mask(tmp_path):
    frames = [_frame(tmp_path, f"s{i}.png") for i in range(4)]

    assert learn_mask(frames) is None
    assert learn_mask(frames[:1]) is None


def test_a_mask_covering_most_of_the_screen_is_discarded(tmp_path):
    noisy = []
    for i in range(4):
        image = Image.effect_noise((W, H), 90 + i).convert("RGB")
        path = str(tmp_path / f"n{i}.png")
        image.save(path)
        noisy.append(path)

    assert learn_mask(noisy) is None


def test_the_mask_is_ignored_for_a_different_screen_size(tmp_path):
    mask = AmbientMask((W, H), {(c, 0) for c in range(12)})
    a, b = _frame(tmp_path, "a.png"), _frame(tmp_path, "b.png", counter=200)
    big_a, big_b = tmp_path / "ba.png", tmp_path / "bb.png"
    Image.open(a).resize((W * 2, H * 2)).save(big_a)
    Image.open(b).resize((W * 2, H * 2)).save(big_b)

    assert mask.covers((W, H)) and not mask.covers((W * 2, H * 2))
    assert mask.masked_diff(str(big_a), str(big_b)).data > 0.001        # su un'altra risoluzione non maschera niente


def test_memory_is_per_slot_and_resolution(tmp_path):
    memory = str(tmp_path / "mem")
    mask = learn_mask(_ambient_frames(tmp_path))
    save_mask(memory, "Shining Crown", mask)

    loaded = load_mask(memory, "Shining Crown", (W, H))
    assert loaded is not None and loaded.cells == mask.cells
    assert load_mask(memory, "Shining Crown", (W * 2, H * 2)) is None
    assert load_mask(memory, "Altra slot", (W, H)) is None
    save_mask(memory, "Shining Crown", AmbientMask((W * 2, H * 2), {(1, 1)}))
    assert load_mask(memory, "Shining Crown", (W, H)).cells == mask.cells       # l'altra risoluzione e' rimasta


def _verifier(tmp_path, frames):
    reader = MagicMock()
    counter = {"n": 0}

    def shot(device):
        path = frames[min(counter["n"], len(frames) - 1)]
        counter["n"] += 1
        return Result.ok(path)

    reader._screenshot.side_effect = shot
    config = AutoPilotConfig(device_id="d", slot_name="s", target_played_amount=Decimal("1"),
                             frame_diff_poll_interval=0.001, frame_diff_max_wait_seconds=1.0)
    return ActionVerifier(MagicMock(), reader, MagicMock(), config)


def test_calm_is_reached_with_the_mask_where_the_jackpot_counters_made_it_impossible(tmp_path):
    moving = [_frame(tmp_path, f"m{i}.png", counter=120 * (i + 1)) for i in range(400)]
    reference = _frame(tmp_path, "ref.png")
    plain = _verifier(tmp_path, moving)
    masked = _verifier(tmp_path, moving)
    masked.ambient_mask = learn_mask(_ambient_frames(tmp_path))

    assert not plain.wait_until_calm("d", reference, 0.3).data.calm
    assert masked.wait_until_calm("d", reference, 0.3).data.calm


def test_the_mask_does_not_hide_moving_reels(tmp_path):
    moving = [_frame(tmp_path, f"r{i}.png", reels=120 * (i + 1)) for i in range(400)]
    masked = _verifier(tmp_path, moving)
    masked.ambient_mask = learn_mask(_ambient_frames(tmp_path))

    assert not masked.wait_until_calm("d", _frame(tmp_path, "ref.png"), 0.3).data.calm


# ---- cablaggio in SlotAutoPilot ---------------------------------------------------------------------------------------

def _autopilot(tmp_path, frames):
    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    deps = _collaborators(config=_config(ambient_learning=True, slot_memory_dir=str(tmp_path / "mem"),
                                         ambient_learn_interval_seconds=0.001))
    seq = iter(frames)
    last = {"p": frames[-1]}

    def shot(device):
        last["p"] = next(seq, last["p"])
        return Result.ok(last["p"])

    deps["screen_reader"]._screenshot.side_effect = shot
    deps["spin_controller"].verifier = MagicMock(ambient_mask=None)
    return SlotAutoPilot(**deps), deps


def test_first_bet_check_learns_the_mask_saves_it_and_gives_it_to_the_verifier(tmp_path):
    autopilot, deps = _autopilot(tmp_path, _ambient_frames(tmp_path))

    autopilot._learn_ambient("d")

    mask = deps["spin_controller"].verifier.ambient_mask
    assert mask is not None and mask.cells
    assert load_mask(str(tmp_path / "mem"), "pirots5", (W, H)).cells == mask.cells


def test_a_saved_mask_is_loaded_without_taking_more_screenshots(tmp_path):
    saved = learn_mask(_ambient_frames(tmp_path))
    save_mask(str(tmp_path / "mem"), "pirots5", saved)
    autopilot, deps = _autopilot(tmp_path, [_frame(tmp_path, "now.png")])

    autopilot._learn_ambient("d")

    assert deps["screen_reader"]._screenshot.call_count == 1
    assert deps["spin_controller"].verifier.ambient_mask.cells == saved.cells


def test_learning_happens_only_once_and_can_be_switched_off(tmp_path):
    autopilot, deps = _autopilot(tmp_path, _ambient_frames(tmp_path))
    autopilot._learn_ambient("d")
    calls = deps["screen_reader"]._screenshot.call_count
    autopilot._learn_ambient("d")
    assert deps["screen_reader"]._screenshot.call_count == calls

    from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
    from orchestrator.tests.unit.test_slot_autopilot_autopilot import _collaborators, _config
    off = SlotAutoPilot(**_collaborators(config=_config(ambient_learning=False)))
    off._learn_ambient("d")
    assert off.screen_reader._screenshot.call_count == 0


def test_relearning_adds_the_new_cells_to_the_old_ones(tmp_path):
    autopilot, deps = _autopilot(tmp_path, _ambient_frames(tmp_path))
    autopilot._learn_ambient("d")
    old = set(deps["spin_controller"].verifier.ambient_mask.cells)
    new_frames = [_frame(tmp_path, f"b{i}.png", banner=40 * i) for i in range(5)]       # un banner nuovo in basso
    seq = iter(new_frames)
    deps["screen_reader"]._screenshot.side_effect = lambda device: Result.ok(next(seq, new_frames[-1]))

    assert autopilot.relearn_ambient("d") is True

    merged = deps["spin_controller"].verifier.ambient_mask.cells
    assert old <= merged and any(row >= 17 for _, row in merged - old)


def test_relearning_with_nothing_moving_returns_false(tmp_path):
    autopilot, deps = _autopilot(tmp_path, [_frame(tmp_path, "s.png")])

    assert autopilot.relearn_ambient("d") is False

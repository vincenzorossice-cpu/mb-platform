"""Test di integrazione REALE (dispositivo ADB fisico, nessun mock), saltato
di default. Si attiva solo impostando la variabile d'ambiente
SLOT_AUTOPILOT_ADB_DEVICE con l'ID del dispositivo — non richiesto per la
suite unit ordinaria, ne' per CI."""
import argparse
import os

import pytest

from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.scripts.run_autopilot import build_autopilot


@pytest.mark.adb
def test_real_device_prepare_and_shutdown():
    device_id = os.environ.get("SLOT_AUTOPILOT_ADB_DEVICE")
    if not device_id:
        pytest.skip("SLOT_AUTOPILOT_ADB_DEVICE non impostata: test di integrazione reale saltato.")

    args = argparse.Namespace(
        device=device_id, slot="_example", target=1.0, max_steps=1,
        dry_run=True, live=False, no_gpt=True, no_hitl=True,
    )
    build_result = build_autopilot(args)
    assert build_result.success
    autopilot = build_result.data
    assert isinstance(autopilot, SlotAutoPilot)

    prepare_result = autopilot.prepare(device_id)
    assert prepare_result.success

    shutdown_result = autopilot.shutdown(device_id)
    assert shutdown_result.success

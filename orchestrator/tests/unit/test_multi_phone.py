import argparse
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from orchestrator.scripts import multi_phone as mp
from orchestrator.scripts.multi_phone import MultiPhone, Phone, PhoneStats

CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.json"
PHONES = [Phone("01", "Uno", "AAA"), Phone("02", "Due", "BBB"), Phone("03", "Tre", "CCC")]


def _child(script: str):
    """Figlio finto: python -c; il primo argomento e' l'ID del telefono."""
    return lambda phone: [sys.executable, "-c", "import sys\nPID = sys.argv[1]\n" + script, phone.id]


def _runner(tmp_path, script, phones=PHONES, connected=None, **kw):
    out = []
    kw.setdefault("watchdog", 0)
    runner = MultiPhone(phones, _child(script), os.environ.copy(),
                        tmp_path / "logs", connected=connected, out=out.append, grace=2, **kw)
    return runner, out


def _args(**kw):
    base = dict(mode="autopilot", slot="Big Bass", target=4.0, max_steps=200, promo_name=None, book="SISAL",
                demo=False, already_active=False, min_bet=None, devices="all", log_dir="logs/multi_phone",
                config=str(CONFIG), live=False, no_gpt=False, reset_slot_memory=False, stagger=0.0, watchdog=0)
    base.update(kw)
    return argparse.Namespace(**base)


ALL_CONNECTED = {p.device_id: "device" for p in PHONES}


# ---- 1. config e selezione ----------------------------------------------------------------------------------------

class TestConfig:
    def test_the_twelve_identities_come_from_the_config(self):
        phones = mp.load_identities(CONFIG).data

        assert [p.id for p in phones] == [f"{i:02d}" for i in range(1, 13)]
        assert phones[0].device_id == "ZE223HNBKJ" and phones[11].device_id == "ZT322ZM4Q3"

    def test_select_some_or_all_and_reject_unknown(self):
        phones = mp.load_identities(CONFIG).data

        assert [p.id for p in mp.select_phones(phones, "01,03").data] == ["01", "03"]
        assert [p.id for p in mp.select_phones(phones, "1, 2").data] == ["01", "02"]
        assert len(mp.select_phones(phones, "all").data) == 12
        assert mp.select_phones(phones, "01,99").error_code == "UNKNOWN_DEVICE"

    def test_a_missing_config_is_a_clear_failure(self, tmp_path):
        assert mp.load_identities(tmp_path / "nope.json").error_code == "CONFIG_UNREADABLE"


# ---- 2. adb ---------------------------------------------------------------------------------------------------------

class TestAdb:
    def test_parse_adb_devices(self):
        text = "List of devices attached\nAAA\tdevice\nBBB\toffline\nCCC\tunauthorized\n\n"

        assert mp.parse_adb_devices(text) == {"AAA": "device", "BBB": "offline", "CCC": "unauthorized"}
        assert mp.parse_adb_devices("List of devices attached\n\n") == {}

    def test_adb_missing_is_a_failure_not_a_crash(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("adb")
        monkeypatch.setattr(subprocess, "run", boom)

        assert mp.adb_devices().error_code == "ADB_UNAVAILABLE"


# ---- 3. comando e ambiente -------------------------------------------------------------------------------------------

class TestCommandAndEnv:
    def test_autopilot_command(self):
        command = mp.build_command(_args(min_bet=0.2, live=True), PHONES[0], python="py")

        assert command[:3] == ["py", "-m", "orchestrator.scripts.run_autopilot"]
        assert command[command.index("--device") + 1] == "AAA" and "--no-hitl" in command and "--live" in command
        assert command[command.index("--min-bet") + 1] == "0.2"
        assert command[command.index("--log-dir") + 1].replace("\\", "/") == "logs/multi_phone/ID-01"

    def test_promo_command(self):
        command = mp.build_command(_args(mode="promo_complete", promo_name="Slot in Esclusiva", demo=True,
                                         already_active=True), PHONES[1], python="py")

        assert "orchestrator.scripts.run_promo_complete" in command and "--demo" in command
        assert command[command.index("--promo-name") + 1] == "Slot in Esclusiva" and "--already-active" in command

    def test_promo_mode_requires_name_and_demo(self):
        with pytest.raises(SystemExit):
            mp._parse_args(["--mode", "promo_complete", "--slot", "x", "--target", "1", "--demo"])
        with pytest.raises(SystemExit):
            mp._parse_args(["--mode", "promo_complete", "--slot", "x", "--target", "1", "--promo-name", "p"])

    def test_env_defaults_dotenv_and_no_secret_values_are_named(self):
        env = mp.build_env({"OCR_URL": "http://ocr"}, {"OPENAI_API_KEY": "sk-secret", "OCR_URL": "ignored"})

        assert env["GPT_MODEL"] == "gpt-6.1-sol" and env["OCR_URL"] == "http://ocr"        # l'ambiente vince su .env
        assert env["OPENAI_API_KEY"] == "sk-secret" and env["CONSOLE_LEVEL_TAG"] == "1"
        assert "sk-secret" not in " ".join(mp.passed_env_names(env)) and "NETCAP_SESSION_TOKEN" not in mp.passed_env_names(env)
        assert mp.build_env({"GPT_MODEL": "altro"}, {})["GPT_MODEL"] == "altro"

    def test_children_get_level_tags_when_asked(self, tmp_path, monkeypatch):
        from orchestrator.scripts import run_autopilot
        monkeypatch.setenv("CONSOLE_LEVEL_TAG", "1")
        root = logging.getLogger()
        saved = list(root.handlers)
        try:
            run_autopilot._configure_logging(str(tmp_path), "slot")
            console = [h for h in root.handlers if type(h) is logging.StreamHandler][-1]
            assert "[%(levelname)s]" in console.formatter._fmt
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved:
                root.addHandler(h)


# ---- 4. processi paralleli e telefoni non collegati ------------------------------------------------------------------

SPINNER = '''
import time
for i in range(1, 4):
    print(f"13:15:1{i} [INFO] Spin {i} | puntata 0.20 | saldo {5000 - i} | vincita 0.00 | giocato {i * 0.2:.2f}/0.60", flush=True)
    time.sleep(0.05)
print("13:15:20 [INFO] Sessione conclusa: 0.60/0.60 EUR giocati in 3 spin.")
print("13:15:20 [INFO] Riepilogo: saldo iniziale=5000, saldo finale=4997, netto=-3 EUR")
print("13:15:20 [INFO] Interventi dell'operatore: 0")
'''


class TestParallelRun:
    def test_lines_are_labelled_and_none_is_lost(self, tmp_path):
        runner, out = _runner(tmp_path, SPINNER, connected=ALL_CONNECTED)

        results = runner.run()

        for phone in PHONES:
            mine = [l for l in out if l.startswith(f"[{phone.tag}]")]
            assert sum("Spin " in l and "puntata" in l for l in mine) == 3
        assert all(s.spins == 3 and s.played == pytest.approx(0.6) and s.net == -3 and s.exit_code == 0 for s in results)
        text = (tmp_path / "logs" / "all.log").read_text(encoding="utf-8")
        assert text.count("[ID-02]") >= 4

    def test_a_phone_that_is_not_connected_is_skipped_and_the_others_run(self, tmp_path):
        connected = {"AAA": "device", "CCC": "device", "BBB": "offline"}
        runner, out = _runner(tmp_path, SPINNER, connected=connected)

        results = runner.run()

        assert any("[ID-02] Telefono non collegato, salto" in l for l in out)
        assert results[1].skipped_reason == "telefono offline" and results[0].spins == 3 and results[2].spins == 3
        assert "ID-02" not in {l.split("]")[0].strip("[") for l in out if "Avviato" in l}
        assert "telefono non collegato" in (tmp_path / "logs" / "alerts.log").read_text(encoding="utf-8")

    def test_nobody_connected_returns_one(self, tmp_path):
        out = []
        code = mp.run(_args(devices="01,02", log_dir=str(tmp_path / "l")), out=out.append, adb=lambda: mp.Result.ok({}),
                      command_builder=_child("print('x')"), env={})

        assert code == 1 and any("nessun telefono collegato" in l for l in out)

    def test_run_returns_zero_when_all_is_fine(self, tmp_path):
        out = []
        code = mp.run(_args(devices="01,02", log_dir=str(tmp_path / "l")), out=out.append,
                      adb=lambda: mp.Result.ok({"ZE223HNBKJ": "device", "ZT322ZSJVS": "device"}),
                      command_builder=_child(SPINNER), env={})

        assert code == 0 and any("=== RIEPILOGO MULTI-PHONE ===" in l for l in out)


# ---- 5. riepilogo ----------------------------------------------------------------------------------------------------

class TestSummary:
    def test_lines_and_total(self):
        a = PhoneStats(PHONES[0], spins=20, played=4.0, net=1.2, interventions=0)
        b = PhoneStats(PHONES[1], spins=18, played=3.6, net=-0.8, alarms=["HITL"])
        c = PhoneStats(PHONES[2], spins=3, played=0.6, exit_code=1, alarms=["slot persa"], reentries=2)

        text = mp.summary_text([a, b, c])

        assert "ID-01: 20 spin, 4.00€ giocati, netto +1.20€, 0 interventi" in text
        assert "ID-02: 18 spin, 3.60€ giocati, netto -0.80€, 1 intervento (HITL)" in text
        assert "ID-03: errore (slot persa, rientrato 2 volte), 3 spin, 0.60€ giocati" in text
        assert "Totale: 41 spin, 8.20€ giocati, netto +0.40€" in text

    def test_a_skipped_phone_is_an_error_line(self):
        stats = PhoneStats(PHONES[0], skipped_reason="telefono non collegato")

        assert mp.summary_line(stats) == "ID-01: errore (telefono non collegato)"

    def test_feed_parses_the_promo_outcome(self):
        stats = PhoneStats(PHONES[0])

        stats.feed("ESITO: navigazione ok, spin 12, giocato 2.40 EUR, free spin 0 (non contati), rientri 1, stop=TARGET_REACHED; "
                   "tempi: navigazione 30 s, gioco 100 s; interventi operatore: 0")

        assert (stats.spins, stats.played, stats.reentries, stats.stop, stats.interventions) == (12, 2.4, 1, "TARGET_REACHED", 0)


# ---- 6. allarmi ------------------------------------------------------------------------------------------------------

class TestAlarms:
    def test_classification(self):
        assert mp.classify_alarm("13:00:00 [WARNING] Chiedo aiuto all'operatore (1/3): non trovo SPIN") == "HITL"
        assert mp.classify_alarm("13:00:00 [WARNING] la slot non e' piu' sullo schermo: rientro") == "slot persa"
        assert mp.classify_alarm("13:00:00 [WARNING] Spin bloccato da un'invariante") == "bloccato"
        assert mp.classify_alarm("13:00:00 [ERROR] qualcosa di grave") == "errore critico"
        assert mp.classify_alarm("13:00:00 [WARNING] Interrotto dall'operatore (KeyboardInterrupt).") is None
        assert mp.classify_alarm("13:00:00 [INFO] Interventi dell'operatore: 0") is None
        assert mp.classify_alarm("13:00:00 Chiedo aiuto all'operatore") is None             # senza livello: mai allarme

    def test_alarm_goes_to_console_and_file_without_blocking_the_others(self, tmp_path):
        script = '''
if PID == "02":
    print("13:00:01 [WARNING] Chiedo aiuto all'operatore (1/3): non trovo il pulsante SPIN", flush=True)
    sys.exit(1)
print("13:00:01 [INFO] Spin 1 | puntata 0.20 | saldo 100 | vincita 0.00 | giocato 0.20/0.20", flush=True)
'''
        runner, out = _runner(tmp_path, script, connected=ALL_CONNECTED)

        results = runner.run()

        marker = runner.marker
        assert any(l.startswith(f"{marker} [ID-02] HITL:") for l in out)
        assert any(l.startswith(f"{marker} [ID-02] processo morto:") for l in out)
        alerts = (tmp_path / "logs" / "alerts.log").read_text(encoding="utf-8")
        assert "ID-02 | HITL" in alerts and "ID-02 | processo morto" in alerts and "ID-01" not in alerts
        assert results[0].exit_code == 0 and results[2].spins == 1 and results[1].failed

    def test_a_silent_phone_raises_the_watchdog(self, tmp_path):
        runner, out = _runner(tmp_path, "import time; time.sleep(1.5)", phones=PHONES[:1],
                              connected=ALL_CONNECTED, watchdog=0.5)

        runner.run()

        assert any("nessun output" in l for l in out)


# ---- 7. Ctrl+C -------------------------------------------------------------------------------------------------------

class TestInterrupt:
    def test_stop_all_terminates_children_and_they_count_as_interrupted(self, tmp_path):
        runner, out = _runner(tmp_path, "import time; print('Spin 1 | puntata 0.20 | saldo 9 | vincita 0 | giocato 0.20/4.00', flush=True); time.sleep(60)",
                              connected=ALL_CONNECTED)
        original_drain = runner._drain
        calls = {"n": 0}

        def drain(timeout):
            calls["n"] += 1
            if calls["n"] == 30:
                raise KeyboardInterrupt
            return original_drain(timeout)
        runner._drain = drain

        results = runner.run()

        assert all(p.poll() is not None for p in runner.procs.values())
        assert all(s.interrupted for s in results) and not any(s.failed for s in results)
        assert any("Ctrl+C" in l for l in out) and "interrotto" in mp.summary_line(results[0])
        assert results[0].spins == 1

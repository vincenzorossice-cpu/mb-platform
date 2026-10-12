"""Multi-Phone Orchestrator: lancia run_autopilot / run_promo_complete su piu' telefoni in parallelo e riunisce
l'output in un'unica console, con righe etichettate [ID-XX], allarmi e riepilogo finale.

Un sottoprocesso per telefono (ognuno ha il suo autopilot, OCR e GPT: nessuno stato condiviso), un thread lettore per
processo e un solo scrittore che stampa: le righe non si mischiano. Niente async/await.

    python -m orchestrator.scripts.multi_phone --mode autopilot --slot "Big Bass Bonanza" --target 4 --devices 01,02
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from orchestrator.core.result import Result

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "orchestrator" / "config" / "config.json"
DEFAULT_LOG_DIR = "logs/multi_phone"
DEFAULT_MODEL = "gpt-6.1-sol"
# Variabili che i figli leggono: se impostate nell'ambiente o in .env vengono passate (mai stampate, solo i nomi).
PASSED_ENV = ("OPENAI_API_KEY", "GPT_MODEL", "OCR_URL", "SCREENSHOT_MODE", "NETCAP_SESSION_TOKEN")
TERMINATE_GRACE_SECONDS = 10.0

_LEVEL = re.compile(r"\[(WARNING|ERROR|CRITICAL)\]")
_SPIN = re.compile(r"Spin (\d+) \| puntata ([\d.]+) \| saldo (-?[\d.]+).*?giocato ([\d.]+)/([\d.]+)")
_SESSION_END = re.compile(r"Sessione conclusa: ([\d.]+)/([\d.]+) EUR giocati in (\d+) spin")
_NET = re.compile(r"Riepilogo:.*?netto(?: totale)?=(-?[\d.]+)")
_REENTRIES = re.compile(r"Riepilogo: (\d+) rientri")
_INTERVENTIONS = re.compile(r"[Ii]nterventi (?:dell'operatore|operatore): (\d+)")
_ESITO = re.compile(r"ESITO: navigazione (\w+), spin (\d+), giocato ([\d.]+) EUR.*?rientri (\d+), stop=([^\s;]+)")

# (tipo di allarme, schema): valutati in ordine sulle righe WARNING/ERROR.
_ALARM_PATTERNS = (
    ("HITL", re.compile(r"aiuto all'operatore|HITL|serve l'operatore", re.IGNORECASE)),
    ("slot persa", re.compile(r"non e' piu' sullo schermo|slot persa|rientr|sessione scaduta", re.IGNORECASE)),
    ("bloccato", re.compile(r"bloccat|non spinno|mi fermo", re.IGNORECASE)),
)


@dataclass
class Phone:
    id: str
    name: str
    device_id: str

    @property
    def tag(self) -> str:
        return f"ID-{self.id}"


@dataclass
class PhoneStats:
    phone: Phone
    spins: int = 0
    played: Optional[float] = None
    balance: Optional[float] = None
    net: Optional[float] = None
    interventions: Optional[int] = None
    reentries: int = 0
    exit_code: Optional[int] = None
    skipped_reason: Optional[str] = None
    interrupted: bool = False
    stop: Optional[str] = None
    alarms: List[str] = field(default_factory=list)       # tipi, in ordine
    last_output: float = 0.0
    silent_alarmed: bool = False

    def feed(self, line: str) -> None:
        match = _SPIN.search(line)
        if match:
            self.spins, self.balance, self.played = int(match.group(1)), float(match.group(3)), float(match.group(4))
        match = _SESSION_END.search(line)
        if match:
            self.played, self.spins = float(match.group(1)), int(match.group(3))
        match = _NET.search(line)
        if match:
            self.net = float(match.group(1))
        match = _REENTRIES.search(line)
        if match:
            self.reentries = int(match.group(1))
        match = _INTERVENTIONS.search(line)
        if match:
            self.interventions = int(match.group(1))
        match = _ESITO.search(line)
        if match:
            self.spins, self.played = int(match.group(2)), float(match.group(3))
            self.reentries, self.stop = int(match.group(4)), match.group(5)

    @property
    def failed(self) -> bool:
        return self.skipped_reason is not None or (self.exit_code not in (None, 0) and not self.interrupted)


# ---------------------------------------------------------------------------------------------------------------------
# Dispositivi
# ---------------------------------------------------------------------------------------------------------------------

def load_identities(config_path) -> Result[List[Phone]]:
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Result.fail(f"config non leggibile ({config_path}): {exc}", "CONFIG_UNREADABLE")
    phones = [Phone(str(i["id"]), str(i.get("name", "")), str(i["device_id"]))
              for i in data.get("identities", []) if i.get("device_id")]
    if not phones:
        return Result.fail("nessuna identita' con device_id nel config", "NO_IDENTITIES")
    return Result.ok(phones)


def select_phones(phones: List[Phone], devices: str) -> Result[List[Phone]]:
    if devices.strip().lower() == "all":
        return Result.ok(list(phones))
    wanted = [d.strip().zfill(2) for d in devices.split(",") if d.strip()]
    by_id = {p.id: p for p in phones}
    unknown = [d for d in wanted if d not in by_id]
    if unknown or not wanted:
        return Result.fail(f"ID telefono sconosciuti: {', '.join(unknown) or devices}", "UNKNOWN_DEVICE")
    return Result.ok([by_id[d] for d in dict.fromkeys(wanted)])


def parse_adb_devices(text: str) -> Dict[str, str]:
    """Output di `adb devices` -> {serial: stato} ('device', 'offline', 'unauthorized'...)."""
    states = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and not line.startswith(("List of", "*")):
            states[parts[0]] = parts[1]
    return states


def adb_devices() -> Result[Dict[str, str]]:
    try:
        completed = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result.fail(f"adb non eseguibile: {exc}", "ADB_UNAVAILABLE")
    return Result.ok(parse_adb_devices(completed.stdout))


# ---------------------------------------------------------------------------------------------------------------------
# Comando e ambiente dei figli
# ---------------------------------------------------------------------------------------------------------------------

def load_dotenv(path) -> Dict[str, str]:
    values = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def build_env(base: Optional[dict] = None, dotenv: Optional[dict] = None) -> dict:
    """Ambiente dei figli: quello corrente + .env (solo per le variabili mancanti) + default del modello."""
    env = dict(os.environ if base is None else base)
    for key, value in (dotenv if dotenv is not None else load_dotenv(PROJECT_ROOT / ".env")).items():
        env.setdefault(key, value)
    env.setdefault("GPT_MODEL", DEFAULT_MODEL)
    if env.get("OCR_URL", "").strip():
        # Con piu' telefoni anche le letture piccole (saldo/puntata dentro lo spin) vanno al server: altrimenti restano
        # su EasyOCR CPU in ogni figlio. Se l'hai impostata tu, non si tocca.
        env.setdefault("OCR_REMOTE_MIN_AREA", "0")
    env["CONSOLE_LEVEL_TAG"] = "1"          # i figli scrivono [WARNING]/[ERROR] in console: serve agli allarmi
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def passed_env_names(env: dict) -> List[str]:
    return [name for name in PASSED_ENV if env.get(name)]


def build_command(args: argparse.Namespace, phone: Phone, python: Optional[str] = None) -> List[str]:
    log_dir = str(Path(args.log_dir) / phone.tag)
    command = [python or sys.executable, "-m"]
    if args.mode == "promo_complete":
        command += ["orchestrator.scripts.run_promo_complete", "--device", phone.device_id, "--book", args.book,
                    "--promo-name", args.promo_name, "--slot-name", args.slot, "--target", str(args.target),
                    "--max-steps", str(args.max_steps), "--log-dir", log_dir, "--no-hitl"]
        if args.demo:
            command.append("--demo")
        if args.already_active:
            command.append("--already-active")
    else:
        command += ["orchestrator.scripts.run_autopilot", "--device", phone.device_id, "--slot", args.slot,
                    "--target", str(args.target), "--max-steps", str(args.max_steps), "--log-dir", log_dir,
                    "--no-hitl"]
    if args.min_bet is not None:
        command += ["--min-bet", str(args.min_bet)]
    for flag, enabled in (("--live", args.live), ("--no-gpt", args.no_gpt),
                          ("--reset-slot-memory", args.reset_slot_memory)):
        if enabled:
            command.append(flag)
    return command


# ---------------------------------------------------------------------------------------------------------------------
# Allarmi e riepilogo
# ---------------------------------------------------------------------------------------------------------------------

def classify_alarm(line: str) -> Optional[str]:
    """Tipo di allarme di una riga di un figlio, o None. Solo righe WARNING/ERROR: le info non allarmano mai."""
    match = _LEVEL.search(line)
    if not match:
        return None
    for kind, pattern in _ALARM_PATTERNS:
        if pattern.search(line.split("]", 1)[-1]):
            return kind
    return "errore critico" if match.group(1) in ("ERROR", "CRITICAL") else None


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def summary_line(stats: PhoneStats) -> str:
    tag = stats.phone.tag
    parts = []
    if stats.spins or stats.played:
        parts += [_count(stats.spins, "spin", "spin"), f"{stats.played or 0:.2f}€ giocati"]
    if stats.net is not None:
        parts.append(f"netto {stats.net:+.2f}€")
    hitl = stats.alarms.count("HITL")
    interventions = max(stats.interventions or 0, hitl)
    if stats.skipped_reason:
        return f"{tag}: errore ({stats.skipped_reason})"
    if stats.failed:
        reasons = [a for a in dict.fromkeys(stats.alarms) if a != "processo morto"] or [f"uscito con codice {stats.exit_code}"]
        if stats.reentries:
            reasons.append(f"rientrato {stats.reentries} volte")
        return f"{tag}: errore ({', '.join(reasons)})" + (f", {', '.join(parts)}" if parts else "")
    if stats.interrupted:
        parts.insert(0, "interrotto")
    if not parts:
        parts.append("nessuna attivita'")
    detail = _count(interventions, "intervento", "interventi") + (" (HITL)" if hitl else "")
    return f"{tag}: {', '.join(parts)}, {detail}"


def summary_text(all_stats: List[PhoneStats]) -> str:
    lines = ["=== RIEPILOGO MULTI-PHONE ===", *[summary_line(s) for s in all_stats]]
    spins = sum(s.spins for s in all_stats)
    played = sum(s.played or 0 for s in all_stats)
    nets = [s.net for s in all_stats if s.net is not None]
    total = f"Totale: {spins} spin, {played:.2f}€ giocati"
    if nets:
        total += f", netto {sum(nets):+.2f}€"
        if len(nets) < len([s for s in all_stats if s.spins]):
            total += " (parziale: netto non disponibile per tutti)"
    lines.append(total)
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------
# Orchestratore
# ---------------------------------------------------------------------------------------------------------------------

def _alarm_marker(stream=None) -> str:
    encoding = getattr(stream or sys.stdout, "encoding", None) or "ascii"
    try:
        "🚨".encode(encoding)
        return "🚨"
    except (UnicodeEncodeError, LookupError):
        return "[ALLARME]"


class MultiPhone:
    def __init__(self, phones: List[Phone], command_builder: Callable[[Phone], List[str]], env: dict,
                 log_dir, connected: Optional[Dict[str, str]] = None, stagger: float = 0.0, watchdog: float = 180.0,
                 out: Callable[[str], None] = None, cwd=PROJECT_ROOT, grace: float = TERMINATE_GRACE_SECONDS):
        self.phones, self.command_builder, self.env = phones, command_builder, env
        self.log_dir, self.stagger, self.watchdog, self.grace, self.cwd = Path(log_dir), stagger, watchdog, grace, cwd
        self.connected = connected
        self.out = out or self._print
        self.marker = _alarm_marker()
        self.stats: Dict[str, PhoneStats] = {p.id: PhoneStats(p) for p in phones}
        self.procs: Dict[str, subprocess.Popen] = {}
        self._lines: "queue.Queue" = queue.Queue()
        self._readers: List[threading.Thread] = []
        self._all_log = None
        self._alerts_log = None
        self._last_alarm: Dict[str, str] = {}

    # ---- uscita -------------------------------------------------------------------------------------------------
    @staticmethod
    def _print(text: str) -> None:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)

    def _emit(self, tag: str, text: str) -> None:
        line = f"[{tag}] {text}"
        self.out(line)
        if self._all_log is not None:
            self._all_log.write(line + "\n")
            self._all_log.flush()

    def _alarm(self, phone: Phone, kind: str, line: str) -> None:
        stats = self.stats[phone.id]
        stats.alarms.append(kind)
        if self._last_alarm.get(phone.id) == f"{kind}|{line}":
            return                                      # la stessa riga ripetuta non riempie la console
        self._last_alarm[phone.id] = f"{kind}|{line}"
        self.out(f"{self.marker} [{phone.tag}] {kind}: {line}")
        if self._alerts_log is not None:
            self._alerts_log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {phone.tag} | {kind} | {line}\n")
            self._alerts_log.flush()

    # ---- ciclo di vita ------------------------------------------------------------------------------------------
    def _start(self, phone: Phone) -> None:
        stats = self.stats[phone.id]
        if self.connected is not None and self.connected.get(phone.device_id) != "device":
            state = self.connected.get(phone.device_id)
            stats.skipped_reason = "telefono non collegato" if state is None else f"telefono {state}"
            self._emit(phone.tag, "Telefono non collegato, salto" + (f" (stato adb: {state})" if state else ""))
            self._alarm(phone, "telefono non collegato", stats.skipped_reason)
            return
        Path(self.log_dir / phone.tag).mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(
                self.command_builder(phone), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=self.env, cwd=str(self.cwd), text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            stats.skipped_reason = f"avvio fallito: {exc}"
            self._alarm(phone, "processo morto", stats.skipped_reason)
            return
        self.procs[phone.id] = proc
        stats.last_output = time.monotonic()
        self._emit(phone.tag, f"Avviato ({phone.name}, {phone.device_id})")
        reader = threading.Thread(target=self._read, args=(phone, proc), daemon=True)
        reader.start()
        self._readers.append(reader)

    def _read(self, phone: Phone, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            self._lines.put((phone, raw.rstrip("\r\n")))
        self._lines.put((phone, None))              # fine dell'output di questo figlio

    def _handle(self, phone: Phone, line: Optional[str]) -> None:
        stats = self.stats[phone.id]
        if line is None:
            proc = self.procs[phone.id]
            stats.exit_code = proc.wait()
            if stats.exit_code != 0 and not stats.interrupted:
                self._alarm(phone, "processo morto", f"uscito con codice {stats.exit_code}")
            else:
                self._emit(phone.tag, "Terminato.")
            return
        if not line.strip():
            return
        stats.last_output, stats.silent_alarmed = time.monotonic(), False
        stats.feed(line)
        kind = classify_alarm(line)
        self._emit(phone.tag, line)
        if kind is not None:
            self._alarm(phone, kind, line)

    def _check_silence(self) -> None:
        now = time.monotonic()
        for phone in self.phones:
            stats, proc = self.stats[phone.id], self.procs.get(phone.id)
            if proc is None or proc.poll() is not None or stats.silent_alarmed or self.watchdog <= 0:
                continue
            if now - stats.last_output > self.watchdog:
                stats.silent_alarmed = True
                self._alarm(phone, "nessun output", f"nessun output da {self.watchdog:.0f} s")

    def _drain(self, timeout: float) -> bool:
        try:
            phone, line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return False
        self._handle(phone, line)
        return True

    def stop_all(self) -> None:
        """Ctrl+C: terminate a tutti, attesa (max `grace` s), kill per chi resta."""
        for phone_id, proc in self.procs.items():
            if proc.poll() is None:
                self.stats[phone_id].interrupted = True
                proc.terminate()
        deadline = time.monotonic() + self.grace
        while time.monotonic() < deadline and any(p.poll() is None for p in self.procs.values()):
            time.sleep(0.1)
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
        for proc in self.procs.values():
            proc.wait()

    def run(self) -> List[PhoneStats]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._all_log = open(self.log_dir / "all.log", "a", encoding="utf-8")
        self._alerts_log = open(self.log_dir / "alerts.log", "a", encoding="utf-8")
        try:
            pending = list(self.phones)
            next_start = time.monotonic()
            try:
                while True:
                    now = time.monotonic()
                    if pending and now >= next_start:
                        self._start(pending.pop(0))
                        next_start = time.monotonic() + (self.stagger if pending else 0)
                    self._drain(0.2)
                    self._check_silence()
                    alive = any(p.poll() is None for p in self.procs.values())
                    if not pending and not alive and self._lines.empty() and all(
                            not r.is_alive() for r in self._readers):
                        break
            except KeyboardInterrupt:
                self.out("Ctrl+C: fermo tutti i telefoni...")
                self.stop_all()
            for reader in self._readers:
                reader.join(timeout=2)
            while self._drain(0.05):
                pass
        finally:
            self._all_log.close()
            self._alerts_log.close()
            self._all_log = self._alerts_log = None
        return [self.stats[p.id] for p in self.phones]


# ---------------------------------------------------------------------------------------------------------------------
# Riga di comando
# ---------------------------------------------------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lancia l'automazione slot su piu' telefoni in parallelo.")
    parser.add_argument("--mode", choices=("autopilot", "promo_complete"), default="autopilot")
    parser.add_argument("--slot", required=True, help="Nome della slot (profilo/memoria; in promo_complete anche da aprire).")
    parser.add_argument("--target", type=float, required=True, help="Importo target da giocare per telefono (EUR).")
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--promo-name", default=None, help="Solo promo_complete.")
    parser.add_argument("--book", default="SISAL", help="Solo promo_complete.")
    parser.add_argument("--demo", action="store_true", help="Solo promo_complete: slot in demo (obbligatorio li').")
    parser.add_argument("--already-active", action="store_true", help="Solo promo_complete.")
    parser.add_argument("--min-bet", type=float, default=None)
    parser.add_argument("--devices", default="all", help='"all" oppure ID separati da virgola: "01,02,03".')
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--no-gpt", action="store_true")
    parser.add_argument("--reset-slot-memory", action="store_true")
    parser.add_argument("--stagger", type=float, default=3.0, help="Secondi tra una partenza e la successiva.")
    parser.add_argument("--watchdog", type=float, default=180.0, help="Allarme se un telefono tace per questi secondi (0 = spento).")
    args = parser.parse_args(argv)
    if args.mode == "promo_complete" and not args.promo_name:
        parser.error("--promo-name e' obbligatorio con --mode promo_complete")
    if args.mode == "promo_complete" and not args.demo:
        parser.error("--demo e' obbligatorio con --mode promo_complete (la slot si apre solo in demo)")
    return args


def run(args: argparse.Namespace, out: Optional[Callable[[str], None]] = None, adb: Callable = adb_devices,
        command_builder: Optional[Callable[[Phone], List[str]]] = None, env: Optional[dict] = None) -> int:
    out = out or MultiPhone._print
    identities = load_identities(args.config)
    if not identities.success:
        out(f"ERRORE: {identities.error}")
        return 2
    selected = select_phones(identities.data, args.devices)
    if not selected.success:
        out(f"ERRORE: {selected.error}")
        return 2
    attached = adb()
    if not attached.success:
        out(f"ERRORE: {attached.error}")
        return 1
    env = env if env is not None else build_env()
    runner = MultiPhone(
        selected.data, command_builder or (lambda phone: build_command(args, phone)), env, args.log_dir,
        connected=attached.data, stagger=args.stagger, watchdog=args.watchdog, out=out,
    )
    out(f"Variabili passate ai telefoni: {', '.join(passed_env_names(env)) or 'nessuna'}")
    results = runner.run()
    out(summary_text(results))
    if not any(s.skipped_reason is None for s in results):
        out("ERRORE: nessun telefono collegato tra quelli richiesti.")
        return 1
    return 1 if any(s.failed for s in results) else 0


def main(argv=None) -> None:
    sys.exit(run(_parse_args(argv)))


if __name__ == "__main__":
    main()

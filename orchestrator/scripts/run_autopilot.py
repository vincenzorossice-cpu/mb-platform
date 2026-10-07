import argparse
# Load credentials from .env
import os
from pathlib import Path
env_file = Path(__file__).resolve().parents[2] / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import logging
import os
import sys
import time
import warnings
from datetime import datetime
from decimal import Decimal

import openai

from orchestrator.core import model_metrics
from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient, resolve_reader_model
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile_builder import ProfileBuilder
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.supervisor import SlotSupervisor
from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import GPTLocatorStrategy
from orchestrator.modules.slot_autopilot.strategies.hitl_strategy import HITLStrategy
from orchestrator.modules.slot_autopilot.strategies.memory_strategy import MemoryStrategy
from orchestrator.modules.slot_autopilot.strategies.ocr_anchor_strategy import OCRAnchorStrategy
from orchestrator.modules.slot_autopilot.strategies.profile_strategy import ProfileStrategy
from orchestrator.modules.slot_autopilot.strategies.spatial_heuristic_strategy import SpatialHeuristicStrategy
from orchestrator.modules.slot_autopilot.strategies.template_strategy import TemplateStrategy
from orchestrator.modules.slot_autopilot.screen_recovery import ScreenRecovery
from orchestrator.modules.slot_autopilot.session import (
    BET_FAILURE_MESSAGES as _BET_FAILURE_MESSAGES,
    progress_line as _progress_line,
    play_session,
)
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier
from orchestrator.modules.slot_ocr.reader import SlotOCRReader
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader, VisionAmountReader
from orchestrator.modules.step_logger.logger import JSONLStepLogger
from orchestrator.modules.touch_injector.injector import TouchInjector

logger = logging.getLogger(__name__)

# Loggers i cui messaggi INFO restano anche sulla console (progresso e eventi chiave);
# tutto il resto va solo su file (a meno che non sia un avviso/errore).
_CONSOLE_INFO_LOGGERS = ("__main__", "orchestrator.scripts.run_autopilot")
_HARMLESS_WARNING_PATTERNS = (".*pin_memory.*", ".*quantize_per_tensor.*")


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name in _CONSOLE_INFO_LOGGERS


def _configure_logging(log_dir: str, slot_name: str) -> str:
    """Dettaglio completo (httpx, risposte GPT, ogni tocco) su file; sulla console solo
    la riga di progresso, gli eventi chiave e gli avvisi/errori. Ritorna il percorso del
    file. Le UserWarning innocue di torch (CPU senza acceleratore) non si mostrano."""
    os.makedirs(log_dir, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slot_name.strip().lower()) or "slot"
    path = os.path.join(log_dir, f"autopilot_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    console.addFilter(_ConsoleFilter())
    root.addHandler(file_handler)
    root.addHandler(console)

    for pattern in _HARMLESS_WARNING_PATTERNS:
        warnings.filterwarnings("ignore", message=pattern)
    logging.getLogger("easyocr").setLevel(logging.ERROR)  # "Using CPU": informativo, una volta
    return path


def _check_openai_key(gpt_client) -> Result[bool]:
    """Controllo gratuito (elenco modelli, nessun token) che la chiave sia
    accettata: una chiave rifiutata fa fallire in silenzio ogni chiamata GPT e
    lascia solo l'operatore. Solo un rifiuto esplicito (401) e' bloccante: rete
    o server momentaneamente giu' non devono impedire di partire."""
    try:
        gpt_client.client.models.list()
    except openai.AuthenticationError as exc:
        return Result.fail(
            "La chiave OpenAI e' stata rifiutata (401 Unauthorized). Imposta una chiave valida "
            "(PowerShell: $env:OPENAI_API_KEY=\"sk-...\"; verificala su platform.openai.com/api-keys) "
            f"oppure rilancia con --no-gpt. Dettaglio: {exc}",
            "OPENAI_KEY_REJECTED",
        )
    except openai.OpenAIError as exc:
        logger.warning("Controllo chiave OpenAI non riuscito (%s): proseguo comunque.", exc)
    return Result.ok(True)


def build_autopilot(args: argparse.Namespace) -> Result[SlotAutoPilot]:
    """Costruisce lo SlotAutoPilot dagli argomenti della riga di comando."""
    return build_autopilot_for(
        device_id=args.device, slot_name=args.slot, target=args.target, max_steps=args.max_steps,
        enable_gpt=not args.no_gpt, enable_hitl=not args.no_hitl, live=getattr(args, "live", False),
        require_profile=getattr(args, "require_profile", False),
    )


def build_autopilot_for(
    device_id: str, slot_name: str, target, max_steps: int = 500, enable_gpt: bool = True,
    enable_hitl: bool = True, live: bool = False, require_profile: bool = False,
) -> Result[SlotAutoPilot]:
    """Unico punto del progetto dove queste classi concrete vengono
    istanziate direttamente: ogni modulo a monte le riceve per DI. Usato dalla riga di comando
    e da CasinoFlow (una istanza per slot/fase)."""
    config = AutoPilotConfig(
        device_id=device_id, slot_name=slot_name, target_played_amount=Decimal(str(target)),
        max_steps=max_steps, enable_gpt_fallback=enable_gpt,
        enable_hitl_fallback=enable_hitl, dry_run=not live,
        # Una slot nuova non ha ancora un profilo: deve poter partire e
        # impararlo (vedi ProfileBuilder). --require-profile ripristina il
        # blocco per chi vuole solo slot gia' curate.
        require_profile=require_profile,
    )
    config_result = config.validate()
    if not config_result.success:
        return Result.fail(config_result.error, config_result.error_code)

    screen_reader = ScreenReader(adb_timeout=config.adb_timeout)
    touch_injector = TouchInjector(screen_reader=screen_reader, adb_timeout=config.adb_timeout)
    ocr_reader = SlotOCRReader(ocr_url=os.environ.get("OCR_URL"))
    if ocr_reader.remote:
        logger.info("OCR remoto: %s", ocr_reader._ocr_url)
    else:
        logger.info("OCR locale (EasyOCR)")
    profile_loader = ProviderProfileLoader(profiles_dir=config.profiles_dir)
    step_logger = JSONLStepLogger(dataset_dir=config.dataset_dir)

    strategies = {
        "profile": ProfileStrategy(profile_loader),
        "memory": MemoryStrategy(memory_dir=config.slot_memory_dir),
        "ocr_anchor": OCRAnchorStrategy(),
        "spatial_heuristic": SpatialHeuristicStrategy(),
        "template": TemplateStrategy(),
    }

    gpt_client = None
    if config.enable_gpt_fallback:
        try:
            gpt_client = GPTDecisionClient()
        except ValueError as exc:
            logger.warning("GPT fallback disabilitato (OPENAI_API_KEY mancante?): %s", exc)
            gpt_client = None
        else:
            key_check = _check_openai_key(gpt_client)
            if not key_check.success:
                return Result.fail(key_check.error, key_check.error_code)
            strategies["gpt"] = GPTLocatorStrategy(gpt_client, verify_icons=True)
            # Rete di sicurezza per saldo e puntata: se l'OCR non li legge o li legge male (es. "50000.00"
            # letto 50.0) si usa la visione; dove l'OCR basta non costa nulla.
            def capture_screen():
                shot = screen_reader._screenshot(device_id)
                return shot.data if shot.success else None

            ocr_reader = ResilientOCRReader(
                ocr_reader, VisionAmountReader(gpt_client, model=resolve_reader_model()), capture=capture_screen,
            )

    if config.enable_hitl_fallback:
        strategies["hitl"] = HITLStrategy(
            screen_reader, ocr_reader, touch_injector, describer=strategies.get("gpt"),
        )

    supervisor = None
    if gpt_client is not None and "gpt" in strategies:
        # Sol guarda lo schermo intero quando i controlli locali si bloccano, prima di chiamare l'operatore.
        supervisor = SlotSupervisor(
            gpt_client, screen_reader, touch_injector, describe=strategies["gpt"].locate_described,
        )
    locator = ElementLocator(strategies, memory_dir=config.slot_memory_dir)
    verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, config)
    screen_recovery = ScreenRecovery(locator, verifier, config)
    bet_controller = BetController(
        locator, verifier, step_logger, config,
        menu_selector=BetMenuSelector(locator, verifier, screen_recovery=screen_recovery, supervisor=supervisor),
        screen_recovery=screen_recovery, supervisor=supervisor,
    )
    spin_controller = SpinController(locator, verifier, step_logger, config)

    autopilot = SlotAutoPilot(
        config=config, screen_reader=screen_reader, touch_injector=touch_injector,
        ocr_reader=ocr_reader, profile_loader=profile_loader, locator=locator,
        bet_controller=bet_controller, spin_controller=spin_controller, step_logger=step_logger,
        profile_builder=ProfileBuilder(profiles_dir=config.profiles_dir, memory_dir=config.slot_memory_dir),
    )
    autopilot.gpt_client = gpt_client   # per il riepilogo dei costi a fine sessione
    autopilot.hitl_strategy = strategies.get("hitl")
    autopilot.supervisor = supervisor
    return Result.ok(autopilot)


def _run_session(autopilot: SlotAutoPilot, args: argparse.Namespace) -> int:
    """Una sessione da riga di comando: il ciclo vero sta in play_session (condiviso con CasinoFlow)."""
    model_metrics.start_run("slot_autopilot", args.slot.replace(" ", "_"))
    started = time.monotonic()
    result = play_session(
        autopilot, args.device, args.slot, Decimal(str(args.target)), args.max_steps, log=logger,
    )
    _log_run_metrics(autopilot, args, result, time.monotonic() - started)
    return 1 if result.failed else 0


def _log_run_metrics(autopilot: SlotAutoPilot, args: argparse.Namespace, result, seconds: float) -> None:
    """Esito della sessione nel file di metriche (performance e tempo; costo e secondi per chiamata sono nelle righe
    delle chiamate dello stesso run). Mai eccezioni: misurare non deve rompere la sessione."""
    try:
        gpt_client = getattr(autopilot, "gpt_client", None)
        hitl = getattr(autopilot, "hitl_strategy", None)
        supervisor = getattr(autopilot, "supervisor", None)
        model_metrics.log_run(
            type_of_run="slot", model=getattr(gpt_client, "model", None), reader_model=resolve_reader_model(),
            slot=args.slot, device=args.device, target=str(args.target), spins=result.spins,
            total_played=str(result.total_played), failed=result.failed, stop_reason=result.stop_reason,
            bet_aborted_reason=result.bet_aborted_reason, seconds=round(seconds, 1),
            seconds_per_spin=round(seconds / result.spins, 1) if result.spins else None,
            operator_interventions=getattr(hitl, "interventions", 0),
            supervisor_calls=getattr(supervisor, "calls", 0),
            calls_by_purpose=dict(getattr(gpt_client, "_usage_by_purpose", {}) or {}),
        )
    except Exception as exc:
        logger.debug("metriche del run non scritte: %s", exc)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SlotAutoPilot: abbassa la puntata al minimo vero ed esegue spin verificati.",
    )
    parser.add_argument("--device", required=True, help="ID del dispositivo ADB.")
    parser.add_argument("--slot", required=True, help="Nome della slot (profilo/memoria).")
    parser.add_argument("--target", type=float, required=True, help="Importo target da giocare (EUR).")
    parser.add_argument("--max-steps", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true", help="Non esegue tap reali (comportamento di default).")
    parser.add_argument("--live", action="store_true", help="Disattiva il dry-run: esegue tap reali.")
    parser.add_argument("--no-gpt", action="store_true", help="Disabilita il fallback GPT-4o-mini Vision.")
    parser.add_argument("--require-profile", action="store_true", help="Rifiuta le slot senza profilo in provider_profiles/.")
    parser.add_argument("--log-dir", default="logs", help="Cartella del file di log completo (default: logs).")
    parser.add_argument("--no-hitl", action="store_true", help="Disabilita il fallback HITL da terminale.")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    log_path = _configure_logging(getattr(args, "log_dir", "logs"), args.slot)
    logger.info("Dettaglio completo su file: %s", log_path)

    build_result = build_autopilot(args)
    if not build_result.success:
        logger.error("Impossibile inizializzare SlotAutoPilot: %s (%s)", build_result.error, build_result.error_code)
        sys.exit(1)
    autopilot = build_result.data

    exit_code = 1
    try:
        exit_code = _run_session(autopilot, args)
    except KeyboardInterrupt:
        logger.warning("Interrotto dall'operatore (KeyboardInterrupt).")
        exit_code = 1
    finally:
        gpt_client = getattr(autopilot, "gpt_client", None)
        if gpt_client is not None and hasattr(gpt_client, "usage_summary"):
            logger.info("%s", gpt_client.usage_summary())
        hitl = getattr(autopilot, "hitl_strategy", None)
        if hitl is not None:
            logger.info("Interventi dell'operatore: %d", getattr(hitl, "interventions", 0))
        shutdown_result = autopilot.shutdown(args.device)
        if not shutdown_result.success:
            logger.error("shutdown() fallito: %s (%s)", shutdown_result.error, shutdown_result.error_code)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

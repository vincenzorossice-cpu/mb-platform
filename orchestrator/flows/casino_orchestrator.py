import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.flows.casino_flow import CasinoFlow, CasinoResult
from orchestrator.modules.signal_parser.interface import Signal

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = "orchestrator/tests/validation/reports"

# Obiettivi di navigazione (linguaggio naturale) dalla Home fino alla promo attivata. Sostituibili con
# `navigation_goals`: i testi dei book cambiano e il passo "Buyin" va confermato con chi conosce il flusso.
_DEFAULT_GOALS = (
    "Vai nella sezione Promozioni del book {book}",
    "Attiva la promozione '{promo}' (pulsante Attiva / Aderisci)",
)
_SLOT_GOAL = "Apri la slot '{slot}' e arriva alla schermata di gioco"
_BUYIN_GOAL = "Conferma l'ingresso (buy-in) nella slot, se richiesto"


@dataclass
class CasinoOrchestrationResult:
    """Esito di una promo casino orchestrata: cosa e' successo nelle fasi, dove e' stato salvato il risultato
    per la dashboard e se e' stato scritto su Netcap."""
    casino_result: CasinoResult
    pending_item_id: Optional[str] = None
    pending_path: Optional[str] = None
    netcap_write: Optional[object] = None
    navigation_log: list[str] = field(default_factory=list)


class CasinoResultStore:
    """Salva l'esito di una promo casino come "pending" per la dashboard di Raffaele (stesso schema di
    SportFlowHandler: un file JSON per esito, con stato mutabile per l'approvazione)."""

    def __init__(self, report_dir: str = DEFAULT_REPORT_DIR):
        self.report_dir = report_dir

    def save_pending(
        self, result: CasinoResult, signal: Signal, confirmed_by: str, gpt_usage: Optional[str] = None,
    ) -> Result[tuple]:
        now = datetime.now(timezone.utc)
        directory = Path(self.report_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            item_id = f"casino_{signal.telegram_message_id}_{int(now.timestamp())}"
            path = directory / f"casino_flow_pending_{signal.telegram_message_id}_{now:%Y%m%d_%H%M%S}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "created_at": now.isoformat(),
                    "flows": [{
                        "item_id": item_id,
                        "signal": asdict(signal),
                        "confirmed_by": confirmed_by,
                        "casino_result": asdict(result),
                        "gpt_usage": gpt_usage,
                        "status": "pending_hitl",
                    }],
                }, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            return Result.fail(f"Cannot save casino result: {exc}", "CASINO_RESULT_SAVE_FAILED")
        return Result.ok((item_id, str(path)))


class CasinoOrchestrator:
    """Collega l'intera promo casino: segnale confermato -> navigazione con visione -> fasi di gioco
    (CasinoFlow, spin dello SlotAutoPilot) -> esito salvato per la dashboard -> (opzionale) Netcap."""

    def __init__(
        self,
        screen_navigator,
        casino_flow: CasinoFlow,
        app_api_client=None,
        result_store: Optional[CasinoResultStore] = None,
        usage_reporter=None,
        write_to_netcap: bool = False,
        navigation_goals: Optional[tuple] = None,
        buyin_phases: tuple = ("real",),
    ):
        self.navigator = screen_navigator
        self.casino_flow = casino_flow
        self.app_api_client = app_api_client
        self.result_store = result_store or CasinoResultStore()
        self.usage_reporter = usage_reporter          # es. GPTDecisionClient (usage_summary)
        # La scrittura su Netcap e' spenta di default: il formato dell'operazione casino va confermato.
        self.write_to_netcap = write_to_netcap
        self.navigation_goals = navigation_goals or _DEFAULT_GOALS
        self.buyin_phases = buyin_phases
        self._device_id = None
        self._signal: Optional[Signal] = None
        self._demo_mode = False
        self._navigation_log: list[str] = []
        # Le fasi aprono la loro slot passando da qui (la navigazione resta di chi la sa fare).
        if self.casino_flow.slot_opener is None:
            self.casino_flow.slot_opener = self._open_slot

    def run(
        self, signal: Signal, promo_config: dict, device_id: str, collaboratore_id: str,
        confirmed_by: str, demo_mode: bool = False, allow_tc_discrepancy: bool = False,
    ) -> Result[CasinoOrchestrationResult]:
        rejection = self._validate(signal, confirmed_by, allow_tc_discrepancy)
        if rejection is not None:
            return rejection

        self._device_id, self._signal, self._demo_mode = device_id, signal, demo_mode
        self._navigation_log = []
        promo_name = promo_config.get("promo_name", "unknown")

        for template in self.navigation_goals:
            goal = template.format(book=signal.book, promo=promo_name)
            step = self._navigate(goal)
            if not step.success:
                return step

        flow_result = self.casino_flow.execute_promo(device_id, collaboratore_id, signal.book, promo_config)
        if not flow_result.success:
            return Result.fail(flow_result.error, flow_result.error_code)
        casino_result = flow_result.data

        usage = self.usage_reporter.usage_summary() if self.usage_reporter is not None else None
        if usage:
            logger.info("%s", usage)

        saved = self.result_store.save_pending(casino_result, signal, confirmed_by, usage)
        if not saved.success:
            logger.error("Esito della promo NON salvato per la dashboard: %s", saved.error)
        item_id, path = saved.data if saved.success else (None, None)

        netcap_write = None
        if self.write_to_netcap:
            netcap_write = self._write_to_netcap(signal, promo_config, casino_result)

        return Result.ok(CasinoOrchestrationResult(
            casino_result=casino_result, pending_item_id=item_id, pending_path=path,
            netcap_write=netcap_write, navigation_log=list(self._navigation_log),
        ))

    # ------------------------------------------------------------------

    def _validate(self, signal: Signal, confirmed_by: str, allow_tc_discrepancy: bool) -> Optional[Result]:
        if signal.category != "casino":
            return Result.fail(f"Il segnale non e' di categoria casino ({signal.category})", "SIGNAL_NOT_CASINO")
        if not confirmed_by or not str(confirmed_by).strip():
            return Result.fail("Segnale non confermato da un operatore", "SIGNAL_NOT_CONFIRMED")
        if signal.status == "tc_discrepancy" and not allow_tc_discrepancy:
            return Result.fail(
                "Il segnale ha discrepanze nei T&C: vanno risolte prima di giocare", "SIGNAL_TC_DISCREPANCY",
            )
        return None

    def _navigate(self, goal: str) -> Result[bool]:
        self._navigation_log.append(goal)
        logger.info("Navigazione: %s", goal)
        result = self.navigator.navigate(
            self._device_id, goal, book=self._signal.book, demo_mode=self._demo_mode, vision_mode=True,
        )
        if not result.success:
            logger.warning("Navigazione fallita (%s): %s", goal, result.error)
            return Result.fail(f"Navigazione fallita su '{goal}': {result.error}", result.error_code)
        return result

    def _open_slot(self, device_id: str, slot_name: str, phase: str) -> Result[bool]:
        """Usato da CasinoFlow prima di ogni fase: dalla Home alla schermata di gioco della slot."""
        goals = [_SLOT_GOAL.format(slot=slot_name)]
        if phase in self.buyin_phases:
            goals.append(_BUYIN_GOAL)
        for goal in goals:
            step = self._navigate(goal)
            if not step.success:
                return step
        return Result.ok(True)

    def _netcap_operation(self, signal: Signal, promo_config: dict, result: CasinoResult) -> dict:
        """Corpo di POST /operazioni per una promo casino. DA CONFERMARE con chi gestisce Netcap: i campi
        sono quelli gia' usati dalla riconciliazione; categoria/tipologia e il profitto vanno validati."""
        return {
            "nome": f"{promo_config.get('promo_name', 'Promo casino')} - {signal.book}",
            "tipologia": signal.bonus_type.capitalize(),
            "categoria": "Casino",
            "bookmaker_id": promo_config["bookmaker_id"],
            "data_apertura": datetime.now(timezone.utc).date().isoformat(),
            "profitto_book": round(result.total_profit, 2),
        }

    def _write_to_netcap(self, signal: Signal, promo_config: dict, result: CasinoResult):
        if self.app_api_client is None or "bookmaker_id" not in promo_config:
            logger.error("Scrittura su Netcap saltata: serve app_api_client e promo_config['bookmaker_id'].")
            return None
        written = self.app_api_client.write_operazione(self._netcap_operation(signal, promo_config, result))
        if not written.success:
            logger.error("Scrittura su Netcap fallita: %s (%s)", written.error, written.error_code)
            return None
        return written.data

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from orchestrator.core.config import load_config
from orchestrator.core.device_manager import DeviceManager
from orchestrator.core.result import Result
from orchestrator.modules.multiplier_engine.interface import CascadeCalculation
from orchestrator.modules.rag_system.interface import TCVerification
from orchestrator.modules.signal_parser.interface import Signal


@dataclass
class SignalFlowResult:
    """Esito di process_signal: segnale parsato, verifica T&C e calcolo cascata
    (se applicabile), pronto per il salvataggio HITL. Non piazza nulla."""
    signal: Signal
    tc_verification: TCVerification
    cascade_calc: Optional[CascadeCalculation]
    identity_ids_available: list[str]
    status: str  # "pending_hitl" | "tc_discrepancy" | "calc_error"
    hitl_item_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ExecutionResult:
    """Esito di execute_operation: cosa e' stato effettivamente piazzato/registrato."""
    operation_id: Optional[int]
    bet_gruppo_id: Optional[str]
    identities_executed: list[str]
    identities_failed: list[str]
    cascade_started: bool
    errors: list[str] = field(default_factory=list)


class SportFlow:
    """Orchestra il flusso P5 Sport completo: segnale -> verifica T&C -> calcolo
    -> HITL -> piazzamento -> registrazione, chiamando solo i moduli gia' scritti."""

    def __init__(
        self,
        signal_parser,
        rag_system,
        multiplier_engine,
        touch_injector,
        cascade_engine,
        app_api_client,
        screen_reader,
        device_manager=None,
        config=None,
    ):
        self.signal_parser = signal_parser
        self.rag_system = rag_system
        self.multiplier_engine = multiplier_engine
        self.touch_injector = touch_injector
        self.cascade_engine = cascade_engine
        self.app_api_client = app_api_client
        self.screen_reader = screen_reader
        self.device_manager = device_manager if device_manager is not None else DeviceManager()
        self.config = config if config is not None else load_config()

        # operation_id -> {"cascade_id", "bet_gruppo_id", "identity_id",
        # "bookmaker_id", "registered_events"} - stato in-memory, stesso limite
        # di CascadeEngine._cascades (non sopravvive a un restart del processo).
        self._cascade_state: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # Step 1-3: parse -> verifica T&C -> calcolo. Non piazza nulla.
    # ------------------------------------------------------------------

    def process_signal(self, raw_text: str, telegram_message_id: str) -> Result[SignalFlowResult]:
        """Parsea il segnale, verifica i T&C e (per sport) calcola la cascata.
        Non piazza nulla: il risultato va salvato come pending HITL dal chiamante."""
        parse_result = self.signal_parser.parse(raw_text, telegram_message_id)
        if not parse_result.success:
            return Result.fail(parse_result.error, parse_result.error_code)
        signal = parse_result.data

        tc_result = self.rag_system.verify_tc(signal)
        if not tc_result.success:
            return Result.fail(tc_result.error, tc_result.error_code)
        tc_verification = tc_result.data

        strategy = signal.strategy or {}
        identity_ids_available = self._resolve_identity_ids(strategy)

        if not tc_verification.verified:
            return Result.ok(SignalFlowResult(
                signal=signal,
                tc_verification=tc_verification,
                cascade_calc=None,
                identity_ids_available=identity_ids_available,
                status="tc_discrepancy",
            ))

        cascade_calc = None
        if signal.category == "sport":
            calc_result = self.multiplier_engine.calculate_cascade(
                back_stake=strategy.get("back_stake", 0.0),
                back_odds=strategy.get("back_odds", []),
                lay_odds=strategy.get("lay_odds", []),
                commission=strategy.get("commission", 0.045),
            )
            if not calc_result.success:
                return Result.ok(SignalFlowResult(
                    signal=signal,
                    tc_verification=tc_verification,
                    cascade_calc=None,
                    identity_ids_available=identity_ids_available,
                    status="calc_error",
                    error=calc_result.error,
                ))
            cascade_calc = calc_result.data

        return Result.ok(SignalFlowResult(
            signal=signal,
            tc_verification=tc_verification,
            cascade_calc=cascade_calc,
            identity_ids_available=identity_ids_available,
            status="pending_hitl",
        ))

    def _resolve_identity_ids(self, strategy: dict) -> list[str]:
        """Le identita' candidate arrivano dal segnale stesso (strategy dict),
        non da un lookup su config: coerente col pattern gia' usato da
        Orchestrator.process_signal (strategy.get('identity_id'))."""
        if strategy.get("identity_id"):
            return [strategy["identity_id"]]
        return list(strategy.get("identity_ids", []))

    # ------------------------------------------------------------------
    # Step 5-6: conferma HITL -> piazza back+lay e registra su Netcap.
    # ------------------------------------------------------------------

    def execute_operation(self, hitl_item_id: str, identity_ids: list[str]) -> Result[ExecutionResult]:
        """Raffaele ha confermato: per ogni identita' selezionata piazza il back
        bet, avvia la cascata (se sport) e registra su Netcap. Un fallimento su
        una identita' non blocca le altre."""
        from orchestrator.flows.sport_flow_handler import SportFlowHandler

        flow_result = SportFlowHandler().get_pending_flow(hitl_item_id)
        if flow_result is None:
            return Result.fail(f"Flow {hitl_item_id} non trovato", "FLOW_NOT_FOUND")

        signal = flow_result.signal
        strategy = signal.strategy or {}
        cascade_calc = flow_result.cascade_calc

        identities_executed: list[str] = []
        identities_failed: list[str] = []
        errors: list[str] = []
        operation_id: Optional[int] = None
        bet_gruppo_id: Optional[str] = None
        cascade_started = False

        for identity_id in identity_ids:
            device_id = self.device_manager.get_device_for_identity(identity_id, self.config)
            if device_id is None:
                identities_failed.append(identity_id)
                errors.append(f"{identity_id}: nessun device mappato")
                continue

            if not self.device_manager.acquire_lock(device_id, operation_id=f"sport_{hitl_item_id}_{identity_id}"):
                identities_failed.append(identity_id)
                errors.append(f"{identity_id}: device {device_id} occupato")
                continue

            try:
                result = self._execute_for_identity(
                    hitl_item_id, identity_id, device_id, signal, strategy, cascade_calc,
                )
                if result is None:
                    identities_failed.append(identity_id)
                    errors.append(f"{identity_id}: fallito (vedi log)")
                    continue
                if isinstance(result, str):
                    identities_failed.append(identity_id)
                    errors.append(f"{identity_id}: {result}")
                    continue

                identity_operazione_id, identity_bet_gruppo_id, identity_cascade_started = result
                operation_id = identity_operazione_id
                bet_gruppo_id = identity_bet_gruppo_id
                cascade_started = cascade_started or identity_cascade_started
                identities_executed.append(identity_id)
            finally:
                self.device_manager.release_lock(device_id)

        return Result.ok(ExecutionResult(
            operation_id=operation_id,
            bet_gruppo_id=bet_gruppo_id,
            identities_executed=identities_executed,
            identities_failed=identities_failed,
            cascade_started=cascade_started,
            errors=errors,
        ))

    def _execute_for_identity(self, hitl_item_id, identity_id, device_id, signal, strategy, cascade_calc):
        """Ritorna (operazione_id, bet_gruppo_id, cascade_started) su successo,
        o una stringa di errore su fallimento."""
        bookmaker_id, _is_exchange = self._resolve_bookmaker(identity_id, signal.book)
        if bookmaker_id is None:
            return f"book {signal.book} non trovato per l'identita'"

        back_result = self.touch_injector.place_back_bet(
            device_id, signal.book, strategy.get("back_stake", 0.0), strategy.get("selections", []),
        )
        if not back_result.success:
            return f"piazzamento back bet fallito: {back_result.error}"
        if not back_result.data.confirmed:
            return "back bet non confermato"

        item_bet_gruppo_id = f"cascade_{hitl_item_id}_{identity_id}" if cascade_calc is not None else None
        cascade_id = None
        if cascade_calc is not None:
            operation = {
                "device_id": device_id,
                "operation_id": hitl_item_id,
                "identity_id": identity_id,
                "markets": strategy.get("markets", []),
                "commission_rate": strategy.get("commission", 0.045),
            }
            cascade_result = self.cascade_engine.start_cascade(operation, cascade_calc)
            if not cascade_result.success:
                return f"avvio cascata fallito: {cascade_result.error}"
            cascade_id = cascade_result.data

        op_result = self.app_api_client.write_operazione({
            "nome": f"P5 Sport - {signal.book}",
            "tipologia": "Ricorrenti",
            "categoria": "Sport",
            "bookmaker_id": bookmaker_id,
            "collaboratori_ids": identity_id,
            "stato": "APERTO",
            "data_apertura": date.today().isoformat(),
            "note": f"Segnale {signal.telegram_message_id}",
        })
        if not op_result.success:
            return f"creazione operazione fallita: {op_result.error}"
        if not op_result.data.success:
            return f"creazione operazione fallita: {op_result.data.message}"

        operazione_id = op_result.data.netcap_id
        if operazione_id is None:
            return "operazione creata senza id"

        if cascade_calc is not None:
            first_event = cascade_calc.events[0]
            riga_result = self.app_api_client.add_riga_cascata(
                operazione_id=operazione_id,
                collaboratore_id=identity_id,
                bookmaker_id=bookmaker_id,
                back_stake=strategy.get("back_stake", 0.0),
                back_odds=self._scalar(cascade_calc.back_odds),
                liability=first_event.liability,
                lay_odds=first_event.lay_odds,
                bet_gruppo_id=item_bet_gruppo_id,
                note=f"Cascata evento 1/{len(cascade_calc.events)}",
            )
        else:
            riga_result = self.app_api_client.add_riga_cascata(
                operazione_id=operazione_id,
                collaboratore_id=identity_id,
                bookmaker_id=bookmaker_id,
                back_stake=strategy.get("back_stake", 0.0),
                back_odds=self._scalar(strategy.get("back_odds")),
                liability=0.0,
                lay_odds=0.0,
                bet_gruppo_id=None,
                note="Giocata casino",
            )
        if not riga_result.success:
            return f"scrittura riga fallita: {riga_result.error}"
        if not riga_result.data.success:
            return f"scrittura riga fallita: {riga_result.data.message}"

        cascade_started = False
        if cascade_calc is not None and cascade_id is not None:
            self._cascade_state[operazione_id] = {
                "cascade_id": cascade_id,
                "bet_gruppo_id": item_bet_gruppo_id,
                "identity_id": identity_id,
                "bookmaker_id": bookmaker_id,
                "registered_events": 1,
            }
            cascade_started = True

        return operazione_id, item_bet_gruppo_id, cascade_started

    def _resolve_bookmaker(self, identity_id: str, book: str) -> tuple[Optional[int], bool]:
        for identity in self.config.get("identities", []):
            if identity.get("id") != identity_id:
                continue
            for entry in identity.get("books", []):
                if entry.get("name") == book:
                    return entry.get("bookmaker_id"), bool(entry.get("is_exchange", False))
        return None, False

    def _scalar(self, value, default: float = 0.0) -> float:
        if isinstance(value, list):
            return value[0] if value else default
        return value if value is not None else default

    # ------------------------------------------------------------------
    # Step 7: monitoraggio cascata (un tick di polling per chiamata).
    # ------------------------------------------------------------------

    def monitor_cascade(self, operation_id: int) -> Result[bool]:
        """Un singolo tick di polling: fa avanzare la cascata di un passo
        (CascadeEngine piazza da solo le bancate successive), registra su
        Netcap le nuove bancate e, se la cascata e' completa, chiude il
        gruppo e l'operazione. Va richiamata periodicamente dal chiamante
        (rispettando CascadeState.next_check_at), non e' un loop bloccante."""
        info = self._cascade_state.get(operation_id)
        if info is None:
            return Result.fail(f"Nessuna cascata attiva per operazione {operation_id}", "CASCADE_NOT_FOUND")

        advance_result = self.cascade_engine.check_and_advance(info["cascade_id"])
        if not advance_result.success:
            return Result.fail(advance_result.error, advance_result.error_code)

        state = advance_result.data
        bet_gruppo_id = info["bet_gruppo_id"]

        registered = info["registered_events"]
        total_placed = len(state.betfair_bet_ids)
        if total_placed > registered:
            for index in range(registered, total_placed):
                self.app_api_client.add_riga_cascata(
                    operazione_id=operation_id,
                    collaboratore_id=info["identity_id"],
                    bookmaker_id=info["bookmaker_id"],
                    back_stake=state.back_stake,
                    back_odds=self._scalar(state.back_odds),
                    liability=state.liabilities[index],
                    lay_odds=state.lay_odds[index],
                    bet_gruppo_id=bet_gruppo_id,
                    note=f"Cascata evento {index + 1}/{len(state.lay_odds)}",
                )
            info["registered_events"] = total_placed

        if state.state == "completed":
            status_result = self.screen_reader.read_bet_status(state.device_id, betfair=True)
            last_status = status_result.data.status if status_result.success else None
            win, lose = self._determine_outcome(last_status)
            self.app_api_client.close_bet_gruppo_outcome(bet_gruppo_id, win, lose)
            self.app_api_client.close_operazione(operation_id)
            return Result.ok(True)

        return Result.ok(False)

    def _determine_outcome(self, last_status: Optional[str]) -> tuple[bool, bool]:
        """Se l'ultima bancata letta su Betfair e' 'settled_won', la bancata ha
        vinto -> la multipla sul book ha perso quella gamba -> book lose. In
        ogni altro caso (esaurimento eventi con ultima bancata persa, o lettura
        non disponibile) si considera la multipla vinta sul book. Logica isolata
        qui apposta: se il mapping economico reale differisce, va corretta solo
        in questo metodo."""
        if last_status == "settled_won":
            return False, True
        return True, False

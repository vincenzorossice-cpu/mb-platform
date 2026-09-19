import os
from datetime import date, datetime

from orchestrator.core.config import load_config
from orchestrator.core.device_manager import DeviceManager
from orchestrator.core.result import Result

from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.book_navigator.navigator import BookNavigator
from orchestrator.modules.cascade_engine.engine import CascadeEngine
from orchestrator.modules.movement_importer.importer import MovementImporter
from orchestrator.modules.multiplier_engine.calculator import MultiplierEngine
from orchestrator.modules.opsec.monitor import OpSec
from orchestrator.modules.rules_engine.engine import RulesEngine
from orchestrator.modules.rules_engine.interface import Account
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.signal_parser.parser import SignalParser
from orchestrator.modules.touch_injector.injector import TouchInjector

DEFAULT_NETCAP_URL = "http://37.221.197.184:3001/api"


class Orchestrator:
    """Coordinatore principale. Chiama i moduli nell'ordine giusto."""

    def __init__(self, config_path: str = None):
        self.config = load_config(config_path)

        self.device_manager = DeviceManager()
        self.screen_reader = ScreenReader()
        self.touch_injector = TouchInjector(screen_reader=self.screen_reader)
        self.book_navigator = BookNavigator(
            screen_reader=self.screen_reader, touch_injector=self.touch_injector
        )
        self.multiplier_engine = MultiplierEngine()
        self.rules_engine = RulesEngine()

        netcap_url = self.config.get("netcap", {}).get("base_url", DEFAULT_NETCAP_URL)
        session_token = os.environ.get("NETCAP_SESSION_TOKEN")
        self.app_api_client = AppApiClient(base_url=netcap_url, session_token=session_token)

        self.movement_importer = MovementImporter(
            app_api_client=self.app_api_client, screen_reader=self.screen_reader
        )
        self.signal_parser = SignalParser()
        self.rag_system = None  # lazy init (ChromaDB pesante)
        self.cascade_engine = CascadeEngine(
            screen_reader=self.screen_reader,
            touch_injector=self.touch_injector,
            multiplier_engine=self.multiplier_engine,
        )
        self.opsec = OpSec()

        self._hitl_queue: list[dict] = []

    # ------------------------------------------------------------------
    # P6 — riconciliazione
    # ------------------------------------------------------------------

    def run_daily_riconciliazione(self, live: bool = False) -> Result[dict]:
        # OpSec gate: verifica correlation risk
        planned_ops = []
        for identity in self.config.get("identities", []):
            for book in identity.get("books", []):
                planned_ops.append({
                    "identity_id": identity["id"],
                    "book": book["name"],
                    "stake_back": 0,
                    "opened_at": datetime.now().isoformat(),
                    "operation_type": "riconciliazione"
                })
        
        opsec_result = self.opsec.check_correlation_risk(planned_ops)
        opsec_info = {"risk_level": "unknown", "warnings": []}
        if opsec_result.success:
            check = opsec_result.data
            opsec_info = {"risk_level": check.risk_level, "warnings": check.warnings}
            if check.blocked:
                print("OpSec BLOCKED: " + "; ".join(check.warnings))
                self.add_hitl_item("opsec_blocked", {
                    "reason": "; ".join(check.warnings),
                    "risk_level": check.risk_level
                }, "critical")
                return Result.fail("OpSec blocked: " + "; ".join(check.warnings), "OPSEC_BLOCKED")
            if check.warnings:
                print("OpSec WARNING: " + "; ".join(check.warnings))
        """Esegue la riconciliazione P6 per tutti gli ID e tutti i book aperti.

        In modalita' live, tutte le rettifiche del ciclo vengono scritte in
        un'unica operazione 'Riconciliazione' (creata lazy, alla prima rettifica
        necessaria), con una riga per rettifica, chiusa subito dopo la scrittura;
        l'operazione viene chiusa una volta sola a fine ciclo e ogni rettifica
        scritta viene poi verificata contro il saldo reale del book."""
        summary = {"total": 0, "adjusted": 0, "no_adjustment": 0, "hitl": 0, "errors": []}
        ctx = {"operazione_id": None, "operazione_create_failed": False, "written": []}

        for identity in self.config.get("identities", []):
            identity_id = identity["id"]
            books = identity.get("books", [])
            if not books:
                continue

            expected_result = self.app_api_client.read_expected_balance(identity_id)
            if not expected_result.success:
                for book in books:
                    summary["total"] += 1
                    summary["errors"].append({
                        "identity_id": identity_id,
                        "book": book["name"],
                        "error": expected_result.error,
                    })
                continue

            expected_by_bookmaker = {eb.bookmaker_id: eb for eb in expected_result.data}

            for book in books:
                summary["total"] += 1
                self._reconcile_one(identity_id, book, expected_by_bookmaker, live, summary, ctx)

        if live and ctx["operazione_id"] is not None:
            close_op_res = self.app_api_client.close_operazione(ctx["operazione_id"])
            if not close_op_res.success:
                summary["errors"].append({
                    "identity_id": None,
                    "book": None,
                    "error": f"Chiusura operazione {ctx['operazione_id']} fallita: {close_op_res.error}",
                })

        if live:
            for identity_id, bookmaker_id, book_name, expected_balance in ctx["written"]:
                verify_res = self.app_api_client.verify_balance_after_rettifica(
                    identity_id, bookmaker_id, expected_balance
                )
                if not verify_res.success:
                    summary["errors"].append({
                        "identity_id": identity_id,
                        "book": book_name,
                        "error": f"Verifica saldo post-rettifica fallita: {verify_res.error}",
                    })
                elif verify_res.data is False:
                    summary["errors"].append({
                        "identity_id": identity_id,
                        "book": book_name,
                        "error": f"Saldo Netcap non coincide dopo la rettifica: atteso {expected_balance}",
                    })

        return Result.ok(summary)

    def _reconcile_one(self, identity_id: str, book: dict, expected_by_bookmaker: dict,
                        live: bool, summary: dict, ctx: dict) -> None:
        device_id = None
        try:
            account = Account(
                account_id=f"{identity_id}_{book['bookmaker_id']}",
                identity_id=identity_id,
                book=book["name"],
                is_betfair=book["is_exchange"],
            )

            opsec_batch = [{
                "identity_id": identity_id,
                "book": book["name"],
                "stake_back": 0.0,
                "opened_at": datetime.now().isoformat(),
                "operation_type": "riconciliazione",
            }]
            opsec_result = self.opsec.check_correlation_risk(opsec_batch)
            if opsec_result.success and opsec_result.data.blocked:
                self.add_hitl_item(
                    "opsec_blocked",
                    {"identity_id": identity_id, "book": book["name"], "warnings": opsec_result.data.warnings},
                    severity="warning",
                )
                return

            device_id = self.device_manager.get_device_for_identity(identity_id, self.config)
            if device_id is None:
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"], "error": "Nessun device mappato",
                })
                return

            if not self.device_manager.acquire_lock(device_id, operation_id=f"reconcile_{identity_id}_{book['name']}"):
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"], "error": "Device busy",
                })
                device_id = None  # non acquisito, non rilasciare
                return

            balance_result = self.screen_reader.read_balance(device_id, book["name"])
            if not balance_result.success:
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"], "error": balance_result.error,
                })
                return

            expected = expected_by_bookmaker.get(book["bookmaker_id"])
            if expected is None:
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"], "error": "Saldo atteso non trovato",
                })
                return

            reconcile_result = self.rules_engine.reconcile(
                balance_result.data, expected.saldo_atteso, account,
                impegnato_netcap=expected.importo_impegnato,
            )
            if not reconcile_result.success:
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"], "error": reconcile_result.error,
                })
                return

            self._apply_reconciliation(
                identity_id, book, reconcile_result.data, live, summary, ctx,
                impegnato_netcap=expected.importo_impegnato,
            )
        finally:
            if device_id is not None:
                self.device_manager.release_lock(device_id)

    def _apply_reconciliation(self, identity_id: str, book: dict, reconciliation, live: bool,
                               summary: dict, ctx: dict, impegnato_netcap: float = 0.0) -> None:
        if reconciliation.action == "no_adjustment":
            summary["no_adjustment"] += 1
        elif reconciliation.action == "adjust":
            if live:
                self._write_rettifica_live(identity_id, book, reconciliation, ctx, summary, impegnato_netcap)
            else:
                print(
                    f"[DRY-RUN] {identity_id}/{book['name']}: rettifica {reconciliation.rectification}"
                )
            summary["adjusted"] += 1
        elif reconciliation.action == "hitl_required":
            # una bancata Betfair non registrata su Netcap fa scattare
            # open_bets_netcap_mismatch qui sopra: va gestita dal flusso
            # dedicato (test_betfair_unregistered_ops.py + dashboard), non
            # accodata come hitl_required generico
            is_betfair_open_bets_case = (
                book.get("is_exchange") and "open_bets_netcap_mismatch" in reconciliation.rules_applied
            )
            if not is_betfair_open_bets_case:
                self.add_hitl_item(
                    "reconciliation",
                    {"identity_id": identity_id, "book": book["name"], "reconciliation": reconciliation},
                    severity="warning",
                )
            summary["hitl"] += 1

    def _write_rettifica_live(self, identity_id: str, book: dict, reconciliation,
                               ctx: dict, summary: dict, impegnato_netcap: float = 0.0) -> None:
        """Scrive la rettifica sull'operazione di riconciliazione condivisa del
        ciclo, creandola lazy alla prima rettifica necessaria."""
        if ctx["operazione_id"] is None:
            if ctx["operazione_create_failed"]:
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"],
                    "error": "Operazione riconciliazione non creata (fallita in precedenza in questo ciclo)",
                })
                return
            create_res = self.app_api_client.create_riconciliazione_operazione(date.today().isoformat())
            if not create_res.success:
                ctx["operazione_create_failed"] = True
                summary["errors"].append({
                    "identity_id": identity_id, "book": book["name"],
                    "error": f"Creazione operazione riconciliazione fallita: {create_res.error}",
                })
                return
            ctx["operazione_id"] = create_res.data

        rect = reconciliation.rectification
        riga_res = self.app_api_client.add_riga_rettifica(
            ctx["operazione_id"], identity_id, book["bookmaker_id"],
            rect.amount, rect.column, rect.reason, rect.bonus_note,
        )
        if not riga_res.success:
            summary["errors"].append({
                "identity_id": identity_id, "book": book["name"],
                "error": f"Scrittura riga rettifica fallita: {riga_res.error}",
            })
            return

        close_res = self.app_api_client.close_riga(riga_res.data)
        if not close_res.success:
            summary["errors"].append({
                "identity_id": identity_id, "book": book["name"],
                "error": f"Chiusura riga {riga_res.data} fallita: {close_res.error}",
            })
            # non interrompere: la verifica finale resta comunque significativa

        # verify_balance_after_rettifica confronta il matrix (totale) grezzo di Netcap:
        # reconciliation.adjusted_balance e' il disponibile telefono (raw+real_bonus),
        # va ricomposto in totale riaggiungendo l'impegnato (non toccato dalla rettifica)
        matrix_atteso_post_rettifica = round(reconciliation.adjusted_balance + impegnato_netcap, 2)
        ctx["written"].append((identity_id, book["bookmaker_id"], book["name"], matrix_atteso_post_rettifica))

    # ------------------------------------------------------------------
    # P7 — import movimenti
    # ------------------------------------------------------------------

    def run_import_movimenti(self, device_id: str, identity_id: str) -> Result[dict]:
        """Importa movimenti PayPal per un identity."""
        if not self.device_manager.acquire_lock(device_id, operation_id=f"import_{identity_id}"):
            return Result.fail("Device busy", "DEVICE_BUSY")

        try:
            import_result = self.movement_importer.import_paypal(device_id, identity_id)
            if not import_result.success:
                return Result.fail(import_result.error, import_result.error_code)

            movements = import_result.data
            save_result = self.movement_importer.save_pending_movements(movements, identity_id)
            errors = [] if save_result.success else [save_result.error]

            print(f"{len(movements)} movimenti salvati. Apri la dashboard per approvarli.")
            return Result.ok({
                "movements_found": len(movements),
                "movements_saved": len(movements) if save_result.success else 0,
                "errors": errors,
            })
        finally:
            self.device_manager.release_lock(device_id)

    # ------------------------------------------------------------------
    # P5 — sport
    # ------------------------------------------------------------------

    def process_signal(self, raw_signal: str, telegram_message_id: str) -> Result[dict]:
        """Processa un segnale Telegram (P5 Sport). Non piazza scommesse reali."""
        parse_result = self.signal_parser.parse_and_verify(raw_signal, telegram_message_id)
        if not parse_result.success:
            return Result.fail(parse_result.error, parse_result.error_code)

        signal = parse_result.data
        strategy = signal.strategy or {}

        opsec_batch = [{
            "identity_id": strategy.get("identity_id", "unknown"),
            "book": signal.book,
            "stake_back": strategy.get("back_stake", 0.0),
            "opened_at": datetime.now().isoformat(),
            "operation_type": signal.category,
        }]
        opsec_result = self.opsec.check_correlation_risk(opsec_batch)
        if opsec_result.success and opsec_result.data.blocked:
            self.add_hitl_item(
                "opsec_blocked_signal",
                {"telegram_message_id": telegram_message_id, "book": signal.book, "warnings": opsec_result.data.warnings},
                severity="warning",
            )
            return Result.ok({
                "signal_parsed": True,
                "tc_verified": signal.tc_verified,
                "calculation": None,
                "cascade_started": False,
            })

        calc_result = self.multiplier_engine.calculate_cascade(
            back_stake=strategy.get("back_stake", 0.0),
            back_odds=strategy.get("back_odds", []),
            lay_odds=strategy.get("lay_odds", []),
            commission=strategy.get("commission", 0.045),
        )
        if not calc_result.success:
            return Result.fail(calc_result.error, calc_result.error_code)

        print(f"Calcolo cascata per segnale {telegram_message_id}: {calc_result.data}")

        return Result.ok({
            "signal_parsed": True,
            "tc_verified": signal.tc_verified,
            "calculation": calc_result.data,
            "cascade_started": False,
        })

    # ------------------------------------------------------------------
    # health check / HITL
    # ------------------------------------------------------------------

    def run_health_check(self) -> dict:
        """Verifica salute del sistema."""
        heartbeat = self.device_manager.heartbeat_all()
        devices_total = len(heartbeat)
        devices_online = sum(1 for d in heartbeat.values() if d["online"])

        netcap_result = self.app_api_client.read_book_status("_healthcheck")
        netcap_reachable = netcap_result.success or netcap_result.error_code not in (
            "NETCAP_UNREACHABLE", "NETCAP_TIMEOUT",
        )

        report = {
            "devices_online": devices_online,
            "devices_total": devices_total,
            "netcap_reachable": netcap_reachable,
            "hitl_pending": len(self._hitl_queue),
        }
        print(f"Health check: {devices_online}/{devices_total} telefoni online, "
              f"Netcap {'raggiungibile' if netcap_reachable else 'NON raggiungibile'}")
        return report

    def get_hitl_queue(self) -> list[dict]:
        """Restituisce la coda HITL per la dashboard."""
        return self._hitl_queue

    def add_hitl_item(self, item_type: str, data: dict, severity: str = "warning"):
        """Aggiunge un elemento alla coda HITL."""
        self._hitl_queue.append({
            "id": f"hitl_{len(self._hitl_queue) + 1}",
            "type": item_type,
            "data": data,
            "severity": severity,
            "created_at": datetime.now().isoformat(),
        })

    # ------------------------------------------------------------------
    # ciclo completo
    # ------------------------------------------------------------------

    def run_full_cycle(self, live: bool = False) -> dict:
        """Esegue un ciclo completo: health check + riconciliazione."""
        health = self.run_health_check()
        riconciliazione_result = self.run_daily_riconciliazione(live=live)

        report = {
            "health": health,
            "riconciliazione": riconciliazione_result.data if riconciliazione_result.success else None,
            "riconciliazione_error": None if riconciliazione_result.success else riconciliazione_result.error,
        }
        print(f"Ciclo completo: {report}")
        return report

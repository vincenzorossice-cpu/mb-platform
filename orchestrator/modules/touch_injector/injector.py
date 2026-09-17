import subprocess
import time
import random
import re
from typing import Optional
from orchestrator.core.result import Result
from orchestrator.modules.touch_injector.interface import (
    TouchInjectorInterface,
    BackBetResult,
    LayBetResult,
)
from orchestrator.modules.screen_reader.interface import ScreenReaderInterface

# FIX-24: caratteri interpretati dalla shell del device (adb shell input text)
# che vanno rimossi dal testo prima di inviarlo, per evitare injection
_DANGEROUS_CHARS_RE = re.compile(r"[;&$`|<>(){}\[\]]")


class TouchInjector(TouchInjectorInterface):
    def __init__(
        self,
        screen_reader: ScreenReaderInterface,
        adb_timeout: int = 10,
        bet_confirm_timeout: int = 15,
    ):
        self.screen_reader = screen_reader
        self.adb_timeout = adb_timeout
        self.bet_confirm_timeout = bet_confirm_timeout

    # --------------------------------------------------------
    # PUBLIC: tap_element
    # --------------------------------------------------------

    def tap_element(self, device_id: str, hint: str, field: str = "text", verify: bool = True) -> Result[bool]:
        """Trova l'elemento via ScreenReader, calcola centro bounds, tap umanizzato.
        Retry fino a 2 volte con re-identificazione su TAP_MISSED.
        Se verify=True (default), verifica che il tap abbia avuto un effetto reale."""
        max_retries = 2
        backoffs = [1.0, 2.0]

        for attempt in range(max_retries + 1):
            elem_result = self.screen_reader.find_element(device_id, hint, field)
            if not elem_result.success:
                if attempt < max_retries:
                    time.sleep(backoffs[attempt])
                    continue
                return Result.fail(f"Element '{hint}' not found after {max_retries + 1} attempts", "TAP_MISSED")

            elem = elem_result.data
            cx, cy = self._bounds_center(elem.bounds)

            # FIX-6: cattura lo schermo pre-tap (solo se verrà verificato) per poter
            # confrontare l'effetto del tap
            pre_screen = self.screen_reader.read_screen(device_id) if verify else None

            tap_result = self._human_tap(device_id, cx, cy)
            if not tap_result.success:
                return tap_result

            if not verify:
                return Result.ok(True)

            return self._verify_tap_effect(device_id, hint, field, pre_screen)

        # FIX-BASSA-1: rimosso return Result.fail(...) finale, irraggiungibile —
        # ogni ramo del for sopra ritorna gia' esplicitamente

    # --------------------------------------------------------
    # PUBLIC: tap_coordinates
    # --------------------------------------------------------

    def tap_coordinates(self, device_id: str, x: int, y: int) -> Result[bool]:
        """Tap su coordinate con jitter e delay umanizzati."""
        return self._human_tap(device_id, x, y)

    # --------------------------------------------------------
    # PUBLIC: input_text
    # --------------------------------------------------------

    def input_text(self, device_id: str, text: str, field_hint: Optional[str] = None) -> Result[bool]:
        """Inserisce testo. Se field_hint è dato, prima tap per focusare il campo."""
        if field_hint:
            tap_result = self.tap_element(device_id, field_hint)
            if not tap_result.success:
                return Result.fail(
                    f"Cannot focus field '{field_hint}': {tap_result.error}",
                    "TEXT_INPUT_FAILED",
                )
            time.sleep(0.3)

        sanitized = _DANGEROUS_CHARS_RE.sub("", text)  # FIX-24: rimuove caratteri pericolosi per la shell del device
        escaped = sanitized.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "text", escaped],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
            if result.returncode != 0:
                return Result.fail(f"input text failed: {result.stderr}", "TEXT_INPUT_FAILED")
            return Result.ok(True)
        except subprocess.TimeoutExpired:
            return Result.fail("ADB timeout during input text", "ADB_TIMEOUT")
        except Exception as e:
            return Result.fail(f"input text error: {e}", "TEXT_INPUT_FAILED")

    # --------------------------------------------------------
    # PUBLIC: navigate_to
    # --------------------------------------------------------

    def navigate_to(self, device_id: str, destination: str, book: str) -> Result[bool]:
        """Naviga verso una schermata target. Verifica con ScreenReader dopo ogni step.
        Max 5 tentativi di navigazione prima di fallire."""
        max_steps = 5
        had_successful_tap = False  # FIX-23: tracciato su tutti gli step, per il messaggio finale

        for step in range(max_steps):
            screen = self.screen_reader.read_screen(device_id)
            if not screen.success:
                return Result.fail(f"Cannot read screen during navigation: {screen.error}", "NAVIGATION_FAILED")

            if screen.data.screen_type == destination:
                return Result.ok(True)

            # Cerca un elemento che possa portare alla destinazione
            nav_hints = self._navigation_hints(destination, book)
            found_hint = False
            for hint in nav_hints:
                elem = self.screen_reader.find_element(device_id, hint)
                if elem.success:
                    found_hint = True
                    cx, cy = self._bounds_center(elem.data.bounds)
                    # FIX-23: cattura l'esito del tap invece di ignorarlo — un tap fallito
                    # non deve far credere che la navigazione sia avanzata
                    tap_result = self._human_tap(device_id, cx, cy)
                    if tap_result.success:
                        had_successful_tap = True
                    break

            if not found_hint:
                return Result.fail(
                    f"No navigation path found to '{destination}' on {book}",
                    "NAVIGATION_FAILED",
                )

        # Verifica finale
        final = self.screen_reader.read_screen(device_id)
        if final.success and final.data.screen_type == destination:
            return Result.ok(True)

        if not had_successful_tap:
            # FIX-23: nessun tap e' mai andato a buon fine in nessuno step
            return Result.fail("Navigation failed: no successful taps", "NAVIGATION_FAILED")

        return Result.fail(
            f"Destination '{destination}' not reached after {max_steps} steps",
            "NAVIGATION_FAILED",
        )

    # --------------------------------------------------------
    # PUBLIC: place_back_bet
    # --------------------------------------------------------

    def place_back_bet(
        self, device_id: str, book: str, stake: float, selections: list[dict],
    ) -> Result[BackBetResult]:
        """Piazza scommessa back. Inserisce stake, conferma, verifica."""
        # Seleziona le quote
        for sel in selections:
            event_hint = sel.get("event", "")
            if event_hint:
                tap_res = self.tap_element(device_id, event_hint)
                if not tap_res.success:
                    return Result.fail(f"Cannot select event: {tap_res.error}", "TAP_MISSED")

            odds_hint = sel.get("odds_hint", "")
            if odds_hint:
                tap_res = self.tap_element(device_id, odds_hint)
                if not tap_res.success:
                    return Result.fail(f"Cannot select odds: {tap_res.error}", "TAP_MISSED")

        # Inserisci stake
        stake_str = f"{stake:.2f}"
        input_res = self.input_text(device_id, stake_str, field_hint="importo")
        if not input_res.success:
            input_res = self.input_text(device_id, stake_str, field_hint="stake")
            if not input_res.success:
                return Result.fail(f"Cannot input stake: {input_res.error}", "TEXT_INPUT_FAILED")

        # Conferma
        confirm_res = self.tap_element(device_id, "scommetti")
        if not confirm_res.success:
            confirm_res = self.tap_element(device_id, "place bet")
            if not confirm_res.success:
                return Result.fail("Cannot confirm bet", "BET_NOT_CONFIRMED")

        # Attendi e verifica conferma
        deadline = time.time() + self.bet_confirm_timeout
        while time.time() < deadline:
            time.sleep(1.0)
            screen = self.screen_reader.read_screen(device_id)
            if not screen.success:
                continue
            texts_lower = " ".join(e.text.lower() for e in screen.data.elements)
            if any(kw in texts_lower for kw in ["confermata", "confirmed", "ricevuta", "accepted"]):
                bet_id = self._extract_bet_id(screen.data.elements)
                return Result.ok(BackBetResult(confirmed=True, bet_id=bet_id, actual_stake=stake))
            if any(kw in texts_lower for kw in ["quota cambiata", "odds changed", "odds have changed"]):
                return Result.fail("Odds changed during placement", "ODDS_CHANGED")
            if any(kw in texts_lower for kw in ["saldo insufficiente", "insufficient"]):
                return Result.fail("Insufficient balance", "BET_NOT_CONFIRMED")

        return Result.fail("Bet not confirmed within timeout", "BET_NOT_CONFIRMED")

    # --------------------------------------------------------
    # PUBLIC: place_lay_bet
    # --------------------------------------------------------

    def place_lay_bet(
        self, device_id: str, market: str, odds: float, stake: float,
    ) -> Result[LayBetResult]:
        """Piazza bancata su Betfair. Implementa verify-then-act su timeout."""
        # Inserisci odds
        odds_str = f"{odds:.2f}"
        input_odds = self.input_text(device_id, odds_str, field_hint="odds")
        if not input_odds.success:
            return Result.fail(f"Cannot input odds: {input_odds.error}", "TEXT_INPUT_FAILED")

        # Inserisci stake
        stake_str = f"{stake:.2f}"
        input_stake = self.input_text(device_id, stake_str, field_hint="stake")
        if not input_stake.success:
            return Result.fail(f"Cannot input lay stake: {input_stake.error}", "TEXT_INPUT_FAILED")

        # Tap conferma
        try:
            confirm_res = self._tap_lay_confirm(device_id)
        except _AdbTimeoutDuringLay:
            # VERIFY-THEN-ACT: ADB timeout, verifico lo stato reale
            return self._verify_lay_state(device_id, odds, stake)

        if not confirm_res.success:
            return confirm_res

        # Attendi conferma — polling fino a bet_confirm_timeout
        return self._wait_lay_confirmation(device_id, odds, stake)

    # --------------------------------------------------------
    # INTERNAL: tap verification
    # --------------------------------------------------------

    def _verify_tap_effect(
        self, device_id: str, hint: str, field: str, pre_screen: Result,
    ) -> Result[bool]:
        """FIX-6: verifica che il tap abbia avuto un effetto reale, invece di
        assumere sempre successo (un tap mancato non deve essere riportato come riuscito)."""
        time.sleep(0.5)
        post_screen = self.screen_reader.read_screen(device_id)
        if not post_screen.success:
            return Result.fail("Cannot verify tap effect", "TAP_VERIFY_FAILED")

        post_elem = self.screen_reader.find_element(device_id, hint, field)
        if not post_elem.success:
            # Elemento non più trovato -> schermata cambiata, il tap ha avuto effetto
            return Result.ok(True)

        if pre_screen.success and self._same_elements(pre_screen.data.elements, post_screen.data.elements):
            # Elemento ancora presente e schermo identico -> il tap non ha avuto effetto
            return Result.fail("Tap had no effect", "TAP_NO_EFFECT")

        # Elemento ancora presente ma schermo cambiato (es. campo di testo) -> tap riuscito
        return Result.ok(True)

    def _same_elements(self, pre_elements: list, post_elements: list) -> bool:
        """FIX-6: confronto semplice per testo, per rilevare se lo schermo è rimasto identico."""
        return [e.text for e in pre_elements] == [e.text for e in post_elements]

    # --------------------------------------------------------
    # INTERNAL: humanized tap
    # --------------------------------------------------------

    def _human_tap(self, device_id: str, x: int, y: int) -> Result[bool]:
        """Tap con jitter spaziale ±5px e delay temporale 0.3-1.5s."""
        jx = x + random.randint(-5, 5)
        jy = y + random.randint(-5, 5)
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "tap", str(jx), str(jy)],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
            delay = random.uniform(0.3, 1.5)
            time.sleep(delay)
            if result.returncode != 0:
                return Result.fail(f"ADB tap failed: {result.stderr}", "TAP_MISSED")
            return Result.ok(True)
        except subprocess.TimeoutExpired:
            return Result.fail("ADB timeout during tap", "ADB_TIMEOUT")
        except Exception as e:
            return Result.fail(f"Tap error: {e}", "TAP_MISSED")

    # --------------------------------------------------------
    # INTERNAL: lay bet helpers
    # --------------------------------------------------------

    def _tap_lay_confirm(self, device_id: str) -> Result[bool]:
        """Tap sul bottone di conferma lay. Alza _AdbTimeoutDuringLay su timeout."""
        elem = self.screen_reader.find_element(device_id, "place lay")
        if not elem.success:
            elem = self.screen_reader.find_element(device_id, "submit")
            if not elem.success:
                return Result.fail("Lay confirm button not found", "BET_NOT_CONFIRMED")

        cx, cy = self._bounds_center(elem.data.bounds)
        jx = cx + random.randint(-5, 5)
        jy = cy + random.randint(-5, 5)
        try:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "tap", str(jx), str(jy)],
                capture_output=True,
                text=True,
                timeout=self.adb_timeout,
            )
            time.sleep(random.uniform(0.3, 1.5))
            if result.returncode != 0:
                return Result.fail(f"Lay confirm tap failed: {result.stderr}", "TAP_MISSED")
            return Result.ok(True)
        except subprocess.TimeoutExpired:
            raise _AdbTimeoutDuringLay()

    def _wait_lay_confirmation(
        self, device_id: str, odds: float, stake: float,
    ) -> Result[LayBetResult]:
        """Polling post-conferma per lay bet."""
        # FIX-21: time.monotonic() invece di time.time() — un read_bet_status lento
        # (fino a piu' iterazioni di adb_timeout) non deve far sforare bet_confirm_timeout
        # in modo silenzioso; il deadline viene ricontrollato prima di ogni iterazione.
        start = time.monotonic()
        while time.monotonic() - start < self.bet_confirm_timeout:
            time.sleep(1.0)
            status = self.screen_reader.read_bet_status(device_id, betfair=True)
            if not status.success:
                continue
            if status.data.status in ("matched", "unsettled"):
                actual_odds = status.data.lay_odds if status.data.lay_odds else odds
                return Result.ok(LayBetResult(
                    confirmed=True, bet_id=None, actual_odds=actual_odds, actual_stake=stake,
                ))
            if status.data.status == "unknown":
                # FIX-5: schermata non riconosciuta durante il polling -> stato ambiguo,
                # mai trattarlo come confirmed
                return Result.fail(
                    "Ambiguous lay state while waiting confirmation: unknown",
                    "AMBIGUOUS_STATE",
                )
            if status.data.status == "not_found":
                return Result.fail("Lay bet not placed (verified)", "LAY_NOT_PLACED_RETRY_SAFE")

        return Result.fail("Lay bet not confirmed within timeout", "BET_NOT_CONFIRMED")

    def _verify_lay_state(
        self, device_id: str, odds: float, stake: float,
    ) -> Result[LayBetResult]:
        """Verify-then-act dopo ADB timeout durante place_lay_bet.
        MAI ritentare ciecamente — verifica lo stato reale."""
        time.sleep(2.0)  # attendi che l'eventuale tap sia processato
        status = self.screen_reader.read_bet_status(device_id, betfair=True)

        if not status.success:
            return Result.fail(
                f"Cannot verify lay state after timeout: {status.error}",
                "AMBIGUOUS_STATE",
            )

        if status.data.status in ("matched", "unsettled"):
            actual_odds = status.data.lay_odds if status.data.lay_odds else odds
            return Result.ok(LayBetResult(
                confirmed=True, bet_id=None, actual_odds=actual_odds, actual_stake=stake,
            ))

        if status.data.status == "unknown":
            # FIX-5: schermata non riconosciuta -> stato ambiguo, mai trattarlo come confirmed
            return Result.fail(
                f"Ambiguous lay state after timeout: {status.data.status}",
                "AMBIGUOUS_STATE",
            )

        if status.data.status == "not_found":
            return Result.fail("Lay bet not placed after timeout (verified)", "LAY_NOT_PLACED_RETRY_SAFE")

        return Result.fail(
            f"Ambiguous lay state after timeout: {status.data.status}",
            "AMBIGUOUS_STATE",
        )

    # --------------------------------------------------------
    # INTERNAL: utilities
    # --------------------------------------------------------

    def _bounds_center(self, bounds: list[int]) -> tuple[int, int]:
        """Calcola il centro di un rettangolo [x1, y1, x2, y2]."""
        return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2

    def _navigation_hints(self, destination: str, book: str) -> list[str]:
        """Restituisce hint di testo per navigare verso la destinazione."""
        hints_map = {
            "balance_page": ["saldo", "balance", "conto", "wallet", "portafoglio"],
            "my_bets": ["le mie scommesse", "my bets", "scommesse", "bet history"],
            "market_page": ["mercati", "markets", "sport", "calcio"],
            "movements": ["movimenti", "transactions", "storico", "history"],
        }
        return hints_map.get(destination, [destination])

    def _extract_bet_id(self, elements) -> Optional[str]:
        """Tenta di estrarre un ID scommessa dagli elementi schermata."""
        for elem in elements:
            match = re.search(r"(?:id|#|n°|ref)[:\s]*(\w{6,})", elem.text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None


class _AdbTimeoutDuringLay(Exception):
    """Eccezione interna per segnalare ADB timeout durante conferma lay.
    Non esce dal modulo — catturata in place_lay_bet."""

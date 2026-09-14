from orchestrator.core.result import Result
from orchestrator.modules.multiplier_engine.interface import (
    MultiplierEngineInterface,
    CascadeCalculation,
    CascadeEventCalc,
    SingleCalculation,
)


class MultiplierEngine(MultiplierEngineInterface):

    def calculate_cascade(
        self,
        back_stake: float,
        back_odds: list[float],
        lay_odds: list[float],
        commission: float = 0.045,
        strategy: str = "equal_profit"
    ) -> Result[CascadeCalculation]:
        # --- Validazione input ---
        if not back_odds or not lay_odds:
            return Result.fail("Quote vuote", "INVALID_ODDS")
        if len(back_odds) != len(lay_odds):
            return Result.fail(
                f"Numero quote back ({len(back_odds)}) != lay ({len(lay_odds)})",
                "INVALID_ODDS"
            )
        if any(o <= 1.0 for o in back_odds):
            return Result.fail(f"Quote back <= 1.0: {back_odds}", "INVALID_ODDS")
        if any(o <= 1.0 for o in lay_odds):
            return Result.fail(f"Quote lay <= 1.0: {lay_odds}", "INVALID_ODDS")
        if back_stake <= 0:
            return Result.fail(f"Back stake <= 0: {back_stake}", "INVALID_ODDS")
        if commission < 0 or commission >= 1:
            return Result.fail(f"Commissione non valida: {commission}", "INVALID_ODDS")

        n = len(back_odds)
        warnings = []

        # --- Quota combinata ---
        combined_odds = 1.0
        for b in back_odds:
            combined_odds *= b

        # --- Fattori di accumulo f_i ---
        # f_1 = 1, f_i = f_{i-1} * (lay_odds_{i-1} - commission) / (1 - commission)
        f = [1.0]
        for j in range(n - 1):
            f.append(f[-1] * (lay_odds[j] - commission) / (1 - commission))

        # --- Calcolo K = sum(f_i * (lay_odds_i - 1)) ---
        K = sum(f[i] * (lay_odds[i] - 1) for i in range(n))

        # --- Calcolo P (profitto equal-profit) ---
        # P = S * ((C-1)*(1-comm) - K) / (K + 1 - comm)
        numerator = (combined_odds - 1) * (1 - commission) - K
        denominator = K + 1 - commission
        P = back_stake * numerator / denominator

        # --- Validazione profitto ---
        # Il profitto può essere negativo (qualifying loss) — è normale in matched betting
        if P <= 0:
            warnings.append(f"Profitto negativo (qualifying loss): P={P:.2f}")

        # --- Calcolo lay_stake per ogni evento ---
        A = P + back_stake  # A_1
        events = []
        for i in range(n):
            lay_stake_i = A / (1 - commission)
            liability_i = lay_stake_i * (lay_odds[i] - 1)

            events.append(CascadeEventCalc(
                index=i,
                lay_stake=round(lay_stake_i, 2),
                liability=round(liability_i, 2),
                lay_odds=lay_odds[i],
                profit_if_won=round(P, 2),
                profit_if_all_lost=round(P, 2)
            ))

            # Aggiorna A per il prossimo evento
            if i < n - 1:
                A = A * (lay_odds[i] - commission) / (1 - commission)

        # --- Total liability ---
        total_liability = max(e.liability for e in events)

        # --- Warning arrotondamento ---
        profits = [e.profit_if_won for e in events]
        spread = max(profits) - min(profits)
        if spread > 0.50:
            warnings.append(f"Errore di arrotondamento cumulativo > 0.50 EUR: {spread:.2f}")

        # --- Profit per scenario ---
        profit_per_scenario = {}
        for i, e in enumerate(events):
            profit_per_scenario[f"event_{i+1}_won"] = e.profit_if_won
        profit_per_scenario["all_lost"] = events[-1].profit_if_all_lost

        calc = CascadeCalculation(
            events=events,
            profit_per_scenario=profit_per_scenario,
            total_liability=round(total_liability, 2),
            is_valid=True,
            warnings=warnings,
            back_stake=back_stake,
            back_odds=back_odds,
            lay_odds=lay_odds,
            commission=commission
        )

        return Result.ok(calc)

    def calculate_single(
        self,
        back_stake: float,
        back_odds: float,
        lay_odds: float,
        commission: float = 0.045
    ) -> Result[SingleCalculation]:
        # --- Validazione ---
        if back_odds <= 1.0 or lay_odds <= 1.0:
            return Result.fail("Quote <= 1.0", "INVALID_ODDS")
        if back_stake <= 0:
            return Result.fail("Back stake <= 0", "INVALID_ODDS")

        # --- Formula matched betting equal-profit ---
        # lay_stake = back_stake * back_odds / (lay_odds - commission)
        lay_stake = back_stake * back_odds / (lay_odds - commission)
        liability = lay_stake * (lay_odds - 1)

        # profitto se back vince (lay perde)
        profit_if_won = back_stake * (back_odds - 1) - liability
        # profitto se back perde (lay vince)
        profit_if_lost = -back_stake + lay_stake * (1 - commission)

        is_valid = True
        warnings = []
        if profit_if_won <= 0 or profit_if_lost <= 0:
            warnings.append(f"Profitto negativo (qualifying loss): won={profit_if_won:.2f}, lost={profit_if_lost:.2f}")

        calc = SingleCalculation(
            lay_stake=round(lay_stake, 2),
            liability=round(liability, 2),
            profit_if_won=round(profit_if_won, 2),
            profit_if_lost=round(profit_if_lost, 2),
            is_valid=is_valid,
            warnings=[]
        )

        return Result.ok(calc)

    def recalculate_from_index(
        self,
        back_stake: float,
        back_odds: list[float],
        lay_odds: list[float],
        commission: float,
        from_index: int
    ) -> Result[CascadeCalculation]:
        """Ricalcola la cascata dall'evento from_index in poi,
        usando le quote attuali. Gli eventi precedenti sono già conclusi."""
        if from_index < 0 or from_index >= len(back_odds):
            return Result.fail(f"from_index non valido: {from_index}", "INVALID_ODDS")

        # Stake virtuale = back_stake * quote eventi già conclusi
        cumulative_back = 1.0
        for j in range(from_index):
            cumulative_back *= back_odds[j]
        virtual_back_stake = back_stake * cumulative_back

        # Quote rimanenti
        remaining_back_odds = back_odds[from_index:]
        remaining_lay_odds = lay_odds[from_index:]

        result = self.calculate_cascade(
            virtual_back_stake,
            remaining_back_odds,
            remaining_lay_odds,
            commission
        )

        if not result.success:
            return Result.fail(
                f"Recalc mostra profitto negativo: {result.error}",
                "ODDS_NEGATIVE_PROFIT"
            )

        return result

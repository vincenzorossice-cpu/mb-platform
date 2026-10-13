import unittest
from orchestrator.config.book_navigation import BOOK_NAVIGATION


NETCAP_BOOKMAKERS = {
    "ADMIRALBET", "BET365", "BETFAIR", "BETFLAG", "BETPASSION", "BETPOINT",
    "BETSSON", "BETWIN360", "BGAME", "BWIN", "CODERE", "DAZNBET", "DOMUSBET",
    "E-PLAY24", "EUROBET", "FASTBET", "GIOCA7", "GIOCHI24", "GIOCO DIGITALE",
    "GOLDBET", "LEOVEGAS", "LOTTOMATICA", "MARATHONBET", "MYLOTTERIES",
    "NETBET", "NETWIN", "PLANET WIN", "POKERSTARS", "QUIGIOCO", "SISAL",
    "SNAI", "SPORTBET", "SPORTIUM", "STAKE", "STANLEYBET", "STARCASINO",
    "STARYES", "SUNBET", "TOTOSI", "UNIBET", "VINCITU", "WILLIAMHILL",
    "ZONAGIOCO",
}

REQUIRED_FIELDS = ("balance_label", "open_bets_path", "book_type")


class TestBookNavigation(unittest.TestCase):

    def test_all_books_have_required_fields(self):
        for name, cfg in BOOK_NAVIGATION.items():
            for field in REQUIRED_FIELDS:
                self.assertIn(field, cfg, f"{name} manca il campo '{field}'")

    def test_exchange_books_have_exchange_type(self):
        self.assertEqual(BOOK_NAVIGATION["BETFAIR"]["book_type"], "exchange")
        self.assertEqual(BOOK_NAVIGATION["BETFLAG"]["book_type"], "exchange")

    def test_book_names_match_netcap(self):
        self.assertEqual(set(BOOK_NAVIGATION.keys()), NETCAP_BOOKMAKERS)

    # LOGIN-MANAGER
    def test_login_url_field_present(self):
        for name, cfg in BOOK_NAVIGATION.items():
            self.assertIn("login_url", cfg, name)

    # LOGIN-MANAGER: i passaggi del login stanno nei recipe, non in BOOK_NAVIGATION
    def test_obsolete_login_fields_removed(self):
        for name, cfg in BOOK_NAVIGATION.items():
            for field in ("needs_manual_login", "login_form_labels", "logged_in_indicators"):
                self.assertNotIn(field, cfg, f"{name} ha ancora '{field}'")


if __name__ == "__main__":
    unittest.main()

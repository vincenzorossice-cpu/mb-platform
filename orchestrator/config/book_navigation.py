"""Configurazione per-book per la navigazione ScreenReader.

Per ogni bookmaker sono mappati:
- balance_url: URL della pagina dove si vede il saldo (web mobile)
- balance_label: etichetta usata per riconoscere il saldo nella pagina
- open_bets_path: percorso di navigazione per le scommesse aperte (dalla pagina del saldo)
- open_bets_url: URL diretto delle scommesse aperte, se noto
- open_bets_label: etichetta per riconoscere le scommesse aperte
- book_type: "bookmaker" | "exchange"
- needs_login: True se richiede login per vedere il saldo (quasi tutti)
- notes: note operative specifiche del book

I percorsi di open_bets_path sono pre-popolati dalla guida
P6-RICONCILIAZIONE-STORICO-SCOMMESSE-BOOK (MB BRAIN), normalizzando i nomi
book della guida sui nomi bookmaker usati da Netcap. I balance_url sono
ripresi da BookNavigator.BOOK_CONFIG (orchestrator/modules/book_navigator/
navigator.py) dove già noti e verificati.

I campi lasciati a None/"" con commento TODO vanno validati sui telefoni.
"""

BOOK_NAVIGATION = {
    # --- Book mappati dalla guida P6 (open_bets_path noto) ---
    "BETFAIR": {
        "balance_url": "https://www.betfair.it/exchange/plus/",
        "balance_label": "Totale saldo reale",
        "open_bets_path": "Conto Personale → Conto Personale → Attività Scommesse → Scommesse Sportsbook / Scommesse Exchange",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Responsabilità",
        "book_type": "exchange",
        "needs_login": True,
        "notes": (
            "Wallet unico sport + exchange. Distingue Sportsbook ed Exchange. "
            "Filtri exchange: non abbinate = Aperte + Non Abbinate; abbinate non "
            "refertate = Aperte + Abbinate; refertate = Chiuse; rimborsate/"
            "annullate = Chiuse → Opzioni → Stato Annullate/Nulle."
        ),
    },
    "SISAL": {
        "balance_url": "https://m.sisal.it",
        "balance_label": "Saldo",
        "open_bets_path": "Icona in alto a destra → scorrere fino a Ultimi Movimenti",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Ultimi Movimenti",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "WILLIAMHILL": {
        "balance_url": "https://m.williamhill.it",
        "balance_label": "Saldo",
        "open_bets_path": "Il Mio Conto → Estratto Conto → filtri di ricerca",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Estratto Conto",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "LEOVEGAS": {
        "balance_url": "https://m.leovegas.it",
        "balance_label": "Saldo",
        "open_bets_path": "Scommesse → Storico Scommesse",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Storico Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "EUROBET": {
        "balance_url": "https://m.eurobet.it",
        "balance_label": "Saldo",
        "open_bets_path": "Le Mie Scommesse",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Mie Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "UNIBET": {
        "balance_url": "https://m.unibet.it",
        "balance_label": "Saldo",
        "open_bets_path": "Scommesse → Storico Scommesse",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Storico Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BET365": {
        "balance_url": "https://www.bet365.it",
        "balance_label": "Balance",
        "open_bets_path": "Icona in alto a destra → Archivio → Scommesse in corso / concluse",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse in corso",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "GIOCO DIGITALE": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "Il Mio Conto → Le Mie Scommesse",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Mie Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "STANLEYBET": {
        "balance_url": "https://m.stanleybet.it",
        "balance_label": "Saldo",
        "open_bets_path": "Profilo → Le Mie Giocate → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Mie Giocate",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "POKERSTARS": {
        "balance_url": "https://m.pokerstars.it",
        "balance_label": "Saldo",
        "open_bets_path": "Account → Scommesse → Cronologia Scommesse → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Cronologia Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BETFLAG": {
        "balance_url": "https://m.betflag.it",
        "balance_label": "Saldo",
        "open_bets_path": "I Miei Movimenti → Scommesse Sportive / Exchange",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse Sportive / Exchange",
        "book_type": "exchange",
        "needs_login": True,
        "notes": (
            "Distingue la sezione Sportbook dalla sezione Exchange. Il calcolo "
            "dell'importo vinto da una bancata non è leggibile direttamente "
            "dall'estratto conto (vedi P5-FONDAMENTI-BETFLAG-BANCARE)."
        ),
    },
    "TOTOSI": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "Movimenti e Giocate → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Movimenti e Giocate",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "NETBET": {
        "balance_url": "https://m.netbet.it",
        "balance_label": "Saldo",
        "open_bets_path": "Nome utente → Le Tue Scommesse → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Tue Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "ADMIRALBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "Nickname in alto → Giocate → Scommesse → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Giocate",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "GOLDBET": {
        "balance_url": "https://m.goldbet.it",
        "balance_label": "Saldo",
        "open_bets_path": "Il Mio Conto → Le Mie Scommesse → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Mie Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "DOMUSBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "Icona in alto a destra → Scommesse Sportive → Le Mie Giocate",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Le Mie Giocate",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "SNAI": {
        "balance_url": "https://m.snai.it",
        "balance_label": "Saldo",
        "open_bets_path": "Il Tuo Conto → Movimenti e Saldo → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Movimenti e Saldo",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "PLANET WIN": {
        "balance_url": "https://m.planetwin365.it",
        "balance_label": "Saldo",
        "open_bets_path": "Il Mio Conto → Lista Scommesse → filtri",
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Lista Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    # --- Book non mappati nella guida P6 (percorso da verificare) ---
    "BETPASSION": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BETPOINT": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BETSSON": {
        "balance_url": "https://m.betsson.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BETWIN360": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BGAME": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "BWIN": {
        "balance_url": "https://m.bwin.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "CODERE": {
        "balance_url": "https://m.codere.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "DAZNBET": {
        "balance_url": "https://m.daznbet.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "E-PLAY24": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "FASTBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "GIOCA7": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "GIOCHI24": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "LOTTOMATICA": {
        "balance_url": "https://m.lottomatica.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "MARATHONBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "MYLOTTERIES": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "NETWIN": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "QUIGIOCO": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "SPORTBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "SPORTIUM": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "STAKE": {
        "balance_url": "https://m.stake.it",
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "STARCASINO": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "STARYES": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "SUNBET": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "VINCITU": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
    "ZONAGIOCO": {
        "balance_url": None,  # TODO: verificare URL
        "balance_label": "Saldo",
        "open_bets_path": "",  # TODO: verificare percorso
        "open_bets_url": None,  # TODO: verificare URL
        "open_bets_label": "Scommesse",
        "book_type": "bookmaker",
        "needs_login": True,
        "notes": "",
    },
}

# Book citati nella guida P6-RICONCILIAZIONE-STORICO-SCOMMESSE-BOOK che non
# hanno un nome corrispondente nell'elenco bookmaker Netcap (probabilmente
# dismessi/non più gestiti). Percorso riportato per riferimento futuro, nel
# caso rientrino tra i book Netcap:
#
# - Better: Area Personale → Movimenti e Giocate → Filtro di ricerca Movimenti (in basso a sinistra)
# - Sportpesa: Icona in alto a destra → Scommesse Sportive → Le Mie Giocate
# - Betaland: Movimenti → Mie Scommesse → filtri
# - Betclic: Le Mie Giocate (in alto a destra) → filtri
# - 888sport: Scommesse → Storico Scommesse
# - Scommettendo: Altro → Schedine → filtri
# - Chancebet: Icona in alto a destra → Scommesse Sportive → Le Mie Giocate
# - Betman: Icona in alto a destra → Scommesse Sportive → Le Mie Giocate
# - Pinterbet: Profilo → Le Mie Giocate → filtri
# - MerkurWin: Icona in alto a destra → Il Mio Conto → Saldo e Movimenti
# - Starvegas: Sezione Scommesse → menù laterale sinistro → Le Mie Scommesse

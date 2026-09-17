from orchestrator.core.config import load_config
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.app_api_client.client import AppApiClient
from orchestrator.modules.rules_engine.engine import RulesEngine
from orchestrator.modules.rules_engine.interface import Account
import os
from datetime import date

config = load_config("orchestrator/config/config.json")
reader = ScreenReader()
token = os.environ.get("NETCAP_SESSION_TOKEN", "")
client = AppApiClient(base_url="http://37.221.197.184:3001/api", session_token=token)
engine = RulesEngine()

today = date.today().isoformat()
print("=== SCRITTURA RETTIFICA SU NETCAP (LIVE - NUOVO FLUSSO) ===")
print("ID 08 (Vincenzo Carfora) / BETFAIR")
print("Data: " + today)
print("")

# 1. Leggi saldo reale
print("1. Leggo saldo reale dal telefono...")
real = reader.read_balance("ZT322ZS58S", "BETFAIR")
if not real.success:
    print("   ERRORE: " + str(real.error))
    exit()
print("   Saldo reale: " + str(real.data.raw_balance) + " EUR")

# 2. Leggi saldo atteso da Netcap
print("2. Leggo saldo atteso da Netcap...")
expected = client.read_expected_balance("08")
if not expected.success:
    print("   ERRORE: " + str(expected.error))
    exit()

betfair_exp = None
for b in expected.data:
    if b.is_exchange:
        betfair_exp = b.saldo_atteso
        break

if betfair_exp is None:
    print("   ERRORE: BETFAIR non trovato per ID 08")
    exit()
print("   Saldo atteso: " + str(betfair_exp) + " EUR")

# 3. Riconcilia
print("3. Riconcilio...")
account = Account(account_id="08", identity_id="08", book="BETFAIR", is_betfair=True)
result = engine.reconcile(real.data, betfair_exp, account)

if not result.success:
    print("   ERRORE: " + str(result.error))
    exit()

r = result.data
print("   Differenza: " + str(r.difference) + " EUR")
print("   Colonna: " + str(r.column))
print("   Action: " + str(r.action))

if r.action != "adjust" or not r.rectification:
    print("   Nessuna rettifica necessaria.")
    exit()

print("   Rettifica: " + str(r.rectification.amount) + " EUR in colonna " + str(r.rectification.column))
print("")

# 4. Crea operazione Riconciliazione
print("4. Creo operazione Riconciliazione su Netcap...")
op_result = client.create_riconciliazione_operazione(today)
if not op_result.success:
    print("   ERRORE: " + str(op_result.error))
    exit()
operazione_id = op_result.data
print("   Operazione creata: ID " + str(operazione_id))

# 5. Aggiungi riga di rettifica
print("5. Aggiungo riga di rettifica...")
riga_result = client.add_riga_rettifica(
    operazione_id=operazione_id,
    collaboratore_id="08",
    bookmaker_id=6,
    amount=r.rectification.amount,
    column=r.rectification.column,
    reason=r.rectification.reason,
    bonus_note=r.bonus_note
)
if not riga_result.success:
    print("   ERRORE: " + str(riga_result.error))
    exit()
riga_id = riga_result.data
print("   Riga creata: ID " + str(riga_id))

# 6. Chiudi riga
print("6. Chiudo la riga...")
close_riga_result = client.close_riga(riga_id)
if close_riga_result.success:
    print("   Riga chiusa")
else:
    print("   ERRORE chiusura riga: " + str(close_riga_result.error))

# 7. Chiudi operazione
print("7. Chiudo l'operazione...")
close_op_result = client.close_operazione(operazione_id)
if close_op_result.success:
    print("   Operazione chiusa")
else:
    print("   ERRORE chiusura operazione: " + str(close_op_result.error))

# 8. Verifica saldo
print("8. Verifico saldo su Netcap...")
expected_after = real.data.raw_balance
verify_result = client.verify_balance_after_rettifica("08", 6, expected_after)
if verify_result.success:
    print("   Saldo Netcap coincide con saldo reale: " + str(verify_result.data))
else:
    print("   Verifica: " + str(verify_result.error))

print("")
print("=== FATTO! Controlla su Netcap ===")
print("Operazione ID: " + str(operazione_id))
print("Riga ID: " + str(riga_id))

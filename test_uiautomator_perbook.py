"""
Test uiautomator per-book per Raffaele.
Esegue: per ogni app di bookmaker installata sul telefono, prova uiautomator dump.
Se bloccato, prova fallback OCR (PaddleOCR).
Stampa una tabella riassuntiva.

USO:
    python test_uiautomator_perbook.py

PREREQUISITI:
    - ADB installato e nel PATH
    - Telefono Android collegato via USB o WiFi ADB
    - App bookmaker installate sul telefono
    - (Opzionale) PaddleOCR installato per testare il fallback OCR
"""

import subprocess
import sys
import time

# Lista app bookmaker da testare (nome, package)
# Aggiornare i package name in base alle app installate
BOOKS = [
    ("Stanleybet", "com.stanleybet.app"),
    ("Sisal", "it.sisal.matchpoint"),
    ("SNAI", "com.snai.app"),
    ("Eurobet", "com.eurobet.client"),
    ("Goldbet", "it.goldbet.app"),
    ("Lottomatica", "it.lottomatica.app"),
    ("Planetwin365", "com.planetwin365.app"),
    ("Betflag", "it.betflag.app"),
    ("Bet365", "com.bet365.android"),
    ("Pinnacle", "com.pinnacle.android"),
]


def check_adb():
    """Verifica che ADB sia installato e che ci sia almeno un device connesso."""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        devices = [l.split("\t")[0] for l in lines[1:] if "\tdevice" in l]
        return devices
    except FileNotFoundError:
        print("ERRORE: ADB non trovato. Installa Android Platform Tools.")
        sys.exit(1)
    except Exception as e:
        print(f"ERRORE ADB: {e}")
        sys.exit(1)


def test_uiautomator(device_id, package_name):
    """Testa uiautomator dump su un'app specifica."""
    # Apri l'app
    try:
        subprocess.run(
            ["adb", "-s", device_id, "shell", "monkey", "-p", package_name, "-c",
             "android.intent.category.LAUNCHER", "1"],
            capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        return "APP_NOT_FOUND", 0
    except Exception as e:
        return f"ERROR: {e}", 0

    time.sleep(3)  # Attendi che l'app si apra

    # Prova uiautomator dump
    try:
        result = subprocess.run(
            ["adb", "-s", device_id, "shell", "uiautomator", "dump", "--compressed", "/dev/tty"],
            capture_output=True, text=True, timeout=10
        )

        xml = result.stdout.strip()
        if not xml or "<hierarchy" not in xml.lower():
            return "BLOCKED", 0

        # Conta elementi testo
        count = xml.count("text=\"")
        non_empty = sum(1 for s in xml.split('text="') if len(s) > 1 and s[0] != '"')
        return "OK", non_empty

    except subprocess.TimeoutExpired:
        return "TIMEOUT", 0
    except Exception as e:
        return f"ERROR: {e}", 0


def test_ocr_fallback(device_id):
    """Testa il fallback OCR su un'app bloccata."""
    try:
        from orchestrator.modules.screen_reader.reader import ScreenReader
        reader = ScreenReader(ocr_confidence_threshold=0.0)  # Non bloccare per confidence
        result = reader._ocr_fallback(device_id)
        if result.success:
            return result.data.confidence
        else:
            return None
    except ImportError:
        return None  # PaddleOCR non installato
    except Exception:
        return None


def main():
    print("=" * 60)
    print("TEST UIAUTOMATOR PER-BOOK")
    print("=" * 60)

    devices = check_adb()
    if not devices:
        print("Nessun dispositivo Android connesso.")
        print("Collega un telefono via USB e assicurati che il debug USB sia attivo.")
        sys.exit(1)

    print(f"\nDispositivi connessi: {len(devices)}")
    for d in devices:
        print(f"  - {d}")

    device_id = devices[0]  # Usa il primo device
    print(f"\nUso device: {device_id}")
    print(f"Test su {len(BOOKS)} app bookmaker...\n")

    results = []
    for book_name, package in BOOKS:
        print(f"[{book_name}] Test in corso...", end=" ", flush=True)
        status, elem_count = test_uiautomator(device_id, package)

        ocr_conf = None
        if status == "BLOCKED":
            print("BLOCKED → provo OCR...", end=" ", flush=True)
            ocr_conf = test_ocr_fallback(device_id)

        results.append({
            "book": book_name,
            "status": status,
            "elements": elem_count,
            "ocr_confidence": ocr_conf,
        })
        print(f"{status}" + (f" ({elem_count} elem)" if status == "OK" else ""))
        if ocr_conf is not None:
            print(f"  → OCR fallback confidence: {ocr_conf:.2f}")
        elif status == "BLOCKED":
            print("  → OCR non disponibile (PaddleOCR non installato)")

    # Tabella riassuntiva
    print("\n" + "=" * 60)
    print("RIASSUNTO")
    print("=" * 60)
    print(f"{'Book':<20} {'uiautomator':<15} {'Elementi':<10} {'OCR conf':<10}")
    print("-" * 60)
    for r in results:
        ocr_str = f"{r['ocr_confidence']:.2f}" if r['ocr_confidence'] is not None else "N/A"
        print(f"{r['book']:<20} {r['status']:<15} {r['elements']:<10} {ocr_str:<10}")

    # Salva risultati su file
    with open("test_uiautomator_results.txt", "w") as f:
        f.write("TEST UIAUTOMATOR PER-BOOK\n")
        f.write(f"Device: {device_id}\n")
        f.write(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"{'Book':<20} {'uiautomator':<15} {'Elementi':<10} {'OCR conf':<10}\n")
        f.write("-" * 60 + "\n")
        for r in results:
            ocr_str = f"{r['ocr_confidence']:.2f}" if r['ocr_confidence'] is not None else "N/A"
            f.write(f"{r['book']:<20} {r['status']:<15} {r['elements']:<10} {ocr_str:<10}\n")

    print(f"\nRisultati salvati in: test_uiautomator_results.txt")
    print("Manda questo file a Vincenzo.")


if __name__ == "__main__":
    main()

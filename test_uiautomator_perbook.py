import subprocess
import sys
import time
import os
import tempfile

BOOKS = [
    ("Stanleybet", "https://m.stanleybet.it"),
    ("Sisal", "https://m.sisal.it"),
    ("SNAI", "https://m.snai.it"),
    ("Eurobet", "https://m.eurobet.it"),
    ("Goldbet", "https://m.goldbet.it"),
    ("Lottomatica", "https://m.lottomatica.it"),
    ("Planetwin365", "https://m.planetwin365.it"),
    ("Betflag", "https://m.betflag.it"),
    ("Bet365", "https://www.bet365.it/#/HO/"),
    ("Betfair", "https://mobile.betfair.com"),
]

def check_adb():
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split("\n")
        devices = [l.split("\t")[0] for l in lines[1:] if "\tdevice" in l]
        return devices
    except Exception as e:
        print("ERRORE ADB: " + str(e))
        sys.exit(1)

def open_chrome(device_id, url):
    try:
        subprocess.run(
            ["adb", "-s", device_id, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass
    time.sleep(5)

def test_uiautomator(device_id):
    try:
        # Metodo corretto: dump su file, poi cat
        subprocess.run(
            ["adb", "-s", device_id, "shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"],
            capture_output=True, text=True, timeout=10
        )
        result = subprocess.run(
            ["adb", "-s", device_id, "shell", "cat", "/sdcard/ui_dump.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml = result.stdout.strip()
        if not xml or "<hierarchy" not in xml.lower():
            return "BLOCKED", 0
        non_empty = sum(1 for s in xml.split('text="') if len(s) > 1 and s[0] != '"')
        return "OK", non_empty
    except subprocess.TimeoutExpired:
        return "TIMEOUT", 0
    except Exception:
        return "ERROR", 0

def test_ocr_fallback(device_id):
    try:
        sys.path.insert(0, ".")
        from orchestrator.modules.screen_reader.reader import ScreenReader
        reader = ScreenReader(ocr_confidence_threshold=0.0)
        result = reader._ocr_fallback(device_id)
        if result.success:
            return result.data.confidence
        return None
    except ImportError:
        return None
    except Exception:
        return None

def main():
    print("=" * 60)
    print("TEST UIAUTOMATOR PER-BOOK (CHROME WEB MOBILE)")
    print("=" * 60)

    devices = check_adb()
    if not devices:
        print("Nessun dispositivo Android connesso.")
        sys.exit(1)

    print("\nDispositivi connessi: " + str(len(devices)))
    for d in devices:
        print("  - " + d)

    device_id = devices[0]
    print("\nUso device: " + device_id)
    print("Test su " + str(len(BOOKS)) + " siti bookmaker su Chrome...\n")

    results = []
    for book_name, url in BOOKS:
        print("[" + book_name + "] Apro " + url + "...", end=" ", flush=True)
        open_chrome(device_id, url)
        print("Test uiautomator...", end=" ", flush=True)
        status, elem_count = test_uiautomator(device_id)

        ocr_conf = None
        if status == "BLOCKED":
            print("BLOCKED -> provo OCR...", end=" ", flush=True)
            ocr_conf = test_ocr_fallback(device_id)

        results.append((book_name, url, status, elem_count, ocr_conf))
        print(status + (" (" + str(elem_count) + " elem)" if status == "OK" else ""))
        if ocr_conf is not None:
            print("  -> OCR fallback confidence: " + str(round(ocr_conf, 2)))
        elif status == "BLOCKED":
            print("  -> OCR non disponibile")

    print("\n" + "=" * 60)
    print("RIASSUNTO")
    print("=" * 60)
    print("Book                uiautomator     Elementi   OCR conf")
    print("-" * 60)
    for r in results:
        book = r[0].ljust(20)
        st = r[2].ljust(15)
        elem = str(r[3]).ljust(10)
        ocr = str(round(r[4], 2)) if r[4] is not None else "N/A"
        print(book + st + elem + ocr)

    with open("test_uiautomator_results.txt", "w") as f:
        f.write("TEST UIAUTOMATOR PER-BOOK (CHROME)\n")
        f.write("Device: " + device_id + "\n")
        f.write("Data: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n")
        f.write("Book                uiautomator     Elementi   OCR conf\n")
        f.write("-" * 60 + "\n")
        for r in results:
            book = r[0].ljust(20)
            st = r[2].ljust(15)
            elem = str(r[3]).ljust(10)
            ocr = str(round(r[4], 2)) if r[4] is not None else "N/A"
            f.write(book + st + elem + ocr + "\n")

    print("\nRisultati salvati in: test_uiautomator_results.txt")
    print("Manda questo file a Vincenzo.")

if __name__ == "__main__":
    main()

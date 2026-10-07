import cv2
import os
import json
import time
from datetime import timedelta
import easyocr

# Configurazione
VIDEO_DIR = os.path.expanduser("~/Downloads/REGISTRAZIONE SCHERMO RAFF")
OUTPUT_DIR = os.path.expanduser("~/Desktop/MB PROJECT/mb-platform/analysis_results")
DIFF_THRESHOLD = 500000  # pixel cambiati per considerare un frame "significativo"
FRAME_INTERVAL = 15  # controlla 1 frame ogni 15 (a 30fps = 2 controlli/sec)

# Inizializza EasyOCR (solo una volta)
print("Caricamento EasyOCR (puo metterci 1 minuto)...")
reader = easyocr.Reader(['it', 'en'], gpu=False)
print("EasyOCR pronto!\n")

def analyze_video(video_path, reader):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0
    
    print(f"Analizzo: {os.path.basename(video_path)}")
    print(f"  Durata: {timedelta(seconds=int(duration))}")
    
    prev_frame = None
    keyframes = []
    frame_count = 0
    saved_count = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        frame_count += 1
        
        if frame_count % FRAME_INTERVAL != 0:
            continue
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        if prev_frame is not None:
            diff = cv2.absdiff(gray, prev_frame)
            changed_pixels = int((diff > 30).sum())
            
            if changed_pixels > DIFF_THRESHOLD:
                timestamp = frame_count / fps
                # OCR su questo frame
                results = reader.readtext(frame)
                texts = [text for bbox, text, conf in results if conf > 0.5 and text.strip()]
                all_text = " ".join(texts).upper()
                
                # Identifica fase
                phase = "unknown"
                if any(kw in all_text for kw in ["PROMOZIONI", "ADERISCI", "ATTIVA"]):
                    phase = "navigazione_promozioni"
                elif "PROMOZIONE ATTIVATA" in all_text:
                    phase = "attivazione_completata"
                elif any(kw in all_text for kw in ["PROVA", "FOR FUN", "DEMO"]):
                    phase = "apertura_slot_demo"
                elif any(kw in all_text for kw in ["SALDO", "PUNTATA", "POSTA", "SPIN", "VINCITA"]):
                    phase = "gioco_slot"
                
                keyframes.append({
                    "timestamp_sec": timestamp,
                    "timestamp_str": str(timedelta(seconds=int(timestamp))),
                    "phase": phase,
                    "texts": texts[:5]
                })
                saved_count += 1
        
        prev_frame = gray
    
    cap.release()
    return keyframes

# Trova tutti i video
video_files = []
for root, dirs, files in os.walk(VIDEO_DIR):
    for file in files:
        if file.endswith('.mp4'):
            video_files.append(os.path.join(root, file))

print(f"Trovati {len(video_files)} video da analizzare\n")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Analizza ogni video
all_results = {}
for video_path in video_files:
    video_name = os.path.basename(video_path)
    keyframes = analyze_video(video_path, reader)
    all_results[video_name] = keyframes
    print(f"  Estratti {len(keyframes)} frame significativi\n")

# Salva risultato
output_file = os.path.join(OUTPUT_DIR, "timeline_raffaele.json")
with open(output_file, "w") as f:
    json.dump(all_results, f, indent=2)

print(f"\n=== ANALISI COMPLETATA ===")
print(f"Risultati salvati in: {output_file}")
print(f"Totale video analizzati: {len(video_files)}")

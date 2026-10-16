import os
import sys
import whisper

folder = sys.argv[1] if len(sys.argv) > 1 else "."
model = whisper.load_model("base")

audio_exts = (".m4a", ".mp3", ".wav", ".ogg", ".opus", ".webm")
audio_files = [f for f in os.listdir(folder) if f.lower().endswith(audio_exts)]

if not audio_files:
    print("Nessun file audio trovato nella cartella.")
    sys.exit(1)

all_transcripts = []

for audio_file in sorted(audio_files):
    audio_path = os.path.join(folder, audio_file)
    print(f"Trascrivo: {audio_file}...")
    
    result = model.transcribe(audio_path, language="it")
    transcript = result["text"].strip()
    
    all_transcripts.append(f"=== {audio_file} ===\n{transcript}\n")
    print(f"  Fatto: {transcript[:80]}...")

output_path = os.path.join(folder, "trascrizioni_complete.txt")
with open(output_path, "w", encoding="utf-8") as f:
    f.write("\n".join(all_transcripts))

print(f"\nTutte le trascrizioni salvate in: {output_path}")

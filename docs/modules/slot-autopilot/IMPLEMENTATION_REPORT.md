# SlotAutoPilot — Report di implementazione

Modulo deterministico che abbassa la puntata al minimo vero e esegue spin
verificati, con GPT-4o-mini Vision e HITL solo come fallback. Implementato
in 12 commit (Commit 1-12), come da piano; questo e' il Commit 13.

## Implementato

- **`orchestrator/modules/slot_ocr/`**: `SlotOCRReader` spostata da
  `casino_flow.py` (re-export lasciato li' per compatibilita'), piu' il
  nuovo metodo additivo `read_text_boxes()` (bbox completo + confidence).
- **`orchestrator/modules/provider_profile/`**: `ElementSpec`/`StateSpec`/
  `MacroSpec`/`ProviderProfile` (dataclass), `ProviderProfileLoader`
  (load/validate/to_abs/to_norm), profilo di esempio non verificato
  (`provider_profiles/_example.json`), e `autopilot_memory.py` (memoria
  generica per elementi che lo schema fisso di `slot_memory/<slot>.json`
  non supporta — vedi sotto).
- **`orchestrator/modules/step_logger/`**: `StepRecord`/`EpisodeSummary`,
  `JSONLStepLogger` (un file `.jsonl` append-only per episodio + un
  `.summary.json` finale). `dataset/` aggiunto a `.gitignore`.
- **`orchestrator/modules/slot_autopilot/`**: `config.py`
  (`AutoPilotConfig`), `value_parser.py` (`parse_amount`, gestisce formati
  valuta e confusioni OCR), `frame_diff.py` (`diff_score`,
  `wait_until_stable`, solo PIL), `verifier.py` (`ActionVerifier`,
  `classify_screen_state`), 7 `LocatorStrategy` in `strategies/` (Profile,
  Memory, OCRAnchor, SpatialHeuristic, Template, GPTLocator, HITL) +
  `locator.py` (`ElementLocator`, catena ordinata con contatori di
  fallimento per coppia strategia/elemento), `bet_controller.py`
  (`BetController.set_min_bet`), `spin_controller.py`
  (`SpinController.spin`, 3 invarianti di sicurezza), `autopilot.py`
  (facciata `SlotAutoPilot`: prepare/set_min_bet/spin/shutdown).
- **`orchestrator/scripts/run_autopilot.py`**: CLI (`--device --slot
  --target --max-steps --dry-run/--live --no-gpt --no-hitl`), unico punto
  del progetto che istanzia le classi concrete (composition root).
- **Test**: 148 nuovi test unitari (pytest puro, `MagicMock`+`patch`,
  nessuna chiamata ADB/OpenAI reale), incluso `FakeSlotDevice`
  (`orchestrator/tests/unit/_fake_slot_device.py`) — l'unico test-double
  stateful della suite — usato per gli end-to-end di `BetController`,
  `SpinController` e `SlotAutoPilot`. Un test `@pytest.mark.adb` reale,
  saltato di default (si attiva solo con la variabile d'ambiente
  `SLOT_AUTOPILOT_ADB_DEVICE`). Suite completa: **695 test verdi + 1
  saltato** (compresi tutti quelli preesistenti — nessuna regressione).

## Adattato rispetto alla spec originale (perche' il codice reale non combaciava)

1. **Screenshot come path, non array**: `ScreenReader._screenshot()` (privato
   ma gia' usato cosi' da `test_slot_autonomous.py`) ritorna un path PNG
   locale, non PIL/numpy. `frame_diff.py`/`verifier.py` lavorano su path,
   solo PIL — niente numpy aggiunto per questo.
2. **Coordinata "richiesta" vs "tappata"**: `TouchInjector.tap_coordinates`
   non restituisce mai la coordinata con jitter (±5px, interno e scartato).
   Il dataset registra sempre la coordinata richiesta (`requested_coords`
   in `StepRecord`, `tapped_coords` in `TapOutcome`).
3. **Memoria per elementi generici**: `slot_memory/<slot>.json` ha uno
   schema chiuso a 4 chiavi, gestito da funzioni in
   `test_slot_autonomous.py` che **non preservano chiavi extra** (le
   riscrivono da zero). Riusate quelle funzioni inalterate solo per
   `"spin"`/`"bet_menu_toggle"`; ogni altro elemento (es. `bet_minus`) usa
   un file satellite separato `slot_memory/<slot>.autopilot.json`, mai in
   conflitto.
4. **opencv-python-headless** aggiunto a `orchestrator/requirements.txt`
   solo per `TemplateStrategy`, import lazy (come `SlotOCRReader` fa con
   `easyocr`) — nessun costo per chi non lo usa mai.
5. **Simboli riusati da `test_slot_autonomous.py`** (importati, mai
   modificati): `resolve_target`, `parse_operator_description`,
   `ask_human_help`, `handle_hitl_escalation`, `load_slot_memory`,
   `save_slot_memory`, `_persist_slot_memory`, `_slot_memory_path`. Altri
   simboli minori (`region_to_point`, `_find_text_coords`,
   `_REGION_FRACTIONS`) sono stati **reimplementati localmente** (poche
   righe ciascuno) dentro le strategie che ne avevano bisogno, per non
   dipendere da piu' simboli "privati" di quel file di quanto
   esplicitamente concordato.

## Lasciato aperto

1. **Falso minimo se il profilo punta a una zona morta**: `ProfileStrategy`
   e' sempre la prima scelta e non "fallisce nel localizzare" se le
   coordinate sono semplicemente sbagliate — localizzare e avere-effetto
   sono passi distinti per design, quindi la catena non scende mai a
   Memory/OCR/GPT/HITL in quel caso. Dimostrato da un test end-to-end che
   passa (`test_dead_zone_profile_coords_produce_false_floor`), non
   nascosto.
2. **`classify_screen_state` e' una semplificazione a 2 soli valori**
   (`idle`/`unsafe`, basata solo sulla presenza del bottone spin via OCR),
   contro i 10 valori di `screen_state` disponibili via GPT Vision. E' la
   base di sicurezza usata da `SpinController` anche quando GPT e'
   disabilitato.
3. **HITL bypassa `ActionVerifier`**: `handle_hitl_escalation` tappa e
   verifica gia' da sola, quindi un candidato HITL arriva con
   `verified=True` senza passare da `ActionVerifier.verified_tap`. Una
   futura modifica alla semantica di quella funzione richiede una revisione
   qui.
4. **Template matching mai validato su un dispositivo reale**: solo
   testato contro un `cv2` mockato; serve una validazione con icone vere
   prima dell'uso in produzione.
5. **`MacroSpec` definito ma non eseguito**: `ProviderProfile.macros' e
   validato dal loader ma nessun controller lo consuma ancora — nessun
   motore di esecuzione macro in questo giro (prossimo incremento
   naturale).

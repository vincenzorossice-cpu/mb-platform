from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ElementSpec:
    """Descrive come localizzare un elemento UI di una slot. Nessun campo e'
    obbligatorio da solo: la validazione del loader impone che almeno uno tra
    (norm_x+norm_y) / ocr_labels / template_path / region_hint sia presente."""
    name: str
    norm_x: Optional[float] = None
    norm_y: Optional[float] = None
    ocr_labels: list = field(default_factory=list)
    template_path: Optional[str] = None
    region_hint: Optional[str] = None  # una delle 9 celle della griglia 3x3
    notes: Optional[str] = None


@dataclass
class StateSpec:
    """Descrive un possibile stato dello schermo (es. 'modal_bet_menu') in
    termini di segnali OCR attesi, per un futuro riconoscimento di stato senza GPT."""
    name: str
    ocr_signature: list = field(default_factory=list)
    diff_score_max: Optional[float] = None


@dataclass
class MacroSpec:
    """Una sequenza ordinata di nomi di elementi da toccare in fila (es. 'apri
    menu puntata -> seleziona minimo -> chiudi menu'). Non ancora eseguita da
    nessun controller in questo giro (vedi report finale)."""
    name: str
    steps: list = field(default_factory=list)  # nomi di ElementSpec, in ordine


@dataclass
class ProviderProfile:
    """Configurazione curata e verificata per una specifica slot/provider:
    dove sono i controlli, come riconoscere gli stati, eventuali macro. E'
    la fonte di verita' con priorita' piu' alta nella catena del locator, ma
    known_min_bet resta solo un SUGGERIMENTO: BetController lo riverifica
    sempre dal vivo, non lo usa mai a occhi chiusi."""
    slot_name: str
    provider: str
    screen_width: int
    screen_height: int
    elements: dict = field(default_factory=dict)   # name -> ElementSpec
    states: dict = field(default_factory=dict)      # name -> StateSpec
    macros: dict = field(default_factory=dict)      # name -> MacroSpec
    known_min_bet: Optional[float] = None
    verified: bool = False
    # (x1, y1, x2, y2) in pixel assoluti dello schermo intero: l'area di gioco
    # vera, esclusa la barra del browser. None = nessun canvas dichiarato,
    # le coordinate normalizzate sono relative allo schermo intero (comportamento
    # storico, retrocompatibile).
    canvas_bbox: Optional[tuple] = None
    # Profilo generato dal sistema (non curato a mano) da una sessione reale.
    # Finche' verified=False non e' fidato: ProfileStrategy lo ignora e
    # auto_sessions_ok conta le sessioni consecutive "pulite" (nessun aiuto
    # di GPT/HITL, nessun tap a vuoto) necessarie per promuoverlo.
    auto_generated: bool = False
    auto_sessions_ok: int = 0

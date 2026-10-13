"""Funzioni pure per leggere una ScreenContent: matching a parola intera e geometria label -> EditText.

Condivise da LoginExecutor e LoginMapper. Limite noto di ScreenReader: scarta i nodi
senza text e senza resource-id, quindi un EditText vuoto e anonimo non e' visibile.
"""
import re
from typing import NamedTuple, Optional

from orchestrator.modules.login_manager.recipe import DEFAULT_LOGGED_IN_INDICATORS, LOGIN_BUTTON_INDICATORS
from orchestrator.modules.screen_reader.interface import ScreenContent, UIElement

EDIT_TEXT_CLASS = "android.widget.EditText"

# --- CAPTCHA ---------------------------------------------------------------------------------------------------
# Il solo testo "reCAPTCHA"/"captcha" NON basta: compare nel badge informativo ("This site is protected by
# reCAPTCHA"), nei link alla privacy policy e nei testi SEO. Un CAPTCHA e' attivo solo con un elemento strutturale.
_CAPTCHA_CHECKBOX_TEXTS = ("Non sono un robot", "I'm not a robot", "I am not a robot")
_CAPTCHA_CHALLENGE_TEXTS = ("Seleziona tutte le immagini", "Seleziona tutti i riquadri", "Seleziona tutti i quadrati",
                            "Select all images", "Select all squares")
_RECAPTCHA_URL_PARTS = ("google.com/recaptcha", "recaptcha/api2", "recaptcha/enterprise")
_BADGE_PARTS = ("badge", "invisible")                        # frame/id del badge, non di una sfida
_HIDDEN_FIELD_PARTS = ("badge", "invisible", "response", "token")   # g-recaptcha-response e simili
MIN_CAPTCHA_IMAGE_SIDE = 150


class CaptchaFinding(NamedTuple):
    kind: str      # "reCAPTCHA" | "captcha"
    reason: str    # perche' e' stato riconosciuto (per il log / il messaggio all'operatore)


def word_in_text(word: str, text: str) -> bool:
    """Parola intera: "PIN" non matcha in "Wrappin'", "Esci" non matcha in "Uscire"."""
    return re.search(r"(?<!\w)" + re.escape(word.lower()) + r"(?!\w)", text.lower()) is not None


def has_indicators(screen_content: Optional[ScreenContent], indicators: list[str]) -> bool:
    """Almeno uno degli indicatori e' presente (parola intera) in un elemento della schermata."""
    if screen_content is None:
        return False
    return any(word_in_text(ind, e.text) for ind in indicators for e in screen_content.elements)


def valid_bounds(bounds) -> bool:
    return bool(bounds) and len(bounds) == 4 and any(bounds)


def element_center(bounds) -> Optional[tuple[int, int]]:
    """Centro [x1,y1,x2,y2] come coordinate intere di tap, None se i bounds non sono validi."""
    if not valid_bounds(bounds):
        return None
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_element_by_text(screen_content: ScreenContent, text: str) -> Optional[UIElement]:
    """Elemento non-EditText il cui testo contiene `text` (parola intera, case-insensitive).

    Preferenza: testo identico, poi classe Button, poi il testo piu' corto, poi il primo
    nell'ordine del documento. Cosi' "Accedi" esatto batte "Accedi per vedere il saldo" e,
    a parita', il Button di submit batte il titolo "Accedi" della pagina.
    """
    best = None
    for e in screen_content.elements:
        if e.class_name == EDIT_TEXT_CLASS or not e.text.strip() or not valid_bounds(e.bounds):
            continue
        if not word_in_text(text, e.text):
            continue
        exact = e.text.strip().lower() == text.strip().lower()
        is_button = bool(e.class_name) and "Button" in e.class_name
        rank = (not exact, not is_button, len(e.text))
        if best is None or rank < best[0]:
            best = (rank, e)
    return best[1] if best else None


def find_last_button_by_text(screen_content: ScreenContent, text: str) -> Optional[UIElement]:
    """ULTIMO Button con quel testo (parola intera): il submit sta in fondo al form, non nell'header.

    Con piu' Button preferisce quelli dal testo identico; a parita' vince l'ultimo nell'ordine
    del documento (un popup di sistema, come Touch to Fill, viene dopo la pagina).
    """
    candidates = [e for e in screen_content.elements
                  if e.class_name and "Button" in e.class_name and e.text.strip()
                  and valid_bounds(e.bounds) and word_in_text(text, e.text)]
    if not candidates:
        return None
    exact = [e for e in candidates if e.text.strip().lower() == text.strip().lower()]
    return (exact or candidates)[-1]


def resolve_tap_target(screen_content: ScreenContent, text: str) -> Optional[UIElement]:
    """Elemento da toccare per un tap_button: l'ultimo Button, altrimenti un elemento non-Button
    (es. il link "Accedi" della home, che e' una View). Stessa regola per Mapper ed Executor, cosi'
    quello che funziona in mapping colpisce lo stesso elemento al replay."""
    return find_last_button_by_text(screen_content, text) or find_element_by_text(screen_content, text)


def element_at_point(screen_content: ScreenContent, x: int, y: int) -> Optional[UIElement]:
    """L'elemento non-EditText con testo che contiene il punto (il piu' piccolo), se esiste."""
    hits = [e for e in screen_content.elements
            if e.class_name != EDIT_TEXT_CLASS and e.text.strip() and valid_bounds(e.bounds)
            and e.bounds[0] <= x <= e.bounds[2] and e.bounds[1] <= y <= e.bounds[3]]
    if not hits:
        return None
    return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))


def screen_signature(screen_content: Optional[ScreenContent]) -> tuple:
    """Firma della schermata (testo, classe, bounds di ogni elemento): se cambia, il tap ha avuto effetto."""
    if screen_content is None:
        return ()
    return tuple((e.text, e.class_name, tuple(e.bounds or [])) for e in screen_content.elements)


def edittext_match(label_element: UIElement, elements: list[UIElement]):
    """(EditText, contenuta, distanza) o None se la label non ha bounds o non c'e' nessun EditText."""
    if not valid_bounds(label_element.bounds):
        return None
    edits = [e for e in elements if e.class_name == EDIT_TEXT_CLASS and valid_bounds(e.bounds)]
    if not edits:
        return None
    lx1, ly1, lx2, ly2 = label_element.bounds
    cx, cy = (lx1 + lx2) / 2, (ly1 + ly2) / 2

    containing = [e for e in edits
                  if e.bounds[0] <= cx <= e.bounds[2] and e.bounds[1] <= cy <= e.bounds[3]]
    if containing:
        smallest = min(containing, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))
        return smallest, True, 0.0

    def distance(e: UIElement) -> float:
        ex, ey = (e.bounds[0] + e.bounds[2]) / 2, (e.bounds[1] + e.bounds[3]) / 2
        return ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5

    nearest = min(edits, key=distance)
    return nearest, False, distance(nearest)


def find_nearest_edittext(label_element: UIElement, elements: list[UIElement]) -> Optional[UIElement]:
    """EditText associato a una label per geometria.

    1. Se un EditText contiene la label (centro nei suoi bounds) -> quello (il piu' piccolo)
    2. Altrimenti il piu' vicino per distanza euclidea tra i centri
    3. Nessun EditText -> None
    """
    hit = edittext_match(label_element, elements)
    return hit[0] if hit else None


def find_field_by_label(screen_content: ScreenContent, label: str) -> Optional[UIElement]:
    """EditText del campo con quella label.

    Prima la label come elemento separato (es. Sisal: EditText vuoto + View "Email / Username"),
    associata all'EditText per geometria; con piu' label candidate vince quella identica,
    poi quella dentro il campo, poi la piu' vicina, poi la piu' corta ("Hai dimenticato la
    password?" non batte "Password"). Fallback: EditText con la label nel proprio text/resource_id.
    """
    elements = screen_content.elements
    best = None
    for e in elements:
        if e.class_name == EDIT_TEXT_CLASS or not e.text.strip() or not word_in_text(label, e.text):
            continue
        hit = edittext_match(e, elements)
        if hit is None:
            continue
        edit, contained, distance = hit
        exact = e.text.strip().lower() == label.strip().lower()
        rank = (not exact, not contained, distance, len(e.text))
        if best is None or rank < best[0]:
            best = (rank, edit)
    if best:
        return best[1]

    wanted = label.strip().lower()
    for e in elements:
        if e.class_name == EDIT_TEXT_CLASS and valid_bounds(e.bounds) and (
                word_in_text(label, e.text) or wanted in (e.resource_id or "").lower()):
            return e
    return None


def _is_visible(e: UIElement) -> bool:
    return valid_bounds(e.bounds) and e.bounds[2] > e.bounds[0] and e.bounds[3] > e.bounds[1]


def find_active_captcha(screen_content: Optional[ScreenContent]) -> Optional[CaptchaFinding]:
    """Il CAPTCHA ATTIVO sulla schermata, o None. Cerca elementi strutturali, mai solo testo:

    a. casella (CheckBox/Button/ImageButton) "Non sono un robot"
    b. frame della sfida: URL reCAPTCHA in testo/resource_id, con bounds visibili, escluso il badge
    c. resource_id con "recaptcha" (es. recaptcha-anchor), visibile, esclusi badge e campi nascosti
    d. sfida a immagini: un prompt ("Seleziona tutte le immagini...") piu' un ImageView grande
    Ignorati: badge "This site is protected by reCAPTCHA", View "reCAPTCHA" invisibile, link alla privacy policy.
    """
    if screen_content is None:
        return None
    elements = screen_content.elements
    for e in elements:
        cls = e.class_name or ""
        if ("CheckBox" in cls or "Button" in cls) and any(word_in_text(t, e.text) for t in _CAPTCHA_CHECKBOX_TEXTS):
            return CaptchaFinding("reCAPTCHA", f"casella '{e.text.strip()}' ({cls.rsplit('.', 1)[-1]})")
        if not _is_visible(e):
            continue
        rid = (e.resource_id or "").lower()
        haystack = f"{e.text} {rid}".lower()
        if (any(part in haystack for part in _RECAPTCHA_URL_PARTS)
                and not any(part in haystack for part in _BADGE_PARTS)):
            return CaptchaFinding("reCAPTCHA", "frame reCAPTCHA della sfida")
        if "recaptcha" in rid and not any(part in rid for part in _HIDDEN_FIELD_PARTS):
            return CaptchaFinding("reCAPTCHA", f"elemento '{e.resource_id}'")

    prompt = next((e for e in elements if any(word_in_text(t, e.text) for t in _CAPTCHA_CHALLENGE_TEXTS)), None)
    if prompt is not None:
        has_image = any(
            "Image" in (e.class_name or "") and _is_visible(e)
            and e.bounds[2] - e.bounds[0] >= MIN_CAPTCHA_IMAGE_SIDE
            and e.bounds[3] - e.bounds[1] >= MIN_CAPTCHA_IMAGE_SIDE
            for e in elements)
        if has_image:
            return CaptchaFinding("captcha", f"sfida a immagini ('{prompt.text.strip()[:40]}')")
    return None


# --- Stato della sessione (unica logica per LoginExecutor.check_session e LoginMapper) ---------------------------

LOGIN_ENTRY_LABELS = ("Accedi", "Login", "Log in", "Sign in")     # i pulsanti per entrare ("Registrati" no)


def session_state(screen_content: Optional[ScreenContent], logged_in_indicators: Optional[list] = None) -> str:
    """Stato della sessione dagli ELEMENTI UI (mai dallo screenshot): "login_required" | "logged_in" | "unknown".

    Un pulsante di login visibile ("Accedi", "Registrati", ...) vuol dire NON loggato, sempre (priorita'); solo
    senza pulsanti di login contano gli indicatori di sessione attiva ("Logout", "Il mio conto", ...). I testi di
    marketing ("Ti diamo il benvenuto") non sono indicatori.
    """
    if has_indicators(screen_content, LOGIN_BUTTON_INDICATORS):
        return "login_required"
    if has_indicators(screen_content, list(logged_in_indicators or []) + list(DEFAULT_LOGGED_IN_INDICATORS)):
        return "logged_in"
    return "unknown"


def login_button_label(screen_content: Optional[ScreenContent]) -> Optional[str]:
    """Il pulsante per entrare presente sulla schermata ("Accedi", "Login", ...), None se non c'e'."""
    return next((label for label in LOGIN_ENTRY_LABELS if has_indicators(screen_content, [label])), None)


def login_form_visible(screen_content: Optional[ScreenContent]) -> bool:
    """Il form di login e' sulla schermata: c'e' il campo password (o PIN). Un campo "Email" da solo (newsletter,
    ricerca) non basta."""
    return screen_content is not None and any(
        find_field_by_label(screen_content, label) is not None for label in ("Password", "PIN"))


def prioritized_elements(screen_content: ScreenContent, limit: int) -> list:
    """[(indice originale, elemento)] al massimo `limit`, in ordine di documento. Se ce ne sono di piu' si tengono
    prima i testi di login/sessione, i campi di testo e le caselle, poi gli altri Button, poi il resto: su una
    pagina da centinaia di elementi il pulsante "Accedi" non resta tagliato fuori."""
    indexed = list(enumerate(screen_content.elements))
    if len(indexed) <= limit:
        return indexed
    keywords = list(LOGIN_BUTTON_INDICATORS) + list(DEFAULT_LOGGED_IN_INDICATORS)

    def tier(item) -> tuple:
        e = item[1]
        cls = e.class_name or ""
        if cls == EDIT_TEXT_CLASS or "CheckBox" in cls or any(word_in_text(k, e.text) for k in keywords):
            return 0, item[0]
        return (1 if "Button" in cls else 2), item[0]

    return sorted(sorted(indexed, key=tier)[:limit], key=lambda item: item[0])

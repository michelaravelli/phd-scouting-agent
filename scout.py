"""
PhD Scouting Agent (v3) - Opportunity Discovery Engine
------------------------------------------------------
Legge il database "Fonti di Scouting" su Notion, esplora ogni fonte
prioritizzando le pagine Call/News e i PDF allegati, analizza il contenuto
con Gemini per identificare opportunità attive (Call for Papers, conferenze,
summer school) e aggiorna il "PhD Scouting Tracker".

Variabili d'ambiente richieste:
  NOTION_TOKEN         - Internal Integration Secret di Notion
  GEMINI_API_KEY        - Chiave API Google AI Studio
  NOTION_DB_FONTI_ID     - ID database "Fonti di Scouting"
  NOTION_DB_TRACKER_ID   - ID database "PhD Scouting Tracker"
  GEMINI_MODEL           - Opzionale, default "gemini-2.5-flash-lite"
"""

import collections
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# Gestione opzionale/tollerante dell'estrazione PDF
try:
    import pypdf

    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


def clean_secret(val, default=""):
    """Pulisce la stringa rimuovendo sintassi Markdown, parentesi e spazi."""
    if not val:
        return default
    match = re.search(r"\((https?://[^\)]+)\)", val)
    if match:
        val = match.group(1)
    notion_id_match = re.search(r"([a-f0-9]{32})", val)
    if "notion.com" in val or "notion.so" in val:
        if notion_id_match:
            return notion_id_match.group(1)
    cleaned = re.sub(r"[\[\]\(\)]", "", val)
    return cleaned.strip()


NOTION_TOKEN = clean_secret(os.environ.get("NOTION_TOKEN"))
GEMINI_API_KEY = clean_secret(os.environ.get("GEMINI_API_KEY"))
DB_FONTI_ID = clean_secret(os.environ.get("NOTION_DB_FONTI_ID"))
DB_TRACKER_ID = clean_secret(os.environ.get("NOTION_DB_TRACKER_ID"))
GEMINI_MODEL = clean_secret(
    os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash-lite"
)

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# Header HTTP completi per superare i blocchi 403/405 degli editori
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not=A?Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

TIPO_OPZIONI = [
    "Journal CFP",
    "Conferenza",
    "Summer School",
    "Evento gratuito online",
    "Lezione/Seminario",
]
COSTO_OPZIONI = ["Gratuito", "A pagamento", "Non specificato"]
MODALITA_OPZIONI = ["In presenza", "Online", "Ibrida"]

STRONG_LINK_KEYWORDS = [
    "call for paper",
    "call for papers",
    "cfp",
    "call for abstract",
    "call for contribution",
    "submission",
    "submit",
    "deadline",
    "abstract",
    "bando",
    "scadenza",
    "iscrizioni aperte",
    "apply now",
    "application",
    "candidatura",
    "convegn",
    "congress",
    "special issue",
]

WEAK_LINK_KEYWORDS = [
    "read more",
    "continua",
    "leggi tutto",
    "more information",
    "further details",
    "details",
    "here",
    "website",
    "conference website",
    "official website",
    "program",
    "programme",
    "info",
]

IGNORE_LINK_PATTERNS = [
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "privacy",
    "cookie",
    "terms-of",
    "termini",
    "mailto:",
    "tel:",
    "javascript:",
    "login",
    "signup",
    "cart",
]

MAX_PAGES_PER_FONTE = 10
MAX_DEPTH = 2
MAX_CHARS_PER_PAGE = 7000
MAX_CHARS_TOTAL = 45000

GEMINI_MIN_INTERVAL_SEC = 5
GEMINI_MAX_RETRIES = 4
GEMINI_BACKOFF_BASE_SEC = 15

_last_gemini_call_ts = 0.0


# ---------------------------------------------------------------------------
# Notion API
# ---------------------------------------------------------------------------


def notion_query_active_fonti():
    url = f"{NOTION_API}/databases/{DB_FONTI_ID}/query"
    payload = {"filter": {"property": "Attiva", "checkbox": {"equals": True}}}
    results, cursor = [], None
    while True:
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            url, headers=NOTION_HEADERS, json=payload, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return results


def get_prop_text(page, name, kind="rich_text"):
    prop = page["properties"].get(name)
    if not prop:
        return ""
    arr = (
        prop.get("title", []) if kind == "title" else prop.get("rich_text", [])
    )
    return "".join(t["plain_text"] for t in arr)


def get_prop_url(page, name):
    prop = page["properties"].get(name)
    return prop.get("url") if prop else None


def get_prop_checkbox(page, name):
    prop = page["properties"].get(name)
    return bool(prop and prop.get("checkbox"))


def get_prop_select(page, name):
    prop = page["properties"].get(name)
    sel = prop.get("select") if prop else None
    return sel.get("name") if sel else None


def update_fonte_status(page_id, hash_map, checked_iso_date):
    hash_json = json.dumps(hash_map, ensure_ascii=False)
    if len(hash_json) > 1900:
        trimmed = dict(list(hash_map.items())[:15])
        hash_json = json.dumps(trimmed, ensure_ascii=False)[:1900]
    url = f"{NOTION_API}/pages/{page_id}"
    payload = {
        "properties": {
            "Ultimo Hash": {"rich_text": [{"text": {"content": hash_json}}]},
            "Ultimo Controllo": {"date": {"start": checked_iso_date}},
        }
    }
    resp = requests.patch(
        url, headers=NOTION_HEADERS, json=payload, timeout=30
    )
    resp.raise_for_status()


def notion_query_all_tracker_keys():
    url = f"{NOTION_API}/databases/{DB_TRACKER_ID}/query"
    payload = {}
    keys = set()
    cursor = None
    while True:
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            url, headers=NOTION_HEADERS, json=payload, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            fonte = get_prop_url(page, "Fonte")
            nome = get_prop_text(page, "Nome", kind="title")
            if fonte:
                keys.add(("url", fonte.rstrip("/").lower()))
            if nome:
                keys.add(("nome", nome.strip().lower()))
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return keys


def is_duplicate(existing_keys, item, link):
    if link and ("url", link.rstrip("/").lower()) in existing_keys:
        return True
    titolo = (item.get("titolo") or "").strip().lower()
    if titolo and ("nome", titolo) in existing_keys:
        return True
    return False


def create_tracker_entry(item, fonte_url):
    tipo = (
        item.get("tipo")
        if item.get("tipo") in TIPO_OPZIONI
        else "Journal CFP"
    )
    costo = (
        item.get("costo")
        if item.get("costo") in COSTO_OPZIONI
        else "Non specificato"
    )
    modalita = (
        item.get("modalita")
        if item.get("modalita") in MODALITA_OPZIONI
        else None
    )

    props = {
        "Nome": {"title": [{"text": {"content": item["titolo"][:200]}}]},
        "Fonte": {"url": item.get("link") or fonte_url},
        "Tipo": {"select": {"name": tipo}},
        "Stato": {"select": {"name": "🆕 Da valutare"}},
        "Costo": {"select": {"name": costo}},
    }
    if modalita:
        props["Modalità"] = {"select": {"name": modalita}}

    scadenza = item.get("scadenza")
    if scadenza and re.match(r"^\d{4}-\d{2}-\d{2}$", scadenza):
        props["Scadenza"] = {"date": {"start": scadenza}}

    if item.get("note"):
        props["Note"] = (
            {"rich_text": [{"text": {"content": item["note"][:1900]}}]}
        )

    payload = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    resp = requests.post(
        f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=30
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Fetching & Content Extraction (HTML e PDF)
# ---------------------------------------------------------------------------


def extract_pdf_text(content_bytes):
    """Estrae il testo da un flusso binario PDF usando pypdf."""
    if not HAS_PYPDF:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(content_bytes))
        text = []
        for page in reader.pages[:10]:  # Massimale prime 10 pagine
            page_text = page.extract_text()
            if page_text:
                text.append(page_text)
        return "\n".join(text)
    except Exception as e:
        print(f"    [!] Errore parsing PDF: {e}")
        return ""


def fetch_resource(url):
    """Recupera la Risorsa (HTML o PDF) gestendo redirect e user-agent."""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()

    if "application/pdf" in ctype or url.lower().endswith(".pdf"):
        if HAS_PYPDF:
            pdf_text = extract_pdf_text(resp.content)
            if pdf_text.strip():
                return pdf_text, "pdf"
        raise ValueError("Contenuto PDF saltato o non leggibile")

    if "html" not in ctype and "text" not in ctype:
        raise ValueError(f"Formato non supportato ({ctype})")

    return resp.text, "html"


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _is_ignored(url):
    low = url.lower()
    return any(p in low for p in IGNORE_LINK_PATTERNS)


def extract_candidate_links(base_url, html, use_weak_keywords):
    soup = BeautifulSoup(html, "html.parser")
    keywords = STRONG_LINK_KEYWORDS + (
        WEAK_LINK_KEYWORDS if use_weak_keywords else []
    )
    found, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _is_ignored(href):
            continue
        label = (a.get_text() or "").lower()
        haystack = f"{href} {label}".lower()

        # Rileva se il link porta a un PDF
        is_pdf = href.lower().endswith(".pdf")

        if not is_pdf and not any(k in haystack for k in keywords):
            continue

        abs_url = urljoin(base_url, href)
        if _is_ignored(abs_url):
            continue
        norm = abs_url.rstrip("/")
        if norm in seen or norm == base_url.rstrip("/"):
            continue

        seen.add(norm)
        found.append(abs_url)

    return found


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def crawl_source(cfp_url, home_url):
    """Esplora prioritariamente la pagina Call/News e poi la Homepage."""
    pages = {}
    visited = set()
    queue = collections.deque()

    # Inserisce per prima la URL specifica della Call/News
    if cfp_url:
        domain = urlparse(cfp_url).netloc
        queue.append((cfp_url, 0, domain, True))

    if home_url:
        domain = urlparse(home_url).netloc
        norm_home = home_url.rstrip("/")
        if not cfp_url or norm_home != cfp_url.rstrip("/"):
            queue.append((home_url, 0, domain, True))

    while queue and len(pages) < MAX_PAGES_PER_FONTE:
        url, depth, home_domain, same_domain_chain = queue.popleft()
        norm = url.rstrip("/")
        if norm in visited:
            continue
        visited.add(norm)

        try:
            raw_content, res_type = fetch_resource(url)
        except Exception as e:
            print(f"    [!] Fetch fallito per {url}: {e}")
            continue

        if res_type == "pdf":
            text = raw_content
            print(f"    (+) PDF letto: {url}")
        else:
            text = extract_text_from_html(raw_content)
            print(f"    (+) Pagina letta: {url}")

        pages[url] = text[:MAX_CHARS_PER_PAGE]

        if (
            depth >= MAX_DEPTH
            or len(pages) >= MAX_PAGES_PER_FONTE
            or res_type == "pdf"
        ):
            continue

        if not same_domain_chain:
            continue

        use_weak = depth >= 1
        try:
            candidates = extract_candidate_links(
                url, raw_content, use_weak_keywords=use_weak
            )
        except Exception:
            candidates = []

        for link in candidates:
            link_norm = link.rstrip("/")
            if link_norm in visited:
                continue
            link_domain = urlparse(link).netloc
            is_same_domain = link_domain == home_domain
            if not is_same_domain and depth == 0:
                continue
            queue.append((link, depth + 1, home_domain, is_same_domain))

    return pages


# ---------------------------------------------------------------------------
# Gemini API Interaction
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Sei un assistente per un ricercatore accademico in semiotica cognitiva, 4E cognition e filosofia della mente.
Il tuo compito è identificare Call for Papers (CFP), conferenze, summer school, e special issue attivi.

Fonte: "{nome_fonte}"
Categoria: {categoria}
Frequenza/periodicità: {frequenza}
Natura submission: {natura}

Istruzioni:
1. Analizza il testo estratto dalla fonte e identifica SOLO opportunità con scadenze/date FUTURE rispetto alla data di oggi ({oggi}).
2. Ignora eventi passati o scadenze già trascorse.
3. Se "Natura submission" è "Aperta tutto l'anno", ignora gli invii generici ordinari e segnala SOLO special issue o chiamate a tema specifico con scadenza determinata.
4. Per la "scadenza", estrai la data dell'ABSTRACT SUBMISSION o la prima data di invio utile in formato YYYY-MM-DD. Se non c'è una data specifica esplicita, imposta null.

Se non trovi alcuna opportunità attiva/futura, rispondi tassativamente con: {{"trovate": []}}

Se trovi opportunità valide, rispondi esclusivamente con un JSON in questo formato:
{{
  "trovate": [
    {{
      "titolo": "Titolo breve della Call / Conferenza",
      "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario",
      "scadenza": "YYYY-MM-DD" | null,
      "link": "URL specifico trovato nel testo oppure null",
      "costo": "Gratuito" | "A pagamento" | "Non specificato",
      "modalita": "In presenza" | "Online" | "Ibrida" | null,
      "note": "Breve sintesi del tema e rilevanza per semiotica/filosofia/cognizione"
    }}
  ]
}}

TEXTO ESTRATTO DALLE PAGINE:
{testo}
"""


class GeminiCallFailedError(Exception):
    pass


def ask_gemini(nome_fonte, categoria, frequenza, natura, combined_text):
    global _last_gemini_call_ts

    prompt = PROMPT_TEMPLATE.format(
        nome_fonte=nome_fonte,
        categoria=categoria or "non specificata",
        frequenza=frequenza or "non specificata",
        natura=natura or "non specificata",
        oggi=datetime.date.today().isoformat(),
        testo=combined_text[:MAX_CHARS_TOTAL],
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "response_mime_type": "application/json",
        },
    }

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        elapsed = time.time() - _last_gemini_call_ts
        if elapsed < GEMINI_MIN_INTERVAL_SEC:
            time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)

        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=60)
        except requests.exceptions.RequestException as e:
            _last_gemini_call_ts = time.time()
            wait = GEMINI_BACKOFF_BASE_SEC * attempt
            print(
                f"    [i] Errore connessione Gemini per {nome_fonte} (tentativo {attempt}): {e}. Retrying..."
            )
            time.sleep(wait)
            continue

        _last_gemini_call_ts = time.time()

        if resp.status_code in (429, 503):
            wait = GEMINI_BACKOFF_BASE_SEC * attempt
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw)
            return parsed.get("trovate", [])
        except (KeyError, IndexError, json.JSONDecodeError):
            raise GeminiCallFailedError(f"Risposta invalida da Gemini: {data}")

    raise GeminiCallFailedError(f"Gemini irraggiungibile per {nome_fonte}")


# ---------------------------------------------------------------------------
# Main Routine
# ---------------------------------------------------------------------------


def main():
    today = datetime.date.today().isoformat()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive trovate: {len(fonti)}")

    try:
        existing_keys = notion_query_all_tracker_keys()
    except Exception as e:
        print(f"  [!] Impossibile leggere il Tracker: {e}")
        existing_keys = set()

    for page in fonti:
        page_id = page["id"]
        nome = get_prop_text(page, "Nome", kind="title") or "(senza nome)"
        home_url = get_prop_url(page, "URL Homepage")
        cfp_url = get_prop_url(page, "URL Call/News")
        richiede_browser = get_prop_checkbox(page, "Richiede Browser")
        categoria = get_prop_select(page, "Categoria")
        frequenza = get_prop_select(page, "Frequenza/Periodicità")
        natura = get_prop_select(page, "Natura Submission")
        old_hash_raw = get_prop_text(page, "Ultimo Hash")

        if not home_url and not cfp_url:
            print(f"- {nome}: nessuna URL disponibile, salto.")
            continue

        if richiede_browser:
            print(f"- {nome}: richiede browser JS, salto.")
            continue

        print(f"\n- {nome}: inizio scouting...")
        try:
            pages = crawl_source(cfp_url, home_url)
        except Exception as e:
            print(f"    [!] Errore durante il crawling: {e}")
            continue

        if not pages:
            print("    Nessuna pagina o risorsa raggiungibile.")
            continue

        # Calcolo Hash corrente
        new_hash_map = {u: hash_text(t) for u, t in pages.items()}

        # Verifica se prima esecuzione o hash aggiornato
        is_first_run = not bool(old_hash_raw and old_hash_raw.strip())
        old_hash_map = {}
        if not is_first_run:
            try:
                old_hash_map = json.loads(old_hash_raw)
            except Exception:
                is_first_run = True

        changed = is_first_run or any(
            new_hash_map.get(u) != old_hash_map.get(u) for u in new_hash_map
        )

        if not changed:
            print(
                "    Nessuna modifica strutturale rilevata (fonte già analizzata)."
            )
            try:
                update_fonte_status(page_id, new_hash_map, today)
            except Exception as e:
                print(f"    [!] Errore aggiornamento status Notion: {e}")
            continue

        print("    Nuovo contenuto o prima esecuzione: invio a Gemini...")
        combined_text = "\n\n".join(
            f"--- Pagina: {u} ---\n{t}" for u, t in pages.items()
        )

        try:
            trovate = ask_gemini(
                nome, categoria, frequenza, natura, combined_text
            )
        except GeminiCallFailedError as e:
            print(f"    [!] Analisi Gemini fallita: {e}")
            continue

        opportunita_aggiunte = 0
        for item in trovate:
            titolo = item.get("titolo")
            if not titolo:
                continue

            # Filtro per scadenze passate
            scadenza = item.get("scadenza")
            if scadenza and scadenza < today:
                print(f"    (scaduto il {scadenza}, salto) {titolo}")
                continue

            link = item.get("link") or cfp_url or home_url
            if is_duplicate(existing_keys, item, link):
                print(f"    (già presente nel Tracker) {titolo}")
                continue

            try:
                create_tracker_entry(item, cfp_url or home_url)
                existing_keys.add(("nome", titolo.strip().lower()))
                if link:
                    existing_keys.add(("url", link.rstrip("/").lower()))
                print(f"    -> AGGIUNTA AL TRACKER: {titolo} [{scadenza}]")
                opportunita_aggiunte += 1
            except Exception as e:
                print(f"    [!] Errore salvataggio Tracker: {e}")

        # Aggiornamento stato su Notion solo dopo completamento dell'analisi
        try:
            update_fonte_status(page_id, new_hash_map, today)
        except Exception as e:
            print(f"    [!] Errore aggiornamento stato fonte: {e}")

    print("\nScouting completato con successo.")


if __name__ == "__main__":
    sys.exit(main())

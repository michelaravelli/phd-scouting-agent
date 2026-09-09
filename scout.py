"""
PhD Scouting Agent (v2.1)
------------------------
Versione corretta:
  - Gestione pulita del JSON restituito da Gemini (rimozione blocchi markdown ```json).
  - User-Agent aggiornato per bypassare blocchi HTTP 403/405 su Wiley, Sage e DeGruyter.
  - Aggiunta opzione CLI `--force` per forzare la ri-analisi di Gemini bypassando il controllo hash.
"""

import os
import re
import sys
import json
import time
import hashlib
import datetime
import collections
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DB_FONTI_ID = os.environ["NOTION_DB_FONTI_ID"]
DB_TRACKER_ID = os.environ["NOTION_DB_TRACKER_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

NOTION_API = "[https://api.notion.com/v1](https://api.notion.com/v1)"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
GEMINI_URL = (
    f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/)"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# User-Agent browser standard per mitigare 403/405 su DeGruyter, Wiley, Sage
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
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
    "call for paper", "call for papers", "cfp", "call for abstract",
    "call for contribution", "submission", "submit", "deadline",
    "abstract", "bando", "scadenza", "iscrizioni aperte", "apply now",
    "application", "candidatura", "convegn", "congress",
]

WEAK_LINK_KEYWORDS = [
    "read more", "continua", "leggi tutto", "more information",
    "further details", "details", "here", "website", "conference website",
    "official website", "program", "programme", "info",
]

IGNORE_LINK_PATTERNS = [
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "privacy", "cookie", "terms-of", "termini", "mailto:",
    "tel:", "javascript:",
]

MAX_PAGES_PER_FONTE = 12
MAX_DEPTH = 3
MAX_CHARS_PER_PAGE = 6000
MAX_CHARS_TOTAL = 45000

GEMINI_MIN_INTERVAL_SEC = 5
GEMINI_MAX_RETRIES = 4
GEMINI_BACKOFF_BASE_SEC = 15

_last_gemini_call_ts = 0.0

# ---------------------------------------------------------------------------
# Notion helpers - Fonti
# ---------------------------------------------------------------------------

def notion_query_active_fonti():
    url = f"{NOTION_API}/databases/{DB_FONTI_ID}/query"
    payload = {"filter": {"property": "Attiva", "checkbox": {"equals": True}}}
    results, cursor = [], None
    while True:
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
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
    arr = prop.get("title", []) if kind == "title" else prop.get("rich_text", [])
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
        trimmed = dict(list(hash_map.items())[:20])
        hash_json = json.dumps(trimmed, ensure_ascii=False)[:1900]
    url = f"{NOTION_API}/pages/{page_id}"
    payload = {
        "properties": {
            "Ultimo Hash": {"rich_text": [{"text": {"content": hash_json}}]},
            "Ultimo Controllo": {"date": {"start": checked_iso_date}},
        }
    }
    resp = requests.patch(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()

# ---------------------------------------------------------------------------
# Notion helpers - Tracker
# ---------------------------------------------------------------------------

def notion_query_all_tracker_keys():
    url = f"{NOTION_API}/databases/{DB_TRACKER_ID}/query"
    payload = {}
    keys = set()
    cursor = None
    while True:
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
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
    tipo = item.get("tipo") if item.get("tipo") in TIPO_OPZIONI else "Journal CFP"
    costo = item.get("costo") if item.get("costo") in COSTO_OPZIONI else "Non specificato"
    modalita = item.get("modalita") if item.get("modalita") in MODALITA_OPZIONI else None

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
        props["Note"] = {"rich_text": [{"text": {"content": item["note"][:1900]}}]}

    payload = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    resp = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()

# ---------------------------------------------------------------------------
# Fetch, parsing e crawling
# ---------------------------------------------------------------------------

def fetch_html(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower():
        raise ValueError(f"contenuto non HTML ({ctype}), salto")
    return resp.text


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
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
    keywords = STRONG_LINK_KEYWORDS + (WEAK_LINK_KEYWORDS if use_weak_keywords else [])
    found, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _is_ignored(href):
            continue
        label = (a.get_text() or "").lower()
        haystack = f"{href} {label}".lower()
        if not any(k in haystack for k in keywords):
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


def crawl_source(entry_urls):
    pages = {}
    visited = set()
    queue = collections.deque()

    for u in entry_urls:
        if not u:
            continue
        norm = u.rstrip("/")
        if norm in visited:
            continue
        domain = urlparse(u).netloc
        queue.append((u, 0, domain, True))

    while queue and len(pages) < MAX_PAGES_PER_FONTE:
        url, depth, home_domain, same_domain_chain = queue.popleft()
        norm = url.rstrip("/")
        if norm in visited:
            continue
        visited.add(norm)

        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"    [!] fetch fallito per {url}: {e}")
            continue

        text = extract_text_from_html(html)
        pages[url] = text[:MAX_CHARS_PER_PAGE]
        print(f"    (+) pagina letta: {url}")

        if depth >= MAX_DEPTH or len(pages) >= MAX_PAGES_PER_FONTE:
            continue
        if not same_domain_chain:
            continue

        use_weak = depth >= 1
        try:
            candidates = extract_candidate_links(url, html, use_weak_keywords=use_weak)
        except Exception as e:
            print(f"    [!] errore estraendo link da {url}: {e}")
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

PROMPT_TEMPLATE = """Sei un assistente che aiuta un dottorando in semiotica cognitiva e filosofia della
mente a monitorare call for papers, conferenze e summer school pertinenti.

Fonte: "{nome_fonte}"
Categoria fonte: {categoria}
Frequenza/periodicita' dichiarata: {frequenza}
Natura submission dichiarata: {natura}

Il testo qui sotto e' stato raccolto navigando la pagina principale della fonte ed eventuali pagine collegate.
Concentrati solo su eventuali annunci di Call for Papers, conferenze, summer school, workshop o seminari con una scadenza o data futura rispetto a oggi ({oggi}).

Se non trovi nulla di rilevante, rispondi con: {{"trovate": []}}

Se trovi una o piu' opportunita' rilevanti, rispondi SOLO con un JSON in questo formato esatto:

{{
  "trovate": [
    {{
      "titolo": "string breve",
      "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario",
      "scadenza": "YYYY-MM-DD, oppure null",
      "link": "URL diretto piu' specifico possibile",
      "costo": "Gratuito" | "A pagamento" | "Non specificato",
      "modalita": "In presenza" | "Online" | "Ibrida" | null,
      "note": "1-2 frasi in italiano su tema e rilevanza"
    }}
  ]
}}

TESTO RACCOLTO:
{testo}
"""


def check_gemini_model():
    try:
        resp = requests.get(
            f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){GEMINI_MODEL}",
            params={"key": GEMINI_API_KEY},
            timeout=15,
        )
        if resp.status_code == 404:
            print(f"ATTENZIONE: modello '{GEMINI_MODEL}' non valido (404).")
            return False
        resp.raise_for_status()
        print(f"Modello Gemini verificato: {GEMINI_MODEL}")
        return True
    except Exception as e:
        print(f"  [!] Impossibile verificare il modello Gemini in anticipo: {e}")
        return True


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
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"},
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
            print(f"  [i] Errore connessione Gemini per {nome_fonte} ({attempt}/{GEMINI_MAX_RETRIES}): {e}. Retrying in {wait:.0f}s...")
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
            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Pulizia blocchi di codice markdown
            raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"^```\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            
            parsed = json.loads(raw)
            return parsed.get("trovate", [])
        except (KeyError, IndexError, json.JSONDecodeError) as err:
            print(f"  [!] Risposta Gemini non interpretabile per {nome_fonte}: {err}")
            return []

    print(f"  [!] Gemini irraggiungibile per {nome_fonte}.")
    return []

# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    force_run = "--force" in sys.argv
    if force_run:
        print("[i] Modalità FORCE attiva: il controllo degli Hash verrà ignorato.")

    today = datetime.date.today().isoformat()
    check_gemini_model()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive da controllare: {len(fonti)}")

    try:
        existing_keys = notion_query_all_tracker_keys()
    except Exception as e:
        print(f"  [!] Errore leggendo il Tracker, procedo senza dedup: {e}")
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

        entry_urls = [u for u in [cfp_url, home_url] if u]
        if not entry_urls or richiede_browser:
            continue

        print(f"\n- {nome}: esploro {entry_urls}")
        try:
            pages = crawl_source(entry_urls)
        except Exception as e:
            print(f"  [!] Errore nel crawling: {e}")
            continue

        if not pages:
            print("  Nessuna pagina raggiungibile, salto.")
            continue

        old_hash_map = {}
        if old_hash_raw and not force_run:
            try:
                old_hash_map = json.loads(old_hash_raw)
            except json.JSONDecodeError:
                old_hash_map = {}

        new_hash_map = {u: hash_text(t) for u, t in pages.items()}
        changed = force_run or any(new_hash_map[u] != old_hash_map.get(u) for u in new_hash_map)

        if not changed:
            print("  Nessuna modifica rilevata su nessuna pagina esplorata.")
            try:
                update_fonte_status(page_id, new_hash_map, today)
            except Exception as e:
                print(f"  [!] Errore aggiornando Notion (Fonti): {e}")
            continue

        print("  Cambiamento rilevato (o --force), chiedo a Gemini...")
        combined_text = "\n\n".join(f"--- Pagina: {u} ---\n{t}" for u, t in pages.items())
        
        trovate = ask_gemini(nome, categoria, frequenza, natura, combined_text)

        for item in trovate:
            if not item.get("titolo"):
                continue
            link = item.get("link") or entry_urls[0]
            if is_duplicate(existing_keys, item, link):
                print(f"  (già presente nel Tracker, salto) {item['titolo']}")
                continue
            try:
                create_tracker_entry(item, entry_urls[0])
                existing_keys.add(("nome", item["titolo"].strip().lower()))
                if link:
                    existing_keys.add(("url", link.rstrip("/").lower()))
                print(f"  -> INSERITO NEL TRACKER: {item['titolo']}")
            except Exception as e:
                print(f"  [!] Errore creando pagina nel Tracker: {e}")

        try:
            update_fonte_status(page_id, new_hash_map, today)
        except Exception as e:
            print(f"  [!] Errore aggiornando Notion (Fonti): {e}")

    print("\nScouting completato.")

if __name__ == "__main__":
    sys.exit(main())

"""
PhD Scouting Agent (v2)
------------------------
Legge il database "Fonti di Scouting" su Notion, esplora ogni fonte con un
piccolo crawler multi-hop (home page + pagina Call/News + link interni/esterni
rilevanti fino a un paio di livelli di profondita'), e se rileva testo nuovo
o cambiato chiede a Gemini di capire se e' comparsa una Call for Papers,
una conferenza o una summer school. I risultati confermati vengono scritti
nel database "PhD Scouting Tracker" con Stato = "Da valutare".

Rispetto alla v1, questa versione:
  - usa i nomi corretti delle proprieta' Notion ("URL Homepage",
    "URL Call/News", ecc. - la v1 cercava una proprieta' "URL" che non
    esiste, motivo per cui non trovava mai nulla).
  - usa ENTRAMBE le URL della fonte come punto di partenza, non solo la home.
  - segue i link fino a un paio di hop di profondita' (non solo 1), incluso
    UN salto fuori dal dominio originale quando il link e' chiaramente
    pertinente (es. una fonte che rimanda al sito ufficiale di una
    conferenza ospitato altrove).
  - calcola un hash per singola pagina fisica invece di un hash aggregato
    per fonte, cosi' da capire con precisione cosa e' cambiato.
  - controlla i duplicati nel Tracker prima di creare una nuova riga.
  - usa "Frequenza/Periodicita'" e "Natura Submission" per dare contesto
    a Gemini (una rivista "aperta tutto l'anno" non va segnalata come se
    avesse una scadenza, a meno di un vero special issue a termine).
  - fa scrivere a Gemini anche Costo e Modalita', quando deducibili.

Variabili d'ambiente richieste (invariate rispetto alla v1, vedi .env.example):
  NOTION_TOKEN          - Internal Integration Secret di Notion
  GEMINI_API_KEY        - chiave API gratuita di Google AI Studio
  NOTION_DB_FONTI_ID     - ID del database "Fonti di Scouting"
  NOTION_DB_TRACKER_ID   - ID del database "PhD Scouting Tracker"
  GEMINI_MODEL           - opzionale, default "gemini-2.0-flash"
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Nota: "gemini-flash-latest" e' un alias mantenuto da Google che punta
# sempre al modello Flash gratuito corrente (gemini-2.0-flash, usato nella
# v1 di questo script, e' stato dismesso il 1 giugno 2026 e ora risponde
# 404). Usare l'alias evita di dover aggiornare questo file ogni volta che
# Google ritira un modello. Se preferisci fissare una versione precisa,
# imposta GEMINI_MODEL nei secrets (es. "gemini-3-flash-preview").

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

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PhD-Scouting-Agent/2.0"
    )
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

# --- Parametri del crawler -------------------------------------------------

# Parole "forti": usate su qualsiasi pagina per decidere se un link merita di
# essere seguito. Sono termini che di solito compaiono in un link/testo
# ancora che porta direttamente a una call, una submission, una scadenza.
STRONG_LINK_KEYWORDS = [
    "call for paper", "call for papers", "cfp", "call for abstract",
    "call for contribution", "submission", "submit", "deadline",
    "abstract", "bando", "scadenza", "iscrizioni aperte", "apply now",
    "application", "candidatura", "convegn", "congress",
]

# Parole "deboli": vengono seguite solo su pagine raggiunte a loro volta
# tramite un match forte (cioe' pagine gia' presunte pertinenti, tipo il
# dettaglio di una news). Da sole, su una home page piena di menu, darebbero
# troppi falsi positivi.
WEAK_LINK_KEYWORDS = [
    "read more", "continua", "leggi tutto", "more information",
    "further details", "details", "here", "website", "conference website",
    "official website", "program", "programme", "info",
]

# Domini/pattern da ignorare sempre (social, legal boilerplate, ecc.)
IGNORE_LINK_PATTERNS = [
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "privacy", "cookie", "terms-of", "termini", "mailto:",
    "tel:", "javascript:",
]

MAX_PAGES_PER_FONTE = 12
MAX_DEPTH = 3  # hop massimi a partire dai punti di ingresso (home + call/news)
MAX_CHARS_PER_PAGE = 6000
MAX_CHARS_TOTAL = 45000

# --- Parametri di chiamata a Gemini -----------------------------------------
# Il piano gratuito ha limiti stretti di richieste al minuto (nell'ordine di
# 5-15 RPM a seconda del modello) e ogni tanto risponde 503 (sovraccarico
# momentaneo dei server, non un errore nostro). GEMINI_MIN_INTERVAL_SEC tiene
# un ritmo prudente tra una chiamata e l'altra; GEMINI_MAX_RETRIES/backoff
# gestiscono i 429/503 riprovando invece di arrendersi al primo colpo.
GEMINI_MIN_INTERVAL_SEC = 13
GEMINI_MAX_RETRIES = 4
GEMINI_BACKOFF_BASE_SEC = 15

_last_gemini_call_ts = 0.0


# ---------------------------------------------------------------------------
# Notion helpers - Fonti
# ---------------------------------------------------------------------------

def notion_query_active_fonti():
    """Ritorna la lista di pagine nel DB Fonti con Attiva = true."""
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
    """Salva l'hash per-pagina (dict url -> hash breve) come JSON, troncato
    per stare dentro ai limiti ragionevoli di un campo testo di Notion."""
    hash_json = json.dumps(hash_map, ensure_ascii=False)
    if len(hash_json) > 1900:
        # se troppo lungo, teniamo solo le prime pagine (le piu' importanti,
        # dato che i punti di ingresso vengono sempre crawlati per primi)
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
    """Ritorna un set di chiavi (url normalizzati + titoli normalizzati) gia'
    presenti nel Tracker, per evitare di creare doppioni a ogni run."""
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
    elif scadenza:
        print(f"    [i] scadenza in formato non valido, ignorata: {scadenza!r}")

    if item.get("note"):
        props["Note"] = {"rich_text": [{"text": {"content": item["note"][:1900]}}]}

    payload = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    resp = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Fetch, parsing e crawling multi-hop
# ---------------------------------------------------------------------------

def fetch_html(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower():
        # niente PDF/immagini/binari in questa v2: li ignoriamo silenziosamente
        # (possibile miglioramento futuro: estrarre testo anche dai PDF)
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
    """Trova nella pagina i link che sembrano portare a call/news/eventi.
    Ritorna una lista di URL assoluti (qualsiasi dominio: il filtro sul
    dominio viene applicato dal chiamante, non qui)."""
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
    """Esplora una fonte a partire da uno o due URL di ingresso (home page e/o
    pagina Call/News), seguendo link pertinenti fino a MAX_DEPTH hop e fino a
    MAX_PAGES_PER_FONTE pagine totali.

    Regole di navigazione:
      - dai punti di ingresso (depth 0) si seguono solo i link "forti"
        restando sullo stesso dominio dell'entry point.
      - una volta su una pagina raggiunta tramite un match forte (depth >= 1),
        si seguono anche i link "deboli" (es. "read more", "continua"),
        ed e' permesso UN salto fuori dal dominio originale (es. la fonte
        rimanda al sito ufficiale della conferenza ospitato altrove). Le
        pagine fuori dominio non vengono pero' ulteriormente espanse: sono
        sempre trattate come foglie.

    Ritorna: dict {url: testo_estratto}
    """
    pages = {}
    visited = set()
    queue = collections.deque()
    entry_domains = set()

    for u in entry_urls:
        if not u:
            continue
        norm = u.rstrip("/")
        if norm in visited:
            continue
        domain = urlparse(u).netloc
        entry_domains.add(domain)
        queue.append((u, 0, domain, True))  # (url, depth, home_domain, is_same_domain_as_home)

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
            # pagina fuori dominio: la leggiamo ma non la espandiamo oltre
            continue

        use_weak = depth >= 1  # sulle pagine "interne" siamo piu' permissivi
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
                # dai punti di ingresso non usciamo mai subito dal dominio:
                # prima dobbiamo essere su una pagina interna gia' pertinente
                continue
            queue.append((link, depth + 1, home_domain, is_same_domain))

    return pages


# ---------------------------------------------------------------------------
# Gemini: capire se nel testo c'e' una nuova CFP / evento
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Sei un assistente che aiuta un dottorando in semiotica cognitiva e filosofia della
mente a monitorare call for papers, conferenze e summer school pertinenti.

Fonte: "{nome_fonte}"
Categoria fonte: {categoria}
Frequenza/periodicita' dichiarata: {frequenza}
Natura submission dichiarata: {natura}
(Se "Natura submission" e' "Aperta tutto l'anno / Generica", NON segnalare la
normale possibilita' di inviare contributi in qualsiasi momento: segnala solo
se c'e' un vero special issue/evento con una scadenza specifica e diversa
dalla submission ordinaria.)

Il testo qui sotto e' stato raccolto navigando la pagina principale della
fonte ed eventuali pagine collegate (news, dettagli, submission, ecc.), fino
a un paio di link di profondita'. Puo' contenere molto rumore (menu, footer,
articoli non pertinenti, contenuti passati): concentrati solo su eventuali
annunci di Call for Papers, conferenze, summer school, workshop o seminari
con una scadenza o data futura rispetto a oggi ({oggi}), o comunque non
ancora scaduti / la cui scadenza non e' stata ancora annunciata ma il cui
processo di call e' chiaramente attivo o in arrivo.

Ignora eventi/scadenze chiaramente passati rispetto a oggi.

Se non trovi nulla di rilevante, rispondi con: {{"trovate": []}}

Se trovi una o piu' opportunita' rilevanti, rispondi SOLO con un JSON in questo
formato esatto (nessun testo fuori dal JSON):

{{
  "trovate": [
    {{
      "titolo": "string breve",
      "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario",
      "scadenza": "YYYY-MM-DD, obbligatoriamente in questo formato, oppure null se nessuna scadenza e' ancora stata annunciata",
      "link": "URL diretto piu' specifico possibile trovato nel testo, altrimenti null",
      "costo": "Gratuito" | "A pagamento" | "Non specificato",
      "modalita": "In presenza" | "Online" | "Ibrida" | null,
      "note": "1-2 frasi in italiano su tema e rilevanza per semiotica cognitiva / filosofia della mente / IA"
    }}
  ]
}}

IMPORTANTE sulla scadenza: se una opportunita' ha piu' scadenze diverse (es.
"abstract submission deadline" e "full paper/submission deadline", tipiche
delle conferenze), usa come "scadenza" la PRIMA/PIU' VICINA nel tempo tra
le due (di solito quella dell'abstract), perche' e' quella che determina
davvero entro quando muoversi. Elenca le altre scadenze (submission, review,
camera ready, ecc.) nel campo "note", in modo sintetico. Se nel testo trovi
piu' date ma non e' chiaro quale sia la prima, scegli comunque la piu'
vicina a oggi tra quelle future. Il formato YYYY-MM-DD e' obbligatorio: non
usare mai formati come "September 2026" o "11 settembre" nel campo scadenza.

TESTO RACCOLTO (puo' includere piu' pagine, separate da '--- Pagina: URL ---'):
{testo}
"""


def check_gemini_model():
    """Verifica una volta sola, a inizio run, che il modello configurato
    esista e supporti generateContent - cosi' se Google lo ha dismesso lo
    sappiamo subito con un messaggio chiaro, invece di scoprirlo dopo 33
    fonti tutte fallite in silenzio (ognuna cattura l'errore e va avanti)."""
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}",
            params={"key": GEMINI_API_KEY},
            timeout=15,
        )
        if resp.status_code == 404:
            print(
                f"ATTENZIONE: il modello '{GEMINI_MODEL}' non esiste o e' stato "
                "dismesso da Google (404). Lo script proseguira' ma NON troverà "
                "nulla, perché ogni chiamata a Gemini fallirà. Aggiorna la "
                "variabile GEMINI_MODEL (es. 'gemini-flash-latest', "
                "'gemini-3-flash-preview', 'gemini-2.5-flash')."
            )
            return False
        resp.raise_for_status()
        print(f"Modello Gemini verificato: {GEMINI_MODEL}")
        return True
    except Exception as e:
        print(f"  [!] Impossibile verificare il modello Gemini in anticipo: {e}")
        return True  # non blocchiamo il run per un errore di rete sul check


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
        # rispettiamo un ritmo minimo tra le chiamate per stare dentro il
        # rate limit del piano gratuito (evita 429 "preventivamente")
        elapsed = time.time() - _last_gemini_call_ts
        if elapsed < GEMINI_MIN_INTERVAL_SEC:
            time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)

        resp = requests.post(GEMINI_URL, json=payload, timeout=60)
        _last_gemini_call_ts = time.time()

        if resp.status_code in (429, 503):
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = float(retry_after)
            else:
                wait = GEMINI_BACKOFF_BASE_SEC * attempt  # backoff lineare crescente
            print(
                f"  [i] Gemini {resp.status_code} per {nome_fonte} "
                f"(tentativo {attempt}/{GEMINI_MAX_RETRIES}), riprovo tra {wait:.0f}s..."
            )
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw)
            return parsed.get("trovate", [])
        except (KeyError, IndexError, json.JSONDecodeError):
            print(f"  [!] Risposta Gemini non interpretabile per {nome_fonte}: {data}")
            return []

    print(f"  [!] Gemini irraggiungibile per {nome_fonte} dopo {GEMINI_MAX_RETRIES} tentativi, salto.")
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.date.today().isoformat()
    check_gemini_model()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive da controllare: {len(fonti)}")

    print("Carico le opportunita' gia' presenti nel Tracker (per evitare doppioni)...")
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
        if not entry_urls:
            print(f"- {nome}: nessuna URL (Homepage/Call-News), salto.")
            continue

        if richiede_browser:
            print(f"- {nome}: richiede browser (JS), non ancora supportato, salto.")
            continue

        print(f"- {nome}: esploro {entry_urls}")
        try:
            pages = crawl_source(entry_urls)
        except Exception as e:
            print(f"  [!] Errore nel crawling: {e}")
            continue

        if not pages:
            print("  Nessuna pagina raggiungibile, salto.")
            continue

        old_hash_map = {}
        if old_hash_raw:
            try:
                old_hash_map = json.loads(old_hash_raw)
            except json.JSONDecodeError:
                old_hash_map = {}

        new_hash_map = {u: hash_text(t) for u, t in pages.items()}
        changed = any(new_hash_map[u] != old_hash_map.get(u) for u in new_hash_map)

        if not changed:
            print("  Nessuna modifica rilevata su nessuna pagina esplorata.")
            try:
                update_fonte_status(page_id, new_hash_map, today)
            except Exception as e:
                print(f"  [!] Errore aggiornando Notion (Fonti): {e}")
            continue

        print("  Cambiamento rilevato, chiedo a Gemini...")
        combined_text = "\n\n".join(
            f"--- Pagina: {u} ---\n{t}" for u, t in pages.items()
        )
        try:
            trovate = ask_gemini(nome, categoria, frequenza, natura, combined_text)
        except Exception as e:
            print(f"  [!] Errore chiamando Gemini: {e}")
            trovate = []

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
                print(f"  -> Aggiunta al Tracker: {item['titolo']}")
            except Exception as e:
                print(f"  [!] Errore creando pagina nel Tracker: {e}")

        try:
            update_fonte_status(page_id, new_hash_map, today)
        except Exception as e:
            print(f"  [!] Errore aggiornando Notion (Fonti): {e}")

    print("Scouting completato.")


if __name__ == "__main__":
    sys.exit(main())

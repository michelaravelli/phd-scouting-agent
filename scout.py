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

Rispetto alla v2 (dopo il debug del rate limiting Gemini), questa versione:
  - salva il nuovo hash SOLO se Gemini ha risposto con successo: se la
    chiamata fallisce, l'hash resta quello vecchio, cosi' la fonte verra'
    ricontrollata al giro successivo invece di risultare "gia' controllata"
    senza esserlo mai stata davvero.
  - supporta FORCE_REANALYZE per un reset one-off di tutti gli hash.
  - da' priorita', nel crawling, ai link che sembrano recenti (anno
    corrente/futuro nell'URL, o nessun anno) rispetto a quelli con anni
    passati (edizioni/CFP archiviate), per non sprecare il budget di pagine.
  - legge anche i PDF (comuni nelle humanities) grazie a pypdf, non solo HTML.
  - prova un feed RSS/Atom (tipico pattern WordPress "<url>/feed/") per ogni
    URL di ingresso, prima del crawling HTML: se esiste, e' una fonte di
    scoperta molto piu' pulita e affidabile dei link <a> indovinati.

Variabili d'ambiente richieste (invariate rispetto alla v1, vedi .env.example):
  NOTION_TOKEN          - Internal Integration Secret di Notion
  GEMINI_API_KEY        - chiave API gratuita di Google AI Studio
  NOTION_DB_FONTI_ID     - ID del database "Fonti di Scouting"
  NOTION_DB_TRACKER_ID   - ID del database "PhD Scouting Tracker"
  GEMINI_MODEL           - opzionale, forza un modello specifico (altrimenti
                            si prova una lista di modelli di riserva, in
                            ordine, e si usa il primo che risponde davvero)
  FORCE_REANALYZE        - opzionale, "true" per ignorare gli hash salvati e
                            rianalizzare tutto da zero (vedi workflow_dispatch)
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

# --- Selezione del modello Gemini, con fallback automatico -----------------
# Abbiamo scoperto (set. 2026) che GET /v1beta/models/{nome} puo' rispondere
# 200 (il modello "esiste") mentre la vera chiamata POST .../generateContent
# sullo stesso modello risponde 404 - un problema segnalato piu' volte sui
# forum ufficiali di Google in queste settimane, non qualcosa che dipende dal
# nostro codice o dal nome scelto. Per non restare bloccati quando capita,
# invece di puntare a un solo modello ne proviamo una lista in ordine, con
# una VERA chiamata di prova (non solo ListModels) a inizio run: usiamo il
# primo che risponde davvero, e ci fermiamo li' per tutto il run.
#
# Se GEMINI_MODEL e' impostata (env/secret), viene provata per prima; poi si
# scende nella lista di riserva sottostante.
#
# Nota (set. 2026): la serie di modelli Gemini 2.5 e' in dismissione (Gemini
# 2.5 Flash chiude il 16 ottobre 2026) e i progetti/chiavi piu' recenti
# spesso perdono l'accesso alle serie in via di dismissione anche PRIMA della
# data ufficiale di fine vita - e' probabilmente per questo che 2.5-flash-lite
# e 2.5-flash rispondono 404 nonostante esistano ancora "sulla carta". La
# generazione attuale e' Gemini 3.x: gemini-3.1-flash-lite e' oggi (set. 2026)
# il modello STABILE (non preview) con la quota gratuita migliore, 30
# richieste/minuto e 1.500/giorno.
_env_model = os.environ.get("GEMINI_MODEL", "").strip()
GEMINI_MODEL_CANDIDATES = [
    m for m in [
        _env_model,
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-2.5-pro",  # ultima spiaggia: quota gratuita piu' bassa ma modello maturo
    ] if m
]
# rimuove eventuali doppioni mantenendo l'ordine
GEMINI_MODEL_CANDIDATES = list(dict.fromkeys(GEMINI_MODEL_CANDIDATES))

GEMINI_MODEL = GEMINI_MODEL_CANDIDATES[0]  # verra' aggiornato da check_gemini_model()

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def _gemini_url(model):
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
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

MAX_PAGES_PER_FONTE = 16
MAX_DEPTH = 3  # hop massimi a partire dai punti di ingresso (home + call/news)
MAX_CHARS_PER_PAGE = 6000
MAX_CHARS_TOTAL = 45000

# --- Parametri di chiamata a Gemini -----------------------------------------
# Il piano gratuito ha limiti di richieste al minuto/giorno che variano per
# modello, e ogni tanto risponde 503 (sovraccarico momentaneo dei server).
# GEMINI_MIN_INTERVAL_SEC tiene un ritmo prudente tra una chiamata e l'altra.
#
# IMPORTANTE: se un modello e' saturo (429 persistente, non solo un picco
# momentaneo), NON ha senso insistere a lungo con backoff crescenti: meglio
# passare subito al modello di riserva successivo. Per questo ogni modello
# ha solo GEMINI_ATTEMPTS_PER_MODEL tentativi (con backoff breve) prima che
# si passi al candidato successivo in GEMINI_MODEL_CANDIDATES; si gira
# sull'intera lista al massimo GEMINI_MAX_MODEL_ROTATIONS volte in totale,
# cosi' il tempo massimo speso per una singola fonte resta limitato anche
# nel caso (raro) in cui tutti i modelli siano temporaneamente saturi.
GEMINI_MIN_INTERVAL_SEC = 3
GEMINI_ATTEMPTS_PER_MODEL = 2
GEMINI_BACKOFF_BASE_SEC = 10
GEMINI_MAX_MODEL_ROTATIONS = 1  # 1 giro completo della lista candidati = al massimo

# Se impostata (workflow_dispatch input "force_reanalyze", o a mano nei
# secrets), ignora tutti gli hash salvati su Notion e tratta ogni fonte come
# "cambiata", forzando una rianalisi completa. Serve soprattutto per casi
# come questo: run precedenti in cui Gemini falliva avevano comunque salvato
# l'hash (bug ora corretto), "avvelenando" lo stato - un reset una tantum
# sblocca tutto senza dover svuotare a mano il campo "Ultimo Hash" su Notion.
FORCE_REANALYZE = os.environ.get("FORCE_REANALYZE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

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

def fetch_page(url):
    """Scarica una pagina e ne estrae il testo, gestendo sia HTML che PDF
    (molte call for papers, specie in ambito umanistico, sono pubblicate
    come PDF anziché come pagina HTML).

    Ritorna (html_raw_o_None, testo_estratto, kind) dove kind e' "html" o
    "pdf". html_raw e' None per i PDF (non hanno link <a> da seguire)."""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()

    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = extract_text_from_pdf(resp.content)
        return None, text, "pdf"

    if "html" in ctype:
        return resp.text, extract_text_from_html(resp.text), "html"

    raise ValueError(f"contenuto non supportato ({ctype}), salto")


def extract_text_from_pdf(content_bytes):
    try:
        from io import BytesIO
        from pypdf import PdfReader
    except ImportError as e:
        raise ValueError(f"pypdf non installato: {e}")

    reader = PdfReader(BytesIO(content_bytes))
    parts = []
    for pg in reader.pages:
        try:
            parts.append(pg.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts).strip()


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


_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _link_priority(url):
    """Punteggio di priorita' per un link candidato, piu' basso = da
    visitare prima. Le fonti spesso hanno pagine archiviate per ogni
    edizione passata (2013, 2017, 2021...): senza una priorita', il budget
    di pagine per fonte viene sprecato su quelle invece che sull'edizione
    corrente/futura. Un link con un anno passato viene esplorato per
    ultimo; uno con l'anno corrente o futuro, o senza anno nell'URL, ha
    priorita' normale/alta."""
    current_year = datetime.date.today().year
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", url)]
    if not years:
        return 0  # nessun anno nell'URL: probabilmente una pagina "sempre attuale"
    if max(years) >= current_year:
        return 0  # anno corrente o futuro: massima priorita'
    return 1 + (current_year - max(years))  # piu' vecchio = piu' in fondo alla coda


def extract_candidate_links(base_url, html, use_weak_keywords):
    """Trova nella pagina i link che sembrano portare a call/news/eventi.
    Ritorna una lista di URL assoluti (qualsiasi dominio: il filtro sul
    dominio viene applicato dal chiamante, non qui), ordinati per priorita'
    (contenuti recenti prima di quelli con anni passati nell'URL)."""
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

    found.sort(key=_link_priority)
    return found


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- Discovery via feed RSS/Atom --------------------------------------------
# Molte delle fonti sono WordPress o OJS e spesso espongono un feed RSS/Atom
# sulla loro pagina di categoria/news (tipicamente aggiungendo "feed/"
# all'URL). Un feed e' testo strutturato e pulito - molto piu' affidabile
# dell'euristica sui link <a> - quindi lo tentiamo per primo, per ogni URL
# di ingresso, prima del crawling HTML classico. Se non esiste, si procede
# come prima senza alcun impatto negativo.

def _local_tag(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def parse_feed(xml_text, max_items=15):
    """Parsa in modo tollerante un feed RSS 2.0 o Atom, ignorando i
    namespace XML. Ritorna una lista di dict {titolo, link, descrizione,
    data}."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items = []
    for el in root.iter():
        if _local_tag(el.tag) not in ("item", "entry"):
            continue
        titolo = link = desc = data = None
        for child in el:
            ctag = _local_tag(child.tag)
            if ctag == "title":
                titolo = (child.text or "").strip()
            elif ctag == "link":
                link = (child.text or child.get("href") or "").strip()
            elif ctag in ("description", "summary", "content", "encoded"):
                if child.text:
                    desc = child.text.strip()
            elif ctag in ("pubDate", "updated", "published", "date"):
                data = (child.text or "").strip()
        if titolo:
            items.append({"titolo": titolo, "link": link or "", "desc": desc or "", "data": data or ""})
        if len(items) >= max_items:
            break
    return items


def discover_feed_items(entry_url, max_items=15):
    """Prova a trovare un feed RSS/Atom per l'URL dato (tipico pattern
    WordPress: <url>/feed/). Ritorna (feed_url, items) oppure (None, [])."""
    base = entry_url.rstrip("/")
    candidates = [base + "/feed/", base + "/feed", urljoin(entry_url, "feed/")]
    tried = set()
    for feed_url in candidates:
        if feed_url in tried:
            continue
        tried.add(feed_url)
        try:
            resp = requests.get(feed_url, headers=REQUEST_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception:
            continue
        head = resp.text[:300].lower()
        ctype = resp.headers.get("Content-Type", "").lower()
        looks_like_feed = "xml" in ctype or "rss" in ctype or "<rss" in head or "<feed" in head
        if not looks_like_feed:
            continue
        items = parse_feed(resp.text, max_items=max_items)
        if items:
            return feed_url, items
    return None, []


def crawl_source(entry_urls):
    """Esplora una fonte a partire da uno o due URL di ingresso (home page e/o
    pagina Call/News), seguendo link pertinenti fino a MAX_DEPTH hop e fino a
    MAX_PAGES_PER_FONTE pagine totali.

    Prima del crawling HTML, prova un feed RSS/Atom per ciascun URL di
    ingresso: se esiste, i suoi item vengono aggiunti sia come testo di
    riepilogo (per dare subito a Gemini titoli/date di tutti i post
    recenti) sia come pagine da visitare (per leggere il testo completo di
    ciascuna call, non solo il riassunto del feed).

    Regole di navigazione:
      - dai punti di ingresso (depth 0) si seguono solo i link "forti"
        restando sullo stesso dominio dell'entry point.
      - una volta su una pagina raggiunta tramite un match forte (depth >= 1),
        si seguono anche i link "deboli" (es. "read more", "continua"),
        ed e' permesso UN salto fuori dal dominio originale (es. la fonte
        rimanda al sito ufficiale della conferenza ospitato altrove). Le
        pagine fuori dominio non vengono pero' ulteriormente espanse: sono
        sempre trattate come foglie.
      - i link scoperti vengono esplorati dando priorita' a quelli che
        sembrano recenti (anno corrente/futuro nell'URL, o nessun anno),
        lasciando per ultimi quelli con anni passati (edizioni archiviate).

    Ritorna: dict {url: testo_estratto}
    """
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
        queue.append((u, 0, domain, True))  # (url, depth, home_domain, is_same_domain_as_home)

        try:
            feed_url, items = discover_feed_items(u)
        except Exception as e:
            feed_url, items = None, []
        if feed_url:
            print(f"    (feed) trovato: {feed_url} ({len(items)} elementi)")
            riepilogo = "\n".join(
                f"- {it['titolo']} ({it['data']}) {it['link']}\n  {it['desc'][:300]}"
                for it in items
            )
            pages[feed_url] = riepilogo[:MAX_CHARS_PER_PAGE]
            for it in items:
                if it["link"] and it["link"].rstrip("/") not in visited:
                    link_domain = urlparse(it["link"]).netloc
                    # gli item di un feed sono gia' un match "forte" per
                    # definizione: li mettiamo in coda a profondita' 1 con
                    # priorita' massima (in testa alla coda), cosi' vengono
                    # letti per intero prima di qualunque link scoperto
                    # euristicamente sull'HTML.
                    queue.appendleft((it["link"], 1, link_domain, link_domain == domain))

    while queue and len(pages) < MAX_PAGES_PER_FONTE:
        url, depth, home_domain, same_domain_chain = queue.popleft()
        norm = url.rstrip("/")
        if norm in visited:
            continue
        visited.add(norm)

        try:
            html, text, kind = fetch_page(url)
        except Exception as e:
            print(f"    [!] fetch fallito per {url}: {e}")
            continue

        pages[url] = text[:MAX_CHARS_PER_PAGE]
        print(f"    (+) pagina letta ({kind}): {url}")

        if kind != "html":
            continue  # un PDF e' sempre una foglia: nessun link da seguire
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
    """Prova, con una VERA chiamata di prova a generateContent (non solo
    GET /models/{nome}, che si e' rivelato inaffidabile: puo' rispondere 200
    anche per modelli che poi rispondono 404 sulla generazione vera), ogni
    modello in GEMINI_MODEL_CANDIDATES in ordine, e fissa il primo che
    funziona davvero come GEMINI_MODEL per il resto del run. Se nessuno
    funziona, lo script prosegue comunque (ogni fonte fallira' e verra'
    riprovata al prossimo giro, senza corrompere gli hash)."""
    global GEMINI_MODEL
    probe_payload = {"contents": [{"parts": [{"text": "rispondi solo: ok"}]}]}

    for candidate in GEMINI_MODEL_CANDIDATES:
        try:
            resp = requests.post(_gemini_url(candidate), json=probe_payload, timeout=20)
        except requests.exceptions.RequestException as e:
            print(f"  [i] Modello '{candidate}' non raggiungibile ({e}), provo il successivo...")
            continue

        if resp.status_code == 200:
            GEMINI_MODEL = candidate
            print(f"Modello Gemini verificato e attivo per questo run: {GEMINI_MODEL}")
            return True

        if resp.status_code == 404:
            print(f"  [i] Modello '{candidate}' risponde 404 su generateContent, provo il successivo...")
            continue

        if resp.status_code in (429, 503):
            # il modello esiste ed e' valido, e' solo temporaneamente occupato:
            # lo teniamo come attivo, ci pensera' il retry in ask_gemini
            GEMINI_MODEL = candidate
            print(
                f"Modello Gemini scelto: {GEMINI_MODEL} (al momento {resp.status_code}, "
                "gestito dal retry durante il run)"
            )
            return True

        print(f"  [i] Modello '{candidate}' risposta inattesa ({resp.status_code}), provo il successivo...")

    print(
        "ATTENZIONE: nessuno dei modelli candidati "
        f"({', '.join(GEMINI_MODEL_CANDIDATES)}) risponde correttamente a "
        "generateContent in questo momento. Lo script proseguira' comunque: "
        "ogni fonte fallira' l'analisi ma NON verra' segnata come controllata, "
        "quindi verra' ritentata al prossimo giro senza perdere nulla."
    )
    return False


class GeminiCallFailedError(Exception):
    """Sollevata quando non siamo riusciti a ottenere una risposta valida da
    Gemini nonostante i retry. Va distinta da un 'trovate: []' legittimo:
    se la chiamata e' fallita, NON dobbiamo salvare il nuovo hash, altrimenti
    la fonte verrebbe marcata come 'gia' controllata' senza che nessuno
    l'abbia davvero analizzata, e il fallimento sparirebbe silenziosamente
    finche' il sito non cambia di nuovo."""


def ask_gemini(nome_fonte, categoria, frequenza, natura, combined_text):
    global _last_gemini_call_ts, GEMINI_MODEL

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

    # partiamo dal modello attualmente attivo (di solito quello scelto da
    # check_gemini_model, o l'ultimo che ha funzionato in questo run) e
    # giriamo sulla lista di candidati al massimo GEMINI_MAX_MODEL_ROTATIONS
    # volte, con solo GEMINI_ATTEMPTS_PER_MODEL tentativi brevi per modello:
    # se un modello e' saturo, meglio passare al successivo che insistere.
    start_idx = (
        GEMINI_MODEL_CANDIDATES.index(GEMINI_MODEL)
        if GEMINI_MODEL in GEMINI_MODEL_CANDIDATES else 0
    )
    total_models_to_try = len(GEMINI_MODEL_CANDIDATES) * GEMINI_MAX_MODEL_ROTATIONS

    for i in range(total_models_to_try):
        model = GEMINI_MODEL_CANDIDATES[(start_idx + i) % len(GEMINI_MODEL_CANDIDATES)]
        model_dead = False  # 404: non ha senso ritentare piu' volte lo stesso modello

        for attempt in range(1, GEMINI_ATTEMPTS_PER_MODEL + 1):
            elapsed = time.time() - _last_gemini_call_ts
            if elapsed < GEMINI_MIN_INTERVAL_SEC:
                time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)

            try:
                resp = requests.post(_gemini_url(model), json=payload, timeout=60)
            except requests.exceptions.RequestException as e:
                _last_gemini_call_ts = time.time()
                wait = GEMINI_BACKOFF_BASE_SEC * attempt
                print(
                    f"  [i] Errore di connessione verso Gemini ('{model}') per {nome_fonte} "
                    f"(tentativo {attempt}/{GEMINI_ATTEMPTS_PER_MODEL}): {e}. Riprovo tra {wait:.0f}s..."
                )
                time.sleep(wait)
                continue

            _last_gemini_call_ts = time.time()

            if resp.status_code == 404:
                print(f"  [i] Modello '{model}' risponde 404, passo al successivo candidato.")
                model_dead = True
                break  # niente altri tentativi su un modello che non esiste/non e' accessibile

            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else GEMINI_BACKOFF_BASE_SEC * attempt
                print(
                    f"  [i] Gemini {resp.status_code} su '{model}' per {nome_fonte} "
                    f"(tentativo {attempt}/{GEMINI_ATTEMPTS_PER_MODEL}), riprovo tra {wait:.0f}s..."
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            try:
                raw = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(raw)
                if model != GEMINI_MODEL:
                    print(f"  [i] Modello attivo aggiornato a '{model}' per il resto del run.")
                    GEMINI_MODEL = model
                return parsed.get("trovate", [])
            except (KeyError, IndexError, json.JSONDecodeError):
                print(f"  [!] Risposta Gemini non interpretabile per {nome_fonte}: {data}")
                raise GeminiCallFailedError(f"risposta non interpretabile: {data}")

        if not model_dead:
            print(f"  [i] Modello '{model}' ancora saturo dopo {GEMINI_ATTEMPTS_PER_MODEL} tentativi, passo al successivo.")

    raise GeminiCallFailedError(
        f"Gemini irraggiungibile per {nome_fonte}: tutti i modelli candidati "
        f"({', '.join(GEMINI_MODEL_CANDIDATES)}) hanno fallito"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.date.today().isoformat()
    check_gemini_model()
    if FORCE_REANALYZE:
        print(
            "MODALITA' RESET FORZATO ATTIVA (FORCE_REANALYZE): ignoro tutti gli "
            "hash salvati, ogni fonte con almeno una pagina raggiungibile verra' "
            "rianalizzata da zero con Gemini."
        )
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
        if old_hash_raw and not FORCE_REANALYZE:
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
        except GeminiCallFailedError as e:
            print(
                f"  [!] {e}. NON aggiorno l'hash: al prossimo giro questa fonte "
                "verrà rivista come 'cambiata' e Gemini verrà richiamato."
            )
            continue  # salta l'update_fonte_status: hash vecchio resta valido
        except Exception as e:
            print(
                f"  [!] Errore imprevisto chiamando Gemini: {e}. NON aggiorno l'hash."
            )
            continue

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

        # arriviamo qui solo se ask_gemini e' andata a buon fine (anche con
        # trovate=[], cioè "controllato, nulla di rilevante"): SOLO in
        # questo caso ha senso salvare il nuovo hash.
        try:
            update_fonte_status(page_id, new_hash_map, today)
        except Exception as e:
            print(f"  [!] Errore aggiornando Notion (Fonti): {e}")

    print("Scouting completato.")


if __name__ == "__main__":
    sys.exit(main())

"""
PhD Scouting Agent v2
---------------------
Legge il database "Fonti" su Notion, controlla le pagine (e i link alle call che
trova al loro interno), e scrive le nuove CFP / eventi nel database
"PhD Scouting Tracker" con Stato = "🆕 Da valutare", usando il template "CFP".

Novita' rispetto alla v1:
  - le pagine nuove nascono dal template "CFP" del Tracker
  - vengono aggiunti solo elementi con una data definita (niente TBA / date mancanti)
  - WikiCFP e PhilEvents hanno un lettore dedicato; per gli altri aggregatori c'e' un
    lettore generico a elenco
  - se Gemini fallisce, la fonte viene ricontrollata alla prossima esecuzione
    (prima l'errore veniva "dimenticato" e la CFP andava persa)
  - segue anche i link "Call for Papers" verso altri domini (es. siti di convegni)
  - controlla tutti i campi URL di una fonte, non solo "URL"
  - niente doppioni nel Tracker
  - --force (o FORCE_RECHECK=1) ignora l'hash e ricontrolla tutto

Variabili d'ambiente: NOTION_TOKEN, GEMINI_API_KEY, NOTION_DB_FONTI_ID,
NOTION_DB_TRACKER_ID, GEMINI_MODEL (opzionale), FORCE_RECHECK (opzionale).
"""

import os
import re
import sys
import json
import time
import io
import hashlib
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

try:
    from pypdf import PdfReader  # per leggere le call pubblicate come PDF
except Exception:
    PdfReader = None

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DB_FONTI_ID = os.environ["NOTION_DB_FONTI_ID"]
DB_TRACKER_ID = os.environ["NOTION_DB_TRACKER_ID"]
# Modelli Gemini in ordine di preferenza. gemini-2.0-flash e' stato SPENTO da Google il
# 1 giugno 2026 (ogni chiamata dava errore). Se GEMINI_MODEL e' impostato viene provato per
# primo; se non esiste piu', lo script passa da solo al successivo.
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL")] if m] + [
    "gemini-3.1-flash-lite", "gemini-3.5-flash"]
_model_idx = 0

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
# versione piu' recente: serve per template e query sul Tracker
NOTION_HEADERS_NEW = {**NOTION_HEADERS, "Notion-Version": "2025-09-03"}



def gemini_url(model):
    return (f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PhD-Scouting-Agent/2.0"
    )
}

try:
    sys.stdout.reconfigure(line_buffering=True)  # il log appare subito anche se il job viene interrotto
except Exception:
    pass

TODAY = datetime.date.today()
START = time.time()
# tempo massimo di lavoro: poi lo script si ferma da solo e le fonti rimaste
# vengono riprese alla prossima esecuzione (il job su GitHub deve durare di piu')
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_MIN", "20")) * 60
# le fonti analizzate con successo da questa versione salvano un hash con questo prefisso:
# cosi' quelle mai analizzate (hash vecchio) vengono riprese in automatico, senza --force
HASH_PREFIX = "v2:"
_gemini_dead = False


def elapsed():
    t = int(time.time() - START)
    return f"{t // 60:02d}:{t % 60:02d}"


TIPO_OPZIONI = ["Journal CFP", "Conferenza", "Summer School",
                "Evento gratuito online", "Lezione/Seminario"]
TIPI_CON_SCADENZA = {"Journal CFP", "Conferenza"}  # questi richiedono una scadenza definita
TEMPLATE_NAME = "CFP"

LINK_KEYWORDS = ["call", "cfp", "news", "announce", "deadline", "papers",
                 "submission", "event", "conference", "bando", "scadenza", "convegn"]
MAX_SUBLINKS_PER_FONTE = 6
CFP_CONTEXT_RE = re.compile(
    r"call\s+for\s+(papers?|abstracts?|contributions?|proposals?|submissions?|panels?)"
    r"|\bcfp\b|convocatoria|appel\s+à", re.I)

CHUNK_CHARS = 15000      # dimensione di ogni pezzo di testo mandato a Gemini
MAX_CHUNKS = 3           # massimo pezzi per pagina
GEMINI_DELAY = 4         # secondi tra una chiamata e l'altra (limiti del piano gratuito)

GENERIC_AGGREGATORS = ["iass-ais.org"]   # elenchi di post senza lettore dedicato
WIKICFP_MAX_PAGES = 3
PHILEVENTS_MAX_PAGES = 3

# filtro "grossolano" per gli aggregatori strutturati (WikiCFP, PhilEvents): serve solo
# a non riempire il Tracker di CFP lontanissime dai tuoi temi
RELEVANT_RE = re.compile(
    r"\b(semio\w*|cognit\w*|mind|consciousness|phenomenolog\w*|embod\w*|enactiv\w*|"
    r"language|linguist\w*|semantic\w*|meaning|narrat\w*|metaphor\w*|myth\w*|"
    r"communicat\w*|interpret\w*|hermeneut\w*|pragmat\w*|peirce|"
    r"artificial|ai|llms?|generative|machine|digital humanities)\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def valid_date(s):
    """'YYYY-MM-DD' -> date, altrimenti None (gestisce null, TBA, testo libero)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def parse_en_date(s):
    """'Dec 1, 2026' -> date."""
    m = re.search(r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s or "")
    if not m or m.group(1).lower() not in MONTHS:
        return None
    try:
        return datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Notion: Fonti
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
    arr = prop.get("title" if kind == "title" else "rich_text", [])
    return "".join(t["plain_text"] for t in arr)


def get_prop_checkbox(page, name):
    prop = page["properties"].get(name)
    return bool(prop and prop.get("checkbox"))


def collect_urls(page):
    """Tutti i campi di tipo URL della fonte (es. sito della rivista + pagina della CFP)."""
    urls, seen = [], set()
    for prop in page["properties"].values():
        if prop.get("type") == "url" and prop.get("url"):
            u = prop["url"].strip()
            if u and u.rstrip("/") not in seen:
                seen.add(u.rstrip("/"))
                urls.append(u)
    return urls


def update_fonte_status(page_id, new_hash, checked_iso_date):
    """Se new_hash e' None aggiorna solo la data dell'ultimo controllo."""
    props = {"Ultimo Controllo": {"date": {"start": checked_iso_date}}}
    if new_hash:
        props["Ultimo Hash"] = {"rich_text": [{"text": {"content": new_hash[:2000]}}]}
    resp = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS,
                          json={"properties": props}, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Notion: Tracker (template, dedupe, creazione)
# ---------------------------------------------------------------------------

_TRACKER = {}


def tracker_info():
    """id del data source del Tracker + id del template 'CFP' (se esiste)."""
    if _TRACKER:
        return _TRACKER
    _TRACKER.update({"id": None, "tpl": None, "props": None})
    try:
        r = requests.get(f"{NOTION_API}/databases/{DB_TRACKER_ID}",
                         headers=NOTION_HEADERS_NEW, timeout=30)
        r.raise_for_status()
        ds_id = r.json()["data_sources"][0]["id"]
        _TRACKER["id"] = ds_id
        sch = requests.get(f"{NOTION_API}/data_sources/{ds_id}", headers=NOTION_HEADERS_NEW, timeout=30)
        sch.raise_for_status()
        _TRACKER["props"] = sch.json().get("properties", {})
        t = requests.get(f"{NOTION_API}/data_sources/{ds_id}/templates",
                         headers=NOTION_HEADERS_NEW, timeout=30)
        t.raise_for_status()
        tpls = t.json().get("templates", [])
        match = next((x for x in tpls if norm(x["name"]) == norm(TEMPLATE_NAME)), None) \
            or next((x for x in tpls if norm(TEMPLATE_NAME) in norm(x["name"])), None)
        if match:
            _TRACKER["tpl"] = match["id"]
            print(f"Template '{match['name']}' trovato nel Tracker.")
        else:
            print(f"[!] Template '{TEMPLATE_NAME}' non trovato: creo le pagine senza template.")
    except Exception as e:
        print(f"[!] Template non disponibile ({e}): creo le pagine senza template.")
    return _TRACKER


def tracker_exists(item, fonte_url):
    info = tracker_info()
    if not info["id"]:
        return False
    conds = [{"property": "Nome", "title": {"equals": item["titolo"][:200]}}]
    if item.get("link"):  # il link vale come identificatore solo se e' specifico dell'item
        conds.append({"property": "Fonte", "url": {"equals": item["link"]}})
    flt = {"or": conds} if len(conds) > 1 else conds[0]
    try:
        r = requests.post(f"{NOTION_API}/data_sources/{info['id']}/query",
                          headers=NOTION_HEADERS_NEW,
                          json={"filter": flt, "page_size": 1}, timeout=30)
        return r.status_code == 200 and bool(r.json().get("results"))
    except Exception:
        return False


def _find_prop(schema, names, types):
    for n in names:
        for k, v in schema.items():
            if norm(k) == norm(n) and v.get("type") in types:
                return k
    return None


_WARNED = set()


def build_props(item, fonte_url, found_iso):
    """Costruisce le proprieta' usando SOLO quelle che esistono nel Tracker (se una e' stata
    rinominata o cancellata, la salta e lo scrive nel log invece di far fallire tutto)."""
    schema = tracker_info().get("props")
    link = item.get("link") or fonte_url
    if not schema:  # schema non leggibile: uso i nomi storici
        return {
            "Nome": {"title": [{"text": {"content": item["titolo"][:200]}}]},
            "Fonte": {"url": link}, "Tipo": {"select": {"name": item["tipo"]}},
            "Stato": {"select": {"name": "🆕 Da valutare"}},
            "Scadenza": {"date": {"start": item["scadenza"]}},
            "Note": {"rich_text": [{"text": {"content": (item.get("note") or "")[:1900]}}]},
        }
    props = {}

    def put(label, names, types, builder, required=False):
        k = _find_prop(schema, names, types)
        if k:
            props[k] = builder(schema[k])
        elif label not in _WARNED:
            _WARNED.add(label)
            print(f"  [!] Nel Tracker non esiste la proprieta' '{label}': la salto.")

    tk = next((k for k, v in schema.items() if v.get("type") == "title"), None)
    if tk:
        props[tk] = {"title": [{"text": {"content": item["titolo"][:200]}}]}
    put("Fonte", ["Fonte", "Link", "URL"], ["url"], lambda d: {"url": link})
    put("Tipo", ["Tipo"], ["select"], lambda d: {"select": {"name": item["tipo"]}})

    def stato(d):
        opts = (d.get(d["type"]) or {}).get("options", [])
        o = next((x["name"] for x in opts if "da valutare" in norm(x["name"])), "🆕 Da valutare")
        return {d["type"]: {"name": o}}
    put("Stato", ["Stato", "Status"], ["select", "status"], stato)
    put("Trovato il", ["Trovato il", "Trovato"], ["date"], lambda d: {"date": {"start": found_iso}})
    put("Costo", ["Costo"], ["select"], lambda d: {"select": {"name": "Non specificato"}})
    put("Scadenza", ["Scadenza"], ["date"], lambda d: {"date": {"start": item["scadenza"]}})
    if item.get("note"):
        put("Note", ["Note"], ["rich_text"],
            lambda d: {"rich_text": [{"text": {"content": item["note"][:1900]}}]})
    return props


def create_tracker_entry(item, fonte_url, found_iso_date):
    props = build_props(item, fonte_url, found_iso_date)
    info = tracker_info()
    if info["id"] and info["tpl"]:
        body = {"parent": {"type": "data_source_id", "data_source_id": info["id"]},
                "properties": props,
                "template": {"type": "template_id", "template_id": info["tpl"]}}
        r = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS_NEW, json=body, timeout=60)
        if r.status_code == 200:
            return "con template"
        print(f"  [!] Template non applicato ({r.text[:200]}), riprovo senza.")

    body = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    r = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Notion {r.status_code}: {r.text[:250]}")
    return "senza template"


# ---------------------------------------------------------------------------
# Fetch pagine normali (+ link alle call)
# ---------------------------------------------------------------------------

def fetch_html(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_text_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


BAD_LINK_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|zip|docx?|xlsx?|xml)(\?|$)|/feed|/atom|/rss|gateway/plugin|"
    r"drive\.google\.com|facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|youtube\.com", re.I)
INDEX_RE = re.compile(r"call|cfp|announce|avis|noticia|news|bando|convocat", re.I)
_SEEN_PAGES = set()   # pagine gia' lette per la fonte corrente (evita doppioni)


def extract_relevant_links(base_url, html, max_links=MAX_SUBLINKS_PER_FONTE):
    """Prima i link accanto a 'Call for Papers' (anche su altri domini, es. il sito
    del convegno), poi quelli dello stesso dominio con parole chiave utili."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    priority, normal, seen = [], [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        abs_url = urljoin(base_url, href).split("#")[0]
        if urlparse(abs_url).scheme not in ("http", "https") or BAD_LINK_RE.search(abs_url):
            continue
        if abs_url in seen or abs_url.rstrip("/") == base_url.rstrip("/"):
            continue
        label = a.get_text(" ", strip=True) or ""
        ctx = a.parent.get_text(" ", strip=True)[:300] if a.parent else label
        same_domain = urlparse(abs_url).netloc == base_domain
        if is_pdf(abs_url) and not (CFP_CONTEXT_RE.search(ctx) or CFP_CONTEXT_RE.search(label)
                                    or re.search(r"call|cfp|bando|convocat", abs_url + " " + label, re.I)):
            continue

        if CFP_CONTEXT_RE.search(ctx) or CFP_CONTEXT_RE.search(label):
            seen.add(abs_url)
            priority.append(abs_url)
        elif same_domain and any(k in f"{href} {label}".lower() for k in LINK_KEYWORDS):
            seen.add(abs_url)
            normal.append(abs_url)

    return (priority + normal)[:max_links]


def content_links(base_url, html, limit=6):
    """Link 'da articolo' dentro una pagina elenco (es. 'Avisos y call for papers' che
    elenca solo i titoli delle singole call): ignora menu, header e footer."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["header", "nav", "footer", "aside", "script", "style"]):
        t.decompose()
    area = soup.find("main") or soup.find("article") or soup.find(class_=re.compile(r"entry-content|post-content|content", re.I)) or soup
    domain = urlparse(base_url).netloc
    out = []
    for a in area.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"]).split("#")[0]
        if (len(label) < 12 or urlparse(href).netloc != domain or BAD_LINK_RE.search(href)
                or is_pdf(href) or href.rstrip("/") == base_url.rstrip("/") or href in out):
            continue
        out.append(href)
        if len(out) >= limit:
            break
    return out


def is_pdf(url):
    return bool(re.search(r"\.pdf(\?|$)", url, re.I))


def fetch_pdf_text(url, max_pages=6):
    if PdfReader is None:
        raise RuntimeError("pypdf non installato (aggiungi 'pypdf' a requirements.txt)")
    r = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    r.raise_for_status()
    if len(r.content) > 6_000_000:
        raise RuntimeError("PDF troppo grande")
    reader = PdfReader(io.BytesIO(r.content))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages[:max_pages])


def pdf_links(base_url, html, limit=2):
    """PDF che sembrano il testo di una call ('Download Call for Papers here')."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        label = a.get_text(" ", strip=True)
        if is_pdf(href) and href not in out and re.search(
                r"call|cfp|download|descarg|scarica|bando|convocat|programme|program", href + " " + label, re.I):
            out.append(href)
        if len(out) >= limit:
            break
    return out


def main_text_from_html(html):
    """Solo il contenuto principale (senza menu, header e footer): per le pagine dei singoli post."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "header", "nav", "footer", "aside"]):
        t.decompose()
    area = (soup.find("article") or soup.find("main")
            or soup.find(class_=re.compile(r"entry-content|post-content", re.I)) or soup)
    text = re.sub(r"\n{2,}", "\n", area.get_text(separator="\n"))
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def page_of(full_text, evidence, default=None):
    """Da quale pagina collegata viene la frase che contiene la data (serve per il link giusto)."""
    if not evidence:
        return default
    idx = full_text.lower().find(evidence.lower()[:40])
    if idx < 0:
        return default
    last = default
    for m in re.finditer(r"--- Pagina collegata: (\S+) ---", full_text):
        if m.start() < idx:
            last = m.group(1)
        else:
            break
    return last


def fetch_source_text(url):
    main_html = fetch_html(url)
    _SEEN_PAGES.add(url.rstrip("/"))
    main_text = extract_text_from_html(main_html)
    if len(main_text) < 400:
        print(f"  [!] Testo molto corto ({len(main_text)} caratteri): la pagina potrebbe "
              f"caricare i contenuti con JavaScript.")
    combined = main_text
    try:
        sublinks = extract_relevant_links(url, main_html)
    except Exception as e:
        print(f"  [!] Errore estraendo i link interni: {e}")
        sublinks = []

    queue, depth2, pdfs = [], 0, 0
    # se la pagina indicata e' essa stessa un elenco di call (es. 'Avisos y call for papers'),
    # segue per prime le singole voci
    if INDEX_RE.search(urlparse(url).path):
        for item_link in content_links(url, main_html):
            queue.append((item_link, 2))
            depth2 += 1
    queue += [(l, 1) for l in sublinks]

    while queue:
        link, depth = queue.pop(0)
        if link.rstrip("/") in _SEEN_PAGES:
            continue
        _SEEN_PAGES.add(link.rstrip("/"))
        try:
            if is_pdf(link):
                if pdfs >= 3:
                    continue
                pdfs += 1
                combined += f"\n\n--- Pagina collegata: {link} ---\n{fetch_pdf_text(link)}"
                print(f"  (+) PDF letto: {link}")
                continue
            sub_html = fetch_html(link)
            combined += f"\n\n--- Pagina collegata: {link} ---\n{extract_text_from_html(sub_html)}"
            print(f"  (+) pagina collegata controllata: {link}")
        except Exception as e:
            print(f"  [!] Errore nel fetch di {link}: {e}")
            continue
        # pagina elenco di call/news: segue anche le singole voci
        if depth == 1 and INDEX_RE.search(urlparse(link).path) and depth2 < 6:
            for item_link in content_links(link, sub_html):
                if depth2 >= 6:
                    break
                if item_link.rstrip("/") not in _SEEN_PAGES:
                    queue.append((item_link, 2))
                    depth2 += 1
        # la scadenza e' spesso solo nel PDF della call ("Download Call for Papers here")
        if depth <= 2:
            for pl in pdf_links(link, sub_html):
                if pl.rstrip("/") not in _SEEN_PAGES:
                    queue.append((pl, 3))
    return combined


# ---------------------------------------------------------------------------
# Lettori dedicati agli aggregatori
# ---------------------------------------------------------------------------

def wikicfp_items(html, page_url):
    """Ritorna (items, ci_sono_gia_scadute). Struttura WikiCFP: una riga col titolo
    + descrizione, e la riga sotto con When / Where / Deadline."""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.select('a[href*="event.showcfp"]'):
        tr = a.find_parent("tr")
        if not tr:
            continue
        link = urljoin(page_url, a["href"])
        if link in seen:
            continue
        seen.add(link)
        td = a.find_parent("td")
        desc_td = td.find_next_sibling("td") if td else None
        desc = desc_td.get_text(" ", strip=True) if desc_td else ""
        nxt = tr.find_next_sibling("tr")
        cells = [c.get_text(" ", strip=True) for c in nxt.find_all("td")] if nxt else []
        when, where, deadline = (cells + ["", "", ""])[:3]
        d = parse_en_date(deadline)
        if not d or d < TODAY:
            continue
        short = a.get_text(" ", strip=True)
        titolo = desc or short
        is_journal = when.strip() in ("", "N/A") and re.search(r"journal|issue|volume|special", desc, re.I)
        items.append({
            "titolo": titolo[:200],
            "tipo": "Journal CFP" if is_journal else "Conferenza",
            "scadenza": d.isoformat(),
            "link": link,
            "note": f"WikiCFP ({short}). Quando: {when}. Dove: {where}.",
            "_text": f"{short} {desc}",
        })
    return items, "Expired CFPs" in soup.get_text()


def parse_wikicfp(url):
    items = []
    for page in range(1, WIKICFP_MAX_PAGES + 1):
        u = url if page == 1 else f"{url}&page={page}"
        batch, expired = wikicfp_items(fetch_html(u), u)
        items += batch
        if not batch or expired:  # le CFP aperte sono in cima: se finiscono, ci fermiamo
            break
    if not items:
        print("  [!] WikiCFP: nessuna CFP aperta letta (struttura della pagina cambiata?).")
    return [i for i in items if RELEVANT_RE.search(i["_text"])]


PHIL_DATE_RE = re.compile(
    r"^\s*(Today|Tomorrow|Ongoing)?\s*(\d{1,2})\s+([A-Z][a-z]{2})(?:\s+(\d{4}))?\s*$")


def phil_label_to_date(label):
    m = PHIL_DATE_RE.match(label or "")
    if not m:
        return None
    word, day, mon, year = m.groups()
    if word == "Today":
        return TODAY
    if word == "Tomorrow":
        return TODAY + datetime.timedelta(days=1)
    if word == "Ongoing" or mon.lower() not in MONTHS:
        return None
    y = int(year) if year else TODAY.year
    try:
        d = datetime.date(y, MONTHS[mon.lower()], int(day))
    except ValueError:
        return None
    if not year and d < TODAY:
        try:
            d = d.replace(year=y + 1)
        except ValueError:
            return None
    return d


def philevents_items(html, page_url, tipo):
    """Ogni evento: etichetta con la data subito prima del link /event/show/ID.
    Per gli elenchi di CFP la data mostrata e' la scadenza."""
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="/event/show/"]')
    items, seen = [], set()
    for i, a in enumerate(links):
        href = urljoin(page_url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        label = a.find_previous(string=PHIL_DATE_RE)
        d = phil_label_to_date(label)
        if not d:
            continue
        nxt = links[i + 1] if i + 1 < len(links) else None
        ctx = []
        for s in a.find_all_next(string=True, limit=60):
            if s.find_parent("a") is a:
                continue
            if PHIL_DATE_RE.match(s) or (nxt is not None and s.find_parent("a") is nxt):
                break
            s = s.strip()
            if s:
                ctx.append(s)
        title = re.sub(r"^(CFP|CFA|Call for (Papers?|Abstracts?|Submissions?))\s*:\s*", "",
                       a.get_text(" ", strip=True), flags=re.I)
        extra = " ".join(ctx)
        items.append({
            "titolo": title[:200], "tipo": tipo, "scadenza": d.isoformat(), "link": href,
            "note": f"PhilEvents. {extra[:300]}", "_text": f"{title} {extra}",
        })
    return items


def parse_philevents(url):
    base = url.split("?")[0]
    if "cfp" not in base.lower():
        print("  [!] PhilEvents: questa pagina elenca eventi, non CFP. Usa come fonte "
              "https://philevents.org/conferenceCfps (e/o /publicationCfps).")
        return []
    tipo = "Journal CFP" if "publication" in base.lower() else "Conferenza"
    items = []
    for n in range(PHILEVENTS_MAX_PAGES):
        u = f"{base}?offset={n * 50}&max=50"
        batch = philevents_items(fetch_html(u), u, tipo)
        items += batch
        if len(batch) < 40:  # ultima pagina
            break
    if not items:
        print("  [!] PhilEvents: nessuna CFP letta (struttura della pagina cambiata?).")
    return [i for i in items if RELEVANT_RE.search(i["_text"])]


POST_RE = re.compile(r"call|cfp|congress|conference|symposium|seminar|convocat|appel|contribution|workshop|school", re.I)


def fetch_listing_text(url, max_posts=12):
    """Aggregatori senza lettore dedicato (es. IASS): l'elenco mostra solo titolo ed estratto,
    la scadenza sta nel post (o nel suo PDF). Quindi apre i post piu' recenti e legge quelli."""
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    domain = urlparse(url).netloc
    posts, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        link = urljoin(url, a["href"]).split("#")[0]
        if (len(title) < 15 or link in seen or urlparse(link).netloc != domain
                or BAD_LINK_RE.search(link) or is_pdf(link) or not POST_RE.search(title)
                or any(x in link for x in ("/category/", "/page/", "/tag/", "/author/"))
                or link.rstrip("/") == url.rstrip("/")):
            continue
        seen.add(link)
        posts.append((title, link))
        if len(posts) >= max_posts:
            break
    if not posts:
        print("  [!] Elenco vuoto: struttura della pagina non riconosciuta.")
    blocks = []
    for title, link in posts:
        try:
            html = fetch_html(link)
            body = main_text_from_html(html)[:3000]
            blocks.append(f"--- Pagina collegata: {link} ---\n{title}\n{body}")
            for pl in pdf_links(link, html, limit=1):
                try:
                    blocks.append(f"--- Pagina collegata: {pl} ---\n{fetch_pdf_text(pl)[:3000]}")
                except Exception as e:
                    print(f"  [!] PDF non letto ({pl}): {e}")
        except Exception as e:
            print(f"  [!] Errore nel fetch di {link}: {e}")
    print(f"  Elenco: letti {len(blocks)} blocchi da {len(posts)} post")
    return "\n\n".join(blocks)


def fetch_any(url):
    """-> ('items', lista) per gli aggregatori strutturati, ('text', testo) per il resto."""
    host = urlparse(url).netloc.lower()
    if "wikicfp.com" in host:
        return "items", parse_wikicfp(url)
    if "philevents.org" in host:
        return "items", parse_philevents(url)
    if any(h in host for h in GENERIC_AGGREGATORS):
        return "text", fetch_listing_text(url)
    return "text", fetch_source_text(url)


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

PROMPT = """Sei un assistente che aiuta un dottorando in semiotica cognitiva e filosofia della
mente a monitorare call for papers, conferenze, summer school e seminari.
Interessi: semiotica (anche semiotica della cultura, dei miti, dei media), semiotica
cognitiva, filosofia della mente, linguistica cognitiva, fenomenologia, linguaggio e IA,
narrazione, metafora, interpretazione.

Oggi e' __OGGI__. Analizza il testo estratto dalla pagina "__NOME__" (__URL__). Puo'
contenere molto rumore (menu, footer): concentrati solo su annunci di Call for Papers,
conferenze, summer school, workshop o seminari.

REGOLE
- Includi SOLO opportunita' con una data DEFINITA e futura, riferita ESPLICITAMENTE a quella
  specifica call/evento nel testo. Scarta TBA, "to be announced", "coming soon", date mancanti o passate.
- NON dedurre mai una data: ignora date generiche o ricorrenti ("di solito entro il 1 febbraio",
  descrizioni di come funziona di norma un processo, FAQ), pagine su come candidarsi a ORGANIZZARE o
  OSPITARE un convegno (co-chair, host), call per revisori/volontari/membri, premi, borse, grant,
  iscrizioni all'associazione.
- "evidenza" = la frase del testo che riporta la data, copiata TESTUALMENTE (max 200 caratteri,
  senza puntini di sospensione). Se non riesci a copiare una frase che contiene la data, NON includere l'elemento.
- "scadenza" = ultimo giorno per inviare abstract/paper (YYYY-MM-DD). Se non e' indicata ma
  l'evento ha una data precisa, lasciala null e compila "data_evento".
- "data_evento" = primo giorno dell'evento (YYYY-MM-DD) oppure null.
- Se una data ha solo giorno e mese, scegli l'anno che la rende la prima data futura.
- Le date possono essere in qualsiasi lingua (es. "15 de marzo de 2027", "15 mars 2027").
- Se il tema e' solo parzialmente affine ai miei interessi, includila comunque: la valutero' io.
  Escludi solo cio' che e' chiaramente lontano (es. medicina, ingegneria).
- "link": URL diretto alla call se presente nel testo, altrimenti null.
- Se non trovi nulla: {"trovate": []}

Rispondi SOLO con JSON in questo formato (nessun testo fuori dal JSON):
{"trovate": [{"titolo": "string breve", "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario", "scadenza": "YYYY-MM-DD o null", "data_evento": "YYYY-MM-DD o null", "evidenza": "frase copiata dal testo", "link": "URL o null", "note": "1-2 frasi in italiano su tema e rilevanza"}]}

TESTO DELLA PAGINA:
__TESTO__
"""


def gemini_request(payload):
    """Chiama Gemini. Se il modello non esiste piu' o ha finito la quota passa al successivo;
    se non ce n'e' nessuno, disattiva Gemini per tutta l'esecuzione (niente attese inutili)."""
    global _model_idx, _gemini_dead
    if _gemini_dead:
        raise RuntimeError("Gemini disattivato per questa esecuzione (errori ripetuti)")
    while _model_idx < len(GEMINI_MODELS):
        model = GEMINI_MODELS[_model_idx]
        resp = None
        for attempt in range(2):
            resp = requests.post(gemini_url(model), json=payload, timeout=60)
            if resp.status_code not in (429, 500, 502, 503):
                break
            quota_over = resp.status_code == 429 and ("limit: 0" in resp.text or "PerDay" in resp.text)
            if quota_over or attempt == 1:
                break
            m = re.search(r'"retryDelay":\s*"(\d+)', resp.text)
            wait = min((int(m.group(1)) + 2) if m else 6, 30)
            print(f"  [!] Gemini occupato ({resp.status_code}), riprovo tra {wait}s...")
            time.sleep(wait)
        if (resp.status_code in (404, 429, 500, 502, 503)
                or (resp.status_code == 400 and "model" in resp.text.lower())):
            print(f"  [!] Modello {model} non utilizzabile ({resp.status_code}): provo il successivo.")
            _model_idx += 1
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    _gemini_dead = True
    raise RuntimeError("nessun modello Gemini utilizzabile (quota finita o modelli spenti): "
                       "imposta GEMINI_MODEL con un modello attivo")


def needs_gemini(url):
    host = urlparse(url).netloc.lower()
    return not ("wikicfp.com" in host or "philevents.org" in host)


def gemini_check():
    """Prova veloce all'avvio: dice subito se Gemini funziona e con quale modello."""
    try:
        gemini_request({"contents": [{"parts": [{"text": "Rispondi solo con: ok"}]}]})
        print(f"Gemini OK (modello: {GEMINI_MODELS[_model_idx]})")
        return True
    except Exception as e:
        print(f"[!!] GEMINI NON FUNZIONA: {e}")
        print("[!!] Le fonti che richiedono Gemini non verranno analizzate (WikiCFP e PhilEvents si').")
        return False


def ask_gemini(nome, url, text):
    prompt = (PROMPT.replace("__OGGI__", TODAY.isoformat()).replace("__NOME__", nome)
              .replace("__URL__", url).replace("__TESTO__", text))
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    data = gemini_request(payload)
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        res = json.loads(raw).get("trovate", [])
        ntext = norm(text)
        twords = set(ntext.split())
        for it in res:
            ev = norm(it.get("evidenza") or "")
            ew = ev.split()
            it["_verificata"] = bool(ev) and (
                ev in ntext or (len(ew) >= 4 and sum(w in twords for w in ew) / len(ew) >= 0.85))
        return res
    except (KeyError, IndexError, json.JSONDecodeError):
        raise RuntimeError(f"risposta di Gemini non interpretabile: {str(data)[:200]}")


def chunk_text(text, size=CHUNK_CHARS, overlap=500):
    return [text[i:i + size] for i in range(0, len(text), size - overlap)] or [""]


# ---------------------------------------------------------------------------
# Regole sulle date
# ---------------------------------------------------------------------------

def finalize_item(item):
    """Applica le regole: data definita e futura. Ritorna (item, None) o (None, motivo)."""
    titolo = (item.get("titolo") or "").strip()
    if not titolo:
        return None, "senza titolo"
    if item.get("_verificata") is False:
        return None, "data non verificabile nel testo, forse dedotta"
    tipo = item.get("tipo") if item.get("tipo") in TIPO_OPZIONI else "Journal CFP"
    scad, evento = valid_date(item.get("scadenza")), valid_date(item.get("data_evento"))
    note = (item.get("note") or "").strip()

    if scad and scad >= TODAY:
        data = scad
    elif tipo not in TIPI_CON_SCADENZA and evento and evento >= TODAY:
        data = evento
        note = (note + " (La data indicata e' quella dell'evento, non una scadenza.)").strip()
    else:
        return None, "senza data definita o gia' passata"

    ev = (item.get("evidenza") or "").strip()
    if ev:
        note = (note + f" Data trovata in: «{ev[:160]}»").strip()
    out = dict(item, titolo=titolo, tipo=tipo, scadenza=data.isoformat(), note=note)
    return out, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    force = "--force" in sys.argv or os.environ.get("FORCE_RECHECK", "").lower() in ("1", "true", "yes")
    today_iso = TODAY.isoformat()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive da controllare: {len(fonti)}" + (" (controllo forzato)" if force else ""))
    tracker_info()
    gemini_check()

    added_total, seen_titles = 0, set()

    for idx, page in enumerate(fonti):
        if time.time() - START > MAX_RUNTIME:
            print(f"[!] Tempo massimo raggiunto ({elapsed()}): restano {len(fonti) - idx} fonti. "
                  f"Verranno riprese alla prossima esecuzione (lancia di nuovo il workflow).")
            break
        page_id = page["id"]
        nome = get_prop_text(page, "Nome", kind="title") or "(senza nome)"
        urls = collect_urls(page)
        old_hash = get_prop_text(page, "Ultimo Hash")

        if not urls:
            print(f"- {nome}: nessun URL, salto.")
            continue
        if get_prop_checkbox(page, "Richiede Browser"):
            print(f"- {nome}: richiede browser (JS), non supportato, salto.")
            continue

        if _gemini_dead and any(needs_gemini(u) for u in urls):
            print(f"- {nome}: salto (Gemini non disponibile), riprovera' la prossima volta.")
            continue

        _SEEN_PAGES.clear()
        print(f"- [{elapsed()}] {nome}: {len(urls)} indirizzo/i")
        parts = []  # (url, tipo, contenuto)
        for u in urls:
            try:
                kind, payload = fetch_any(u)
                parts.append((u, kind, payload))
            except Exception as e:
                print(f"  [!] Errore nel fetch di {u}: {e}")
        if not parts:
            continue

        canon = "\n".join(
            "\n".join(sorted(f"{i['link']}|{i['scadenza']}" for i in p)) if k == "items" else p
            for _, k, p in parts)
        new_hash = HASH_PREFIX + hash_text(canon)

        if new_hash == old_hash and not force:
            print("  Nessuna modifica rilevata.")
            try:
                update_fonte_status(page_id, None, today_iso)
            except Exception as e:
                print(f"  [!] Errore aggiornando Notion (Fonti): {e}")
            continue

        print("  Cambiamento rilevato, analizzo..." if not force else "  Analisi forzata...")
        all_ok, found = True, []
        for u, kind, payload in parts:
            if kind == "items":
                print(f"  {u}: {len(payload)} voci lette dall'aggregatore")
                found += payload
                continue
            for n, chunk in enumerate(chunk_text(payload)[:MAX_CHUNKS], 1):
                if len(chunk.strip()) < 50:
                    continue
                try:
                    res = ask_gemini(nome, u, chunk)
                    print(f"  {u} (parte {n}): Gemini ha trovato {len(res)} voci")
                    for it in res:
                        if not it.get("link"):
                            it["link"] = page_of(payload, it.get("evidenza"), None)
                    found += res
                except Exception as e:
                    all_ok = False
                    print(f"  [!] Errore Gemini: {e}")
                time.sleep(GEMINI_DELAY)

        for raw in found:
            item, why = finalize_item(raw)
            if not item:
                print(f"  - scartata ({why}; scadenza={raw.get('scadenza')}, "
                      f"evento={raw.get('data_evento')}): {(raw.get('titolo') or '?')[:70]}")
                continue
            key = norm(item["titolo"])
            if key in seen_titles or tracker_exists(item, urls[0]):
                print(f"  = gia' presente: {item['titolo'][:70]}")
                continue
            try:
                how = create_tracker_entry(item, urls[0], today_iso)
                seen_titles.add(key)
                added_total += 1
                print(f"  -> Aggiunta ({how}): {item['titolo'][:70]} [{item['scadenza']}]")
            except Exception as e:
                all_ok = False
                print(f"  [!] Errore creando la pagina nel Tracker: {e}")

        try:
            if all_ok:
                update_fonte_status(page_id, new_hash, today_iso)
            else:
                update_fonte_status(page_id, None, today_iso)
                print("  [!] Hash NON aggiornato: la fonte verra' ricontrollata alla prossima esecuzione.")
        except Exception as e:
            print(f"  [!] Errore aggiornando Notion (Fonti): {e}")

    print(f"Scouting completato. Nuove voci aggiunte: {added_total}.")


if __name__ == "__main__":
    sys.exit(main())

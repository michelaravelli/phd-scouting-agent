"""
PhD Scouting Agent
-------------------
Legge il database "Fonti" su Notion, controlla se le pagine sono cambiate
rispetto all'ultimo controllo, e se sì chiede a Gemini di capire se è
comparsa una nuova Call for Papers / conferenza / summer school.
I risultati confermati vengono scritti nel database "PhD Scouting Tracker"
con Stato = "🆕 Da valutare", pronti per il tuo filtro manuale.

Variabili d'ambiente richieste (vedi .env.example):
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
import hashlib
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DB_FONTI_ID = os.environ["NOTION_DB_FONTI_ID"]
DB_TRACKER_ID = os.environ["NOTION_DB_TRACKER_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

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
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PhD-Scouting-Agent/1.0"
    )
}

TIPO_OPZIONI = [
    "Journal CFP",
    "Conferenza",
    "Summer School",
    "Evento gratuito online",
    "Lezione/Seminario",
]

# Parole chiave usate per riconoscere, tra i link della pagina principale di una
# fonte, quelli che probabilmente portano a call for papers / news / annunci.
LINK_KEYWORDS = [
    "call", "cfp", "news", "announce", "deadline", "papers",
    "submission", "event", "conference", "bando", "scadenza", "convegn",
]
MAX_SUBLINKS_PER_FONTE = 5


# ---------------------------------------------------------------------------
# Notion helpers
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
    if kind == "title":
        arr = prop.get("title", [])
    else:
        arr = prop.get("rich_text", [])
    return "".join(t["plain_text"] for t in arr)


def get_prop_url(page, name):
    prop = page["properties"].get(name)
    return prop.get("url") if prop else None


def get_prop_checkbox(page, name):
    prop = page["properties"].get(name)
    return bool(prop and prop.get("checkbox"))


def update_fonte_status(page_id, new_hash, checked_iso_date):
    url = f"{NOTION_API}/pages/{page_id}"
    payload = {
        "properties": {
            "Ultimo Hash": {"rich_text": [{"text": {"content": new_hash[:2000]}}]},
            "Ultimo Controllo": {"date": {"start": checked_iso_date}},
        }
    }
    resp = requests.patch(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


def create_tracker_entry(item, fonte_url, found_iso_date):
    tipo = item.get("tipo") if item.get("tipo") in TIPO_OPZIONI else "Journal CFP"
    props = {
        "Nome": {"title": [{"text": {"content": item["titolo"][:200]}}]},
        "Fonte": {"url": item.get("link") or fonte_url},
        "Tipo": {"select": {"name": tipo}},
        "Stato": {"select": {"name": "🆕 Da valutare"}},
        "Trovato il": {"date": {"start": found_iso_date}},
        "Costo": {"select": {"name": "Non specificato"}},
    }
    if item.get("scadenza"):
        props["Scadenza"] = {"date": {"start": item["scadenza"]}}
    if item.get("note"):
        props["Note"] = {"rich_text": [{"text": {"content": item["note"][:1900]}}]}

    payload = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    resp = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Fetch & hash della pagina fonte (+ eventuali sottopagine collegate)
# ---------------------------------------------------------------------------

def fetch_html(url):
    """Scarica l'HTML grezzo di una pagina."""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_text_from_html(html):
    """Estrae il testo visibile e ripulito da un HTML grezzo."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_relevant_links(base_url, html, max_links=MAX_SUBLINKS_PER_FONTE):
    """Trova, tra i link della pagina, quelli che sembrano portare a
    call for papers / news / annunci, restando sullo stesso dominio."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    found, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text() or "").lower()
        haystack = f"{href} {label}".lower()
        if not any(k in haystack for k in LINK_KEYWORDS):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc != base_domain:
            continue  # niente link verso altri siti
        if abs_url in seen or abs_url.rstrip("/") == base_url.rstrip("/"):
            continue

        seen.add(abs_url)
        found.append(abs_url)
        if len(found) >= max_links:
            break

    return found


def fetch_source_text(url):
    """Scarica la pagina principale di una fonte + fino a
    MAX_SUBLINKS_PER_FONTE sottopagine collegate rilevanti (call/news/cfp...),
    e restituisce tutto come un unico blocco di testo."""
    main_html = fetch_html(url)
    combined = extract_text_from_html(main_html)

    try:
        sublinks = extract_relevant_links(url, main_html)
    except Exception as e:
        print(f"  [!] Errore estraendo i link interni: {e}")
        sublinks = []

    for link in sublinks:
        try:
            sub_html = fetch_html(link)
            sub_text = extract_text_from_html(sub_html)
            combined += f"\n\n--- Pagina collegata: {link} ---\n{sub_text}"
            print(f"  (+) sottopagina controllata: {link}")
        except Exception as e:
            print(f"  [!] Errore nel fetch della sottopagina {link}: {e}")

    return combined


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gemini: capire se nel testo c'è una nuova CFP / evento
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Sei un assistente che aiuta un dottorando in semiotica cognitiva e filosofia della
mente a monitorare call for papers, conferenze e summer school pertinenti.

Analizza il seguente testo, estratto dalla pagina web "{nome_fonte}" ({url_fonte}).
Il testo può contenere molto rumore (menu, footer, ecc): concentrati solo su
eventuali annunci di Call for Papers, conferenze, summer school, workshop o
seminari con una scadenza o data futura.

Se non trovi nulla di rilevante, rispondi con: {{"trovate": []}}

Se trovi una o più opportunità rilevanti, rispondi SOLO con un JSON in questo
formato esatto (nessun testo fuori dal JSON):

{{
  "trovate": [
    {{
      "titolo": "string breve",
      "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario",
      "scadenza": "YYYY-MM-DD oppure null se non specificata",
      "link": "URL diretto se presente nel testo, altrimenti null",
      "note": "1-2 frasi in italiano su tema e rilevanza per semiotica cognitiva / filosofia della mente / IA"
    }}
  ]
}}

TESTO DELLA PAGINA:
{testo}
"""


def ask_gemini(nome_fonte, url_fonte, text):
    prompt = PROMPT_TEMPLATE.format(
        nome_fonte=nome_fonte, url_fonte=url_fonte, testo=text[:20000]
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"},
    }
    resp = requests.post(GEMINI_URL, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)
        return parsed.get("trovate", [])
    except (KeyError, IndexError, json.JSONDecodeError):
        print(f"  [!] Risposta Gemini non interpretabile per {nome_fonte}: {data}")
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.date.today().isoformat()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive da controllare: {len(fonti)}")

    for page in fonti:
        page_id = page["id"]
        nome = get_prop_text(page, "Nome", kind="title") or "(senza nome)"
        url = get_prop_url(page, "URL")
        richiede_browser = get_prop_checkbox(page, "Richiede Browser")
        old_hash = get_prop_text(page, "Ultimo Hash")

        if not url:
            print(f"- {nome}: nessun URL, salto.")
            continue

        if richiede_browser:
            print(f"- {nome}: richiede browser (JS), non ancora supportato in questa v1, salto.")
            continue

        print(f"- {nome}: controllo {url}")
        try:
            text = fetch_source_text(url)
        except Exception as e:
            print(f"  [!] Errore nel fetch: {e}")
            continue

        new_hash = hash_text(text)

        if new_hash == old_hash:
            print("  Nessuna modifica rilevata.")
            try:
                update_fonte_status(page_id, new_hash, today)
            except Exception as e:
                print(f"  [!] Errore aggiornando Notion (Fonti): {e}")
            continue

        print("  Cambiamento rilevato, chiedo a Gemini...")
        try:
            trovate = ask_gemini(nome, url, text)
        except Exception as e:
            print(f"  [!] Errore chiamando Gemini: {e}")
            trovate = []

        for item in trovate:
            if not item.get("titolo"):
                continue
            try:
                create_tracker_entry(item, url, today)
                print(f"  -> Aggiunta al Tracker: {item['titolo']}")
            except Exception as e:
                print(f"  [!] Errore creando pagina nel Tracker: {e}")

        try:
            update_fonte_status(page_id, new_hash, today)
        except Exception as e:
            print(f"  [!] Errore aggiornando Notion (Fonti): {e}")

    print("Scouting completato.")


if __name__ == "__main__":
    sys.exit(main())

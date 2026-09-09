"""
PhD Scouting Agent (v2 Refactored)
----------------------------------
Legge il database "Fonti di Scouting" su Notion, controlla le pagine
(partendo da URL Call/News e poi URL Homepage) e le loro sottopagine/link.
Chiede a Gemini di estrarre le Call for Papers / conferenze / summer school attive.
Scrive i risultati in "PhD Scouting Tracker".
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

LINK_KEYWORDS = [
    "call", "cfp", "news", "announce", "deadline", "papers",
    "submission", "event", "conference", "bando", "scadenza", "convegn",
    "congress", "contribution", "read more", "continua", "in evidenza"
]
MAX_SUBLINKS_PER_FONTE = 6


# ---------------------------------------------------------------------------
# Notion Helpers (con nomi proprietà allineati allo screenshot)
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
    if item.get("scadenza") and re.match(r"^\d{4}-\d{2}-\d{2}$", str(item["scadenza"])):
        props["Scadenza"] = {"date": {"start": item["scadenza"]}}
    if item.get("note"):
        props["Note"] = {"rich_text": [{"text": {"content": item["note"][:1900]}}]}

    payload = {"parent": {"database_id": DB_TRACKER_ID}, "properties": props}
    resp = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Extraction & Deep Crawling
# ---------------------------------------------------------------------------

def fetch_html(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_text_and_links(base_url, html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text() or "").strip()
        haystack = f"{href} {label}".lower()
        if any(k in haystack for k in LINK_KEYWORDS):
            abs_url = urljoin(base_url, href)
            if abs_url not in seen and abs_url.rstrip("/") != base_url.rstrip("/"):
                seen.add(abs_url)
                links.append((abs_url, label))

    return text, links[:MAX_SUBLINKS_PER_FONTE]


def fetch_source_deep(url_primary, url_secondary=None):
    urls_to_try = [u for u in [url_primary, url_secondary] if u]
    combined_text = ""
    visited = set()

    for target_url in urls_to_try:
        if target_url in visited:
            continue
        try:
            html = fetch_html(target_url)
            visited.add(target_url)
            text, sublinks = extract_text_and_links(target_url, html)
            combined_text += f"\n\n=== CONTENUTO DA: {target_url} ===\n{text}"

            for sub_url, label in sublinks:
                if sub_url not in visited:
                    try:
                        sub_html = fetch_html(sub_url)
                        visited.add(sub_url)
                        sub_text, _ = extract_text_and_links(sub_url, sub_html)
                        combined_text += f"\n\n--- SOTTOPAGINA ({label}): {sub_url} ---\n{sub_text[:4000]}"
                        print(f"  (+) Navigato sottolink: {sub_url}")
                    except Exception as e:
                        print(f"  [!] Impossibile scaricare sottolink {sub_url}: {e}")
        except Exception as e:
            print(f"  [!] Errore scaricando {target_url}: {e}")

    return combined_text, list(visited)


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gemini Prompting
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Sei un assistente per un dottorando in filosofia, semiotica cognitiva e filosofia della mente.
Data odierna: {data_oggi}.

Analizza il seguente testo estratto dal sito "{nome_fonte}".

IL TUO COMPITO:
Individua TUTTE le opzioni attive di:
1. Call for Papers per riviste (special issues o numeri tematici).
2. Call for Abstracts / Papers per conferenze e convegni.
3. Call per Summer School, Winter School o workshop.

REGOLE ESSENZIALI:
- Ignora le call già scadute prima del {data_oggi}.
- Se una conferenza o evento si terrà nel 2026 o 2027 e le deadline non sono ancora chiaramente scadute o stanno per uscire, INCLUDILA.
- Se nel testo vedi un URL diretto alla call specifica, estrailo nel campo "link".

Rispondi ESCLUSIVAMENTE con un JSON valido nel seguente formato:

{{
  "trovate": [
    {{
      "titolo": "Titolo chiaro ed esplicativo dell'opportunità",
      "tipo": "Journal CFP" | "Conferenza" | "Summer School" | "Evento gratuito online" | "Lezione/Seminario",
      "scadenza": "YYYY-MM-DD" oppure null se non indicata,
      "link": "URL diretto se visibile nel testo, altrimenti null",
      "note": "Sintesi di 1-2 frasi in italiano su argomenti, curatori/organizzatori e contatti"
    }}
  ]
}}

Se non trovi alcuna opportunità attiva o non scaduta, rispondi semplicemente con: {{"trovate": []}}

TEXTO DA ANALIZZARE:
{testo}
"""


def ask_gemini(nome_fonte, url_fonte, text, today_str):
    prompt = PROMPT_TEMPLATE.format(
        nome_fonte=nome_fonte,
        data_oggi=today_str,
        testo=text[:28000]
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
        print(f"  [!] Risposta Gemini non parsabile per {nome_fonte}")
        return []


# ---------------------------------------------------------------------------
# Main Execution Loop
# ---------------------------------------------------------------------------

def main():
    today = datetime.date.today().isoformat()
    fonti = notion_query_active_fonti()
    print(f"Fonti attive nel database: {len(fonti)}")

    for page in fonti:
        page_id = page["id"]
        nome = get_prop_text(page, "Nome", kind="title") or "(Senza Titolo)"
        url_call = get_prop_url(page, "URL Call/News")
        url_home = get_prop_url(page, "URL Homepage")
        old_hash = get_prop_text(page, "Ultimo Hash")

        # Inizio ricerca dando priorità a URL Call/News se presente, altrimenti URL Homepage
        primary_url = url_call or url_home
        secondary_url = url_home if url_call else None

        if not primary_url:
            print(f"- {nome}: nessun URL impostato, salto.")
            continue

        print(f"\n- Controllo fonte: {nome}")
        print(f"  URL principale: {primary_url}")

        combined_text, visited_urls = fetch_source_deep(primary_url, secondary_url)

        if not combined_text.strip():
            print("  [!] Nessun testo estratto.")
            continue

        new_hash = hash_text(combined_text)

        if new_hash == old_hash:
            print("  Nessuna modifica rispetto all'ultimo controllo.")
            try:
                update_fonte_status(page_id, new_hash, today)
            except Exception as e:
                print(f"  [!] Errore aggiornamento stato Notion: {e}")
            continue

        print("  Rilevate modifiche o primo controllo. Analisi con Gemini...")
        trovate = ask_gemini(nome, primary_url, combined_text, today)

        if not trovate:
            print("  Nessuna Call attiva rilevata da Gemini.")
        else:
            for item in trovate:
                if not item.get("titolo"):
                    continue
                try:
                    create_tracker_entry(item, primary_url, today)
                    print(f"  -> INSERITO NEL TRACKER: {item['titolo']} (Scadenza: {item.get('scadenza')})")
                except Exception as e:
                    print(f"  [!] Errore inserimento in Tracker Notion: {e}")

        try:
            update_fonte_status(page_id, new_hash, today)
        except Exception as e:
            print(f"  [!] Errore aggiornamento stato Notion: {e}")

    print("\nScouting completato con successo.")


if __name__ == "__main__":
    sys.exit(main())

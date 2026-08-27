# PhD Scouting Agent

Controlla automaticamente 2 volte a settimana (martedì e venerdì) le fonti elencate
nel database Notion **"🔍 Fonti di Scouting"**, e se rileva una nuova Call for
Papers / conferenza / summer school, la aggiunge al database **"PhD Scouting
Tracker"** con stato `🆕 Da valutare`, pronta per il tuo filtro manuale.

## Come funziona (in breve)

1. Legge le fonti attive dal database Fonti (via Notion API).
2. Scarica il testo di ogni pagina e ne calcola un hash (un'"impronta digitale").
3. Se l'hash è diverso da quello salvato l'ultima volta → qualcosa è cambiato.
4. In quel caso chiede a **Gemini** (gratuito) di leggere il testo e capire se
   contiene una nuova opportunità rilevante, estraendone titolo, tipo, scadenza.
5. Se sì, crea una nuova riga nel PhD Scouting Tracker.
6. Aggiorna l'hash salvato, così al prossimo giro non segnala di nuovo la stessa cosa.

**Nota v1**: le fonti marcate "Richiede Browser" (siti che caricano i contenuti
via JavaScript) vengono per ora saltate. È un miglioramento facile da aggiungere
in un secondo momento con Playwright, se serve.

## Setup — cosa devi fare tu

### 1. Chiave Gemini (gratuita)
- Vai su https://aistudio.google.com
- "Get API key" → "Create API key" → copiala

### 2. Integration Notion
- Vai su https://www.notion.so/my-integrations → "New integration"
- Copia l'"Internal Integration Secret"
- Apri sia il database **Fonti di Scouting** sia **PhD Scouting Tracker** in
  Notion → "..." in alto a destra → "Connessioni" → collega l'integration
  appena creata a entrambi (senza questo passaggio lo script non può leggerli/scriverli)

### 3. Repository GitHub
- Crea un repository (anche privato) e carica tutti i file di questa cartella,
  mantenendo la struttura (in particolare `.github/workflows/scouting.yml`
  deve restare in quel percorso esatto).

### 4. Secrets su GitHub
Nel repository → Settings → Secrets and variables → Actions → "New repository secret".
Creane 4, con questi nomi esatti (i valori li trovi nel tuo `.env` compilato):

| Nome secret            | Valore |
|-------------------------|--------|
| `NOTION_TOKEN`           | Internal Integration Secret di Notion |
| `GEMINI_API_KEY`         | Chiave Gemini |
| `NOTION_DB_FONTI_ID`     | `688803c0e01747efaa29b51e9ee25886` |
| `NOTION_DB_TRACKER_ID`   | `a89fb9bdaa534d318296bba6cd7547d1` |

### 5. Primo test manuale
- Nel repository → tab "Actions" → workflow "PhD Scouting Agent" → "Run workflow"
- Controlla i log: dovrebbe elencare le fonti controllate
- Controlla il database Fonti su Notion: "Ultimo Controllo" e "Ultimo Hash"
  dovrebbero essersi aggiornati
- Se qualche fonte aveva già contenuti che sembrano una CFP, dovresti vedere
  nuove righe nel PhD Scouting Tracker

Al primo giro è normale che tutte le fonti risultino "cambiate" (perché
l'hash è vuoto) — è per questo che conviene lanciarlo una volta a mano prima
di lasciarlo andare in automatico, così vedi che tipo di risultati produce.

### 6. Automatico
Una volta verificato che funziona, non devi fare altro: il cron in
`.github/workflows/scouting.yml` lo esegue da solo ogni martedì e venerdì.

## Test in locale (opzionale)
```bash
pip install -r requirements.txt
cp .env.example .env   # poi compila con le tue chiavi
export $(cat .env | xargs)  # su Mac/Linux
python scout.py
```

## File in questa cartella
- `scout.py` — script principale
- `requirements.txt` — dipendenze Python
- `.github/workflows/scouting.yml` — automazione GitHub Actions
- `.env.example` — template variabili d'ambiente (per test locali)

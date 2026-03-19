# Weight Tracker

Web app per tracciare l'andamento del peso corporeo nel tempo. Gira in un container Docker, usa SQLite come database, espone un'interfaccia responsive accessibile da browser desktop e mobile.

## Funzionalità

- **Multi-utente** — account separati con autenticazione locale (bcrypt)
- **Grafico interattivo** — peso nel tempo con media mobile e trend lineare
- **Periodi configurabili** — 1S / 1M / 3M / 6M / 1A / tutto lo storico
- **Peso obiettivo** — linea di riferimento attivabile da impostazioni
- **Statistiche** — min, max, media, variazione nel periodo visualizzato
- **Import dati storici** — da file CSV, file JSON, o inserimento manuale multiplo con gestione dei conflitti
- **Export CSV** — scarica tutto lo storico in un file `.csv`
- **Responsive** — usabile da mobile e da desktop

## Stack

| Layer | Tecnologia |
|---|---|
| Backend | Python 3.12 + FastAPI |
| Database | SQLite via SQLAlchemy |
| Frontend | Jinja2 + Tailwind CSS (CDN) + Chart.js |
| Container | Docker + Docker Compose |

## Struttura del progetto

```
weight_tracker/
├── main.py              # Route FastAPI (pagine + API REST)
├── models.py            # Modelli SQLAlchemy (User, Weight, UserSettings)
├── database.py          # Connessione SQLite + sessione DB
├── auth.py              # Hashing password (bcrypt) + JWT token
├── requirements.txt     # Dipendenze Python
├── Dockerfile
├── docker-compose.yml
└── templates/
    ├── base.html        # Layout base (Tailwind config, font)
    ├── login.html
    ├── register.html
    ├── dashboard.html   # Grafico + inserimento + lista misurazioni
    ├── settings.html    # Impostazioni account e grafico
    └── import.html      # Import dati storici (CSV / JSON / manuale)
```

## Avvio rapido

### Con Docker Compose (raccomandato)

```bash
# Clona il repo e avvia
git clone <repo-url>
cd weight_tracker

# Imposta una SECRET_KEY sicura prima di avviare in produzione
export SECRET_KEY=cambia-questa-chiave

docker compose up -d
```

L'app è disponibile su `http://localhost:8000`.

### Sviluppo locale (senza Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

DATABASE_URL=sqlite:///./dev.db SECRET_KEY=dev-secret uvicorn main:app --reload
```

## Configurazione

Le variabili d'ambiente si passano in `docker-compose.yml` o via shell:

| Variabile | Default | Descrizione |
|---|---|---|
| `SECRET_KEY` | `change-this-secret-in-production` | Chiave per firmare i JWT — **cambiare in produzione** |
| `DATABASE_URL` | `sqlite:////data/weight_tracker.db` | Path del database SQLite |

Il database è persistito in un volume Docker (`weight_data`) e sopravvive ai riavvii del container.

## API REST

Tutte le route API richiedono autenticazione via cookie (`wt_token`).

| Metodo | Path | Descrizione |
|---|---|---|
| `GET` | `/api/weights?days=30` | Lista misurazioni (0 = tutto lo storico) |
| `POST` | `/api/weights` | Aggiunge una misurazione `{"weight": 80.5}` |
| `DELETE` | `/api/weights/{id}` | Elimina una misurazione |
| `GET` | `/api/export/csv` | Scarica lo storico completo in CSV |
| `POST` | `/api/import/preview` | Analizza dati da importare, ritorna conflitti |
| `POST` | `/api/import/confirm` | Conferma l'importazione con risoluzione conflitti |

### Formato CSV export/import

```
data,peso_kg
2025-01-15 12:00:00,80.5
2025-01-16 12:00:00,80.2
```

Colonne accettate in import: `data` / `date`, `peso_kg` / `weight` / `peso`.
Formati data supportati: `YYYY-MM-DD`, `DD/MM/YYYY` (con o senza orario).

### Formato JSON import

```json
[
  {"data": "2025-01-15", "peso_kg": 80.5},
  {"date": "2025-01-16", "weight": 80.2}
]
```

## Deploy su internet

Il container non gestisce TLS. Per esporlo su internet usa un reverse proxy davanti:

**Traefik** (esempio label in `docker-compose.yml`):
```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.weight.rule=Host(`peso.tuodominio.com`)"
  - "traefik.http.routers.weight.tls.certresolver=letsencrypt"
```

**Nginx** (esempio minimo):
```nginx
server {
    listen 443 ssl;
    server_name peso.tuodominio.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Aggiornamento

```bash
git pull
docker compose up -d --build
```

Il database SQLite non viene toccato dall'aggiornamento (è nel volume Docker).

## Backup del database

```bash
# Copia il DB fuori dal volume
docker compose cp app:/data/weight_tracker.db ./backup_$(date +%Y%m%d).db
```

## Sicurezza

- Le password sono hashate con **bcrypt** (non reversibile)
- L'autenticazione usa **JWT** con scadenza a 30 giorni
- Il cookie è `httponly` e `samesite=lax`
- **Cambia sempre `SECRET_KEY`** prima di esporre l'app su internet — se la chiave viene compromessa tutti i token esistenti vanno invalidati
- Per deployment pubblici, usa sempre HTTPS via reverse proxy

## Impostazioni per account

Ogni utente può configurare:

| Impostazione | Default | Descrizione |
|---|---|---|
| Periodo grafico default | 30 giorni | Finestra temporale all'apertura |
| Giorni media mobile | 7 | Finestra per il calcolo della moving average |
| Peso obiettivo | — | Linea di riferimento orizzontale nel grafico |
| Mostra linea obiettivo | No | Toggle visibilità nel grafico |

# Il mio golf — Versione pubblica multi-utente

App Flask multi-utente per la gestione dati golf, deployata su Railway.
Adattata dalla versione locale `C:/Claude/golf/federgolf/` (Docker + PostgreSQL).

---

## 1. ARCHITETTURA

**Singola app Flask** (no Docker, no servizi separati):

```
Browser → Flask app (porta $PORT) → PostgreSQL (Railway)
                  │
                  ├── APScheduler (job notturno 3:00)
                  └── Playwright (headless, in thread)
```

| Modulo | File | Ruolo |
|--------|------|-------|
| App factory | `app/__init__.py` | Flask + LoginManager + scheduler init |
| Modelli DB | `app/models.py` | Schema, CRUD utenti, salvataggio gare/campi |
| Autenticazione | `app/auth.py` | Login/logout blueprint |
| Route | `app/routes.py` | Tutte le route web + API |
| Scraper | `app/scraper.py` | Playwright (gare) + requests API (campi) |
| Scheduler | `app/scheduler.py` | APScheduler cron job notturno |

---

## 2. STRUTTURA FILE

```
C:/Claude/golf-public/
├── railway.toml          # Build command + start command per Railway
├── nixpacks.toml         # Dipendenze sistema per Playwright
├── Procfile              # gunicorn (fallback)
├── requirements.txt
├── .env.example          # Template variabili d'ambiente
├── .gitignore
├── CLAUDE.md
├── app/
│   ├── __init__.py       # create_app()
│   ├── auth.py           # Blueprint auth: /login, /logout
│   ├── models.py         # DB schema + CRUD
│   ├── routes.py         # Blueprint main: tutte le route
│   ├── scraper.py        # Playwright gare + API campi
│   └── scheduler.py      # APScheduler job notturno
└── templates/
    ├── base.html          # Layout mobile-first con hamburger menu
    ├── login.html         # Pagina login
    ├── menu.html          # Home (4 card)
    ├── risultati.html     # Storico gare utente
    ├── campi.html         # Elenco campi (client-side filter)
    ├── campo_detail.html  # Dettaglio campo con CR/SR
    ├── handicap.html      # Calcolo Playing HCP
    ├── simulazione.html   # Simulazione variazione HCP
    ├── admin_utenti.html  # Lista utenti (solo admin)
    └── admin_utenti_form.html  # Form crea/modifica utente
```

---

## 3. DATABASE

### Variabili d'ambiente richieste

```bash
SECRET_KEY=<token casuale 32 byte>        # flask session
FERNET_KEY=<chiave Fernet>                # cifratura password Federgolf
DATABASE_URL=postgresql://...             # Railway lo genera automaticamente
SCHEDULER_HOUR=3                          # ora job notturno (opzionale)
SCHEDULER_MINUTE=0                        # minuto job notturno (opzionale)
```

**Generazione chiavi:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"    # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FERNET_KEY
```

### Schema DB

**`utenti`** — utenti dell'app:
| Colonna | Tipo | Note |
|---------|------|------|
| `id` | SERIAL PK | — |
| `email` | TEXT UNIQUE | login |
| `password_hash` | TEXT | bcrypt |
| `nome`, `cognome` | TEXT | — |
| `federgolf_username` | TEXT | numero tesserato |
| `federgolf_password_enc` | TEXT | cifrata con Fernet |
| `attivo` | BOOLEAN | disabilita senza cancellare |
| `admin` | BOOLEAN | accesso pannello admin |
| `creato_il` | TIMESTAMP | — |

**`gare`** — storico gare (come versione locale + `utente_id`):
- Aggiunta colonna `utente_id INTEGER REFERENCES utenti(id) ON DELETE CASCADE`
- UNIQUE: `(utente_id, data_raw, gara)` — dedup per utente

**`campi`** e **`campi_rating`** — condivisi tra tutti gli utenti (identici alla versione locale)

**`scraper_logs`** — log delle esecuzioni scraper:
| Colonna | Tipo | Note |
|---------|------|------|
| `id` | SERIAL PK | — |
| `utente_id` | INTEGER FK | — |
| `tipo` | TEXT | "gare" o "campi" |
| `avviato_il` | TIMESTAMP | — |
| `completato_il` | TIMESTAMP | — |
| `stato` | TEXT | running/done/error |
| `gare_aggiunte` | INTEGER | — |
| `errore` | TEXT | stacktrace se error |

---

## 4. AUTENTICAZIONE E SICUREZZA

- **Flask-Login** per sessioni (cookie `remember_me`)
- **bcrypt** per password utenti
- **Fernet** (cryptography) per credenziali Federgolf cifrate nel DB
- **Registrazione disabilitata** — solo admin può creare utenti via `/admin/utenti/nuovo`
- **Admin required** su `/admin/*` e `POST /api/scraper/run_campi`
- **Login required** su tutte le route tranne `/login`, `/health`

---

## 5. SCRAPER

### Gare (per utente)
- `scrape_and_save(username, password, utente_id)` in `app/scraper.py`
- Logica Playwright identica alla versione locale
- Chiamato: manualmente da `POST /api/scraper/run` (utente corrente) o dal job notturno

### Campi (condivisi)
- `scrape_campi_api()` — API pubblica Federgolf (no login)
- Solo admin può avviarlo: `POST /api/scraper/run_campi`

### Job notturno
- APScheduler `BackgroundScheduler` (thread daemon)
- Cron: ogni notte alle 3:00 (configurabile via env)
- Per ogni utente attivo con credenziali → scraper + log in `scraper_logs`
- **Un solo worker gunicorn** (necessario per APScheduler con threading)

---

## 6. ROUTE PRINCIPALI

| Route | Metodo | Accesso | Descrizione |
|-------|--------|---------|-------------|
| `/login` | GET/POST | — | Login |
| `/logout` | GET | login | Logout |
| `/` | GET | login | Menu principale |
| `/risultati` | GET | login | Storico gare utente corrente |
| `/campi` | GET | login | Elenco campi (condivisi) |
| `/campi/<id>` | GET | login | Dettaglio campo |
| `/handicap` | GET | login | Calcolo Playing HCP |
| `/simulazione` | GET | login | Simulazione variazione HCP |
| `/api/campi` | GET | login | JSON campi con CR/SR |
| `/api/campi_tutti` | GET | login | JSON tutti i campi |
| `/api/campi_rating` | GET | login | JSON rating per campo |
| `/api/simulazione_hcp` | GET | login | JSON simulazione |
| `/api/scraper/run` | POST | login | Avvia scraper gare (utente corrente) |
| `/api/scraper/status` | GET | login | Stato scraper gare |
| `/api/scraper/run_campi` | POST | **admin** | Avvia scraper campi |
| `/api/scraper/status_campi` | GET | login | Stato scraper campi |
| `/admin/utenti` | GET | **admin** | Lista utenti |
| `/admin/utenti/nuovo` | GET/POST | **admin** | Crea utente |
| `/admin/utenti/<id>/modifica` | GET/POST | **admin** | Modifica utente |
| `/health` | GET | — | Health check Railway |

---

## 7. DEPLOY SU RAILWAY

### Prima configurazione

1. Crea un nuovo progetto Railway
2. Aggiungi servizio **PostgreSQL** — Railway genera `DATABASE_URL` automaticamente
3. Collega il repository GitHub
4. Aggiungi variabili d'ambiente in Railway:
   - `SECRET_KEY` (generare con `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `FERNET_KEY` (generare con `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `DATABASE_URL` è già impostata da Railway
5. Deploy — Railway usa `railway.toml` per build e start command

### Creare il primo utente admin

Via Railway console o connettendosi al DB:
```python
# In una shell Python con le variabili d'ambiente impostate:
from app.models import create_user
create_user("tua@email.com", "password", "Nome", "Cognome", "161705", "federgolf_pass", admin=True)
```

Oppure, più semplicemente, usa lo script di bootstrap:
```bash
# Da Railway console (Run > New Command):
python -c "
from app import create_app
app = create_app()
with app.app_context():
    from app.models import create_user
    create_user('admin@example.com', 'password_sicura', 'Admin', '', '161705', 'fed_password', admin=True)
"
```

### Aggiornamenti

```bash
git push origin main  # Railway fa redeploy automatico
```

---

## 8. SVILUPPO LOCALE

```bash
cd C:/Claude/golf-public

# Crea .env da template
copy .env.example .env
# Modifica .env con i valori reali

# Installa dipendenze
pip install -r requirements.txt
playwright install chromium

# Avvia (richiede PostgreSQL locale o DATABASE_URL puntata a Railway)
flask --app "app:create_app()" run --debug
```

---

## 9. DIFFERENZE RISPETTO ALLA VERSIONE LOCALE

| Aspetto | Versione locale (Docker) | Versione pubblica (Railway) |
|---------|--------------------------|------------------------------|
| Architettura | 3 container (db, scraper, web) | Single Flask app |
| Utenti | Singolo (hardcoded in .env) | Multi-utente con DB |
| Auth | Nessuna | Flask-Login + bcrypt |
| Credenziali Federgolf | .env | DB cifrate con Fernet |
| Scraper trigger | HTTP proxy web→scraper | Diretto in thread |
| Scraper notturno | No | APScheduler cron |
| Gare per utente | No (tabella unica) | Sì (utente_id FK) |
| Campi | Condivisi | Condivisi (identico) |
| Deploy | docker compose up | git push → Railway |

---

## 10. NOTE PER CLAUDE CODE

- **Aggiorna sempre questo CLAUDE.md** alla fine di ogni sessione
- **Un solo worker gunicorn** — non aumentare mai i workers (APScheduler non è multi-process)
- **FERNET_KEY non cambiare mai** dopo il primo deploy — invaliderebbe tutte le password Federgolf salvate
- **SECRET_KEY non cambiare mai** dopo il primo deploy — invaliderebbe tutte le sessioni

---

## 11. CHANGELOG

| Data | Modifica |
|------|----------|
| 2026-06-28 | Creazione progetto da versione locale federgolf/ — architettura multi-utente |
| 2026-06-28 | Schema DB: tabella utenti, gare.utente_id, scraper_logs |
| 2026-06-28 | Auth: Flask-Login + bcrypt + Fernet per credenziali Federgolf |
| 2026-06-28 | Scheduler: APScheduler cron job notturno alle 3:00 |
| 2026-06-28 | UI: template mobile-first con hamburger menu, Tailwind CDN |
| 2026-06-28 | Deploy: railway.toml + nixpacks.toml → Dockerfile (fix Playwright su Railway) |
| 2026-06-28 | Fix porta: ${PORT:-8080} in Dockerfile CMD, nessun startCommand in railway.toml |
| 2026-06-28 | Fix scraper campi: _api_post_with_retry (3 tentativi, backoff 2s/4s), timeout 30s, log dettagliato per circolo, pausa 0.3s tra richieste |
| 2026-06-28 | Porting scrape_regioni.py + scrape_detail.py in app/scraper.py: scrape_campi_completo() con 3 fasi (geografico 341 circoli, dettaglio club_id/buche_tot/contatti, CR/SR API 223 circoli) |
| 2026-06-28 | models.py: save_campo aggiornato con club_id + COALESCE su tutti i campi geografici; aggiunti update_campo_details() e get_campi_with_club_id() |
| 2026-06-28 | Fix combo vuote: Dockerfile --workers 2 → 1 (bug critico: con 2 workers il polling status colpisce il worker sbagliato); caricaCampi() nei template ora ha try/catch + Array.isArray check |
| 2026-07-09 | Fix campi_rating vuota: scrape_campi_api usa club_id dal DB (fase 1) invece di HTML scraping pagina handicap (la select viene caricata via JS, requests riceveva select vuota → 0 ratings, nessun errore) |
| 2026-07-09 | Rimossa _api_get_clubs() (obsoleta); admin panel: pannello "Aggiorna campi" con trigger e polling stato; railway.toml: healthcheckPath / → /health |
| 2026-07-11 | Simulatore HCP: aggiunto selettore "Tipo di score" (Stableford/Medal) in simulazione.html; /api/simulazione_hcp calcola Playing Handicap e Gross Score da Punti Stableford quando tipo_score=stableford, mostra Gross Score calcolato prima del risultato finale |
| 2026-07-11 | Fix crash /api/simulazione_hcp quando campi_rating.par è NULL: aggiunto _effective_par() (fallback su campi.par_tot, poi 72/36 in base a buche) usato sia in /api/simulazione_hcp che in /api/campi_rating (condiviso con la pagina Handicap di gioco); validazione esplicita se hcp_attuale è None per lo Stableford |

"""
Database models e funzioni di accesso per il mio golf (versione multi-utente).
"""

import os
from datetime import datetime

import bcrypt
import psycopg2
import psycopg2.extras
from cryptography.fernet import Fernet
from flask_login import UserMixin

# ---------------------------------------------------------------------------
# Connessione
# ---------------------------------------------------------------------------

def get_conn():
    url = os.environ.get("DATABASE_URL", "")
    if url:
        # Railway fornisce DATABASE_URL con prefisso postgres:// → normalizza
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return psycopg2.connect(url)
    # Fallback variabili separate
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        dbname=os.environ.get("DB_NAME", "federgolf"),
        user=os.environ.get("DB_USER", "federgolf"),
        password=os.environ.get("DB_PASSWORD", "federgolf"),
    )


# ---------------------------------------------------------------------------
# Fernet (cifratura credenziali Federgolf)
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    key = os.environ["FERNET_KEY"].encode()
    return Fernet(key)


def encrypt_password(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# User (Flask-Login)
# ---------------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, row: dict):
        self.id = row["id"]
        self.email = row["email"]
        self.nome = row.get("nome") or ""
        self.cognome = row.get("cognome") or ""
        self.admin = row.get("admin", False)
        self.attivo = row.get("attivo", True)
        self.federgolf_username = row.get("federgolf_username") or ""

    def get_id(self):
        return str(self.id)

    @property
    def nome_completo(self):
        return f"{self.nome} {self.cognome}".strip() or self.email


# ---------------------------------------------------------------------------
# Init DB
# ---------------------------------------------------------------------------

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            # Utenti
            cur.execute("""
                CREATE TABLE IF NOT EXISTS utenti (
                    id                       SERIAL PRIMARY KEY,
                    email                    TEXT UNIQUE NOT NULL,
                    password_hash            TEXT NOT NULL,
                    nome                     TEXT,
                    cognome                  TEXT,
                    federgolf_username       TEXT,
                    federgolf_password_enc   TEXT,
                    attivo                   BOOLEAN NOT NULL DEFAULT TRUE,
                    admin                    BOOLEAN NOT NULL DEFAULT FALSE,
                    creato_il                TIMESTAMP DEFAULT NOW()
                )
            """)

            # Gare (con utente_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gare (
                    id                       SERIAL PRIMARY KEY,
                    utente_id                INTEGER REFERENCES utenti(id) ON DELETE CASCADE,
                    data_raw                 VARCHAR(10) NOT NULL,
                    data                     DATE NOT NULL,
                    gara                     TEXT NOT NULL DEFAULT '',
                    motivazione_variazione   TEXT,
                    esecutore                TEXT,
                    giro                     TEXT,
                    formula                  TEXT,
                    buche                    TEXT,
                    valida                   TEXT,
                    playing_hcp              TEXT,
                    par                      TEXT,
                    cr                       TEXT,
                    sr                       TEXT,
                    stbl                     TEXT,
                    ags                      TEXT,
                    pcc                      TEXT,
                    sd                       TEXT,
                    correzione_sd            TEXT,
                    correzione               TEXT,
                    index_vecchio            TEXT,
                    index_nuovo              TEXT,
                    variazione               TEXT,
                    tipo_giocatore_nuovo     TEXT,
                    scraped_at               TIMESTAMP DEFAULT NOW()
                )
            """)
            # Unique per utente: stessa gara non duplicata
            cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE gare
                        ADD CONSTRAINT gare_utente_data_gara_key
                        UNIQUE (utente_id, data_raw, gara);
                EXCEPTION WHEN duplicate_table THEN NULL;
                END $$
            """)

            # Campi (condivisi tra tutti gli utenti)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campi (
                    id            SERIAL PRIMARY KEY,
                    nome          TEXT NOT NULL,
                    citta         TEXT,
                    regione       TEXT,
                    provincia     TEXT,
                    buche_tot     TEXT,
                    par_tot       TEXT,
                    indirizzo     TEXT,
                    telefono      TEXT,
                    email         TEXT,
                    sito_web      TEXT,
                    club_id       TEXT,
                    aggiornato_al TIMESTAMP DEFAULT NOW(),
                    UNIQUE(nome)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campi_rating (
                    id          SERIAL PRIMARY KEY,
                    campo_id    INTEGER REFERENCES campi(id) ON DELETE CASCADE,
                    percorso    TEXT NOT NULL DEFAULT '',
                    tee_colore  TEXT,
                    genere      TEXT,
                    buche       INTEGER,
                    cr          NUMERIC(5,1),
                    sr          INTEGER,
                    par         INTEGER,
                    UNIQUE (campo_id, percorso, tee_colore, genere)
                )
            """)

            # Log scraping notturno
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_logs (
                    id              SERIAL PRIMARY KEY,
                    utente_id       INTEGER REFERENCES utenti(id) ON DELETE CASCADE,
                    tipo            TEXT NOT NULL DEFAULT 'gare',
                    avviato_il      TIMESTAMP DEFAULT NOW(),
                    completato_il   TIMESTAMP,
                    stato           TEXT NOT NULL DEFAULT 'running',
                    gare_aggiunte   INTEGER DEFAULT 0,
                    errore          TEXT
                )
            """)

        conn.commit()


# ---------------------------------------------------------------------------
# Utenti — CRUD
# ---------------------------------------------------------------------------

def load_user_by_id(user_id: int) -> User | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM utenti WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return User(row) if row else None


def load_user_by_email(email: str) -> User | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM utenti WHERE email = %s", (email,))
            row = cur.fetchone()
            return User(row) if row else None


def check_password(email: str, password: str) -> User | None:
    """Verifica credenziali e ritorna User se valide."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM utenti WHERE email = %s AND attivo = TRUE", (email,))
            row = cur.fetchone()
    if not row:
        return None
    if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return User(row)
    return None


def create_user(email: str, password: str, nome: str, cognome: str,
                federgolf_username: str, federgolf_password: str,
                admin: bool = False) -> int:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    fed_enc = encrypt_password(federgolf_password) if federgolf_password else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO utenti
                    (email, password_hash, nome, cognome,
                     federgolf_username, federgolf_password_enc, admin)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (email, pw_hash, nome, cognome, federgolf_username, fed_enc, admin))
            uid = cur.fetchone()[0]
        conn.commit()
    return uid


def update_user(user_id: int, nome: str, cognome: str,
                federgolf_username: str, federgolf_password: str,
                attivo: bool, admin: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if federgolf_password:
                fed_enc = encrypt_password(federgolf_password)
                cur.execute("""
                    UPDATE utenti SET nome=%s, cognome=%s,
                        federgolf_username=%s, federgolf_password_enc=%s,
                        attivo=%s, admin=%s
                    WHERE id=%s
                """, (nome, cognome, federgolf_username, fed_enc, attivo, admin, user_id))
            else:
                cur.execute("""
                    UPDATE utenti SET nome=%s, cognome=%s,
                        federgolf_username=%s,
                        attivo=%s, admin=%s
                    WHERE id=%s
                """, (nome, cognome, federgolf_username, attivo, admin, user_id))
        conn.commit()


def update_user_password(user_id: int, password: str) -> None:
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE utenti SET password_hash=%s WHERE id=%s", (pw_hash, user_id))
        conn.commit()


def list_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.id, u.email, u.nome, u.cognome,
                       u.federgolf_username, u.attivo, u.admin, u.creato_il,
                       COUNT(g.id) AS n_gare
                FROM utenti u
                LEFT JOIN gare g ON g.utente_id = u.id
                GROUP BY u.id
                ORDER BY u.creato_il
            """)
            return [dict(r) for r in cur.fetchall()]


def get_active_users_with_credentials() -> list[dict]:
    """Ritorna utenti attivi con credenziali Federgolf (per il job notturno)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, federgolf_username, federgolf_password_enc
                FROM utenti
                WHERE attivo = TRUE
                  AND federgolf_username IS NOT NULL AND federgolf_username <> ''
                  AND federgolf_password_enc IS NOT NULL AND federgolf_password_enc <> ''
            """)
            rows = cur.fetchall()
    result = []
    for r in rows:
        try:
            result.append({
                "id": r["id"],
                "federgolf_username": r["federgolf_username"],
                "federgolf_password": decrypt_password(r["federgolf_password_enc"]),
            })
        except Exception:
            pass
    return result


def get_user_federgolf_credentials(user_id: int) -> tuple[str, str] | tuple[None, None]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT federgolf_username, federgolf_password_enc
                FROM utenti WHERE id = %s
            """, (user_id,))
            row = cur.fetchone()
    if not row or not row["federgolf_username"] or not row["federgolf_password_enc"]:
        return None, None
    try:
        return row["federgolf_username"], decrypt_password(row["federgolf_password_enc"])
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Log scraping
# ---------------------------------------------------------------------------

def log_scraper_start(utente_id: int, tipo: str = "gare") -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scraper_logs (utente_id, tipo, stato)
                VALUES (%s, %s, 'running') RETURNING id
            """, (utente_id, tipo))
            log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def log_scraper_done(log_id: int, gare_aggiunte: int = 0) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scraper_logs
                SET stato='done', completato_il=NOW(), gare_aggiunte=%s
                WHERE id=%s
            """, (gare_aggiunte, log_id))
        conn.commit()


def log_scraper_error(log_id: int, errore: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scraper_logs
                SET stato='error', completato_il=NOW(), errore=%s
                WHERE id=%s
            """, (errore[:2000], log_id))
        conn.commit()


def get_recent_logs(utente_id: int, limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, tipo, avviato_il, completato_il, stato, gare_aggiunte, errore
                FROM scraper_logs WHERE utente_id=%s
                ORDER BY avviato_il DESC LIMIT %s
            """, (utente_id, limit))
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Gare
# ---------------------------------------------------------------------------

def save_records(records: list[dict], utente_id: int) -> int:
    """Upsert gare per utente. Ritorna il numero di righe inserite/aggiornate."""
    import re
    DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

    COLUMN_MAP = {
        "Data": "data_raw", "Gara": "gara",
        "Motivazione variazione": "motivazione_variazione",
        "Esecutore": "esecutore", "Giro": "giro", "Formula": "formula",
        "Buche": "buche", "Valida": "valida", "Playing HCP": "playing_hcp",
        "Par": "par", "CR": "cr", "SR": "sr", "Stbl": "stbl", "AGS": "ags",
        "PCC": "pcc", "SD": "sd", "Correzione SD": "correzione_sd",
        "Correzione": "correzione", "Index Vecchio": "index_vecchio",
        "Index Nuovo": "index_nuovo", "Variazione": "variazione",
        "Tipo Giocatore Nuovo": "tipo_giocatore_nuovo",
    }
    KEY_COLS = {"utente_id", "data_raw", "gara"}

    added = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for rec in records:
                data_raw = rec.get("Data", "").strip()
                if not DATE_RE.match(data_raw):
                    continue
                data_obj = datetime.strptime(data_raw, "%d/%m/%Y").date()

                row = {COLUMN_MAP[k]: v for k, v in rec.items() if k in COLUMN_MAP}
                row["data"] = data_obj
                row["utente_id"] = utente_id
                if not row.get("gara"):
                    row["gara"] = ""

                cols = list(row.keys())
                vals = [row[c] for c in cols]
                placeholders = ", ".join(["%s"] * len(cols))
                col_names = ", ".join(cols)

                update_parts = []
                for c in cols:
                    if c in KEY_COLS:
                        continue
                    if c == "data":
                        update_parts.append("data = EXCLUDED.data")
                    else:
                        update_parts.append(
                            f"{c} = CASE WHEN EXCLUDED.{c} IS NOT NULL AND EXCLUDED.{c} <> '' "
                            f"THEN EXCLUDED.{c} ELSE gare.{c} END"
                        )
                update_parts.append("scraped_at = NOW()")
                update_clause = ", ".join(update_parts)

                cur.execute(f"""
                    INSERT INTO gare ({col_names})
                    VALUES ({placeholders})
                    ON CONFLICT (utente_id, data_raw, gara) DO UPDATE SET
                        {update_clause}
                """, vals)
                if cur.rowcount:
                    added += 1
        conn.commit()
    return added


# ---------------------------------------------------------------------------
# Campi (condivisi)
# ---------------------------------------------------------------------------

def save_campo(campo: dict) -> int | None:
    nome = (campo.get("nome") or "").strip()
    if not nome:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO campi (nome, citta, regione, provincia, buche_tot, par_tot,
                                   indirizzo, telefono, email, sito_web, aggiornato_al)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (nome) DO UPDATE SET
                    citta=EXCLUDED.citta,
                    regione=CASE WHEN campi.regione IS NOT NULL AND campi.regione <> ''
                                 THEN campi.regione ELSE EXCLUDED.regione END,
                    provincia=EXCLUDED.provincia,
                    buche_tot=EXCLUDED.buche_tot,
                    par_tot=EXCLUDED.par_tot,
                    indirizzo=EXCLUDED.indirizzo,
                    telefono=EXCLUDED.telefono,
                    email=EXCLUDED.email,
                    sito_web=EXCLUDED.sito_web,
                    aggiornato_al=NOW()
                RETURNING id
            """, (
                nome,
                campo.get("citta", ""), campo.get("regione", ""),
                campo.get("provincia", ""), campo.get("buche_tot", ""),
                campo.get("par_tot", ""), campo.get("indirizzo", ""),
                campo.get("telefono", ""), campo.get("email", ""),
                campo.get("sito_web", ""),
            ))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None


def save_campo_ratings(campo_id: int, ratings: list[dict]) -> int:
    added = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for r in ratings:
                try:
                    cr_raw = str(r.get("cr", "") or "").replace(",", ".").strip()
                    sr_raw = str(r.get("sr", "") or "").strip()
                    par_raw = str(r.get("par", "") or "").strip()
                    cr_val = float(cr_raw) if cr_raw else None
                    sr_val = int(float(sr_raw)) if sr_raw else None
                    par_val = int(float(par_raw)) if par_raw else None
                    buche_val = int(str(r.get("buche", 18))) if r.get("buche") else 18
                except (ValueError, TypeError):
                    continue

                cur.execute("""
                    INSERT INTO campi_rating
                        (campo_id, percorso, tee_colore, genere, buche, cr, sr, par)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (campo_id, percorso, tee_colore, genere) DO UPDATE SET
                        buche=EXCLUDED.buche, cr=EXCLUDED.cr,
                        sr=EXCLUDED.sr, par=EXCLUDED.par
                """, (
                    campo_id,
                    (r.get("percorso") or "").strip(),
                    (r.get("tee_colore") or "").strip(),
                    (r.get("genere") or "").strip(),
                    buche_val, cr_val, sr_val, par_val,
                ))
                added += cur.rowcount
        conn.commit()
    return added

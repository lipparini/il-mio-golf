"""
Route principali dell'app multi-utente.
"""

import threading
from datetime import datetime
from functools import wraps

import psycopg2.extras
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from .models import (
    get_conn, save_campo, save_campo_ratings,
    create_user, update_user, update_user_password, list_users,
    get_user_federgolf_credentials,
    log_scraper_start, log_scraper_done, log_scraper_error, get_recent_logs,
)

main_bp = Blueprint("main", __name__)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# Stato scraping gare per utente: {utente_id: {status, message, added, last_run}}
_scraping_state: dict[int, dict] = {}
_scraping_lock = threading.Lock()

_campi_state = {"status": "idle", "message": "", "added_campi": 0, "added_ratings": 0, "last_run": None}
_campi_lock = threading.Lock()


def _run_scrape_for_user(app, utente_id: int, username: str, password: str):
    from .scraper import scrape_and_save
    log_id = log_scraper_start(utente_id, "gare")
    with _scraping_lock:
        _scraping_state[utente_id] = {"status": "running", "message": "Scraping in corso...", "added": 0, "last_run": None}
    try:
        added = scrape_and_save(username, password, utente_id)
        log_scraper_done(log_id, added)
        with _scraping_lock:
            _scraping_state[utente_id] = {
                "status": "done",
                "message": f"{added} gare aggiunte/aggiornate.",
                "added": added,
                "last_run": datetime.now().isoformat(timespec="seconds"),
            }
    except Exception as exc:
        log_scraper_error(log_id, str(exc))
        with _scraping_lock:
            _scraping_state[utente_id] = {
                "status": "error",
                "message": str(exc),
                "added": 0,
                "last_run": None,
            }


def _run_scrape_campi():
    from .scraper import scrape_campi_completo

    def _set_progress(msg: str):
        with _campi_lock:
            _campi_state["message"] = msg

    with _campi_lock:
        _campi_state["status"] = "running"
        _campi_state["message"] = "Avvio scraping campi..."
    try:
        stats = scrape_campi_completo(progress_cb=_set_progress)
        f1 = stats.get("fase1", {})
        f2 = stats.get("fase2", {})
        f3 = stats.get("fase3", {})
        n_campi   = f1.get("upsert", 0)
        n_ratings = f3.get("ratings", 0)
        n_errori  = f1.get("errori", 0) + f2.get("errori", 0) + f3.get("errori", 0)
        msg = f"Completato: {n_campi} campi, {n_ratings} rating CR/SR salvati."
        if n_errori:
            msg += f" ({n_errori} errori totali)"
        with _campi_lock:
            _campi_state.update({"status": "done", "message": msg,
                                  "added_campi": n_campi,
                                  "added_ratings": n_ratings,
                                  "last_run": datetime.now().isoformat(timespec="seconds")})
    except Exception as exc:
        with _campi_lock:
            _campi_state.update({"status": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Menu principale
# ---------------------------------------------------------------------------

@main_bp.route("/")
@login_required
def menu():
    stats = {"gare": 0, "campi": 0, "hcp_attuale": None}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) AS n FROM gare WHERE utente_id=%s", (current_user.id,))
                stats["gare"] = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) AS n FROM campi")
                stats["campi"] = cur.fetchone()["n"]
                cur.execute("""
                    SELECT index_nuovo FROM gare
                    WHERE utente_id=%s AND index_nuovo IS NOT NULL AND index_nuovo <> ''
                    ORDER BY data DESC LIMIT 1
                """, (current_user.id,))
                row = cur.fetchone()
                if row:
                    stats["hcp_attuale"] = row["index_nuovo"]
    except Exception:
        pass
    return render_template("menu.html", stats=stats)


# ---------------------------------------------------------------------------
# Sezione 1 — Risultati (per utente corrente)
# ---------------------------------------------------------------------------

@main_bp.route("/risultati")
@login_required
def risultati():
    anno = request.args.get("anno", "", type=str)
    formule_sel = request.args.getlist("formula")
    esecutore = request.args.get("esecutore", "")
    valida = request.args.get("valida", "")
    uid = current_user.id

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT EXTRACT(YEAR FROM data)::int AS y
                    FROM gare WHERE utente_id=%s ORDER BY y DESC
                """, (uid,))
                anni = [r["y"] for r in cur.fetchall()]

                cur.execute("""
                    SELECT DISTINCT formula FROM gare
                    WHERE utente_id=%s AND formula IS NOT NULL AND formula <> ''
                    ORDER BY formula
                """, (uid,))
                formule_all = [r["formula"] for r in cur.fetchall()]

                cur.execute("""
                    SELECT DISTINCT esecutore FROM gare
                    WHERE utente_id=%s AND esecutore IS NOT NULL AND esecutore <> ''
                    ORDER BY esecutore
                """, (uid,))
                esecutori = [r["esecutore"] for r in cur.fetchall()]

                conditions = ["utente_id = %s"]
                params = [uid]

                if anno:
                    conditions.append("EXTRACT(YEAR FROM data)::int = %s")
                    params.append(int(anno))
                if formule_sel:
                    conditions.append("formula = ANY(%s)")
                    params.append(formule_sel)
                if esecutore:
                    conditions.append("esecutore = %s")
                    params.append(esecutore)
                if valida:
                    conditions.append("valida = %s")
                    params.append(valida)

                where = "WHERE " + " AND ".join(conditions)

                cur.execute(f"""
                    SELECT id, data_raw, gara, motivazione_variazione, esecutore, giro,
                           formula, buche, valida, playing_hcp, par, cr, sr, stbl,
                           ags, pcc, sd, correzione_sd, correzione,
                           index_vecchio, index_nuovo, variazione, tipo_giocatore_nuovo
                    FROM gare {where}
                    ORDER BY data DESC
                """, params)
                gare = cur.fetchall()

                cur.execute("SELECT COUNT(*) AS n FROM gare WHERE utente_id=%s", (uid,))
                total_db = cur.fetchone()["n"]

                cur.execute("""
                    SELECT id, sd FROM gare
                    WHERE utente_id=%s AND sd IS NOT NULL AND sd <> ''
                      AND sd ~ '^-?[0-9]+([,\\.][0-9]+)?$'
                    ORDER BY data DESC LIMIT 20
                """, (uid,))
                last20 = cur.fetchall()
                scored = []
                for r in last20:
                    try:
                        scored.append((r["id"], float(str(r["sd"]).replace(",", "."))))
                    except ValueError:
                        pass
                scored.sort(key=lambda x: x[1])
                hcp_contributing_ids = {r_id for r_id, _ in scored[:8]}

        db_error = None
    except Exception as e:
        gare, anni, formule_all, esecutori = [], [], [], []
        total_db = 0
        db_error = str(e)
        hcp_contributing_ids = set()

    return render_template(
        "risultati.html",
        gare=gare, total_db=total_db, db_error=db_error,
        anno=anno, formule_sel=formule_sel, esecutore=esecutore, valida=valida,
        anni=anni, formule_all=formule_all, esecutori=esecutori,
        hcp_contributing_ids=hcp_contributing_ids,
    )


# ---------------------------------------------------------------------------
# Sezione 2 — Campi da golf (condivisi)
# ---------------------------------------------------------------------------

@main_bp.route("/campi")
@login_required
def campi():
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.id, c.nome, c.par_tot,
                           COUNT(DISTINCT cr.percorso) AS n_percorsi
                    FROM campi c
                    LEFT JOIN campi_rating cr ON cr.campo_id = c.id
                    GROUP BY c.id, c.nome, c.par_tot
                    ORDER BY c.nome
                """)
                lista_campi = cur.fetchall()
                total_campi = len(lista_campi)
        db_error = None
    except Exception as e:
        lista_campi, total_campi, db_error = [], 0, str(e)

    return render_template("campi.html", campi=lista_campi, total_campi=total_campi, db_error=db_error)


@main_bp.route("/campi/<int:campo_id>")
@login_required
def campo_detail(campo_id):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, nome, citta, regione, indirizzo, telefono, email, sito_web FROM campi WHERE id=%s", (campo_id,))
                campo = cur.fetchone()
                if not campo:
                    return "Campo non trovato", 404
                cur.execute("""
                    SELECT percorso, tee_colore, genere, buche,
                           cr::float AS cr, sr, par
                    FROM campi_rating WHERE campo_id=%s
                    ORDER BY buche DESC, percorso, genere, tee_colore
                """, (campo_id,))
                ratings = cur.fetchall()
        db_error = None
    except Exception as e:
        campo, ratings, db_error = None, [], str(e)

    return render_template("campo_detail.html", campo=campo, ratings=ratings, db_error=db_error)


@main_bp.route("/api/campi")
@login_required
def api_campi():
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, nome, citta, regione FROM campi
                    WHERE id IN (SELECT DISTINCT campo_id FROM campi_rating)
                    ORDER BY nome
                """)
                campi = cur.fetchall()
        return jsonify([dict(c) for c in campi])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/campi_tutti")
@login_required
def api_campi_tutti():
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    WITH max_par_18 AS (
                        SELECT campo_id, MAX(par) AS max_par
                        FROM campi_rating WHERE buche = 18
                        GROUP BY campo_id
                    )
                    SELECT c.id, c.nome, c.regione,
                           EXISTS(SELECT 1 FROM campi_rating cr WHERE cr.campo_id = c.id) AS has_rating,
                           CASE
                               WHEN c.buche_tot IS NOT NULL AND c.buche_tot <> '' AND c.buche_tot <> '0'
                               THEN c.buche_tot
                               WHEN NOT EXISTS(SELECT 1 FROM campi_rating cr WHERE cr.campo_id = c.id)
                               THEN NULL
                               WHEN mp.max_par IS NULL OR mp.max_par < 68 THEN '9'
                               ELSE '18'
                           END AS tipo_buche
                    FROM campi c
                    LEFT JOIN max_par_18 mp ON mp.campo_id = c.id
                    ORDER BY c.nome
                """)
                campi = cur.fetchall()
        return jsonify([dict(c) for c in campi])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/api/campi_rating")
@login_required
def api_campi_rating():
    campo_id = request.args.get("campo_id", type=int)
    if not campo_id:
        return jsonify([])
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, campo_id, COALESCE(percorso,'') AS percorso,
                           tee_colore, genere, buche, cr::float AS cr, sr, par
                    FROM campi_rating WHERE campo_id=%s
                    ORDER BY buche DESC, percorso, genere, tee_colore
                """, (campo_id,))
                ratings = cur.fetchall()
        return jsonify([dict(r) for r in ratings])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Sezione 3 — Calcolo Handicap di gioco
# ---------------------------------------------------------------------------

@main_bp.route("/handicap")
@login_required
def handicap():
    hcp_attuale = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT index_nuovo FROM gare
                    WHERE utente_id=%s AND index_nuovo IS NOT NULL AND index_nuovo <> ''
                    ORDER BY data DESC LIMIT 1
                """, (current_user.id,))
                row = cur.fetchone()
                if row:
                    hcp_attuale = row["index_nuovo"].replace(",", ".")
    except Exception:
        pass
    return render_template("handicap.html", hcp_attuale=hcp_attuale)


# ---------------------------------------------------------------------------
# Sezione 4 — Simulazione variazione HCP
# ---------------------------------------------------------------------------

@main_bp.route("/simulazione")
@login_required
def simulazione():
    sd_list = []
    hcp_attuale = None
    uid = current_user.id
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT data_raw, sd, gara, esecutore FROM gare
                    WHERE utente_id=%s AND sd IS NOT NULL AND sd <> ''
                      AND sd ~ '^-?[0-9]+([,.][0-9]+)?$'
                    ORDER BY data DESC LIMIT 20
                """, (uid,))
                for r in cur.fetchall():
                    try:
                        sd_list.append({
                            "data": r["data_raw"], "sd": float(str(r["sd"]).replace(",", ".")),
                            "gara": r["gara"] or "", "esecutore": r["esecutore"] or "",
                        })
                    except ValueError:
                        pass

                cur.execute("""
                    SELECT index_nuovo FROM gare
                    WHERE utente_id=%s AND index_nuovo IS NOT NULL AND index_nuovo <> ''
                    ORDER BY data DESC LIMIT 1
                """, (uid,))
                row = cur.fetchone()
                if row:
                    hcp_attuale = row["index_nuovo"].replace(",", ".")
    except Exception:
        pass

    return render_template("simulazione.html", sd_list=sd_list, hcp_attuale=hcp_attuale)


@main_bp.route("/api/simulazione_hcp")
@login_required
def api_simulazione_hcp():
    try:
        gross_score = float(request.args.get("gross_score", 0))
        campo_id = int(request.args.get("campo_id", 0))
        rating_id = request.args.get("rating_id", type=int)
        tee_colore = request.args.get("tee_colore", "")
        genere = request.args.get("genere", "")
        percorso = request.args.get("percorso", "")
        pcc = float(request.args.get("pcc", 0))
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Parametri non validi: {e}"}), 400

    cr = sr = par = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if rating_id:
                    cur.execute("SELECT cr::float AS cr, sr, par FROM campi_rating WHERE id=%s", (rating_id,))
                else:
                    cur.execute("""
                        SELECT cr::float AS cr, sr, par FROM campi_rating
                        WHERE campo_id=%s AND COALESCE(percorso,'')=%s
                          AND tee_colore=%s AND genere=%s LIMIT 1
                    """, (campo_id, percorso, tee_colore, genere))
                row = cur.fetchone()
                if row:
                    cr, sr, par = row["cr"], row["sr"], row["par"]
    except Exception as e:
        return jsonify({"error": f"Errore DB: {e}"}), 500

    if cr is None or sr is None:
        return jsonify({"error": "CR/SR non trovati."}), 400

    sd_nuovo = round((113.0 / sr) * (gross_score - cr - pcc), 1)

    sd_list_db = []
    hcp_attuale_raw = None
    uid = current_user.id
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT data_raw, sd, gara FROM gare
                    WHERE utente_id=%s AND sd IS NOT NULL AND sd <> ''
                      AND sd ~ '^-?[0-9]+([,.][0-9]+)?$'
                    ORDER BY data DESC LIMIT 19
                """, (uid,))
                for r in cur.fetchall():
                    try:
                        sd_list_db.append({"data": r["data_raw"], "sd": float(str(r["sd"]).replace(",", ".")),
                                           "gara": r["gara"] or "", "nuovo": False})
                    except ValueError:
                        pass
                cur.execute("""
                    SELECT index_nuovo FROM gare
                    WHERE utente_id=%s AND index_nuovo IS NOT NULL AND index_nuovo <> ''
                    ORDER BY data DESC LIMIT 1
                """, (uid,))
                row = cur.fetchone()
                if row:
                    hcp_attuale_raw = row["index_nuovo"]
    except Exception as e:
        return jsonify({"error": f"Errore DB: {e}"}), 500

    sd_list_20 = [{"data": "simulato", "sd": sd_nuovo, "gara": "Gara simulata", "nuovo": True}] + sd_list_db
    n = len(sd_list_20)
    indexed = sorted(enumerate(sd_list_20), key=lambda x: x[1]["sd"])
    best8_indices = sorted([i for i, _ in indexed[:min(8, n)]])
    best8_vals = [sd_list_20[i]["sd"] for i in best8_indices]
    hcp_nuovo = round(sum(best8_vals) / len(best8_vals), 1) if best8_vals else None

    hcp_attuale = None
    if hcp_attuale_raw:
        try:
            hcp_attuale = float(str(hcp_attuale_raw).replace(",", "."))
        except ValueError:
            pass

    variazione = round(hcp_nuovo - hcp_attuale, 1) if hcp_nuovo is not None and hcp_attuale is not None else None

    return jsonify({
        "sd_nuovo": sd_nuovo, "cr": cr, "sr": sr, "par": par,
        "sd_list": sd_list_20, "best8_indices": best8_indices,
        "hcp_nuovo": hcp_nuovo, "hcp_attuale": hcp_attuale,
        "variazione": variazione, "n_giri_disponibili": n,
    })


# ---------------------------------------------------------------------------
# API scraper — gare
# ---------------------------------------------------------------------------

@main_bp.route("/api/scraper/run", methods=["POST"])
@login_required
def scraper_run():
    uid = current_user.id
    with _scraping_lock:
        state = _scraping_state.get(uid, {})
        if state.get("status") == "running":
            return jsonify({"status": "already_running"})

    username, password = get_user_federgolf_credentials(uid)
    if not username or not password:
        return jsonify({"status": "error", "message": "Credenziali Federgolf non configurate. Contatta l'amministratore."}), 400

    t = threading.Thread(target=_run_scrape_for_user, args=(None, uid, username, password), daemon=True)
    t.start()
    return jsonify({"status": "started"})


@main_bp.route("/api/scraper/status")
@login_required
def scraper_status():
    uid = current_user.id
    with _scraping_lock:
        state = dict(_scraping_state.get(uid, {"status": "idle", "message": ""}))
    return jsonify(state)


# ---------------------------------------------------------------------------
# API scraper — campi (solo admin)
# ---------------------------------------------------------------------------

@main_bp.route("/api/scraper/run_campi", methods=["POST"])
@admin_required
def scraper_run_campi():
    with _campi_lock:
        if _campi_state["status"] == "running":
            return jsonify({"status": "already_running"})
    t = threading.Thread(target=_run_scrape_campi, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@main_bp.route("/api/scraper/status_campi")
@login_required
def scraper_status_campi():
    with _campi_lock:
        return jsonify(dict(_campi_state))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@main_bp.route("/health")
def health():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin — gestione utenti
# ---------------------------------------------------------------------------

@main_bp.route("/admin/utenti")
@admin_required
def admin_utenti():
    users = list_users()
    return render_template("admin_utenti.html", users=users)


@main_bp.route("/admin/utenti/nuovo", methods=["GET", "POST"])
@admin_required
def admin_utenti_nuovo():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        nome = request.form.get("nome", "").strip()
        cognome = request.form.get("cognome", "").strip()
        fg_user = request.form.get("federgolf_username", "").strip()
        fg_pass = request.form.get("federgolf_password", "")
        is_admin = "admin" in request.form

        if not email or not password:
            error = "Email e password sono obbligatori."
        else:
            try:
                create_user(email, password, nome, cognome, fg_user, fg_pass, is_admin)
                flash(f"Utente {email} creato con successo.", "success")
                return redirect(url_for("main.admin_utenti"))
            except Exception as e:
                if "unique" in str(e).lower():
                    error = "Email già registrata."
                else:
                    error = str(e)

    return render_template("admin_utenti_form.html", utente=None, error=error, titolo="Nuovo utente")


@main_bp.route("/admin/utenti/<int:uid>/modifica", methods=["GET", "POST"])
@admin_required
def admin_utenti_modifica(uid: int):
    users = list_users()
    utente = next((u for u in users if u["id"] == uid), None)
    if not utente:
        abort(404)

    error = None
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        cognome = request.form.get("cognome", "").strip()
        fg_user = request.form.get("federgolf_username", "").strip()
        fg_pass = request.form.get("federgolf_password", "")
        attivo = "attivo" in request.form
        is_admin = "admin" in request.form
        new_password = request.form.get("new_password", "")

        try:
            update_user(uid, nome, cognome, fg_user, fg_pass, attivo, is_admin)
            if new_password:
                update_user_password(uid, new_password)
            flash(f"Utente aggiornato.", "success")
            return redirect(url_for("main.admin_utenti"))
        except Exception as e:
            error = str(e)

    return render_template("admin_utenti_form.html", utente=utente, error=error, titolo="Modifica utente")

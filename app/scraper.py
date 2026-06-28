"""
Scraper Federgolf adattato per versione multi-utente.
Le funzioni prendono username/password come parametri (non da env).
"""

import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .models import save_records, save_campo, save_campo_ratings

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
BASE_URL = "https://areariservata.federgolf.it"
HANDICAP_PAGE_URL = "https://www.federgolf.it/settore-tecnico/calcolo-hcp/"
AJAX_URL = "https://www.federgolf.it/wp-admin/admin-ajax.php"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Playwright — navigazione gare
# ---------------------------------------------------------------------------

def login(page, username: str, password: str):
    log(f"Login: {username} ...")
    page.goto(BASE_URL, wait_until="networkidle")
    page.fill('input[name="User"]', username)
    page.fill('input[name="Password"]', password)
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle")
    if page.query_selector('input[name="Password"]'):
        raise RuntimeError("Login FALLITO — credenziali non valide.")
    log("Login: OK")


def _wait_for_viewdetail_frame(page, timeout_ms=15000):
    start = time.time()
    while time.time() - start < timeout_ms / 1000:
        for frame in page.frames:
            if "ViewDetail" in frame.url:
                return frame
        page.wait_for_timeout(300)
    return None


def navigate_to_risultati(page):
    log(f"URL post-login: {page.url}")

    tesserati = page.evaluate("""
        () => {
            const el = Array.from(document.querySelectorAll('a, span, li'))
                            .find(e => e.textContent.trim() === 'TESSERATI');
            if (el) { el.click(); return true; }
            return false;
        }
    """)
    if tesserati:
        page.wait_for_timeout(800)

    try:
        page.wait_for_selector('a:has-text("Anagrafica tesserati")', timeout=5000)
    except PlaywrightTimeoutError:
        raise RuntimeError("'Anagrafica tesserati' non trovato nel DOM.")

    page.evaluate("""
        () => {
            const el = Array.from(document.querySelectorAll('a'))
                            .find(a => a.textContent.trim() === 'Anagrafica tesserati');
            if (el) el.click();
        }
    """)
    page.wait_for_load_state("networkidle")

    view_frame = _wait_for_viewdetail_frame(page)
    if view_frame is None:
        raise RuntimeError("Frame ViewDetail non trovato.")
    log(f"Frame trovato: {view_frame.url}")

    try:
        view_frame.wait_for_selector('li.tab-header, li[aria-label="Generale"]', timeout=10000)
    except PlaywrightTimeoutError:
        raise RuntimeError("Barra dei tab non trovata nel frame.")

    clicked = view_frame.evaluate("""
        () => {
            const el = Array.from(document.querySelectorAll('a, li')).find(e =>
                e.textContent.trim() === 'Risultati' &&
                !e.classList.contains('menu-action') &&
                e.getAttribute('href') !== '/Risultati/ShowGrid'
            );
            if (el) { el.click(); return true; }
            return false;
        }
    """)
    if not clicked:
        raise RuntimeError("Tab 'Risultati' non trovato nel frame.")

    deadline = time.time() + 10
    table_visible = False
    while time.time() < deadline:
        table_visible = view_frame.evaluate("""
            () => {
                for (const table of document.querySelectorAll('table')) {
                    if (table.offsetParent === null) continue;
                    const th = table.querySelector('thead th, tr th, tr td');
                    if (th && th.textContent.trim() === 'Data') return true;
                }
                return false;
            }
        """)
        if table_visible:
            break
        page.wait_for_timeout(500)

    if not table_visible:
        raise RuntimeError("Tabella risultati non visibile dopo 10s.")

    log("Tabella risultati visibile: OK")
    return view_frame


def extract_records(frame) -> list[dict]:
    _try_show_all_rows(frame)
    _scroll_table_container(frame)

    all_records = []
    page_num = 1
    while True:
        records = _parse_gare_table(frame)
        log(f"Pagina {page_num}: {len(records)} righe.")
        all_records.extend(records)
        btn = _find_next_page_button(frame)
        if not btn:
            break
        btn.click()
        frame.wait_for_load_state("networkidle")
        page_num += 1

    log(f"Estrazione completata: {len(all_records)} righe totali.")
    return all_records


def _try_show_all_rows(frame):
    changed = frame.evaluate("""
        () => {
            for (const sel of document.querySelectorAll('select')) {
                const opts = Array.from(sel.options).map(o => o.value);
                if (opts.some(v => ['25','50','100','200','500','-1','all','tutti'].includes(v.toLowerCase()))) {
                    sel.value = sel.options[sel.options.length - 1].value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            }
            return false;
        }
    """)
    if changed:
        frame.wait_for_timeout(1500)


def _scroll_table_container(frame):
    scrolled = frame.evaluate("""
        () => {
            const table = document.querySelector('table');
            if (!table) return false;
            let el = table.parentElement;
            while (el && el !== document.body) {
                if (el.scrollHeight > el.clientHeight + 10) {
                    el.scrollTop = el.scrollHeight;
                    return true;
                }
                el = el.parentElement;
            }
            return false;
        }
    """)
    if scrolled:
        frame.wait_for_timeout(1000)


def _find_next_page_button(frame):
    for sel in ['a:has-text("Successivo")', 'button:has-text("Successivo")',
                'a:has-text(">")', '.pagination .next a', 'li.next a']:
        btn = frame.query_selector(sel)
        if btn and btn.is_visible() and btn.is_enabled():
            return btn
    return None


def _parse_gare_table(frame) -> list[dict]:
    result = frame.evaluate("""
        () => {
            for (const table of document.querySelectorAll('table')) {
                if (table.offsetParent === null) continue;
                const allTh = Array.from(table.querySelectorAll('thead th'));
                if (!allTh.length) {
                    const fr = table.querySelector('tr');
                    if (fr) allTh.push(...fr.querySelectorAll('th, td'));
                }
                const headers = allTh.map(th => th.textContent.trim());
                if (!headers.length || headers[0] !== 'Data') continue;
                const rows = Array.from(
                    table.querySelectorAll('tbody tr').length
                        ? table.querySelectorAll('tbody tr')
                        : Array.from(table.querySelectorAll('tr')).slice(1)
                );
                const records = [];
                for (const row of rows) {
                    const cells = Array.from(row.querySelectorAll('td'));
                    if (!cells.length) continue;
                    const values = cells.map(c => c.textContent.trim());
                    if (!values.some(v => v)) continue;
                    const rec = {};
                    headers.forEach((h, i) => { rec[h] = values[i] ?? ''; });
                    records.push(rec);
                }
                return records;
            }
            return null;
        }
    """)
    if not result:
        return []
    return [rec for rec in result if DATE_RE.match(rec.get("Data", ""))]


def scrape_and_save(username: str, password: str, utente_id: int) -> int:
    """Esegue lo scraping gare per un utente e salva in DB."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login(page, username, password)
            frame = navigate_to_risultati(page)
            records = extract_records(frame)
            return save_records(records, utente_id)
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Scraper campi — API pubblica (condivisa, nessun utente_id)
# ---------------------------------------------------------------------------

_TEE_GENERE_API = {
    "NERO": "M", "BIANCO": "M", "GIALLO": "M", "VERDE": "M",
    "BLU": "F", "ROSSO": "F", "ARANCIO": "F",
}


def _api_get_clubs() -> list[dict]:
    resp = requests.get(HANDICAP_PAGE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    sel = soup.find("select", {"id": "circolo"})
    if not sel:
        raise RuntimeError("Select #circolo non trovato")
    return [
        {"nome": o.text.strip(), "club_id": o.get("value", "").strip()}
        for o in sel.find_all("option")
        if o.get("value", "").strip()
    ]


def _api_post_with_retry(session, data: dict, max_attempts: int = 3) -> requests.Response:
    """POST all'AJAX API con retry su errori di rete (non su 4xx/5xx)."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.post(AJAX_URL, data=data, timeout=30)
            r.raise_for_status()
            return r
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            if attempt < max_attempts:
                time.sleep(2 ** attempt)   # backoff: 2s, 4s
        except requests.exceptions.Timeout as e:
            last_exc = e
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
    raise last_exc


def _api_get_percorsi(session, club_id: str) -> list[dict]:
    r = _api_post_with_retry(session, {"action": "club-courses", "club_id": club_id})
    data = r.json()
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            result.append({"id": item[0], "nome": item[1]})
    return result


def _api_get_tees(session, percorso_id) -> list[str]:
    r = _api_post_with_retry(session, {"action": "courses-tees", "percorso_id": str(percorso_id)})
    data = r.json()
    if not isinstance(data, list):
        return []
    tees = []
    for item in data:
        if isinstance(item, (list, tuple)) and item:
            tees.append(str(item[0]))
        elif isinstance(item, str):
            tees.append(item)
    return tees


def _api_get_cr_sr(session, club_id: str, percorso_id, tee: str):
    r = _api_post_with_retry(session, {
        "action": "course-handicap",
        "club_id": club_id,
        "percorso_id": str(percorso_id),
        "tee": tee,
        "handicap": "18.0",
    })
    data = r.json()
    if not data or not isinstance(data, list) or not isinstance(data[0], (list, tuple)):
        return None, None, None, None, None
    row = data[0]
    if len(row) < 3:
        return None, None, None, None, None
    return row[1], row[2], row[3] if len(row) > 3 else None, row[4] if len(row) > 4 else None, row[0]


def scrape_campi_api() -> dict:
    """
    Scraping CR/SR da API pubblica Federgolf (requests + BeautifulSoup, NO Playwright).
    Campi condivisi tra tutti gli utenti.

    Nota: l'API handicap restituisce ~221-226 circoli (solo quelli con rating WHS).
    I restanti ~115 circoli affiliati senza CR/SR non appaiono in questa API.
    """
    stats = {"campi": 0, "ratings": 0, "errori": 0, "skip_no_percorsi": 0}

    log("API campi: recupero lista circoli (NO Playwright — solo HTTP) ...")
    try:
        clubs = _api_get_clubs()
    except Exception as e:
        log(f"  ERRORE FATALE recupero lista circoli: {e}")
        return stats

    log(f"  {len(clubs)} circoli nell'API handicap Federgolf.")
    log(f"  Nota: ~120 circoli senza rating WHS non compaiono in questa API.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": HANDICAP_PAGE_URL,
    })

    for i, club in enumerate(clubs, 1):
        nome = club["nome"]
        club_id = club["club_id"]

        # Progresso ogni 10 circoli
        if i % 10 == 0 or i == 1:
            log(f"  [{i}/{len(clubs)}] campi={stats['campi']} rating={stats['ratings']} errori={stats['errori']}")

        try:
            percorsi = _api_get_percorsi(session, club_id)
            if not percorsi:
                log(f"  [{i}] {nome}: nessun percorso, skip.")
                stats["skip_no_percorsi"] += 1
                time.sleep(0.2)
                continue

            campo_id = save_campo({"nome": nome})
            if not campo_id:
                log(f"  [{i}] {nome}: impossibile salvare campo nel DB.")
                stats["errori"] += 1
                continue
            stats["campi"] += 1

            ratings_batch = []
            for perc in percorsi:
                perc_id = perc.get("id") or perc.get("ID") or perc.get("term_id")
                perc_nome = perc.get("nome") or perc.get("name") or perc.get("post_title") or str(perc_id)
                if not perc_id:
                    continue

                try:
                    tees = _api_get_tees(session, perc_id)
                except Exception as e:
                    log(f"    [{i}] {nome} / percorso '{perc_nome}': ERRORE tees — {e}")
                    stats["errori"] += 1
                    continue

                for tee in tees:
                    try:
                        cr, sr, par, buche, genere = _api_get_cr_sr(session, club_id, perc_id, tee)
                        if cr is None and sr is None:
                            continue
                        if not genere:
                            genere = _TEE_GENERE_API.get(tee.upper(), "M")
                        if buche is None:
                            nome_lower = perc_nome.lower()
                            if "nove" in nome_lower or "prime" in nome_lower or "seconde" in nome_lower:
                                buche = 9
                            elif par is not None:
                                try:
                                    buche = 9 if int(par) <= 40 else 18
                                except (ValueError, TypeError):
                                    buche = 18
                            else:
                                buche = 18
                        ratings_batch.append({
                            "percorso": perc_nome,
                            "tee_colore": tee,
                            "genere": genere,
                            "buche": int(buche),
                            "cr": cr, "sr": sr, "par": par,
                        })
                    except Exception as e:
                        log(f"    [{i}] {nome} / tee={tee}: ERRORE CR/SR — {e}")
                        stats["errori"] += 1

            if ratings_batch:
                stats["ratings"] += save_campo_ratings(campo_id, ratings_batch)
            log(f"  [{i}] {nome}: {len(percorsi)} percorsi, {len(ratings_batch)} rating salvati.")

            # Piccola pausa per non sovraccaricare il server
            time.sleep(0.3)

        except Exception as e:
            log(f"  ERRORE circolo '{nome}': {e}")
            stats["errori"] += 1

    log(f"API campi completato: {stats}")
    return stats

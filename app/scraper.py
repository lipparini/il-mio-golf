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


def _api_get_percorsi(session, club_id: str) -> list[dict]:
    r = session.post(AJAX_URL, data={"action": "club-courses", "club_id": club_id}, timeout=15)
    r.raise_for_status()
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
    r = session.post(AJAX_URL, data={"action": "courses-tees", "percorso_id": str(percorso_id)}, timeout=15)
    r.raise_for_status()
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
    r = session.post(AJAX_URL, data={
        "action": "course-handicap",
        "club_id": club_id,
        "percorso_id": str(percorso_id),
        "tee": tee,
        "handicap": "18.0",
    }, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data or not isinstance(data, list) or not isinstance(data[0], (list, tuple)):
        return None, None, None, None, None
    row = data[0]
    if len(row) < 3:
        return None, None, None, None, None
    return row[1], row[2], row[3] if len(row) > 3 else None, row[4] if len(row) > 4 else None, row[0]


def scrape_campi_api() -> dict:
    """Scraping CR/SR da API pubblica Federgolf. Campi condivisi tra tutti gli utenti."""
    stats = {"campi": 0, "ratings": 0, "errori": 0}

    log("API campi: recupero lista circoli ...")
    clubs = _api_get_clubs()
    log(f"  {len(clubs)} circoli trovati.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": HANDICAP_PAGE_URL,
    })

    for i, club in enumerate(clubs, 1):
        nome = club["nome"]
        club_id = club["club_id"]
        log(f"  [{i}/{len(clubs)}] {nome}")

        try:
            percorsi = _api_get_percorsi(session, club_id)
            if not percorsi:
                continue

            campo_id = save_campo({"nome": nome})
            if not campo_id:
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
                    log(f"    ERRORE tees '{perc_nome}': {e}")
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
                        log(f"    ERRORE CR/SR '{perc_nome}' tee={tee}: {e}")
                        stats["errori"] += 1

            if ratings_batch:
                stats["ratings"] += save_campo_ratings(campo_id, ratings_batch)

        except Exception as e:
            log(f"  ERRORE circolo '{nome}': {e}")
            stats["errori"] += 1

    log(f"API campi completato: {stats}")
    return stats

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

from .models import (save_records, save_campo, save_campo_ratings,
                     update_campo_details, get_campi_with_club_id)

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
BASE_URL          = "https://areariservata.federgolf.it"
HANDICAP_PAGE_URL = "https://www.federgolf.it/settore-tecnico/calcolo-hcp/"
AJAX_URL          = "https://www.federgolf.it/wp-admin/admin-ajax.php"
REGIONI_AJAX_URL  = "https://www.federgolf.it/?ajax-request=jnews"
DETAIL_BASE_URL   = "https://www.federgolf.it/dettaglio-golf-club/"

_FEDERGOLF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.federgolf.it/",
    "Origin":  "https://www.federgolf.it",
}

_REGIONI_URLS = [
    ("Abruzzo",               "https://www.federgolf.it/regioni/abruzzo/"),
    ("Basilicata",            "https://www.federgolf.it/regioni/basilicata/"),
    ("Calabria",              "https://www.federgolf.it/regioni/calabria/"),
    ("Campania",              "https://www.federgolf.it/regioni/campania/"),
    ("Emilia-Romagna",        "https://www.federgolf.it/regioni/emilia-romagna/"),
    ("Friuli-Venezia Giulia", "https://www.federgolf.it/regioni/friuli-venezia-giulia/"),
    ("Lazio",                 "https://www.federgolf.it/regioni/lazio/"),
    ("Liguria",               "https://www.federgolf.it/regioni/liguria/"),
    ("Lombardia",             "https://www.federgolf.it/regioni/lombardia/"),
    ("Marche",                "https://www.federgolf.it/regioni/marche/"),
    ("Molise",                "https://www.federgolf.it/regioni/molise/"),
    ("Piemonte",              "https://www.federgolf.it/regioni/piemonte/"),
    ("Puglia",                "https://www.federgolf.it/regioni/puglia/"),
    ("Sardegna",              "https://www.federgolf.it/regioni/sardegna/"),
    ("Sicilia",               "https://www.federgolf.it/regioni/sicilia/"),
    ("Toscana",               "https://www.federgolf.it/regioni/toscana/"),
    ("Trentino-Alto Adige",   "https://www.federgolf.it/regioni/trentino-alto-adige/"),
    ("Umbria",                "https://www.federgolf.it/regioni/umbria/"),
    ("Valle d'Aosta",         "https://www.federgolf.it/regioni/valle-d-aosta/"),
    ("Veneto",                "https://www.federgolf.it/regioni/veneto/"),
]


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
# Scraper geografico — federgolf.it/regioni/ (fase 1)
# ---------------------------------------------------------------------------

def _parse_address(raw: str) -> tuple[str, str]:
    """Estrae (citta, provincia) da un indirizzo Federgolf tipo '(12345) CITTÀ PV'."""
    raw = raw.strip()
    m = re.search(r'\(\d{5}\)\s+(.+?)\s+([A-Z]{2})\s*$', raw)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", "")


def _extract_regione_id(html_text: str) -> str | None:
    """Estrae il GUID regione_id dal HTML della pagina regionale (per la paginazione AJAX)."""
    m = re.search(r'"regione_id":"([a-f0-9\-]{36})"', html_text)
    if not m:
        m = re.search(r'regione_id\\":\\"([a-f0-9\-]{36})', html_text)
    return m.group(1) if m else None


def _parse_clubs_geografici(html_text: str, regione_nome: str) -> list[dict]:
    """
    Parsa gli articoli .jeg_post_module_37_fig dal HTML.
    Ritorna lista di dict {nome, regione, indirizzo, citta, provincia, club_id}.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    clubs = []
    for art in soup.find_all("article", class_="jeg_post_module_37_fig"):
        strong = art.find("strong")
        nome = strong.get_text(strip=True) if strong else ""
        if not nome:
            continue
        spans = art.find_all("span", class_="jeg_post_module_37_fig_icon_text")
        indirizzo = spans[0].get_text(strip=True) if spans else ""
        citta, provincia = _parse_address(indirizzo)
        # club_id dal link "Scopri di più" (?club_id=<GUID>)
        club_id = None
        link = art.find("a", href=re.compile(r'club_id='))
        if link:
            m = re.search(r'club_id=([a-f0-9\-]{36})', link.get("href", ""))
            if m:
                club_id = m.group(1)
        clubs.append({
            "nome":      nome,
            "regione":   regione_nome,
            "indirizzo": indirizzo,
            "citta":     citta,
            "provincia": provincia,
            "club_id":   club_id,
        })
    return clubs


def _scrape_regione_clubs(sess: requests.Session, regione_nome: str, page_url: str) -> list[dict]:
    """Scarica tutti i club di una regione con paginazione AJAX 'Load More'."""
    try:
        r = sess.get(page_url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log(f"  [{regione_nome}] ERRORE fetch: {e}")
        return []

    regione_id = _extract_regione_id(r.text)
    all_clubs  = _parse_clubs_geografici(r.text, regione_nome)

    if not regione_id:
        log(f"  [{regione_nome}] {len(all_clubs)} club (regione_id non trovato, no paginazione)")
        return all_clubs

    page_num = 2
    while True:
        time.sleep(0.3)
        ajax_data = {
            "lang":                                          "it_IT",
            "action":                                        "jnews_module_ajax_jnews_block_37",
            "module":                                        "true",
            "data[filter]":                                  "0",
            "data[filter_type]":                             "all",
            "data[current_page]":                            str(page_num),
            "data[attribute][post_type]":                    "post",
            "data[attribute][content_type]":                 "all",
            "data[attribute][number_post]":                  "6",
            "data[attribute][sort_by]":                      "latest",
            "data[attribute][pagination_mode]":              "loadmore",
            "data[attribute][pagination_number_post][size]": "4",
            "data[attribute][column_class]":                 "jeg_col_3o3",
            "data[attribute][class]":                        "jnews_block_37",
            "data[attribute][regione_id]":                   regione_id,
            "data[attribute][paged]":                        "1",
        }
        try:
            resp = sess.post(REGIONI_AJAX_URL, data=ajax_data, timeout=30)
            resp.raise_for_status()
            jdata = resp.json()
        except Exception as e:
            log(f"  [{regione_nome}] AJAX pagina {page_num} ERRORE: {e}")
            break

        content = jdata.get("content", "")
        if not content or content.strip() in ("", "false", "null"):
            break

        all_clubs.extend(_parse_clubs_geografici(content, regione_nome))

        if not jdata.get("next", False):
            break
        page_num += 1

    with_id = sum(1 for c in all_clubs if c.get("club_id"))
    log(f"  [{regione_nome}] {len(all_clubs)} club ({with_id} con club_id)")
    return all_clubs


def scrape_regioni_geografiche(progress_cb=None) -> dict:
    """
    Fase 1: scraping geografico da federgolf.it/regioni/ (20 regioni, ~341 circoli).
    Inserisce/aggiorna nome, regione, citta, provincia, indirizzo, club_id.
    """
    stats = {"upsert": 0, "errori": 0}
    n_tot = len(_REGIONI_URLS)
    log("=== Fase 1: scraping geografico (20 regioni) ===")

    sess = requests.Session()
    sess.headers.update(_FEDERGOLF_HEADERS)

    for idx, (regione_nome, page_url) in enumerate(_REGIONI_URLS, 1):
        if progress_cb:
            progress_cb(f"Fase 1/3: geografico — {regione_nome} ({idx}/{n_tot}) · {stats['upsert']} circoli")
        clubs = _scrape_regione_clubs(sess, regione_nome, page_url)
        for club in clubs:
            try:
                if save_campo(club):
                    stats["upsert"] += 1
                else:
                    stats["errori"] += 1
            except Exception as e:
                log(f"  ERRORE DB '{club.get('nome')}': {e}")
                stats["errori"] += 1
        time.sleep(0.8)

    log(f"=== Fase 1 completata: {stats} ===")
    return stats


# ---------------------------------------------------------------------------
# Scraper dettaglio — federgolf.it/dettaglio-golf-club/ (fase 2)
# ---------------------------------------------------------------------------

def _parse_detail_page(html_text: str) -> dict:
    """
    Estrae buche_tot, telefono, email, sito_web dalla pagina dettaglio circolo.
    Struttura HTML: <div class="tabella-dettagli-container">
                      <div class="tabella-dettagli-label">Telefono</div>
                      <div class="tabella-dettagli-value">...</div>
                    </div>
    """
    soup = BeautifulSoup(html_text, "html.parser")
    data = {}
    for container in soup.find_all("div", class_="tabella-dettagli-container"):
        label_el = container.find("div", class_="tabella-dettagli-label")
        value_el = container.find("div", class_="tabella-dettagli-value")
        if not label_el or not value_el:
            continue
        label = label_el.get_text(strip=True)
        if label == "Numero buche":
            val = value_el.get_text(strip=True)
            if val and val.isdigit():
                data["buche_tot"] = val
        elif label == "Telefono":
            val = value_el.get_text(strip=True)
            if val:
                data["telefono"] = val
        elif label == "E-mail":
            a = value_el.find("a", href=re.compile(r'^mailto:'))
            if a:
                data["email"] = a["href"].replace("mailto:", "").strip()
            else:
                val = value_el.get_text(strip=True)
                if "@" in val:
                    data["email"] = val
        elif label == "Sito web":
            a = value_el.find("a", href=True)
            if a and a["href"].startswith("http"):
                data["sito_web"] = a["href"].rstrip("/")
            else:
                val = value_el.get_text(strip=True)
                if val.startswith("http"):
                    data["sito_web"] = val.rstrip("/")
    return data


def scrape_details(progress_cb=None) -> dict:
    """
    Fase 2: scraping pagine dettaglio per tutti i circoli con club_id nel DB.
    Aggiorna buche_tot, telefono, email, sito_web.
    """
    stats = {"aggiornati": 0, "vuoti": 0, "errori": 0}
    log("=== Fase 2: scraping dettaglio circoli ===")

    campi = get_campi_with_club_id()
    n_tot = len(campi)
    log(f"  {n_tot} circoli con club_id nel DB.")

    sess = requests.Session()
    sess.headers.update(_FEDERGOLF_HEADERS)

    for i, campo in enumerate(campi, 1):
        if i % 50 == 0 or i == 1:
            log(f"  [{i}/{n_tot}] aggiornati={stats['aggiornati']} errori={stats['errori']}")
        if progress_cb and (i % 50 == 0 or i == 1):
            progress_cb(f"Fase 2/3: dettaglio circoli [{i}/{n_tot}]")
        try:
            url = f"{DETAIL_BASE_URL}?club_id={campo['club_id']}"
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            detail = _parse_detail_page(r.text)
            if update_campo_details(campo["id"], detail):
                stats["aggiornati"] += 1
            else:
                stats["vuoti"] += 1
        except Exception as e:
            log(f"  [{i}] {campo['nome']}: ERRORE — {e}")
            stats["errori"] += 1
        time.sleep(0.3)

    log(f"=== Fase 2 completata: {stats} ===")
    return stats


# ---------------------------------------------------------------------------
# Scraper campi — API pubblica (condivisa, nessun utente_id)
# ---------------------------------------------------------------------------

_TEE_GENERE_API = {
    "NERO": "M", "BIANCO": "M", "GIALLO": "M", "VERDE": "M",
    "BLU": "F", "ROSSO": "F", "ARANCIO": "F",
}



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
    """
    Risposta API: [{"percorso_id": "<guid>", "nome_percorso": "18 Buche Par 71"}, ...]
    Normalizza in {"id": ..., "nome": ...} per il resto del codice.
    """
    r = _api_post_with_retry(session, {"action": "club-courses", "club_id": club_id})
    data = r.json()
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if isinstance(item, dict):
            pid  = item.get("percorso_id") or item.get("id") or item.get("ID") or item.get("term_id")
            nome = item.get("nome_percorso") or item.get("nome") or item.get("name") or item.get("post_title") or str(pid)
            if pid:
                result.append({"id": pid, "nome": nome})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            result.append({"id": item[0], "nome": item[1]})
    return result


def _api_get_tees(session, percorso_id) -> list[str]:
    """
    Risposta API: [["BIANCO","BIANCO"],["GIALLO","GIALLO"], ...]
    """
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
        elif isinstance(item, int):
            tees.append(str(item))
    return tees


def _api_get_cr_sr(session, club_id: str, percorso_id, tee: str):
    """
    Risposta API: [["ACAYA - 18 Buche Par 71", 73.9, 136, "Bianco", 25, 0]]
      row[0] = descrizione percorso (stringa)
      row[1] = CR (float)
      row[2] = SR (int)
      row[3] = tee colore capitalizzato (non usato — viene da parametro)
      row[4] = Course Handicap calcolato dall'API per HCP Index 18.0
               (per i percorsi a 9 buche l'API applica la regola WHS e usa
               internamente metà indice, 9.0 — vedi _par_da_playing_handicap)
    Ritorna (cr, sr, playing_hcp_18): il par si ricava altrove invertendo la
    formula del playing handicap, molto più affidabile del nome del percorso
    (alcuni percorsi, es. "Prime Nove", non contengono "Par NN" nel nome).
    """
    r = _api_post_with_retry(session, {
        "action": "course-handicap",
        "club_id": club_id,
        "percorso_id": str(percorso_id),
        "tee": tee,
        "handicap": "18.0",
    })
    data = r.json()
    if not data or not isinstance(data, list):
        return None, None, None
    row = data[0]
    if isinstance(row, (list, tuple)):
        cr = row[1] if len(row) >= 2 else None
        sr = row[2] if len(row) >= 3 else None
        ph18 = row[4] if len(row) >= 5 else None
        return cr, sr, ph18
    if isinstance(row, dict):
        cr = row.get("cr") or row.get("CR") or row.get("course_rating")
        sr = row.get("sr") or row.get("SR") or row.get("slope_rating")
        ph18 = row.get("playing_handicap") or row.get("course_handicap")
        return cr, sr, ph18
    return None, None, None


def _par_da_playing_handicap(cr, sr, ph18, buche):
    """
    Ricava il par invertendo la formula WHS del Playing Handicap che l'API
    restituisce già calcolato per HCP Index 18.0 (row[4] di course-handicap):
        PH = round(HI_eff * SR/113 + CR - Par)  =>  Par = round(HI_eff*SR/113 + CR) - PH
    HI_eff è 9.0 per i percorsi a 9 buche (regola WHS: si usa metà indice),
    18.0 per i percorsi a 18 buche. L'arrotondamento commuta con la
    traslazione per un intero, quindi la formula inversa è esatta (verificato
    con l'API reale su percorsi con par noto dal nome, es. "18 Buche Par 71",
    "Seconde Nove Par 35/36").
    """
    if cr is None or sr is None or ph18 is None:
        return None
    hi_eff = 9.0 if buche == 9 else 18.0
    return round(hi_eff * sr / 113 + cr) - ph18


def scrape_campi_api(progress_cb=None) -> dict:
    """
    Scraping CR/SR da API pubblica Federgolf (requests, NO Playwright).
    Usa i club_id già nel DB dalla fase 1 geografica — più robusto dello scraping HTML
    della pagina handicap (che carica la select via JS).
    """
    stats = {"campi": 0, "ratings": 0, "errori": 0, "skip_no_percorsi": 0}

    log("API campi: recupero circoli con club_id dal database ...")
    try:
        clubs_db = get_campi_with_club_id()
    except Exception as e:
        log(f"  ERRORE FATALE recupero circoli dal DB: {e}")
        return stats

    n_tot = len(clubs_db)
    log(f"  {n_tot} circoli con club_id nel DB.")
    if not clubs_db:
        log("  Nessun circolo con club_id — esegui prima la fase 1 (geografico).")
        return stats

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": HANDICAP_PAGE_URL,
    })

    for i, club in enumerate(clubs_db, 1):
        campo_id = club["id"]
        club_id  = club["club_id"]
        nome     = club["nome"]

        if i % 10 == 0 or i == 1:
            log(f"  [{i}/{n_tot}] campi={stats['campi']} rating={stats['ratings']} errori={stats['errori']}")
            if progress_cb:
                progress_cb(f"Fase 3/3: CR/SR — [{i}/{n_tot}] · {stats['ratings']} rating")

        try:
            percorsi = _api_get_percorsi(session, club_id)
            if not percorsi:
                log(f"  [{i}] {nome}: nessun percorso, skip.")
                stats["skip_no_percorsi"] += 1
                time.sleep(0.2)
                continue

            stats["campi"] += 1

            ratings_batch = []
            for perc in percorsi:
                perc_id   = perc.get("id")
                perc_nome = perc.get("nome") or str(perc_id)
                if not perc_id:
                    continue

                # Fallback se il nome non contiene "Par NN" (es. "Prime Nove") — il par
                # vero viene ricavato dall'API in _par_da_playing_handicap qui sotto.
                par_m = re.search(r'[Pp]ar\s*(\d+)', perc_nome)
                par_from_nome = int(par_m.group(1)) if par_m else None
                nome_lower = perc_nome.lower()
                if "nove" in nome_lower or "prime" in nome_lower or "seconde" in nome_lower:
                    buche_from_nome = 9
                else:
                    buche_from_nome = 18

                try:
                    tees = _api_get_tees(session, perc_id)
                except Exception as e:
                    log(f"    [{i}] {nome} / '{perc_nome}': ERRORE tees — {e}")
                    stats["errori"] += 1
                    continue

                for tee in tees:
                    try:
                        cr, sr, ph18 = _api_get_cr_sr(session, club_id, perc_id, tee)
                        if cr is None or sr is None:
                            continue
                        par = _par_da_playing_handicap(cr, sr, ph18, buche_from_nome)
                        if par is None:
                            par = par_from_nome
                        genere = _TEE_GENERE_API.get(tee.upper(), "M")
                        log(f"    Salvato rating: campo={nome} percorso={perc_nome} tee={tee} CR={cr} SR={sr} par={par}")
                        ratings_batch.append({
                            "percorso": perc_nome,
                            "tee_colore": tee,
                            "genere": genere,
                            "buche": buche_from_nome,
                            "cr": cr, "sr": sr, "par": par,
                        })
                    except Exception as e:
                        log(f"    [{i}] {nome} / tee={tee}: ERRORE CR/SR — {e}")
                        stats["errori"] += 1

            if ratings_batch:
                stats["ratings"] += save_campo_ratings(campo_id, ratings_batch)
            log(f"  [{i}] {nome}: {len(percorsi)} percorsi, {len(ratings_batch)} rating.")
            time.sleep(0.3)

        except Exception as e:
            log(f"  ERRORE circolo '{nome}': {e}")
            stats["errori"] += 1

    log(f"API campi completato: {stats}")
    return stats


def scrape_campi_completo(progress_cb=None) -> dict:
    """
    Scraping completo campi in 3 fasi sequenziali:
    1. Geografico:  ~341 circoli da federgolf.it/regioni/
                    → nome, regione, citta, provincia, indirizzo, club_id
    2. Dettaglio:   ~339 circoli con club_id
                    → buche_tot, telefono, email, sito_web
    3. CR/SR API:   ~341 circoli dall'API admin-ajax.php
                    → campi_rating (percorso, tee, CR, SR, par)
    """
    log("=== scrape_campi_completo: inizio ===")
    stats = {
        "fase1": scrape_regioni_geografiche(progress_cb=progress_cb),
        "fase2": scrape_details(progress_cb=progress_cb),
        "fase3": scrape_campi_api(progress_cb=progress_cb),
    }
    log(f"=== scrape_campi_completo: terminato — {stats} ===")
    return stats

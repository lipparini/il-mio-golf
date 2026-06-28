"""
Job notturno: ogni notte alle 3:00 scarica le gare di tutti gli utenti attivi.
"""

import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = BackgroundScheduler(daemon=True)
log = logging.getLogger(__name__)


def _run_nightly(app):
    with app.app_context():
        from .models import get_active_users_with_credentials, log_scraper_start, log_scraper_done, log_scraper_error
        from .scraper import scrape_and_save

        users = get_active_users_with_credentials()
        log.info(f"Job notturno: {len(users)} utenti da aggiornare.")

        for user in users:
            log_id = log_scraper_start(user["id"], "gare")
            try:
                added = scrape_and_save(
                    user["federgolf_username"],
                    user["federgolf_password"],
                    user["id"],
                )
                log_scraper_done(log_id, added)
                log.info(f"  Utente {user['id']}: {added} gare aggiornate.")
            except Exception as e:
                log_scraper_error(log_id, str(e))
                log.error(f"  Utente {user['id']} ERRORE: {e}")


def start_scheduler(app):
    hour = int(os.environ.get("SCHEDULER_HOUR", 3))
    minute = int(os.environ.get("SCHEDULER_MINUTE", 0))

    _scheduler.add_job(
        _run_nightly,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[app],
        id="nightly_scrape",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    if not _scheduler.running:
        _scheduler.start()
        log.info(f"Scheduler avviato: job notturno alle {hour:02d}:{minute:02d}.")

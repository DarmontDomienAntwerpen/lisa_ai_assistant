"""Dagelijkse database-backup: dumpt alle tabellen als JSON en mailt dat als
bijlage naar Darmont Digital zelf, via de Gmail API (app/gmail_sender.py) —
niet SMTP, want Railway blokkeert uitgaande SMTP-poorten op het Trial/Hobby-
plan. Puur Python (geen afhankelijkheid van een `pg_dump`-binary die mogelijk
niet aanwezig is in de Railway-runtime).

Beschermt tegen schijf-/volume-defect of per-ongeluk-verwijderen van de
Postgres-service — een gewone process-crash heeft dit NIET nodig (Postgres
herstelt zichzelf via de eigen write-ahead-log), dit is enkel voor het
scenario waarbij de data zelf verloren gaat.

Entrypoint voor een Railway cron-service, analoog aan de GDPR-retentiepurge
in conversation_store.py: `python -m app.backup_db`, dagelijks.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

TABLES = ["tenants", "customers", "conversations", "usage_log", "calls"]


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Niet serialiseerbaar: {type(value)}")


async def _dump_all_tables(pool) -> dict:
    dump = {}
    async with pool.acquire() as conn:
        for table in TABLES:
            rows = await conn.fetch(f"SELECT * FROM {table}")  # noqa: S608 — vaste, interne tabellenlijst, geen user input
            dump[table] = [dict(r) for r in rows]
    return dump


async def _run_backup() -> None:
    import asyncio

    from app import gmail_sender
    from app.config import close_pool, get_pool

    pool = await get_pool()
    dump = await _dump_all_tables(pool)
    await close_pool()

    row_counts = {t: len(rows) for t, rows in dump.items()}
    now = datetime.now(timezone.utc)
    filename = f"lisa-backup-{now.strftime('%Y-%m-%d')}.json"
    payload = json.dumps(dump, default=_json_default, ensure_ascii=False).encode("utf-8")

    if not gmail_sender.is_configured():
        print(f"[backup] Gmail-verzendaccount niet geconfigureerd — dump NIET verstuurd. Rijen per tabel: {row_counts}")
        return

    from app.config import GMAIL_SENDER_ADDRESS

    await asyncio.to_thread(
        gmail_sender.send_email,
        GMAIL_SENDER_ADDRESS,  # naar jezelf — geen apart backup-mailadres nodig
        f"Lisa: dagelijkse database-backup — {now.strftime('%Y-%m-%d')}",
        f"Automatische backup, rijen per tabel: {row_counts}",
        (payload, "application", "json", filename),
    )
    print(f"[backup] verstuurd, rijen per tabel: {row_counts}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_backup())

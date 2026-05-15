"""Shadow-mode benefit logging.

Records planned vs. actual every 30 min and computes hypothetical savings.
Stores data in /data/shadow_log.db (SQLite, rolling 30-day window).

Hypothetical savings formula (per slot):
    actual_import = max(0, -grid_flow_kw * 0.5)   # kWh
    planned_grid  = max(0, actual_load_kw - actual_pv_kw - planned_battery_delta_kw * 0.5)
    savings_slot  = (actual_import - planned_import) * tariff_price_pln_per_kwh

Positive = optimizer would have reduced import cost.
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path("/data/shadow_log.db")
RETENTION_DAYS = 30

_db: Optional[sqlite3.Connection] = None
_last_purge_date: Optional[str] = None


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("""
            CREATE TABLE IF NOT EXISTS shadow_slots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                slot        INTEGER NOT NULL,
                rule        TEXT NOT NULL,
                actual_import_kwh  REAL NOT NULL,
                planned_import_kwh REAL NOT NULL,
                tariff_price REAL NOT NULL,
                savings_pln REAL NOT NULL
            )
        """)
        _db.execute("CREATE INDEX IF NOT EXISTS idx_ts ON shadow_slots(ts)")
        _db.commit()
    return _db


def _maybe_purge(db: sqlite3.Connection) -> None:
    global _last_purge_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _last_purge_date == today:
        return
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
    db.execute("DELETE FROM shadow_slots WHERE ts < ?", (cutoff,))
    _last_purge_date = today


def _sum_since(prefix: str) -> float:
    try:
        row = _get_db().execute(
            "SELECT COALESCE(SUM(savings_pln), 0) FROM shadow_slots WHERE ts >= ?",
            (prefix,),
        ).fetchone()
        return round(float(row[0]), 2)
    except Exception as exc:
        log.warning("shadow_log sum query failed: %s", exc)
        return 0.0


def record(
    ts: datetime,
    slot: int,
    rule: str,
    actual_grid_kw: float,
    actual_load_kw: float,
    actual_pv_kw: float,
    planned_battery_delta_kw: float,
    tariff_price: float,
) -> float:
    """Log one 30-min slot and return the slot's hypothetical savings in PLN."""
    actual_import_kwh = max(0.0, -actual_grid_kw * 0.5)
    net_load = actual_load_kw - actual_pv_kw
    planned_import_kwh = max(0.0, (net_load + planned_battery_delta_kw) * 0.5)
    savings_pln = (actual_import_kwh - planned_import_kwh) * tariff_price

    try:
        db = _get_db()
        db.execute(
            "INSERT INTO shadow_slots (ts, slot, rule, actual_import_kwh, planned_import_kwh, tariff_price, savings_pln) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), slot, rule,
             round(actual_import_kwh, 4), round(planned_import_kwh, 4),
             round(tariff_price, 4), round(savings_pln, 4)),
        )
        _maybe_purge(db)
        db.commit()
    except Exception as exc:
        log.warning("shadow_log.record failed: %s", exc)

    return savings_pln


def today_savings() -> float:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return _sum_since(today + "T00:00:00")


def month_savings() -> float:
    month = datetime.utcnow().strftime("%Y-%m")
    return _sum_since(month + "-01T00:00:00")


def recent_rows(n: int = 48) -> list[dict]:
    try:
        rows = _get_db().execute(
            "SELECT ts, slot, rule, actual_import_kwh, planned_import_kwh, savings_pln "
            "FROM shadow_slots ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [
            {
                "ts": r[0], "slot": r[1], "rule": r[2],
                "actual_import_kwh": r[3], "planned_import_kwh": r[4],
                "savings_pln": r[5],
            }
            for r in reversed(rows)
        ]
    except Exception as exc:
        log.warning("shadow_log.recent_rows failed: %s", exc)
        return []

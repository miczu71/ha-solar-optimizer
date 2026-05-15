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

log = logging.getLogger(__name__)

DB_PATH = Path("/data/shadow_log.db")
RETENTION_DAYS = 30


def _conn() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("""
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_ts ON shadow_slots(ts)")
    db.commit()
    return db


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
    planned_grid_kw = net_load + planned_battery_delta_kw
    planned_import_kwh = max(0.0, planned_grid_kw * 0.5)

    savings_pln = (actual_import_kwh - planned_import_kwh) * tariff_price

    try:
        db = _conn()
        db.execute(
            "INSERT INTO shadow_slots (ts, slot, rule, actual_import_kwh, planned_import_kwh, tariff_price, savings_pln) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), slot, rule,
             round(actual_import_kwh, 4), round(planned_import_kwh, 4),
             round(tariff_price, 4), round(savings_pln, 4)),
        )
        _purge_old(db)
        db.commit()
        db.close()
    except Exception as exc:
        log.warning("shadow_log.record failed: %s", exc)

    return savings_pln


def _purge_old(db: sqlite3.Connection) -> None:
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
    db.execute("DELETE FROM shadow_slots WHERE ts < ?", (cutoff,))


def today_savings() -> float:
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        db = _conn()
        row = db.execute(
            "SELECT COALESCE(SUM(savings_pln), 0) FROM shadow_slots WHERE ts >= ?",
            (today + "T00:00:00",),
        ).fetchone()
        db.close()
        return round(float(row[0]), 2)
    except Exception as exc:
        log.warning("shadow_log.today_savings failed: %s", exc)
        return 0.0


def month_savings() -> float:
    try:
        month = datetime.utcnow().strftime("%Y-%m")
        db = _conn()
        row = db.execute(
            "SELECT COALESCE(SUM(savings_pln), 0) FROM shadow_slots WHERE ts >= ?",
            (month + "-01T00:00:00",),
        ).fetchone()
        db.close()
        return round(float(row[0]), 2)
    except Exception as exc:
        log.warning("shadow_log.month_savings failed: %s", exc)
        return 0.0


def recent_rows(n: int = 48) -> list[dict]:
    try:
        db = _conn()
        rows = db.execute(
            "SELECT ts, slot, rule, actual_import_kwh, planned_import_kwh, savings_pln "
            "FROM shadow_slots ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        db.close()
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

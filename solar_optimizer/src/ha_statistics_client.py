"""Read HA long-term statistics directly from the SQLite recorder database.

HA statistics REST API does not exist (WebSocket-only). This module opens
the DB read-only via sqlite3 (built-in, no extra dependency) using the
/config mount added by 'map: config:ro' in config.yaml.
"""
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)

HA_DB_PATH = "/config/home-assistant_v2.db"


def get_ha_statistics_30min(
    statistic_ids: list[str], days_back: int = 365
) -> dict[str, pd.Series]:
    """Return hourly long-term statistics upsampled to 30-min for each entity.

    Uses the 'statistics' table (kept forever) rather than recorder history
    (purged after 7 days). Each hourly value is forward-filled into both
    30-min sub-slots of that hour.
    """
    since_ts = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp()

    try:
        conn = sqlite3.connect(
            f"file:{HA_DB_PATH}?mode=ro",
            uri=True,
            timeout=15.0,
            check_same_thread=False,
        )
    except Exception as exc:
        log.warning("Cannot open HA SQLite DB at %s: %s", HA_DB_PATH, exc)
        return {}

    try:
        placeholders = ",".join("?" * len(statistic_ids))
        rows = conn.execute(
            f"""
            SELECT sm.statistic_id, s.start_ts, s.mean, s.sum
            FROM statistics s
            JOIN statistics_meta sm ON s.metadata_id = sm.id
            WHERE sm.statistic_id IN ({placeholders})
              AND s.start_ts >= ?
            ORDER BY sm.statistic_id, s.start_ts
            """,
            statistic_ids + [since_ts],
        ).fetchall()
    except Exception as exc:
        log.error("HA statistics query failed: %s", exc)
        return {}
    finally:
        conn.close()

    grouped: dict[str, list] = defaultdict(list)
    for statistic_id, start_ts, mean, sum_val in rows:
        grouped[statistic_id].append((start_ts, mean, sum_val))

    result: dict[str, pd.Series] = {}
    for eid, records in grouped.items():
        index = pd.to_datetime([r[0] for r in records], unit="s", utc=True)
        # mean for power/temp sensors, sum for energy (total_increasing) sensors
        values = [r[1] if r[1] is not None else r[2] for r in records]
        s = pd.Series(values, index=index, dtype=float).dropna()
        if s.empty:
            result[eid] = s
            continue
        # Upsample 1h → 30min: forward-fill hourly value into both sub-slots
        result[eid] = s.resample("30min").ffill()

    log.info(
        "HA statistics: %d entities, %d total 30-min slots over last %d days",
        len(result),
        sum(len(v) for v in result.values()),
        days_back,
    )
    return result

"""Persistence for DHW thermal parameters learned from historical data.

Auto-calibration (previously InfluxDB-based) has been removed.
Parameters are set via config defaults (dhw_loss_rate_c_per_hour, dhw_cop)
and saved here if manually tuned or externally calibrated.
"""
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
PARAMS_FILE = Path("/data/learned_params.json")


def load_params() -> Optional[dict]:
    try:
        if PARAMS_FILE.exists():
            return json.loads(PARAMS_FILE.read_text())
    except Exception as exc:
        log.warning("Could not load learned params: %s", exc)
    return None


def save_params(params: dict) -> None:
    try:
        PARAMS_FILE.write_text(json.dumps(params, indent=2))
        log.info("Saved learned thermal params: loss_rate=%.3f °C/h  cop=%.2f",
                 params.get("dhw_loss_rate_c_per_hour", 0),
                 params.get("dhw_cop", 0))
    except Exception as exc:
        log.warning("Could not save learned params: %s", exc)

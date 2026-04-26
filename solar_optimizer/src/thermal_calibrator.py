"""Weekly auto-calibration of DHW thermal parameters from InfluxDB history.

Fits two parameters:
  dhw_loss_rate_c_per_hour — tank heat loss during idle (pump off) periods
  dhw_cop                  — heat pump COP during active heating periods

Results saved to /data/learned_params.json and reloaded each replan.
Config defaults are used as fallback when calibration data is insufficient.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)
PARAMS_FILE = Path("/data/learned_params.json")
MIN_IDLE_SAMPLES = 20   # minimum idle-period data points before fitting loss rate
MIN_HEAT_SAMPLES = 10   # minimum heating-period data points before fitting COP
IDLE_POWER_THRESHOLD_W = 50    # below this = pump idle
HEAT_POWER_THRESHOLD_W = 200   # above this = pump heating


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


def calibrate_dhw_params(influx, cfg) -> dict:
    """Query 30 days of InfluxDB data and fit DHW thermal parameters.

    Returns a dict with dhw_loss_rate_c_per_hour and dhw_cop.
    Falls back to cfg defaults if insufficient data.
    """
    result = {
        "dhw_loss_rate_c_per_hour": cfg.dhw_loss_rate_c_per_hour,
        "dhw_cop": cfg.dhw_cop,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "calibrated": False,
        "loss_rate_samples": 0,
        "cop_samples": 0,
    }
    try:
        # Query 30-min resampled series
        dhw_temp_series = influx._query_resampled("°C", "heiko_hot_water_dhw_temperature",
                                                   days_back=30, resample="5m", agg="mean")
        hp_power_series = influx._query_resampled("W", "heiko_heat_pump_electrical_power",
                                                   days_back=30, resample="5m", agg="mean")

        if dhw_temp_series.empty or hp_power_series.empty:
            log.warning("Thermal calibration: insufficient InfluxDB data, keeping defaults")
            return result

        # Align on common timestamps
        import pandas as pd
        df = pd.DataFrame({"temp": dhw_temp_series, "power_w": hp_power_series}).dropna()
        if len(df) < MIN_IDLE_SAMPLES + MIN_HEAT_SAMPLES:
            log.warning("Thermal calibration: only %d aligned samples, need %d",
                        len(df), MIN_IDLE_SAMPLES + MIN_HEAT_SAMPLES)
            return result

        dt_hours = 5 / 60  # 5-min resampling in hours

        # ── Loss rate from idle periods ──────────────────────────────────────
        idle_mask = df["power_w"] < IDLE_POWER_THRESHOLD_W
        idle_df = df[idle_mask].copy()
        if len(idle_df) >= MIN_IDLE_SAMPLES:
            # dT/dt from consecutive idle samples (same idle window)
            idle_df["temp_next"] = idle_df["temp"].shift(-1)
            idle_df["dt"] = (idle_df["temp_next"] - idle_df["temp"]) / dt_hours
            # Only keep clearly dropping samples (losses > 0.01 °C/h) in idle mode
            drops = idle_df["dt"][idle_df["dt"] < -0.01]
            if len(drops) >= MIN_IDLE_SAMPLES:
                fitted_loss = float(-drops.median())
                fitted_loss = max(0.1, min(3.0, fitted_loss))  # sanity clamp 0.1–3°C/h
                result["dhw_loss_rate_c_per_hour"] = round(fitted_loss, 3)
                result["loss_rate_samples"] = len(drops)
                log.info("Calibrated DHW loss rate: %.3f °C/h (from %d samples)",
                         fitted_loss, len(drops))

        # ── COP from heating periods ─────────────────────────────────────────
        heat_mask = df["power_w"] >= HEAT_POWER_THRESHOLD_W
        heat_df = df[heat_mask].copy()
        if len(heat_df) >= MIN_HEAT_SAMPLES:
            heat_df["temp_next"] = heat_df["temp"].shift(-1)
            heat_df["dT"] = heat_df["temp_next"] - heat_df["temp"]
            # Thermal mass: 4.186 kJ/(kg·K) × tank_liters kg ÷ 3600 → kWh/°C
            thermal_mass_kwh_per_c = cfg.dhw_tank_liters * 4.186 / 3600
            heat_df["thermal_kwh"] = heat_df["dT"].clip(lower=0) * thermal_mass_kwh_per_c
            # Loss during heating slot
            loss_kwh = result["dhw_loss_rate_c_per_hour"] * dt_hours * thermal_mass_kwh_per_c
            heat_df["thermal_kwh"] = (heat_df["thermal_kwh"] + loss_kwh).clip(lower=0)
            heat_df["elec_kwh"] = heat_df["power_w"] / 1000 * dt_hours
            valid = heat_df[(heat_df["elec_kwh"] > 0.001) & (heat_df["thermal_kwh"] > 0)]
            if len(valid) >= MIN_HEAT_SAMPLES:
                cop_values = (valid["thermal_kwh"] / valid["elec_kwh"]).clip(1.0, 6.0)
                fitted_cop = float(cop_values.median())
                result["dhw_cop"] = round(fitted_cop, 2)
                result["cop_samples"] = len(valid)
                log.info("Calibrated DHW COP: %.2f (from %d samples)", fitted_cop, len(valid))

        result["calibrated"] = True

    except Exception as exc:
        log.warning("DHW calibration failed, keeping defaults: %s", exc, exc_info=True)

    return result

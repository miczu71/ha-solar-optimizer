"""Feature engineering for ML training: base-load disentanglement and feature matrix."""
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from influx_client import InfluxClient

log = logging.getLogger(__name__)


def build_training_features(influx: InfluxClient, days_back: int = 90) -> pd.DataFrame:
    house = influx.house_consumption_30min(days_back=days_back)
    hp = influx.heatpump_power_30min(days_back=days_back)
    ac_s = influx.ac_salon_energy_30min(days_back=days_back)
    ac_p = influx.ac_pietro_30min(days_back=days_back)
    ac_d = influx.ac_poddasze_30min(days_back=days_back)
    outdoor = influx.outdoor_temp_30min(days_back=days_back)
    pv = influx.pv_power_30min(days_back=days_back)

    df = pd.concat(
        [house, hp, ac_s, ac_p, ac_d, outdoor, pv],
        axis=1,
        join="outer",
    )
    df.columns = ["house_kwh", "hp_kwh", "ac_salon_kwh", "ac_pietro_kwh",
                  "ac_poddasze_kwh", "outdoor_temp", "pv_kwh"]
    df = df.resample("30min").mean()

    # AC, PV and heatpump are genuinely 0 when not running — fill gaps with 0
    for col in ["hp_kwh", "ac_salon_kwh", "ac_pietro_kwh", "ac_poddasze_kwh", "pv_kwh"]:
        df[col] = df[col].fillna(0.0)

    # Forward-fill short outages in critical sensors (up to 2 hours)
    df["house_kwh"] = df["house_kwh"].ffill(limit=4)
    df["outdoor_temp"] = df["outdoor_temp"].ffill(limit=8)

    # Only drop rows where critical sensors are still missing
    df = df.dropna(subset=["house_kwh", "outdoor_temp"])

    if df.empty:
        log.warning("Training dataset is empty after join/resample")
        return df

    df["base_load_kwh"] = (
        df["house_kwh"] - df["hp_kwh"] - df["ac_salon_kwh"]
        - df["ac_pietro_kwh"] - df["ac_poddasze_kwh"]
    ).clip(lower=0)

    df["slot"] = df.index.hour * 2 + df.index.minute // 30
    df["day_of_week"] = df.index.dayofweek
    df["month"] = df.index.month
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)

    df["lag_1d"] = df["base_load_kwh"].shift(48)
    df["lag_7d"] = df["base_load_kwh"].shift(48 * 7)

    daily_pv = df["pv_kwh"].resample("1D").sum()
    df["pv_yesterday_kwh"] = df.index.normalize().map(
        lambda d: daily_pv.get(d - pd.Timedelta(days=1), np.nan)
    )

    df = df.dropna(subset=["lag_1d", "lag_7d", "pv_yesterday_kwh"])
    log.info("Training dataset: %d rows spanning %d days", len(df), days_back)
    return df


FEATURE_COLS = [
    "slot", "day_of_week", "month", "is_weekend",
    "outdoor_temp", "lag_1d", "lag_7d", "pv_yesterday_kwh",
]
TARGET_COL = "base_load_kwh"


def build_forecast_row(
    slot: int,
    now: datetime,
    outdoor_temp: float,
    lag_1d: float,
    lag_7d: float,
    pv_yesterday_kwh: float,
) -> dict:
    return {
        "slot": slot,
        "day_of_week": now.weekday(),
        "month": now.month,
        "is_weekend": int(now.weekday() >= 5),
        "outdoor_temp": outdoor_temp,
        "lag_1d": lag_1d,
        "lag_7d": lag_7d,
        "pv_yesterday_kwh": pv_yesterday_kwh,
    }

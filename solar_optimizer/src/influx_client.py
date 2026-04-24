"""InfluxDB v1 query helpers for historical load and PV data.

HA stores data with measurement = unit_of_measurement (e.g. "W", "kWh", "°C")
and entity_id (without domain prefix) as a tag.
Example: FROM "W" WHERE "entity_id" = 'house_consumption_power'
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from influxdb import DataFrameClient

from config import Config

log = logging.getLogger(__name__)


class InfluxClient:
    def __init__(self, cfg: Config) -> None:
        self._client = DataFrameClient(
            host=cfg.influx_host,
            port=cfg.influx_port,
            username=cfg.influx_username,
            password=cfg.influx_password,
            database=cfg.influx_database,
        )

    def _query_resampled(
        self,
        measurement: str,
        entity_id: str,
        days_back: int = 90,
        resample: str = "30m",
        agg: str = "mean",
    ) -> pd.Series:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        q = (
            f'SELECT {agg}("value") AS val '
            f'FROM "{measurement}" '
            f"WHERE \"entity_id\" = '{entity_id}' AND time >= '{since}' "
            f"GROUP BY time({resample}) fill(null)"
        )
        result = self._client.query(q)
        if not result:
            return pd.Series(dtype=float)
        df: pd.DataFrame = result[measurement]
        return df["val"].dropna()

    def house_consumption_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("W", "house_consumption_power", days_back=days_back)
        return s * 0.5 / 1000

    def heatpump_power_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("W", "heiko_heat_pump_electrical_power", days_back=days_back)
        return s * 0.5 / 1000

    def ac_salon_energy_30min(self, days_back: int = 90) -> pd.Series:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        q = (
            f'SELECT difference(last("value")) AS val '
            f'FROM "kWh" '
            f"WHERE \"entity_id\" = 'klima_salon_total_energy' AND time >= '{since}' "
            f"GROUP BY time(30m) fill(null)"
        )
        result = self._client.query(q)
        if not result:
            return pd.Series(dtype=float)
        df: pd.DataFrame = result["kWh"]
        return df["val"].clip(lower=0).dropna()

    def ac_pietro_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("W", "miernik_energii_klimatyzacje_power_a", days_back=days_back)
        return s * 0.5 / 1000

    def ac_poddasze_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("W", "miernik_energii_klimatyzacje_power_b", days_back=days_back)
        return s * 0.5 / 1000

    def outdoor_temp_30min(self, days_back: int = 90) -> pd.Series:
        return self._query_resampled("°C", "temperature_weather_station", days_back=days_back)

    def pv_power_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("W", "inverter_input_power", days_back=days_back)
        return s * 0.5 / 1000

    def rolling_mean_base_load(self, days_back: int = 7) -> pd.Series:
        house = self.house_consumption_30min(days_back=days_back)
        hp = self.heatpump_power_30min(days_back=days_back)
        ac_s = self.ac_salon_energy_30min(days_back=days_back)
        ac_p = self.ac_pietro_30min(days_back=days_back)
        ac_d = self.ac_poddasze_30min(days_back=days_back)

        combined = pd.DataFrame({
            "house": house,
            "hp": hp,
            "ac_s": ac_s,
            "ac_p": ac_p,
            "ac_d": ac_d,
        }).fillna(0)

        if not isinstance(combined.index, pd.DatetimeIndex) or combined.empty:
            raise ValueError("No time-indexed InfluxDB data available for rolling mean")

        combined["base"] = (
            combined["house"]
            - combined["hp"]
            - combined["ac_s"]
            - combined["ac_p"]
            - combined["ac_d"]
        ).clip(lower=0)

        combined["slot"] = combined.index.hour * 2 + combined.index.minute // 30
        return combined.groupby("slot")["base"].mean()

    def pv_total_yesterday(self) -> float:
        """Return total PV generation (kWh) for yesterday (UTC date)."""
        pv = self.pv_power_30min(days_back=2)
        if pv.empty:
            return 5.0
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        pv_yest = pv[pv.index.date == yesterday]
        return float(pv_yest.sum()) if not pv_yest.empty else 5.0

    def check_data_availability(self) -> dict[str, int]:
        """Return number of days (out of last 90) that have at least one data point."""
        sensors = {
            "house": ("W", "house_consumption_power"),
            "heatpump": ("W", "heiko_heat_pump_electrical_power"),
            "outdoor_temp": ("°C", "temperature_weather_station"),
        }
        result = {}
        for key, (meas, eid) in sensors.items():
            q = (
                f'SELECT count("value") FROM "{meas}" '
                f"WHERE \"entity_id\" = '{eid}' AND time >= now() - 90d "
                f"GROUP BY time(1d) fill(0)"
            )
            try:
                res = self._client.query(q)
                if res:
                    df = list(res.values())[0]
                    result[key] = int((df.iloc[:, 0] > 0).sum())
                else:
                    result[key] = 0
            except Exception as exc:
                log.warning("InfluxDB availability check failed for %s: %s", eid, exc)
                result[key] = -1
        return result

"""InfluxDB v1 query helpers for historical load and PV data."""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from influxdb import DataFrameClient

from config import Config

log = logging.getLogger(__name__)

SLOT_MINUTES = 30


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
        field: str = "value",
        days_back: int = 90,
        resample: str = "30min",
        agg: str = "mean",
    ) -> pd.Series:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        q = (
            f'SELECT {agg}("{field}") AS val '
            f'FROM "{measurement}" '
            f"WHERE time >= '{since}' "
            f"GROUP BY time({resample}) fill(null)"
        )
        result = self._client.query(q)
        if not result:
            return pd.Series(dtype=float)
        df: pd.DataFrame = result[measurement]
        return df["val"].dropna()

    def house_consumption_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("sensor.house_consumption_power", days_back=days_back)
        return s * 0.5 / 1000

    def heatpump_power_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("sensor.heiko_heat_pump_electrical_power", days_back=days_back)
        return s * 0.5 / 1000

    def ac_salon_energy_30min(self, days_back: int = 90) -> pd.Series:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        q = (
            f'SELECT difference(last("value")) AS val '
            f'FROM "sensor.klima_salon_total_energy" '
            f"WHERE time >= '{since}' "
            f"GROUP BY time(30m) fill(null)"
        )
        result = self._client.query(q)
        if not result:
            return pd.Series(dtype=float)
        df: pd.DataFrame = result["sensor.klima_salon_total_energy"]
        return df["val"].clip(lower=0).dropna()

    def ac_pietro_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled(
            "sensor.miernik_energii_klimatyzacje_power_a", days_back=days_back
        )
        return s * 0.5 / 1000

    def ac_poddasze_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled(
            "sensor.miernik_energii_klimatyzacje_power_b", days_back=days_back
        )
        return s * 0.5 / 1000

    def outdoor_temp_30min(self, days_back: int = 90) -> pd.Series:
        return self._query_resampled(
            "sensor.temperature_weather_station", days_back=days_back
        )

    def pv_power_30min(self, days_back: int = 90) -> pd.Series:
        s = self._query_resampled("sensor.inverter_input_power", days_back=days_back)
        return s * 0.5 / 1000

    def rolling_mean_base_load(self, days_back: int = 7) -> pd.DataFrame:
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

        combined["base"] = (
            combined["house"]
            - combined["hp"]
            - combined["ac_s"]
            - combined["ac_p"]
            - combined["ac_d"]
        ).clip(lower=0)

        combined["slot"] = (
            combined.index.hour * 2 + combined.index.minute // 30
        )
        return combined.groupby("slot")["base"].mean()

    def check_data_availability(self) -> dict[str, int]:
        result = {}
        sensors = {
            "house": "sensor.house_consumption_power",
            "heatpump": "sensor.heiko_heat_pump_electrical_power",
            "outdoor_temp": "sensor.temperature_weather_station",
        }
        for key, meas in sensors.items():
            q = f'SELECT count("value") FROM "{meas}" WHERE time >= now() - 90d'
            try:
                res = self._client.query(q)
                if res:
                    df = list(res.values())[0]
                    count = int(df.iloc[0, 0]) if not df.empty else 0
                    result[key] = count // 48
                else:
                    result[key] = 0
            except Exception as exc:
                log.warning("InfluxDB availability check failed for %s: %s", meas, exc)
                result[key] = -1
        return result

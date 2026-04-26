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

    @staticmethod
    def _bare_id(entity_id: str) -> str:
        """Strip HA domain prefix: 'sensor.foo' → 'foo'."""
        return entity_id.split(".")[-1] if "." in entity_id else entity_id

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

    # ---- Deferrable device helpers ----------------------------------------

    def _device_raw_w_30min(self, entity_id: str, days_back: int = 90) -> pd.Series:
        """Raw 30-min mean power (W) for any W-measurement entity."""
        return self._query_resampled("W", self._bare_id(entity_id), days_back=days_back)

    def device_power_30min(self, entity_id: str, days_back: int = 90) -> pd.Series:
        """30-min slot energy (kWh) from a W power sensor."""
        s = self._device_raw_w_30min(entity_id, days_back=days_back)
        return s * 0.5 / 1000

    def device_energy_diff_30min(self, entity_id: str, days_back: int = 90) -> pd.Series:
        """30-min slot energy (kWh) from a total_increasing kWh sensor."""
        bare = self._bare_id(entity_id)
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        q = (
            f'SELECT difference(last("value")) AS val '
            f'FROM "kWh" '
            f"WHERE \"entity_id\" = '{bare}' AND time >= '{since}' "
            f"GROUP BY time(30m) fill(null)"
        )
        result = self._client.query(q)
        if not result:
            return pd.Series(dtype=float)
        df = result.get("kWh")
        if df is None or df.empty:
            return pd.Series(dtype=float)
        return df["val"].clip(lower=0).dropna()

    @staticmethod
    def _default_pattern(load_cfg: dict) -> dict:
        return {
            "power_w": float(load_cfg.get("power_w", 1000)),
            "duration_min": float(load_cfg.get("duration_min", 60)),
            "typical_start_slot": int(load_cfg.get("earliest_slot", 20)),
            "daily_run_prob": 0.7,
            "source": "config_defaults",
        }

    @staticmethod
    def _analyse_runs(series_w: pd.Series, threshold_w: float, defaults: dict) -> dict:
        """Detect contiguous active segments above threshold and return learned statistics."""
        if series_w.empty:
            return InfluxClient._default_pattern(defaults)

        active = series_w > threshold_w
        runs_power: list[float] = []
        runs_duration: list[float] = []
        runs_start_slot: list[int] = []
        days_with_runs: set = set()

        in_run = False
        run_start_idx = 0
        run_vals: list[float] = []

        for i in range(len(series_w)):
            ts = series_w.index[i]
            val = float(series_w.iloc[i])
            is_active = bool(active.iloc[i])

            if is_active and not in_run:
                in_run = True
                run_start_idx = i
                run_vals = [val]
                if isinstance(ts, pd.Timestamp):
                    runs_start_slot.append(ts.hour * 2 + ts.minute // 30)
                    days_with_runs.add(ts.date())
            elif is_active and in_run:
                run_vals.append(val)
            elif not is_active and in_run:
                in_run = False
                duration_slots = i - run_start_idx
                if duration_slots >= 1 and run_vals:
                    runs_power.append(sum(run_vals) / len(run_vals))
                    runs_duration.append(duration_slots * 30.0)
                run_vals = []

        if in_run and run_vals:
            runs_power.append(sum(run_vals) / len(run_vals))
            runs_duration.append((len(series_w) - run_start_idx) * 30.0)

        total_days = (
            max(1, len(series_w.index.normalize().unique()))
            if isinstance(series_w.index, pd.DatetimeIndex) else 1
        )
        daily_run_prob = round(len(days_with_runs) / total_days, 3)

        def _median(lst: list[float]) -> float:
            s = sorted(lst)
            n = len(s)
            return (s[n // 2] + s[(n - 1) // 2]) / 2

        return {
            "power_w": _median(runs_power) if runs_power else float(defaults.get("power_w", 1000)),
            "duration_min": _median(runs_duration) if runs_duration else float(defaults.get("duration_min", 60)),
            "typical_start_slot": (
                int(_median([float(x) for x in runs_start_slot]))
                if runs_start_slot else int(defaults.get("earliest_slot", 20))
            ),
            "daily_run_prob": daily_run_prob,
            "source": "learned",
        }

    def learn_device_patterns(self, loads_cfg: list[dict]) -> dict:
        """Learn run patterns for deferrable loads from InfluxDB with HA stats fallback.

        Returns {name: {power_w, duration_min, typical_start_slot, daily_run_prob, source}}
        Falls back to HA long-term statistics (SQLite) when InfluxDB has no data.
        """
        patterns: dict = {}
        for load in loads_cfg:
            name = load.get("name", "load")
            threshold_w = float(load.get("run_threshold_w", 50))
            series_w: pd.Series | None = None

            # 1. InfluxDB: power_entity (W)
            if "power_entity" in load:
                try:
                    s = self._device_raw_w_30min(load["power_entity"])
                    if not s.empty:
                        series_w = s
                except Exception as exc:
                    log.debug("InfluxDB W fetch failed for '%s': %s", name, exc)

            # 2. InfluxDB: energy_entity (kWh diff → W equivalent)
            if series_w is None and "energy_entity" in load:
                try:
                    s = self.device_energy_diff_30min(load["energy_entity"])
                    if not s.empty:
                        series_w = s * 2000  # kWh/slot → W (÷ 0.5h × 1000)
                except Exception as exc:
                    log.debug("InfluxDB kWh fetch failed for '%s': %s", name, exc)

            # 3. HA long-term statistics fallback (SQLite, months of hourly data)
            if (series_w is None or series_w.empty) and "power_entity" in load:
                try:
                    from ha_statistics_client import get_ha_statistics_30min
                    stats = get_ha_statistics_30min([load["power_entity"]], days_back=90)
                    s = stats.get(load["power_entity"])
                    if s is not None and not s.empty:
                        series_w = s
                        log.info("Device '%s': using HA long-term stats for pattern learning", name)
                except Exception as exc:
                    log.debug("HA stats fallback failed for '%s': %s", name, exc)

            if series_w is None or series_w.empty:
                patterns[name] = self._default_pattern(load)
                log.info("Device '%s': no historical data, using config defaults", name)
                continue

            patterns[name] = self._analyse_runs(series_w, threshold_w, load)
            log.info(
                "Learned '%s': power=%.0fW dur=%.0fmin start_slot=%d prob=%.2f [%s]",
                name, patterns[name]["power_w"], patterns[name]["duration_min"],
                patterns[name]["typical_start_slot"], patterns[name]["daily_run_prob"],
                patterns[name]["source"],
            )

        return patterns

    def device_ran_today(self, entity_id: str, run_threshold_w: float = 50.0) -> bool:
        """Return True if the device exceeded run_threshold_w in any 30-min slot today (InfluxDB)."""
        bare = self._bare_id(entity_id)
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        q = (
            f'SELECT max("value") AS val '
            f'FROM "W" '
            f"WHERE \"entity_id\" = '{bare}' AND time >= '{since}' "
            f"GROUP BY time(30m) fill(null)"
        )
        try:
            result = self._client.query(q)
            if not result:
                return False
            df = result.get("W")
            if df is None or df.empty:
                return False
            return bool((df["val"].dropna() > run_threshold_w).any())
        except Exception as exc:
            log.debug("device_ran_today InfluxDB failed for '%s': %s", bare, exc)
            return False

    # ---- Base-load helpers ------------------------------------------------

    def rolling_mean_base_load(
        self,
        days_back: int = 7,
        deferrable_power_entities: list[str] | None = None,
    ) -> pd.Series:
        house = self.house_consumption_30min(days_back=days_back)
        hp = self.heatpump_power_30min(days_back=days_back)
        ac_s = self.ac_salon_energy_30min(days_back=days_back)
        ac_p = self.ac_pietro_30min(days_back=days_back)
        ac_d = self.ac_poddasze_30min(days_back=days_back)

        data: dict[str, pd.Series] = {
            "house": house, "hp": hp, "ac_s": ac_s, "ac_p": ac_p, "ac_d": ac_d,
        }

        # Fetch deferrable-load power so their cycles don't inflate base-load ML signal
        if deferrable_power_entities:
            for eid in deferrable_power_entities:
                col = f"def_{self._bare_id(eid)}"
                try:
                    s = self.device_power_30min(eid, days_back=days_back)
                    if not s.empty:
                        data[col] = s
                except Exception as exc:
                    log.debug("Failed to fetch deferrable power for %s: %s", eid, exc)

        combined = pd.DataFrame(data).fillna(0)

        if not isinstance(combined.index, pd.DatetimeIndex) or combined.empty:
            raise ValueError("No time-indexed InfluxDB data available for rolling mean")

        base = (
            combined["house"]
            - combined["hp"]
            - combined["ac_s"]
            - combined["ac_p"]
            - combined["ac_d"]
        )
        for col in [c for c in combined.columns if c.startswith("def_")]:
            base = base - combined[col]

        combined["base"] = base.clip(lower=0)
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

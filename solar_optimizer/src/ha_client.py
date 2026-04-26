"""HA REST API wrapper with sign-convention normalization."""
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import Config

log = logging.getLogger(__name__)

TIMEOUT = 10.0


class HAClient:
    def __init__(self, cfg: Config) -> None:
        self._base = cfg.ha_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {cfg.ha_token}",
            "Content-Type": "application/json",
        }
        self._solcast_cache: dict[str, list[dict]] = {}
        self.tz: ZoneInfo = ZoneInfo("UTC")  # updated by init_timezone()

    def init_timezone(self) -> None:
        """Read HA's configured timezone from /api/config. Call once at startup."""
        try:
            r = httpx.get(f"{self._base}/api/config", headers=self._headers, timeout=TIMEOUT)
            r.raise_for_status()
            tz_str = r.json().get("time_zone", "UTC")
            self.tz = ZoneInfo(tz_str)
            log.info("HA timezone: %s", tz_str)
        except Exception as exc:
            log.warning("Could not read HA timezone, defaulting to UTC: %s", exc)

    @property
    def local_now(self) -> datetime:
        """Current time in HA's configured timezone."""
        return datetime.now(tz=self.tz)

    def get_state(self, entity_id: str) -> dict[str, Any]:
        url = f"{self._base}/api/states/{entity_id}"
        r = httpx.get(url, headers=self._headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def get_state_value(self, entity_id: str, default: float = 0.0) -> float:
        try:
            state = self.get_state(entity_id)["state"]
            return float(state)
        except Exception:
            log.warning("Failed to read %s, using default %s", entity_id, default)
            return default

    def get_state_str(self, entity_id: str, default: str = "") -> str:
        try:
            return str(self.get_state(entity_id)["state"])
        except Exception:
            log.warning("Failed to read str state %s, using default '%s'", entity_id, default)
            return default

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        url = f"{self._base}/api/services/{domain}/{service}"
        r = httpx.post(url, headers=self._headers, json=data, timeout=TIMEOUT)
        r.raise_for_status()

    @property
    def soc_percent(self) -> float:
        return self.get_state_value("sensor.battery_state_of_capacity")

    @property
    def soc_min_from_backup(self) -> float:
        return self.get_state_value("number.battery_backup_power_soc", default=10.0)

    @property
    def grid_import_w(self) -> float:
        raw = self.get_state_value("sensor.power_meter_active_power")
        return max(0.0, -raw)

    @property
    def grid_export_w(self) -> float:
        raw = self.get_state_value("sensor.power_meter_active_power")
        return max(0.0, raw)

    @property
    def battery_charge_w(self) -> float:
        raw = self.get_state_value("sensor.battery_charge_discharge_power")
        return max(0.0, raw)

    @property
    def battery_discharge_w(self) -> float:
        raw = self.get_state_value("sensor.battery_charge_discharge_power")
        return max(0.0, -raw)

    @property
    def pv_power_w(self) -> float:
        return self.get_state_value("sensor.inverter_input_power")

    @property
    def house_load_w(self) -> float:
        return self.get_state_value("sensor.house_consumption_power")

    @property
    def dhw_tank_temp(self) -> float:
        return self.get_state_value("sensor.heiko_heat_pump_water_temperature", default=45.0)

    @property
    def dhw_setpoint(self) -> float:
        return self.get_state_value("number.heiko_heat_pump_dhw_setpoint", default=48.0)

    @property
    def heatpump_power_w(self) -> float:
        return self.get_state_value("sensor.heiko_heat_pump_electrical_power")

    @property
    def outdoor_temp(self) -> float:
        return self.get_state_value("sensor.temperature_weather_station", default=15.0)

    def is_workday(self, entity_id: str = "binary_sensor.workday") -> bool:
        """Return True if HA workday sensor reports today as a working day.

        Handles all Polish public holidays automatically — the HA workday integration
        already excludes them. Falls back to weekday check if entity unavailable.
        """
        try:
            return self.get_state(entity_id)["state"] == "on"
        except Exception as exc:
            log.warning("Could not read workday entity %s (%s) — falling back to weekday check", entity_id, exc)
            return self.local_now.weekday() < 5

    def get_ha_float(self, entity_id: str, default: float = 0.0) -> float:
        return self.get_state_value(entity_id, default=default)

    def get_ha_bool(self, entity_id: str) -> bool:
        return self.get_state_str(entity_id, default="off").lower() in ("on", "true", "1")

    @property
    def bath_request(self) -> bool:
        try:
            return self.get_state("input_boolean.temperatura_do_kapieli")["state"] == "on"
        except Exception:
            return False

    def get_history_today_30min(self, entity_ids: list[str]) -> dict[str, list]:
        """Fetch today's HA history for given entities and resample into 48 half-hour slots.

        Returns {entity_id: [48 values or None]} where None means no data for that slot.
        """
        today = self.local_now
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        url = f"{self._base}/api/history/period/{start.isoformat()}"
        params = {
            "filter_entity_id": ",".join(entity_ids),
            "end_time": today.isoformat(),
            "minimal_response": "true",
            "no_attributes": "true",
        }
        result: dict[str, list] = {eid: [None] * 48 for eid in entity_ids}
        sums: dict[str, list] = {eid: [0.0] * 48 for eid in entity_ids}
        counts: dict[str, list] = {eid: [0] * 48 for eid in entity_ids}
        try:
            r = httpx.get(url, headers=self._headers, params=params, timeout=20.0)
            r.raise_for_status()
            for entity_hist in r.json():
                if not entity_hist:
                    continue
                eid = entity_hist[0].get("entity_id", "")
                if eid not in result:
                    continue
                for pt in entity_hist:
                    try:
                        val = float(pt["state"])
                        ts = datetime.fromisoformat(pt["last_changed"].replace("Z", "+00:00"))
                        ts_local = ts.astimezone(self.tz)
                        slot = ts_local.hour * 2 + ts_local.minute // 30
                        if 0 <= slot < 48:
                            sums[eid][slot] += val
                            counts[eid][slot] += 1
                    except (ValueError, KeyError, TypeError):
                        continue
            for eid in entity_ids:
                result[eid] = [
                    round(sums[eid][s] / counts[eid][s], 2) if counts[eid][s] > 0 else None
                    for s in range(48)
                ]
        except Exception as exc:
            log.warning("HA history fetch failed: %s", exc)
        return result

    def get_solcast_forecast(self) -> list[dict]:
        """Returns merged today+tomorrow 30-min forecast slots, with per-entity cache fallback."""
        slots: list[dict] = []
        for entity in (
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
        ):
            try:
                state = self.get_state(entity)
                detailed = state.get("attributes", {}).get("detailedForecast", [])
                if detailed:
                    self._solcast_cache[entity] = detailed
                    slots.extend(detailed)
                elif entity in self._solcast_cache:
                    log.info("Solcast %s returned empty; using cache (%d slots)", entity, len(self._solcast_cache[entity]))
                    slots.extend(self._solcast_cache[entity])
            except Exception as exc:
                log.warning("Solcast fetch failed for %s: %s", entity, exc)
                if entity in self._solcast_cache:
                    log.info("Using cached Solcast data for %s (%d slots)", entity, len(self._solcast_cache[entity]))
                    slots.extend(self._solcast_cache[entity])
        return slots

    def set_dhw_setpoint(self, value: float) -> None:
        self.call_service(
            "number", "set_value",
            {"entity_id": "number.heiko_heat_pump_dhw_setpoint", "value": value},
        )

    def set_dhw_restart_dt(self, value: float) -> None:
        self.call_service(
            "number", "set_value",
            {"entity_id": "number.heiko_heat_pump_dhw_restart_dt", "value": value},
        )

    def forcible_charge(self, duration_min: int, power_w: int) -> None:
        self.call_service(
            "huawei_solar", "forcible_charge",
            {"duration_min": duration_min, "power_w": power_w},
        )

    def stop_forcible_charge(self) -> None:
        self.call_service("huawei_solar", "stop_forcible_charge", {})

    def set_ac_setpoint(self, entity_id: str, temperature: float) -> None:
        self.call_service(
            "climate", "set_temperature",
            {"entity_id": entity_id, "temperature": temperature},
        )

    def device_ran_today_ha(self, entity_id: str, run_threshold_w: float = 50.0) -> bool:
        """Return True if HA history shows power > threshold at any 30-min slot today."""
        history = self.get_history_today_30min([entity_id])
        slots = history.get(entity_id, [])
        return any(v is not None and v > run_threshold_w for v in slots)

    def get_ac_state(self, entity_id: str) -> dict[str, Any]:
        return self.get_state(entity_id)

"""HA REST API wrapper with sign-convention normalization."""
import logging
from typing import Any

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
        return self.get_state_value("sensor.heiko_hot_water_dhw_temperature", default=45.0)

    @property
    def dhw_setpoint(self) -> float:
        return self.get_state_value("number.heiko_heat_pump_dhw_setpoint", default=48.0)

    @property
    def heatpump_power_w(self) -> float:
        return self.get_state_value("sensor.heiko_heat_pump_electrical_power")

    @property
    def outdoor_temp(self) -> float:
        return self.get_state_value("sensor.temperature_weather_station", default=15.0)

    @property
    def bath_request(self) -> bool:
        try:
            return self.get_state("input_boolean.temperatura_do_kapieli")["state"] == "on"
        except Exception:
            return False

    def get_solcast_forecast(self) -> list[dict]:
        slots: list[dict] = []
        for entity in (
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
        ):
            try:
                state = self.get_state(entity)
                detailed = state.get("attributes", {}).get("detailedForecast", [])
                slots.extend(detailed)
            except Exception as exc:
                log.warning("Solcast fetch failed for %s: %s", entity, exc)
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

    def get_ac_state(self, entity_id: str) -> dict[str, Any]:
        return self.get_state(entity_id)

"""Translates optimizer schedule into HA service calls with anti-thrash logic.

In shadow mode all calls are logged but not sent.
"""
import logging
from typing import Optional

from config import Config
from ha_client import HAClient
from optimizer import OptimizeResult

log = logging.getLogger(__name__)

HEAT_EPSILON_KWH = 0.02

AC_ENTITIES = {
    "salon": "climate.153931628323418_climate",
    "pietro": "climate.152832117304366_climate",
    "poddasze": "climate.152832117518705_climate",
}

AC_NOMINAL_SETPOINTS = {
    "salon": 22.0,
    "pietro": 22.0,
    "poddasze": 22.0,
}


class Executor:
    def __init__(self, cfg: Config, ha: HAClient, shadow: bool = True) -> None:
        self._cfg = cfg
        self._ha = ha
        self._shadow = shadow
        self._last_dhw_setpoint: Optional[float] = None
        self._last_dhw_restart_dt: Optional[float] = None
        self._last_ac_setpoints: dict[str, float] = {}
        self._forcible_charge_active = False

    def apply_slot(
        self,
        slot: int,
        result: OptimizeResult,
        dhw_enabled: bool,
        battery_enabled: bool,
        ac_enabled: bool,
    ) -> None:
        if dhw_enabled:
            self._apply_dhw(slot, result)
        if battery_enabled:
            self._apply_battery(slot, result)
        if ac_enabled:
            self._apply_ac(slot, result)

    def _apply_dhw(self, slot: int, result: OptimizeResult) -> None:
        heat = result.dhw_heat_energy[slot] if slot < len(result.dhw_heat_energy) else 0.0

        try:
            bath_req = self._ha.bath_request
        except Exception:
            bath_req = False

        if bath_req or heat > HEAT_EPSILON_KWH:
            setpoint = self._cfg.dhw_solar_setpoint
            restart_dt = self._cfg.dhw_restart_dt_aggressive
        else:
            setpoint = self._cfg.dhw_baseline_setpoint
            restart_dt = self._cfg.dhw_restart_dt_default

        self._write_dhw(setpoint, restart_dt)

    def _write_dhw(self, setpoint: float, restart_dt: float) -> None:
        if setpoint != self._last_dhw_setpoint:
            log.info("DHW setpoint -> %.1fdegC (shadow=%s)", setpoint, self._shadow)
            if not self._shadow:
                self._ha.set_dhw_setpoint(setpoint)
            self._last_dhw_setpoint = setpoint

        if restart_dt != self._last_dhw_restart_dt:
            log.info("DHW restart_dt -> %.1fdegC (shadow=%s)", restart_dt, self._shadow)
            if not self._shadow:
                self._ha.set_dhw_restart_dt(restart_dt)
            self._last_dhw_restart_dt = restart_dt

    def _apply_battery(self, slot: int, result: OptimizeResult) -> None:
        target_w = result.offpeak_precharge_w[slot] if slot < len(result.offpeak_precharge_w) else 0.0

        if target_w > 50 and not self._forcible_charge_active:
            power_w = int(min(target_w, self._cfg.battery_max_charge_power_w))
            log.info("Forcible charge -> %d W for 30 min (shadow=%s)", power_w, self._shadow)
            if not self._shadow:
                self._ha.forcible_charge(duration_min=30, power_w=power_w)
            self._forcible_charge_active = True
        elif target_w <= 50 and self._forcible_charge_active:
            log.info("Stop forcible charge (shadow=%s)", self._shadow)
            if not self._shadow:
                self._ha.stop_forcible_charge()
            self._forcible_charge_active = False

    def _apply_ac(self, slot: int, result: OptimizeResult) -> None:
        for unit, entity_id in AC_ENTITIES.items():
            delta = result.ac_deltas.get(unit, [0.0] * 48)
            if slot >= len(delta):
                continue
            d = delta[slot]
            target = AC_NOMINAL_SETPOINTS[unit] + d
            last = self._last_ac_setpoints.get(unit)
            if last is None or abs(target - last) >= 0.5:
                log.info("AC %s setpoint -> %.1fdegC (shadow=%s)", unit, target, self._shadow)
                if not self._shadow:
                    self._ha.set_ac_setpoint(entity_id, target)
                self._last_ac_setpoints[unit] = target

    def failsafe(self) -> None:
        log.warning("Executor failsafe triggered -- restoring defaults (shadow=%s)", self._shadow)
        if not self._shadow:
            try:
                self._ha.set_dhw_setpoint(self._cfg.dhw_baseline_setpoint)
                self._ha.set_dhw_restart_dt(self._cfg.dhw_restart_dt_default)
            except Exception as exc:
                log.error("DHW failsafe write failed: %s", exc)
            try:
                if self._forcible_charge_active:
                    self._ha.stop_forcible_charge()
            except Exception as exc:
                log.error("Battery failsafe write failed: %s", exc)
        self._last_dhw_setpoint = None
        self._last_dhw_restart_dt = None
        self._forcible_charge_active = False

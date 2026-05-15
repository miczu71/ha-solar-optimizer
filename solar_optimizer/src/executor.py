"""Translates a Plan into HA service calls.

Default: shadow mode — logs every intended action but sends nothing.
Live mode: activated by config.shadow_mode=False AND the relevant switch_live flag.
Anti-thrash: only sends a service call when the target value differs from last sent.
"""
import logging
from typing import Literal, Optional

from config import Config
from ha_client import HAClient
from planner import BatteryAction, DHWAction, ACAction, Plan

log = logging.getLogger(__name__)

HEAT_EPSILON = 0.5  # °C — minimum setpoint change to trigger a write


class Executor:
    def __init__(self, cfg: Config, ha: HAClient) -> None:
        self._cfg = cfg
        self._ha = ha
        self._last_dhw_setpoint: Optional[float] = None
        self._last_dhw_restart_dt: Optional[float] = None
        self._last_ac_setpoints: dict[str, float] = {}
        self._forcible_charge_active = False

    def _live(self, domain: Literal["battery", "dhw", "ac"], mqtt_publisher) -> bool:
        if self._cfg.shadow_mode:
            return False
        if domain == "battery":
            return mqtt_publisher.is_battery_live()
        if domain == "dhw":
            return mqtt_publisher.is_dhw_live()
        if domain == "ac":
            return mqtt_publisher.is_ac_live()
        return False

    def apply_plan(self, plan: Plan, mqtt_publisher, current_slot: int) -> None:
        self._apply_battery(plan.battery, mqtt_publisher)
        self._apply_dhw(plan.dhw, mqtt_publisher)
        for ac_action in plan.ac_actions:
            self._apply_ac(ac_action, mqtt_publisher)

    # ------------------------------------------------------------------
    # Battery
    # ------------------------------------------------------------------

    def _apply_battery(self, action: BatteryAction, mqtt) -> None:
        live = self._live("battery", mqtt)

        if action.type == "grid_charge":
            if not self._forcible_charge_active:
                pw = min(action.grid_charge_power_w, self._cfg.battery_max_charge_power_w)
                log.info("Battery: forcible_charge %d W / 30 min (live=%s rule=%s)", pw, live, action.rule)
                if live:
                    self._ha.forcible_charge(duration_min=30, power_w=pw)
                self._forcible_charge_active = True
        else:
            if self._forcible_charge_active:
                log.info("Battery: stop_forcible_charge (live=%s rule=%s)", live, action.rule)
                if live:
                    self._ha.stop_forcible_charge()
                self._forcible_charge_active = False

    # ------------------------------------------------------------------
    # DHW
    # ------------------------------------------------------------------

    def _apply_dhw(self, action: DHWAction, mqtt) -> None:
        live = self._live("dhw", mqtt)

        if self._last_dhw_setpoint is None or abs(action.setpoint - self._last_dhw_setpoint) >= HEAT_EPSILON:
            log.info("DHW: setpoint %.1f°C (live=%s) — %s", action.setpoint, live, action.reason)
            if live:
                self._ha.set_dhw_setpoint(action.setpoint)
            self._last_dhw_setpoint = action.setpoint

        if self._last_dhw_restart_dt is None or abs(action.restart_dt - self._last_dhw_restart_dt) >= 0.1:
            log.info("DHW: restart_dt %.1f°C (live=%s)", action.restart_dt, live)
            if live:
                self._ha.set_dhw_restart_dt(action.restart_dt)
            self._last_dhw_restart_dt = action.restart_dt

    # ------------------------------------------------------------------
    # AC
    # ------------------------------------------------------------------

    def _apply_ac(self, action: ACAction, mqtt) -> None:
        live = self._live("ac", mqtt)
        nominal = 22.0
        target = nominal + action.setpoint_delta
        last = self._last_ac_setpoints.get(action.unit)
        if last is None or abs(target - last) >= 0.5:
            log.info("AC %s: setpoint %.1f°C (live=%s) — %s", action.unit, target, live, action.reason)
            if live:
                self._ha.set_ac_setpoint(action.entity_id, target)
            self._last_ac_setpoints[action.unit] = target

    # ------------------------------------------------------------------
    # Failsafe
    # ------------------------------------------------------------------

    def failsafe(self, mqtt_publisher=None) -> None:
        live_dhw = self._live("dhw", mqtt_publisher) if mqtt_publisher else False
        live_bat = self._live("battery", mqtt_publisher) if mqtt_publisher else False
        log.warning("Executor failsafe triggered (live_dhw=%s live_bat=%s)", live_dhw, live_bat)
        if live_dhw:
            try:
                self._ha.set_dhw_setpoint(self._cfg.dhw_baseline_setpoint)
                self._ha.set_dhw_restart_dt(self._cfg.dhw_restart_dt_default)
            except Exception as exc:
                log.error("DHW failsafe failed: %s", exc)
        if live_bat and self._forcible_charge_active:
            try:
                self._ha.stop_forcible_charge()
            except Exception as exc:
                log.error("Battery failsafe failed: %s", exc)
        self._last_dhw_setpoint = None
        self._last_dhw_restart_dt = None
        self._forcible_charge_active = False

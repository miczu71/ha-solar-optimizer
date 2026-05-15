"""Rule-based planner: determines what the optimizer should do each 30-min slot.

Decision hierarchy (battery, then DHW, then AC):
  R0  SAFETY    soc < reserve floor → idle, protect battery
  R1  PV_CHARGE pv_now > load_now AND soc < max → inverter charges naturally, log only
  R2  PEAK      is_peak_now → idle, never grid-charge during peak
  R3  TOPUP     last off-peak window before peak AND shortfall exists → grid_charge
  R4  EXPORT    PV surplus today covers load + free battery space → idle, export surplus
  R5  CATCH_ALL no other condition → idle

DHW:
  PV surplus > 1.5 kW sustained → heat_to_58
  tank < 45°C AND within 2h of morning demand peak → heat_comfort
  else → coast

AC:
  within 30 min of peak start AND outdoor > 28°C AND any unit cooling → precool -1°C
  else → release overrides
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from tariff import (
    OffpeakWindow,
    is_peak,
    next_offpeak_window,
    offpeak_hours_remaining_tonight,
    PEAK_PRICE,
    OFFPEAK_PRICE,
)

log = logging.getLogger(__name__)

AC_UNITS = ["salon", "pietro", "poddasze"]
AC_ENTITIES = {
    "salon":    "climate.153931628323418_climate",
    "pietro":   "climate.152832117304366_climate",
    "poddasze": "climate.152832117518705_climate",
}


@dataclass
class BatteryAction:
    type: Literal["idle", "pv_charge", "grid_charge"]
    rule: str
    reason: str
    target_soc_pct: float = 0.0
    grid_charge_start: Optional[datetime] = None
    grid_charge_end: Optional[datetime] = None
    grid_charge_power_w: int = 2500


@dataclass
class DHWAction:
    type: Literal["coast", "heat_solar", "heat_comfort"]
    setpoint: float
    restart_dt: float
    reason: str


@dataclass
class ACAction:
    unit: str
    entity_id: str
    setpoint_delta: float
    reason: str


@dataclass
class Plan:
    generated_at: datetime
    battery: BatteryAction
    dhw: DHWAction
    ac_actions: list[ACAction]
    # 96-slot arrays covering today (0-47) and tomorrow (48-95)
    pv_forecast_kw: list[float] = field(default_factory=list)
    load_forecast_kw: list[float] = field(default_factory=list)
    is_peak_96: list[bool] = field(default_factory=list)
    # 49-point SoC trajectory for the rest of today (from current slot to slot 47+1)
    soc_trajectory: list[float] = field(default_factory=list)
    # Savings estimate (PLN) if this plan were executed vs. current automations
    savings_estimate_pln: float = 0.0


class Planner:
    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def plan(
        self,
        now: datetime,
        soc_pct: float,
        pv_now_kw: float,
        load_now_kw: float,
        pv_forecast_kw_96: list[float],
        load_forecast_kw_96: list[float],
        is_peak_96: list[bool],
        workday_today: bool,
        workday_tomorrow: bool,
        dhw_tank_temp: float,
        outdoor_temp: float,
        ac_states: dict[str, str],
        bath_requested: bool,
    ) -> Plan:
        cfg = self._cfg

        # Current slot index in today's 48-slot day
        current_slot = now.hour * 2 + now.minute // 30

        # Battery constants
        cap = cfg.battery_capacity_kwh
        soc_reserve = cfg.soc_reserve_pct         # 16 % — hardware backup floor
        soc_max = cfg.soc_max_percent              # 95 %
        soc_kwh = soc_pct / 100 * cap
        soc_min_kwh = soc_reserve / 100 * cap

        battery_action = self._plan_battery(
            now, current_slot, soc_pct, soc_kwh, soc_min_kwh, soc_max,
            cap, pv_now_kw, load_now_kw,
            pv_forecast_kw_96, load_forecast_kw_96, is_peak_96,
            workday_today, workday_tomorrow,
        )

        dhw_action = self._plan_dhw(
            now, current_slot, pv_now_kw, load_now_kw, dhw_tank_temp,
            workday_today, bath_requested,
        )

        ac_actions = self._plan_ac(now, outdoor_temp, ac_states, is_peak_96, current_slot)

        # Simulate SoC trajectory from current slot to end of day
        soc_traj = self._simulate_soc(
            soc_pct, current_slot, pv_forecast_kw_96[:48], load_forecast_kw_96[:48],
            is_peak_96[:48], battery_action, cap, soc_min_kwh, soc_max,
        )

        log.info(
            "Plan rule=%s reason=%s | DHW=%s | AC=%d units",
            battery_action.rule, battery_action.reason,
            dhw_action.type,
            len([a for a in ac_actions if a.setpoint_delta != 0]),
        )

        return Plan(
            generated_at=now,
            battery=battery_action,
            dhw=dhw_action,
            ac_actions=ac_actions,
            pv_forecast_kw=pv_forecast_kw_96,
            load_forecast_kw=load_forecast_kw_96,
            is_peak_96=is_peak_96,
            soc_trajectory=soc_traj,
        )

    # ------------------------------------------------------------------
    # Battery planning
    # ------------------------------------------------------------------

    def _plan_battery(
        self,
        now: datetime,
        current_slot: int,
        soc_pct: float,
        soc_kwh: float,
        soc_min_kwh: float,
        soc_max_pct: float,
        cap: float,
        pv_now_kw: float,
        load_now_kw: float,
        pv_96: list[float],
        load_96: list[float],
        is_peak_96: list[bool],
        workday_today: bool,
        workday_tomorrow: bool,
    ) -> BatteryAction:
        cfg = self._cfg
        soc_max_kwh = cap * soc_max_pct / 100

        # R0: safety floor
        if soc_pct < cfg.soc_reserve_pct:
            return BatteryAction(
                type="idle", rule="R0",
                reason=f"Below {cfg.soc_reserve_pct:.0f}% reserve floor — battery protected",
                target_soc_pct=soc_pct,
            )

        # R2: peak hour — never grid-charge
        if is_peak(now, workday_today):
            return BatteryAction(
                type="idle", rule="R2",
                reason="Peak tariff active — grid-charge forbidden now",
                target_soc_pct=soc_pct,
            )

        # Compute shortfall for next 24h peak windows
        peak_load_kwh, pv_during_peak_kwh = self._peak_energy_balance_24h(
            current_slot, pv_96, load_96, is_peak_96
        )
        shortfall_kwh = max(0.0, peak_load_kwh - pv_during_peak_kwh - soc_kwh)

        # R1: PV surplus available NOW and battery not full
        pv_surplus_kw = pv_now_kw - load_now_kw
        if pv_surplus_kw > 0.2 and soc_pct < soc_max_pct:
            return BatteryAction(
                type="pv_charge", rule="R1",
                reason=f"PV surplus {pv_surplus_kw:.1f} kW → battery filling naturally",
                target_soc_pct=soc_max_pct,
            )

        # R4: Export day — PV will cover everything, no grid charge needed
        slots_remaining = 48 - current_slot
        pv_remaining = sum(pv_96[current_slot:48])
        load_remaining = sum(load_96[current_slot:48])
        free_kwh = (soc_max_kwh - soc_kwh) * 0.95
        if pv_remaining > load_remaining + free_kwh * 0.5:
            return BatteryAction(
                type="idle", rule="R4",
                reason=f"PV remaining {pv_remaining:.1f} kWh covers load {load_remaining:.1f} kWh + free battery — exporting surplus",
                target_soc_pct=soc_max_pct,
            )

        # R3: Pre-peak top-up via grid charging during off-peak window
        if shortfall_kwh > 0.3:
            target_kwh = min(soc_max_kwh, soc_kwh + shortfall_kwh + 0.5)
            target_soc_pct = round(target_kwh / cap * 100, 0)
            window = next_offpeak_window(now, workday_today, workday_tomorrow)
            offpeak_hrs = offpeak_hours_remaining_tonight(now, workday_today, workday_tomorrow)
            max_chargeable_kwh = offpeak_hrs * cfg.battery_max_charge_power_w / 1000 * 0.95
            if max_chargeable_kwh >= shortfall_kwh * 0.8:
                return BatteryAction(
                    type="grid_charge", rule="R3",
                    reason=(
                        f"Peak load {peak_load_kwh:.1f} kWh, PV during peak {pv_during_peak_kwh:.1f} kWh, "
                        f"shortfall {shortfall_kwh:.1f} kWh → grid-charge to {target_soc_pct:.0f}%"
                    ),
                    target_soc_pct=target_soc_pct,
                    grid_charge_start=window.start,
                    grid_charge_end=window.end,
                    grid_charge_power_w=cfg.battery_max_charge_power_w,
                )

        # R5: no action needed
        return BatteryAction(
            type="idle", rule="R5",
            reason="No action required — energy balance looks OK",
            target_soc_pct=soc_pct,
        )

    def _peak_energy_balance_24h(
        self,
        current_slot: int,
        pv_96: list[float],
        load_96: list[float],
        is_peak_96: list[bool],
    ) -> tuple[float, float]:
        """Sum load and PV during peak slots in the next 24h from current slot."""
        peak_load = 0.0
        pv_peak = 0.0
        for i in range(current_slot, min(current_slot + 48, 96)):
            if i < len(is_peak_96) and is_peak_96[i]:
                load_val = load_96[i] if i < len(load_96) else 0.5
                pv_val = pv_96[i] if i < len(pv_96) else 0.0
                # kW × 0.5h = kWh per slot
                peak_load += load_val * 0.5
                pv_peak += pv_val * 0.5
        return peak_load, pv_peak

    # ------------------------------------------------------------------
    # DHW planning
    # ------------------------------------------------------------------

    def _plan_dhw(
        self,
        now: datetime,
        current_slot: int,
        pv_now_kw: float,
        load_now_kw: float,
        dhw_temp: float,
        workday_today: bool,
        bath_requested: bool,
    ) -> DHWAction:
        cfg = self._cfg

        # Bath manually requested → heat aggressively
        if bath_requested:
            return DHWAction(
                type="heat_comfort",
                setpoint=cfg.dhw_solar_setpoint,
                restart_dt=cfg.dhw_restart_dt_aggressive,
                reason="Manual bath request active",
            )

        # PV surplus > 1.5 kW → heat from solar
        pv_surplus = pv_now_kw - load_now_kw
        if pv_surplus > 1.5 and dhw_temp < cfg.dhw_max_temp - 1:
            return DHWAction(
                type="heat_solar",
                setpoint=cfg.dhw_solar_setpoint,
                restart_dt=cfg.dhw_restart_dt_aggressive,
                reason=f"PV surplus {pv_surplus:.1f} kW → heating DHW from solar",
            )

        # Comfort floor: tank cold AND morning demand approaching
        demand_hour = 7 if workday_today else 9
        demand_slot = demand_hour * 2
        slots_to_demand = demand_slot - current_slot
        if dhw_temp < cfg.dhw_comfort_min and 0 <= slots_to_demand <= 4:
            return DHWAction(
                type="heat_comfort",
                setpoint=cfg.dhw_solar_setpoint,
                restart_dt=cfg.dhw_restart_dt_aggressive,
                reason=f"Tank {dhw_temp:.0f}°C below {cfg.dhw_comfort_min:.0f}°C, demand in {slots_to_demand*30} min",
            )

        return DHWAction(
            type="coast",
            setpoint=cfg.dhw_baseline_setpoint,
            restart_dt=cfg.dhw_restart_dt_default,
            reason="Coasting on stored heat",
        )

    # ------------------------------------------------------------------
    # AC planning
    # ------------------------------------------------------------------

    def _plan_ac(
        self,
        now: datetime,
        outdoor_temp: float,
        ac_states: dict[str, str],
        is_peak_96: list[bool],
        current_slot: int,
    ) -> list[ACAction]:
        actions = []

        # Find next peak-start slot
        in_peak = is_peak_96[current_slot] if current_slot < len(is_peak_96) else False
        next_peak_starts_in_slots = None
        if not in_peak:
            for i in range(current_slot, min(current_slot + 2, len(is_peak_96))):
                if is_peak_96[i]:
                    next_peak_starts_in_slots = i - current_slot
                    break

        # Pre-cool if peak starts in next 1 slot (30 min) and it's warm outside
        if next_peak_starts_in_slots == 1 and outdoor_temp > 28:
            for unit in AC_UNITS:
                mode = ac_states.get(unit, "off")
                if mode in ("cool", "auto"):
                    actions.append(ACAction(
                        unit=unit,
                        entity_id=AC_ENTITIES[unit],
                        setpoint_delta=-1.0,
                        reason=f"Pre-cool before peak (outdoor {outdoor_temp:.0f}°C)",
                    ))
        elif in_peak:
            # Inside peak — release any pre-cool override
            for unit in AC_UNITS:
                actions.append(ACAction(
                    unit=unit,
                    entity_id=AC_ENTITIES[unit],
                    setpoint_delta=0.0,
                    reason="Peak started — release pre-cool override",
                ))

        return actions

    # ------------------------------------------------------------------
    # SoC trajectory simulation
    # ------------------------------------------------------------------

    def _simulate_soc(
        self,
        soc_init_pct: float,
        start_slot: int,
        pv_48: list[float],
        load_48: list[float],
        is_peak_48: list[bool],
        battery_action: BatteryAction,
        cap_kwh: float,
        soc_min_kwh: float,
        soc_max_pct: float,
    ) -> list[float]:
        """Simulate battery SoC from start_slot to slot 48 given the planned action."""
        soc_max_kwh = cap_kwh * soc_max_pct / 100
        ETA = 0.95
        soc = max(soc_min_kwh, min(soc_max_kwh, soc_init_pct / 100 * cap_kwh))
        traj = [round(soc / cap_kwh * 100, 1)]

        for t in range(start_slot, 48):
            pv = pv_48[t] if t < len(pv_48) else 0.0
            load = load_48[t] if t < len(load_48) else 0.3
            surplus = max(0.0, pv - load)
            deficit = max(0.0, load - pv)

            # Battery charges from PV surplus
            soc = min(soc + surplus * ETA, soc_max_kwh)
            # Battery discharges to cover load deficit
            soc = max(soc - deficit / ETA, soc_min_kwh)

            # Grid charge during off-peak (R3)
            if (battery_action.type == "grid_charge"
                    and not is_peak_48[t]
                    and soc < battery_action.target_soc_pct / 100 * cap_kwh):
                charge_kwh = min(
                    self._cfg.battery_max_charge_power_w / 1000 * 0.5 * ETA,
                    battery_action.target_soc_pct / 100 * cap_kwh - soc,
                )
                soc = min(soc + charge_kwh, soc_max_kwh)

            traj.append(round(soc / cap_kwh * 100, 1))

        return traj

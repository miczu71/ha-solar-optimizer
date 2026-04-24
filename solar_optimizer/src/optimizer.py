"""LP energy optimizer using PuLP/CBC.

Decision variables per 30-min slot t in {0..47}:
  dhw_heat_energy[t]  -- thermal kWh delivered to DHW tank
  ac_delta[t, unit]   -- AC setpoint offset (degC), bounded +-2
  offpeak_precharge[t]-- grid forcible charge power (W), 0 during peak slots
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pulp

from config import Config
from thermal_model import DHWModel, ACRoomModel

log = logging.getLogger(__name__)

SLOTS = 48
SLOT_HOURS = 0.5
ETA_CHARGE = 0.95
ETA_DISCHARGE = 0.95
INV_ETA_DISCHARGE = 1.0 / ETA_DISCHARGE
PEAK_PRICE = 1.23
OFFPEAK_PRICE = 0.63

AC_UNITS = ["salon", "pietro", "poddasze"]


def g12w_peak_vector(reference: datetime) -> list[bool]:
    """Returns 48-element list: True=peak for each 30-min slot starting at midnight of reference."""
    peak = []
    wd = reference.weekday()
    is_workday = wd < 5
    for slot in range(SLOTS):
        hour = slot // 2
        if is_workday and (6 <= hour < 13 or 15 <= hour < 22):
            peak.append(True)
        else:
            peak.append(False)
    return peak


@dataclass
class OptimizeResult:
    status: str
    dhw_heat_energy: list[float] = field(default_factory=list)
    ac_deltas: dict[str, list[float]] = field(default_factory=dict)
    offpeak_precharge_w: list[float] = field(default_factory=list)
    soc_trajectory: list[float] = field(default_factory=list)
    dhw_temp_trajectory: list[float] = field(default_factory=list)
    grid_import_kwh: list[float] = field(default_factory=list)
    grid_export_kwh: list[float] = field(default_factory=list)
    objective_value: Optional[float] = None
    pv_forecast_kwh_total: float = 0.0
    load_forecast_kwh_total: float = 0.0


def run_optimizer(
    cfg: Config,
    pv_forecast_kwh: list[float],
    base_load_kwh: list[float],
    soc_init: float,
    soc_min: float,
    dhw_temp_init: float,
    dhw_demand_slots: list[bool],
    outdoor_temps: list[float],
    ac_room_temps: dict[str, float],
    now: Optional[datetime] = None,
    enable_battery: bool = True,
    enable_dhw: bool = True,
    enable_ac: bool = True,
) -> OptimizeResult:
    if now is None:
        now = datetime.now(timezone.utc)

    is_peak = g12w_peak_vector(now)
    dhw_model = DHWModel(
        tank_liters=cfg.dhw_tank_liters,
        loss_rate_c_per_hour=cfg.dhw_loss_rate_c_per_hour,
        cop=cfg.dhw_cop,
        comfort_min=cfg.dhw_comfort_min,
        max_temp=cfg.dhw_max_temp,
    )

    bat_cap_kwh = cfg.battery_capacity_kwh
    soc_max_kwh = bat_cap_kwh * cfg.soc_max_percent / 100
    soc_min_kwh = bat_cap_kwh * max(cfg.soc_min_percent, soc_min) / 100
    bat_max_charge_kwh = cfg.battery_max_charge_power_w / 1000 * SLOT_HOURS
    bat_max_discharge_kwh = cfg.battery_max_discharge_power_w / 1000 * SLOT_HOURS

    inv_dhw_cop = 1.0 / cfg.dhw_cop
    inv_tm = 1.0 / dhw_model.thermal_mass_kwh_per_c

    # Clamp initial conditions to LP variable bounds.
    # The real battery can momentarily exceed soc_max (PV overcharge) or dip below
    # soc_min (backup reserve not yet enforced) — clamping prevents LP infeasibility.
    soc_init_kwh = min(soc_max_kwh, max(soc_min_kwh, soc_init * bat_cap_kwh / 100))
    raw_soc_kwh = soc_init * bat_cap_kwh / 100
    if abs(soc_init_kwh - raw_soc_kwh) > 0.05:
        log.warning(
            "SoC %.1f%% (%.3f kWh) outside LP bounds [%.1f%%, %.1f%%] -- clamped to %.3f kWh",
            soc_init, raw_soc_kwh, cfg.soc_min_percent, cfg.soc_max_percent, soc_init_kwh,
        )

    dhw_temp_init_clamped = max(0.0, min(cfg.dhw_max_temp + 2, dhw_temp_init))

    prob = pulp.LpProblem("solar_optimizer", pulp.LpMinimize)

    dhw = [pulp.LpVariable(f"dhw_{t}", lowBound=0) for t in range(SLOTS)]
    ac = {
        u: [pulp.LpVariable(f"ac_{u}_{t}", lowBound=-2, upBound=2) for t in range(SLOTS)]
        for u in AC_UNITS
    }
    precharge = [
        pulp.LpVariable(f"precharge_{t}", lowBound=0,
                        upBound=0 if is_peak[t] else cfg.battery_max_charge_power_w / 1000 * SLOT_HOURS)
        for t in range(SLOTS)
    ]

    grid_import = [pulp.LpVariable(f"gi_{t}", lowBound=0) for t in range(SLOTS)]
    grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0) for t in range(SLOTS)]
    pv_to_load = [pulp.LpVariable(f"pv2l_{t}", lowBound=0) for t in range(SLOTS)]
    pv_to_bat = [pulp.LpVariable(f"pv2b_{t}", lowBound=0) for t in range(SLOTS)]
    bat_to_load = [pulp.LpVariable(f"b2l_{t}", lowBound=0) for t in range(SLOTS)]
    soc = [pulp.LpVariable(f"soc_{t}", lowBound=soc_min_kwh, upBound=soc_max_kwh)
           for t in range(SLOTS + 1)]

    # Lower bound is 0 so the tank can freely decay when DHW control is disabled
    # without causing LP infeasibility. Comfort floor constraints are added separately.
    dhw_temp = [pulp.LpVariable(f"dhwt_{t}", lowBound=0.0, upBound=cfg.dhw_max_temp + 2)
                for t in range(SLOTS + 1)]

    dhw_thrash = [pulp.LpVariable(f"dthr_{t}", lowBound=0) for t in range(1, SLOTS)]

    w_import = 1.0
    w_cost = 0.3
    w_thrash = 0.05
    w_wear = 0.02

    price = [PEAK_PRICE if p else OFFPEAK_PRICE for p in is_peak]

    prob += (
        w_import * pulp.lpSum(grid_import)
        + w_cost * pulp.lpSum(price[t] * grid_import[t] for t in range(SLOTS))
        + w_thrash * pulp.lpSum(dhw_thrash)
        + w_wear * pulp.lpSum(
            pv_to_bat[t] + bat_to_load[t] for t in range(SLOTS)
        )
    )

    prob += soc[0] == soc_init_kwh
    prob += dhw_temp[0] == dhw_temp_init_clamped

    for t in range(SLOTS):
        pv = pv_forecast_kwh[t]
        base = base_load_kwh[t]

        # PuLP does not support LpVariable / float -- use multiplication by inverse
        dhw_elec = dhw[t] * inv_dhw_cop if enable_dhw else 0

        ac_elec = pulp.lpSum(
            ACRoomModel.estimate_power_w(ac[u][t], outdoor_temps[t]) * (SLOT_HOURS / 1000)
            for u in AC_UNITS
        ) if enable_ac else 0

        load_total = base + dhw_elec + ac_elec

        prob += pv_to_load[t] + pv_to_bat[t] + grid_export[t] == pv
        prob += pv_to_load[t] <= pv
        prob += pv_to_load[t] <= load_total

        prob += pv_to_load[t] + bat_to_load[t] + grid_import[t] == load_total
        prob += bat_to_load[t] <= bat_max_discharge_kwh

        prob += soc[t + 1] == (
            soc[t]
            + ETA_CHARGE * (pv_to_bat[t] + (precharge[t] if enable_battery else 0))
            - bat_to_load[t] * INV_ETA_DISCHARGE
        )
        prob += pv_to_bat[t] + (precharge[t] if enable_battery else 0) <= bat_max_charge_kwh

        dhw_contrib = dhw[t] * inv_tm if enable_dhw else 0
        loss = dhw_model.loss_rate_c_per_hour * SLOT_HOURS
        prob += dhw_temp[t + 1] == dhw_temp[t] + dhw_contrib - loss

        if enable_dhw and dhw_demand_slots[t]:
            prob += dhw_temp[t] >= cfg.dhw_comfort_min

        max_heat = dhw_model.thermal_mass_kwh_per_c * (cfg.dhw_max_temp - cfg.dhw_comfort_min)
        prob += dhw[t] <= max_heat

        if t > 0:
            prob += dhw_thrash[t - 1] >= dhw[t] - dhw[t - 1]
            prob += dhw_thrash[t - 1] >= dhw[t - 1] - dhw[t]

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=30)
    prob.solve(solver)

    status = pulp.LpStatus[prob.status]
    log.info(
        "Optimizer status=%s objective=%.4f pv_total=%.2f kWh load_total=%.2f kWh",
        status,
        pulp.value(prob.objective) or 0,
        sum(pv_forecast_kwh),
        sum(base_load_kwh),
    )

    if prob.status != pulp.LpStatusOptimal:
        return OptimizeResult(status=status)

    def v(var) -> float:
        val = pulp.value(var)
        return float(val) if val is not None else 0.0

    return OptimizeResult(
        status=status,
        dhw_heat_energy=[v(dhw[t]) for t in range(SLOTS)],
        ac_deltas={u: [v(ac[u][t]) for t in range(SLOTS)] for u in AC_UNITS},
        offpeak_precharge_w=[v(precharge[t]) / SLOT_HOURS * 1000 for t in range(SLOTS)],
        soc_trajectory=[v(soc[t]) / bat_cap_kwh * 100 for t in range(SLOTS + 1)],
        dhw_temp_trajectory=[v(dhw_temp[t]) for t in range(SLOTS + 1)],
        grid_import_kwh=[v(grid_import[t]) for t in range(SLOTS)],
        grid_export_kwh=[v(grid_export[t]) for t in range(SLOTS)],
        objective_value=pulp.value(prob.objective),
        pv_forecast_kwh_total=sum(pv_forecast_kwh),
        load_forecast_kwh_total=sum(base_load_kwh),
    )

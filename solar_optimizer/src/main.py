"""Entrypoint: initializes subsystems, wires planner → shadow_log → executor, starts scheduler + API."""
import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from api import app, set_state
from config import Config
from executor import Executor
from ha_client import HAClient
from ha_statistics_client import get_ha_statistics_30min
from mqtt_publisher import MQTTPublisher
from planner import Planner, Plan
from tariff import (
    OFFPEAK_PRICE, PEAK_PRICE,
    is_peak, peak_vector_96,
)
import shadow_log

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
log = logging.getLogger("main")

CONSECUTIVE_FAILURE_LIMIT = 3
_consecutive_failures = 0


def _read_version() -> str:
    try:
        with open("/app/addon_config.yaml") as f:
            for line in f:
                m = re.match(r'^version:\s*["\']?([^"\'\\s]+)["\']?', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


# ------------------------------------------------------------------
# PV forecast helpers
# ------------------------------------------------------------------

def build_pv_forecast_96(ha: HAClient) -> list[float]:
    """Return 96-slot kW array: today (0-47) + tomorrow (48-95) from Solcast."""
    slots_raw = ha.get_solcast_forecast()
    today_by_slot: dict[int, float] = {}
    tomorrow_by_slot: dict[int, float] = {}
    now_local = ha.local_now
    today_date = now_local.date()
    tomorrow_date = (now_local + timedelta(days=1)).date()

    for entry in slots_raw:
        try:
            period_start = entry.get("period_start") or entry.get("PeriodStart") or ""
            if not period_start:
                continue
            dt = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
            dt_local = dt.astimezone(ha.tz)
            slot = dt_local.hour * 2 + dt_local.minute // 30
            kw = float(entry.get("pv_estimate", entry.get("PvEstimate", 0)))
            if dt_local.date() == today_date:
                today_by_slot[slot] = kw
            elif dt_local.date() == tomorrow_date:
                tomorrow_by_slot[slot] = kw
        except Exception:
            continue

    today = [today_by_slot.get(s, 0.0) for s in range(48)]
    tomorrow = [tomorrow_by_slot.get(s, 0.0) for s in range(48)]
    return today + tomorrow


def _rolling_mean(days_back: int) -> pd.Series:
    """Per-slot mean base load (kW) from HA long-term statistics."""
    try:
        entity_ids = [
            "sensor.house_consumption_power",
            "sensor.heiko_heat_pump_electrical_power",
            "sensor.miernik_energii_klimatyzacje_power_a",
            "sensor.miernik_energii_klimatyzacje_power_b",
        ]
        stats = get_ha_statistics_30min(entity_ids, days_back=days_back)
        if not stats:
            return pd.Series(dtype=float)
        empty = pd.Series(dtype=float)
        house = stats.get("sensor.house_consumption_power", empty)
        hp    = stats.get("sensor.heiko_heat_pump_electrical_power", empty)
        ac_p  = stats.get("sensor.miernik_energii_klimatyzacje_power_a", empty)
        ac_d  = stats.get("sensor.miernik_energii_klimatyzacje_power_b", empty)
        df = pd.concat([house, hp, ac_p, ac_d], axis=1, join="outer").fillna(0)
        df.columns = ["house", "hp", "ac_p", "ac_d"]
        # Return base load in kW (subtract flexible loads, keep positive)
        df["base_kw"] = (df["house"] - df["hp"] - df["ac_p"] - df["ac_d"]).clip(lower=0) / 1000
        df["slot"] = df.index.hour * 2 + df.index.minute // 30
        return df.groupby("slot")["base_kw"].mean()
    except Exception as exc:
        log.debug("HA stats rolling mean (%dd) failed: %s", days_back, exc)
        return pd.Series(dtype=float)


def build_load_forecast_96(ha: HAClient, cfg: Config) -> list[float]:
    """96-slot kW load forecast: rolling mean repeated for today + tomorrow."""
    rolling = _rolling_mean(cfg.load_history_days)
    if not rolling.empty:
        base = [float(rolling.get(s, 0.3)) for s in range(48)]
    else:
        log.warning("No HA stats for load forecast — using flat 0.3 kW/slot")
        base = [0.3] * 48

    # Tomorrow: same pattern (no seasonal adjustment in Phase 1)
    return base + base


# ------------------------------------------------------------------
# AC state reader
# ------------------------------------------------------------------

def _ac_states(ha: HAClient) -> dict[str, str]:
    units = {"salon": "climate.153931628323418_climate",
             "pietro": "climate.152832117304366_climate",
             "poddasze": "climate.152832117518705_climate"}
    result = {}
    for name, eid in units.items():
        try:
            result[name] = ha.get_state(eid).get("state", "off")
        except Exception:
            result[name] = "off"
    return result


# ------------------------------------------------------------------
# Main replan function
# ------------------------------------------------------------------

def replan(
    cfg: Config,
    ha: HAClient,
    planner: Planner,
    executor: Executor,
    mqtt: MQTTPublisher,
) -> None:
    global _consecutive_failures
    try:
        now_local = ha.local_now
        current_slot = now_local.hour * 2 + now_local.minute // 30

        workday_today    = ha.is_workday(cfg.workday_entity)
        workday_tomorrow = ha.is_workday(cfg.workday_tomorrow_entity)

        pv_96   = build_pv_forecast_96(ha)
        load_96 = build_load_forecast_96(ha, cfg)
        is_peak_96 = peak_vector_96(now_local, workday_today, workday_tomorrow)

        soc      = ha.soc_percent
        pv_now   = ha.pv_power_w / 1000
        load_now = ha.house_load_w / 1000
        dhw_temp = ha.dhw_tank_temp
        outdoor  = ha.outdoor_temp
        ac_states = _ac_states(ha)
        bath_req  = ha.bath_request

        plan = planner.plan(
            now=now_local,
            soc_pct=soc,
            pv_now_kw=pv_now,
            load_now_kw=load_now,
            pv_forecast_kw_96=pv_96,
            load_forecast_kw_96=load_96,
            is_peak_96=is_peak_96,
            workday_today=workday_today,
            workday_tomorrow=workday_tomorrow,
            dhw_tank_temp=dhw_temp,
            outdoor_temp=outdoor,
            ac_states=ac_states,
            bath_requested=bath_req,
        )

        executor.apply_plan(plan, mqtt, current_slot)

        # Shadow-mode benefit tracking
        tariff_price = PEAK_PRICE if is_peak_96[current_slot] else OFFPEAK_PRICE
        grid_kw = (ha.grid_export_w - ha.grid_import_w) / 1000  # positive = exporting
        bat_delta_kw = (ha.battery_charge_w - ha.battery_discharge_w) / 1000  # pos = charging

        # Planned battery delta: positive means "plan wants to charge"
        planned_bat_delta = 0.0
        if plan.battery.type == "grid_charge":
            planned_bat_delta = cfg.battery_max_charge_power_w / 1000
        elif plan.battery.type == "pv_charge":
            planned_bat_delta = max(0.0, pv_now - load_now)  # PV surplus going to battery
        # idle / discharge: 0 (battery natural behaviour, not dispatcher-controlled)

        slot_savings = shadow_log.record(
            ts=datetime.now(timezone.utc),
            slot=current_slot,
            rule=plan.battery.rule,
            actual_grid_kw=grid_kw,
            actual_load_kw=load_now,
            actual_pv_kw=pv_now,
            planned_battery_delta_kw=planned_bat_delta,
            tariff_price=tariff_price,
        )

        today_pln  = shadow_log.today_savings()
        month_pln  = shadow_log.month_savings()

        # Build plan summary sentence
        bat = plan.battery
        if bat.type == "grid_charge" and bat.grid_charge_start:
            plan_text = (
                f"Grid-charge {bat.grid_charge_start.strftime('%H:%M')}–"
                f"{bat.grid_charge_end.strftime('%H:%M')} → {bat.target_soc_pct:.0f}%"
            )
        elif bat.type == "pv_charge":
            plan_text = f"Charging from PV ({pv_now:.1f} kW surplus) → battery at {soc:.0f}%"
        else:
            plan_text = bat.reason

        mqtt.publish_status(
            f"Battery {soc:.0f}% | PV {pv_now:.1f} kW | Grid {'export' if grid_kw>0 else 'import'} {abs(grid_kw):.1f} kW",
            last_run=datetime.now(timezone.utc),
            rule=bat.rule,
        )
        mqtt.publish_plan_summary(plan_text)
        mqtt.publish_savings(today_pln, month_pln)
        mqtt.publish_mode(cfg.shadow_mode)

        # Share state with the API (dashboard)
        set_state("last_plan", plan)
        set_state("last_run", datetime.now(timezone.utc))
        set_state("pv_96", pv_96)
        set_state("load_96", load_96)
        set_state("is_peak_96", is_peak_96)
        set_state("cfg", cfg)
        set_state("ha", ha)
        set_state("savings_today", today_pln)
        set_state("savings_month", month_pln)

        log.info(
            "Replan OK rule=%s pv=%.1f kWh load=%.1f kWh soc=%.0f%% shadow_savings=%.2f PLN",
            bat.rule,
            sum(pv_96[:48]),
            sum(load_96[:48]),
            soc,
            today_pln,
        )
        _consecutive_failures = 0

    except Exception as exc:
        log.error("Replan error: %s", exc, exc_info=True)
        _consecutive_failures += 1
        if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            log.critical("Too many consecutive failures — triggering failsafe")
            executor.failsafe(mqtt)


def main() -> None:
    cfg = Config.load()
    version = _read_version()
    log.info("Starting Solar Optimizer v%s shadow_mode=%s", version, cfg.shadow_mode)

    ha = HAClient(cfg)
    ha.init_timezone()
    mqtt = MQTTPublisher(cfg)
    planner = Planner(cfg)
    executor = Executor(cfg, ha)

    mqtt.connect()

    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "0.0.0.0", "port": 8099, "log_level": "warning"},
        daemon=True,
    )
    server_thread.start()
    log.info("HTTP server started on port 8099")

    set_state("version", version)
    set_state("cfg", cfg)
    set_state("ha", ha)
    set_state("replan_fn", lambda: replan(cfg, ha, planner, executor, mqtt))

    replan(cfg, ha, planner, executor, mqtt)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: replan(cfg, ha, planner, executor, mqtt),
        "interval",
        minutes=cfg.replan_interval_minutes,
        id="replan",
        max_instances=1,
    )
    scheduler.start()
    log.info("Scheduler started, replan every %d min", cfg.replan_interval_minutes)

    server_thread.join()


if __name__ == "__main__":
    main()

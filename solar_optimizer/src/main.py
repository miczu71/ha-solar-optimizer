"""Entrypoint: initializes all subsystems, starts APScheduler and FastAPI."""
import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from api import app, set_state
from config import Config
from data_pipeline import build_forecast_row, build_training_features_ha_stats
from executor import Executor
from forecaster import LoadForecaster
from ha_client import HAClient
from influx_client import InfluxClient
from mqtt_publisher import MQTTPublisher
from optimizer import OptimizeResult, run_optimizer
from thermal_calibrator import calibrate_dhw_params, load_params, save_params
from thermal_model import DHWModel

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
log = logging.getLogger("main")

CONSECUTIVE_FAILURE_LIMIT = 3
_consecutive_failures = 0
_HISTORY_FILE = "/data/plan_history.jsonl"
_HISTORY_MAX_RECORDS = 1440  # ~30 days at 48 replans/day
_learned_params: dict = {}   # populated from /data/learned_params.json at startup / after calibration


def _read_addon_version() -> str:
    try:
        with open("/app/addon_config.yaml") as f:
            for line in f:
                m = re.match(r'^version:\s*["\']?([^"\'\\s]+)["\']?', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


def build_pv_forecast(ha: HAClient) -> list[float]:
    """Parse Solcast detailedForecast into 48-slot kWh array.

    Solcast pv_estimate is in kW (average power for the 30-min interval).
    Multiply by 0.5 to convert to kWh per slot.
    """
    slots_raw = ha.get_solcast_forecast()
    by_slot: dict[int, float] = {}
    now_local = ha.local_now
    for entry in slots_raw:
        try:
            period_start = entry.get("period_start") or entry.get("PeriodStart") or ""
            if not period_start:
                continue
            dt = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
            dt_local = dt.astimezone(ha.tz) if dt.tzinfo else dt.replace(tzinfo=ha.tz)
            if dt_local.date() != now_local.date():
                continue
            slot = dt_local.hour * 2 + dt_local.minute // 30
            kw = float(entry.get("pv_estimate", entry.get("PvEstimate", 0)))
            by_slot[slot] = kw * 0.5  # kW -> kWh per 30-min slot
        except Exception:
            continue
    return [by_slot.get(s, 0.0) for s in range(48)]


def build_base_load_forecast(
    influx: InfluxClient,
    forecaster: LoadForecaster,
    ha: HAClient,
    cfg: Config,
    now_local: datetime,
    phase: int,
) -> list[float]:
    if phase == 2 and forecaster.is_ready():
        try:
            outdoor = ha.outdoor_temp
            lag_1d = {}
            lag_7d = {}
            pv_yesterday = 5.0
            try:
                lag_1d = influx.rolling_mean_base_load(days_back=1)
            except Exception as exc:
                log.debug("lag_1d InfluxDB fetch failed: %s", exc)
            try:
                lag_7d = influx.rolling_mean_base_load(days_back=7)
            except Exception as exc:
                log.debug("lag_7d InfluxDB fetch failed: %s", exc)
            try:
                pv_yesterday = influx.pv_total_yesterday()
            except Exception as exc:
                log.debug("pv_yesterday InfluxDB fetch failed: %s", exc)
            rows = [
                build_forecast_row(
                    slot=s,
                    now=now_local,
                    outdoor_temp=outdoor,
                    lag_1d=float(lag_1d.get(s, 0.3)),
                    lag_7d=float(lag_7d.get(s, 0.3)),
                    pv_yesterday_kwh=pv_yesterday,
                )
                for s in range(48)
            ]
            return forecaster.predict_48slots(rows)
        except Exception as exc:
            log.warning("LightGBM forecast failed, falling back to rolling mean: %s", exc)

    try:
        rolling = influx.rolling_mean_base_load(days_back=7)
        return [float(rolling.get(s, 0.3)) for s in range(48)]
    except Exception as exc:
        log.warning("Rolling mean failed, using flat 0.3 kWh/slot: %s", exc)
        return [0.3] * 48


def _find_best_deferrable_start(
    pv_forecast: list[float],
    base_load: list[float],
    result: OptimizeResult,
    load: dict,
) -> Optional[int]:
    """Sliding-window search for the slot that minimises additional grid import."""
    power_w = load.get("power_w", 1000)
    duration_slots = max(1, round(load.get("duration_min", 60) / 30))
    earliest = int(load.get("earliest_slot", 0))
    latest = int(load.get("latest_slot", 47))
    load_kwh = power_w / 1000 * 0.5

    best_slot, best_score = None, float("inf")
    for start in range(earliest, min(latest - duration_slots + 2, 48 - duration_slots + 1)):
        grid_added = 0.0
        for t in range(start, start + duration_slots):
            if t >= 48:
                break
            pv_surplus = max(0.0, pv_forecast[t] - base_load[t])
            grid_added += max(0.0, load_kwh - pv_surplus)
        if grid_added < best_score:
            best_score = grid_added
            best_slot = start
    return best_slot


def _save_daily_summary(result: OptimizeResult, now_local: datetime, phase: int) -> None:
    """Append a per-replan record to plan_history.jsonl for the history tab."""
    try:
        pv_total = sum(result.pv_forecast_kwh) if result.pv_forecast_kwh else result.pv_forecast_kwh_total
        export_total = sum(result.grid_export_kwh)
        import_total = sum(result.grid_import_kwh)
        self_cons = max(0.0, (pv_total - export_total) / pv_total * 100) if pv_total > 0 else 0.0
        record = {
            "date": now_local.strftime("%Y-%m-%d"),
            "time": now_local.isoformat(),
            "phase": phase,
            "pv_total_kwh": round(pv_total, 3),
            "load_total_kwh": round(result.load_forecast_kwh_total, 3),
            "grid_import_total_kwh": round(import_total, 3),
            "grid_export_total_kwh": round(export_total, 3),
            "self_cons_pct": round(self_cons, 1),
        }
        with open(_HISTORY_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
        with open(_HISTORY_FILE) as f:
            lines = f.readlines()
        if len(lines) > _HISTORY_MAX_RECORDS:
            with open(_HISTORY_FILE, "w") as f:
                f.writelines(lines[-_HISTORY_MAX_RECORDS:])
    except Exception as exc:
        log.warning("Failed to save plan history: %s", exc)


def replan(
    cfg: Config,
    ha: HAClient,
    influx: InfluxClient,
    executor: Executor,
    mqtt: MQTTPublisher,
    forecaster: LoadForecaster,
    phase: int,
) -> None:
    global _consecutive_failures
    try:
        if not mqtt.is_enabled():
            log.info("Optimizer disabled via switch, skipping replan")
            return

        now_utc = datetime.now(timezone.utc)
        now_local = ha.local_now

        pv_forecast = build_pv_forecast(ha)
        base_load = build_base_load_forecast(influx, forecaster, ha, cfg, now_local, phase)

        # Store forecasts in state so /compare can build naive/JIT trajectory
        set_state("last_pv_forecast", pv_forecast)
        set_state("last_base_load", base_load)

        soc = ha.soc_percent
        soc_min = max(cfg.soc_min_percent, ha.soc_min_from_backup)
        dhw_temp = ha.dhw_tank_temp
        outdoor = ha.outdoor_temp

        slot = now_local.hour * 2 + now_local.minute // 30

        # Read workday flag from HA (handles all Polish public holidays)
        is_workday = ha.is_workday(cfg.workday_entity)

        # Comfort profile: shift DHW demand window on weekends/holidays
        demand_hour = cfg.dhw_demand_hour_weekday if is_workday else cfg.dhw_demand_hour_weekend
        demand_slot = demand_hour * 2

        dhw_demand_slots = [False] * 48
        # Mark comfort window: demand_slot to demand_slot+4 (2h window)
        for t in range(demand_slot, min(48, demand_slot + 4)):
            dhw_demand_slots[t] = True
        if ha.bath_request:
            demand_end = min(48, slot + 4)
            for t in range(slot, demand_end):
                dhw_demand_slots[t] = True
            log.info("Bath requested: marking slots %d–%d as DHW demand", slot, demand_end - 1)

        # Read manual overrides from HA input helpers
        force_soc_pct = ha.get_ha_float(cfg.force_soc_entity, default=0.0)
        force_soc_hour = int(ha.get_ha_float(cfg.force_soc_deadline_entity, default=8.0))
        vacation_mode = ha.get_ha_bool(cfg.vacation_mode_entity)
        vacation_dhw = (
            ha.get_ha_float(cfg.vacation_dhw_setpoint_entity, default=55.0)
            if vacation_mode else None
        )
        if force_soc_pct > 0:
            log.info("Force-SoC override: target %.0f%% by %02d:00", force_soc_pct, force_soc_hour)
        if vacation_mode:
            log.info("Vacation mode: DHW setpoint %.0f°C", vacation_dhw)

        result = run_optimizer(
            cfg=cfg,
            pv_forecast_kwh=pv_forecast,
            base_load_kwh=base_load,
            soc_init=soc,
            soc_min=soc_min,
            dhw_temp_init=dhw_temp,
            dhw_demand_slots=dhw_demand_slots,
            outdoor_temps=[outdoor] * 48,
            ac_room_temps={u: 22.0 for u in ["salon", "pietro", "poddasze"]},
            now=now_local,
            enable_battery=mqtt.is_battery_enabled(),
            enable_dhw=mqtt.is_dhw_enabled(),
            enable_ac=mqtt.is_ac_enabled(),
            is_workday=is_workday,
            force_soc_pct=force_soc_pct,
            force_soc_deadline_hour=force_soc_hour,
            vacation_dhw_setpoint=vacation_dhw,
            learned_dhw_loss_rate=_learned_params.get("dhw_loss_rate_c_per_hour"),
            learned_dhw_cop=_learned_params.get("dhw_cop"),
        )

        if result.status != "Optimal":
            log.error("Solver returned %s -- holding current setpoints", result.status)
            _consecutive_failures += 1
            if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                executor.failsafe()
                mqtt._switch_states["enabled"] = False
            return

        _consecutive_failures = 0

        if not cfg.shadow_mode:
            executor.apply_slot(
                slot=slot,
                result=result,
                dhw_enabled=mqtt.is_dhw_enabled(),
                battery_enabled=mqtt.is_battery_enabled(),
                ac_enabled=mqtt.is_ac_enabled(),
            )
        else:
            log.info("Shadow mode: would apply slot %d DHW=%.3f kWh precharge=%.0f W",
                     slot, result.dhw_heat_energy[slot], result.offpeak_precharge_w[slot])

        mqtt.publish_plan(result, phase=phase, last_run=now_utc)
        mqtt.publish_current_slot(result, slot, dhw_cop=cfg.dhw_cop)
        set_state("last_result", result)
        set_state("last_run", now_utc)
        set_state("phase", phase)

        pv_total = sum(pv_forecast)
        if pv_total > 0:
            self_cons = max(0.0, (pv_total - sum(result.grid_export_kwh)) / pv_total * 100)
            mqtt.publish_self_consumption(self_cons)

        naive_import = sum(max(0.0, base_load[t] - pv_forecast[t]) for t in range(48))
        avoided = max(0.0, naive_import - sum(result.grid_import_kwh))
        mqtt.publish_grid_import_avoided(avoided)

        # PLN savings vs naive baseline
        mqtt.publish_savings(result.savings_pln, result.optimized_cost_pln)

        # Deferrable load scheduling (advisory — find best start window)
        load_starts: list[tuple[str, str]] = []
        for load_cfg in cfg.deferrable_loads:
            name = load_cfg.get("name", "load")
            best_slot = _find_best_deferrable_start(pv_forecast, base_load, result, load_cfg)
            if best_slot is not None:
                h, m = divmod(best_slot * 30, 60)
                start_str = f"{h:02d}:{m:02d}"
                load_starts.append((name, start_str))
                mqtt.publish_deferrable_load(name, start_str)
                log.info("Deferrable load '%s': best start %s", name, start_str)

        # Morning plan summary (rich text for push notification)
        mqtt.publish_morning_plan(result, pv_forecast, base_load, load_starts, is_workday,
                                  force_soc_pct=force_soc_pct, vacation_mode=vacation_mode)

        _save_daily_summary(result, now_local, phase)

    except Exception as exc:
        log.error("Replan error: %s", exc, exc_info=True)
        _consecutive_failures += 1
        if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            log.critical("Too many consecutive failures -- triggering failsafe")
            executor.failsafe()


def _try_train(cfg: Config, influx: InfluxClient, forecaster: LoadForecaster) -> int:
    """Attempt ML training; return phase (2 if trained, 1 otherwise)."""
    if not cfg.ml_enabled:
        return 1
    if forecaster.is_ready():
        return 2

    availability = influx.check_data_availability()
    log.info("InfluxDB data availability: %s", json.dumps(availability))
    influx_days = min(availability.values(), default=0)

    if influx_days >= 30:
        log.info("Enough InfluxDB data (%d days) -- training LightGBM", influx_days)
        if forecaster.train(influx):
            return 2
    else:
        log.info(
            "InfluxDB only has %d days -- trying HA long-term statistics (up to 365 days)",
            influx_days,
        )
        try:
            ha_df = build_training_features_ha_stats(days_back=365)
            if not ha_df.empty and forecaster.train_from_df(ha_df):
                return 2
        except Exception as exc:
            log.warning("HA statistics training failed: %s", exc)

    return 1


def main() -> None:
    global _learned_params
    cfg = Config.load()
    version = _read_addon_version()
    log.info("Starting Solar Optimizer v%s shadow_mode=%s", version, cfg.shadow_mode)

    # Load auto-calibrated thermal params from previous run
    stored = load_params()
    if stored:
        _learned_params = stored
        log.info("Loaded learned thermal params: loss_rate=%.3f °C/h  cop=%.2f",
                 stored.get("dhw_loss_rate_c_per_hour", 0), stored.get("dhw_cop", 0))

    ha = HAClient(cfg)
    ha.init_timezone()

    influx = InfluxClient(cfg)
    forecaster = LoadForecaster()
    mqtt = MQTTPublisher(cfg)
    executor = Executor(cfg, ha, shadow=cfg.shadow_mode)

    mqtt.connect()

    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "0.0.0.0", "port": 8099, "log_level": "warning"},
        daemon=True,
    )
    server_thread.start()
    log.info("HTTP server starting on port 8099")

    phase = _try_train(cfg, influx, forecaster)
    set_state("phase", phase)
    set_state("version", version)
    set_state("cfg", cfg)
    set_state("ha", ha)

    def _replan():
        replan(cfg, ha, influx, executor, mqtt, forecaster, phase)

    def _retrain():
        nonlocal phase
        new_phase = _try_train(cfg, influx, forecaster)
        if new_phase != phase:
            log.info("Phase changed %d -> %d after retrain", phase, new_phase)
            phase = new_phase
        set_state("phase", phase)

    def _calibrate_thermal():
        global _learned_params
        log.info("Running weekly DHW thermal calibration…")
        try:
            params = calibrate_dhw_params(influx, cfg)
            if params.get("calibrated"):
                save_params(params)
                _learned_params = params
            else:
                log.info("Thermal calibration skipped: insufficient data")
        except Exception as exc:
            log.warning("Thermal calibration error: %s", exc)

    set_state("replan_fn", _replan)

    _replan()

    scheduler = BackgroundScheduler()
    scheduler.add_job(_replan, "interval", minutes=cfg.replan_interval_minutes, id="replan", max_instances=1)
    scheduler.add_job(_retrain, "cron", day_of_week="sun", hour=3, id="retrain")
    scheduler.add_job(_calibrate_thermal, "cron", day_of_week="sun", hour=4, id="thermal_calibrate")
    scheduler.start()
    log.info("Scheduler started, replan every %d min", cfg.replan_interval_minutes)

    server_thread.join()


if __name__ == "__main__":
    main()

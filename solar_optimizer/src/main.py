"""Entrypoint: initializes all subsystems, starts APScheduler and FastAPI."""
import json
import logging
import re
import sys
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
from optimizer import OptimizeResult, g12w_peak_vector, run_optimizer
from thermal_model import DHWModel

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
log = logging.getLogger("main")

CONSECUTIVE_FAILURE_LIMIT = 3
_consecutive_failures = 0


def _read_addon_version() -> str:
    try:
        with open("/app/addon_config.yaml") as f:
            for line in f:
                m = re.match(r'^version:\s*["\']?([^"\'\ s]+)["\']?', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


def build_pv_forecast(ha: HAClient) -> list[float]:
    slots_raw = ha.get_solcast_forecast()
    by_slot: dict[int, float] = {}
    now = datetime.now(timezone.utc)
    for entry in slots_raw:
        try:
            period_start = entry.get("period_start") or entry.get("PeriodStart") or ""
            if not period_start:
                continue
            dt = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
            if dt.date() != now.date():
                continue
            slot = dt.hour * 2 + dt.minute // 30
            kwh = float(entry.get("pv_estimate", entry.get("PvEstimate", 0)))
            by_slot[slot] = kwh
        except Exception:
            continue
    return [by_slot.get(s, 0.0) for s in range(48)]


def build_base_load_forecast(
    influx: InfluxClient,
    forecaster: LoadForecaster,
    ha: HAClient,
    cfg: Config,
    now: datetime,
    phase: int,
) -> list[float]:
    if phase == 2 and forecaster.is_ready():
        try:
            outdoor = ha.outdoor_temp
            rows = [
                build_forecast_row(
                    slot=s,
                    now=now,
                    outdoor_temp=outdoor,
                    lag_1d=0.3,
                    lag_7d=0.3,
                    pv_yesterday_kwh=5.0,
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

        now = datetime.now(timezone.utc)
        pv_forecast = build_pv_forecast(ha)
        base_load = build_base_load_forecast(influx, forecaster, ha, cfg, now, phase)

        soc = ha.soc_percent
        soc_min = max(cfg.soc_min_percent, ha.soc_min_from_backup)
        dhw_temp = ha.dhw_tank_temp
        outdoor = ha.outdoor_temp

        dhw_demand_slots = [False] * 48

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
            now=now,
            enable_battery=mqtt.is_battery_enabled(),
            enable_dhw=mqtt.is_dhw_enabled(),
            enable_ac=mqtt.is_ac_enabled(),
        )

        if result.status != "Optimal":
            log.error("Solver returned %s -- holding current setpoints", result.status)
            _consecutive_failures += 1
            if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                executor.failsafe()
                mqtt._switch_states["enabled"] = False
            return

        _consecutive_failures = 0
        slot = now.hour * 2 + now.minute // 30

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

        mqtt.publish_plan(result, phase=phase, last_run=now)
        set_state("last_result", result)
        set_state("last_run", now)
        set_state("phase", phase)

        pv_total = sum(pv_forecast)
        if pv_total > 0:
            self_cons = max(0.0, (pv_total - sum(result.grid_export_kwh)) / pv_total * 100)
            mqtt.publish_self_consumption(self_cons)

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
    cfg = Config.load()
    version = _read_addon_version()
    log.info("Starting Solar Optimizer v%s shadow_mode=%s", version, cfg.shadow_mode)

    ha = HAClient(cfg)
    influx = InfluxClient(cfg)
    forecaster = LoadForecaster()
    mqtt = MQTTPublisher(cfg)
    executor = Executor(cfg, ha, shadow=cfg.shadow_mode)

    mqtt.connect()

    phase = _try_train(cfg, influx, forecaster)
    # Publish phase immediately so the UI shows the correct mode before the first replan
    set_state("phase", phase)

    def _replan():
        replan(cfg, ha, influx, executor, mqtt, forecaster, phase)

    def _retrain():
        nonlocal phase
        new_phase = _try_train(cfg, influx, forecaster)
        if new_phase != phase:
            log.info("Phase changed %d → %d after retrain", phase, new_phase)
            phase = new_phase
        set_state("phase", phase)

    set_state("replan_fn", _replan)

    _replan()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _replan,
        "interval",
        minutes=cfg.replan_interval_minutes,
        id="replan",
        max_instances=1,
    )
    scheduler.add_job(
        _retrain,
        "cron",
        day_of_week="sun",
        hour=3,
        id="retrain",
    )
    scheduler.start()
    log.info("Scheduler started, replan every %d min", cfg.replan_interval_minutes)

    uvicorn.run(app, host="0.0.0.0", port=8099, log_level="warning")


if __name__ == "__main__":
    main()

"""Reads add-on options from /data/options.json (written by HA Supervisor)."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))


@dataclass
class Config:
    ha_url: str = "http://supervisor/core"
    ha_token: str = field(default_factory=lambda: os.environ.get("SUPERVISOR_TOKEN", ""))
    mqtt_host: str = "core-mosquitto"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    replan_interval_minutes: int = 30
    battery_capacity_kwh: float = 5.0
    battery_max_charge_power_w: int = 2500
    battery_max_discharge_power_w: int = 2500
    soc_min_percent: int = 10
    soc_max_percent: int = 95
    dhw_tank_liters: int = 200
    dhw_max_temp: float = 58.0
    dhw_comfort_min: float = 45.0
    dhw_baseline_setpoint: float = 48.0
    dhw_solar_setpoint: float = 58.0
    dhw_restart_dt_default: float = 5.0
    dhw_restart_dt_aggressive: float = 2.0
    dhw_loss_rate_c_per_hour: float = 0.8
    dhw_cop: float = 3.0
    legionella_hours: int = 24
    ml_enabled: bool = True
    shadow_mode: bool = True
    workday_entity: str = "binary_sensor.workday"
    workday_tomorrow_entity: str = "binary_sensor.workday_tomorrow"
    dhw_demand_hour_weekday: int = 7
    dhw_demand_hour_weekend: int = 9
    force_soc_entity: str = "input_number.optimizer_force_soc_target"
    force_soc_deadline_entity: str = "input_number.optimizer_force_soc_deadline_hour"
    vacation_mode_entity: str = "input_boolean.optimizer_vacation_mode"
    vacation_dhw_setpoint_entity: str = "input_number.optimizer_vacation_dhw_setpoint"
    deferrable_loads: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        if not OPTIONS_PATH.exists():
            return cls()
        with OPTIONS_PATH.open() as f:
            data = json.load(f)
        obj = cls()
        for key, val in data.items():
            if hasattr(obj, key):
                setattr(obj, key, val)
        return obj

    @property
    def battery_capacity_wh(self) -> float:
        return self.battery_capacity_kwh * 1000

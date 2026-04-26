"""MQTT discovery and state publishing for all optimizer entities."""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from config import Config
from optimizer import OptimizeResult

log = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "solar_optimizer"


SENSOR_CONFIGS = {
    "status": {
        "name": "Optimizer Status",
        "icon": "mdi:solar-power-variant",
        "value_template": "{{ value_json.state }}",
    },
    "self_consumption_today": {
        "name": "Optimizer Self Consumption Today",
        "unit_of_measurement": "%",
        "icon": "mdi:percent",
        "value_template": "{{ value_json.state }}",
    },
    "grid_import_avoided_kwh": {
        "name": "Optimizer Grid Import Avoided",
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "value_template": "{{ value_json.state }}",
    },
    "battery_plan": {
        "name": "Optimizer Battery Plan",
        "icon": "mdi:battery-charging",
        "value_template": "{{ value_json.state }}",
    },
    "dhw_next_window": {
        "name": "Optimizer DHW Next Window",
        "icon": "mdi:water-boiler",
        "value_template": "{{ value_json.state }}",
    },
    "load_forecast_kwh": {
        "name": "Optimizer Load Forecast 24h",
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "value_template": "{{ value_json.state }}",
    },
    "load_forecast_error_24h": {
        "name": "Optimizer Forecast Error 24h",
        "unit_of_measurement": "%",
        "icon": "mdi:chart-line-variant",
        "value_template": "{{ value_json.state }}",
    },
    "pv_surplus_triggered_today": {
        "name": "Optimizer PV Surplus Today",
        "icon": "mdi:solar-power",
        "value_template": "{{ value_json.state }}",
    },
    "savings_pln": {
        "name": "Optimizer Savings Today PLN",
        "unit_of_measurement": "PLN",
        "icon": "mdi:cash-plus",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "morning_plan": {
        "name": "Optimizer Morning Plan",
        "icon": "mdi:weather-sunny-alert",
        "value_template": "{{ value_json.state }}",
    },
    # Per-slot planned values — update every 30 min, enable plan-vs-reality charts
    "planned_pv_w": {
        "name": "Optimizer Planned PV Power",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "planned_load_w": {
        "name": "Optimizer Planned Load Power",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "planned_grid_import_w": {
        "name": "Optimizer Planned Grid Import",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "planned_soc_pct": {
        "name": "Optimizer Planned Battery SoC",
        "unit_of_measurement": "%",
        "icon": "mdi:battery-charging",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "planned_dhw_temp_c": {
        "name": "Optimizer Planned DHW Temperature",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
}

SWITCH_CONFIGS = {
    "enabled": "Optimizer Enabled",
    "battery_control": "Optimizer Battery Control",
    "dhw_control": "Optimizer DHW Control",
    "ac_control": "Optimizer AC Control",
}


class MQTTPublisher:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION1,
            client_id=NODE_ID,
            clean_session=True,
        )
        if cfg.mqtt_username:
            self._client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._switch_states: dict[str, bool] = {
            "enabled": True,
            "battery_control": False,
            "dhw_control": False,
            "ac_control": False,
        }
        self._callbacks: dict[str, Any] = {}

    def connect(self) -> None:
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()
        time.sleep(1)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            log.error("MQTT connection failed rc=%d", rc)
            return
        log.info("MQTT connected rc=%d", rc)
        self._publish_discovery()
        for key in SWITCH_CONFIGS:
            topic = f"{DISCOVERY_PREFIX}/switch/{NODE_ID}_{key}/set"
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode().strip().lower()
        for key in SWITCH_CONFIGS:
            if f"_{key}/set" in msg.topic:
                state = payload in ("on", "true", "1")
                self._switch_states[key] = state
                self._publish_switch_state(key, state)
                cb = self._callbacks.get(f"switch_{key}")
                if cb:
                    cb(state)

    def on_switch(self, key: str, callback) -> None:
        self._callbacks[f"switch_{key}"] = callback

    def _state_topic(self, kind: str, name: str) -> str:
        return f"{DISCOVERY_PREFIX}/{kind}/{NODE_ID}_{name}/state"

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": [NODE_ID],
            "name": "Solar Optimizer",
            "model": "ha-solar-optimizer",
            "manufacturer": "Custom",
        }
        for name, extra in SENSOR_CONFIGS.items():
            config = {
                "unique_id": f"{NODE_ID}_{name}",
                "state_topic": self._state_topic("sensor", name),
                "device": device,
                **extra,
            }
            self._client.publish(
                f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}_{name}/config",
                json.dumps(config),
                retain=True,
            )

        for name, friendly in SWITCH_CONFIGS.items():
            config = {
                "unique_id": f"{NODE_ID}_{name}",
                "name": friendly,
                "state_topic": self._state_topic("switch", name),
                "command_topic": f"{DISCOVERY_PREFIX}/switch/{NODE_ID}_{name}/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device,
            }
            self._client.publish(
                f"{DISCOVERY_PREFIX}/switch/{NODE_ID}_{name}/config",
                json.dumps(config),
                retain=True,
            )
        log.info("MQTT discovery published")

    def _publish_switch_state(self, name: str, state: bool) -> None:
        self._client.publish(
            self._state_topic("switch", name),
            "ON" if state else "OFF",
            retain=True,
        )

    def publish_plan(self, result: OptimizeResult, phase: int, last_run: datetime) -> None:
        now_str = last_run.isoformat()

        dhw_next = "unknown"
        for i, kwh in enumerate(result.dhw_heat_energy):
            if kwh > 0.02:
                slot_hour = i // 2
                slot_min = (i % 2) * 30
                dhw_next = f"{slot_hour:02d}:{slot_min:02d}"
                break

        bat_plan = json.dumps(result.offpeak_precharge_w)
        pv_surplus = any(e > 0.1 for e in result.grid_export_kwh)

        states = {
            "status": {"state": f"OK phase={phase} last={now_str}", "phase": phase,
                       "last_run": now_str, "solver": result.status,
                       "objective": result.objective_value},
            "battery_plan": {"state": bat_plan[:255], "plan": result.offpeak_precharge_w,
                             "soc_trajectory": result.soc_trajectory},
            "dhw_next_window": {"state": dhw_next, "dhw_plan": result.dhw_heat_energy,
                                "temp_trajectory": result.dhw_temp_trajectory},
            "load_forecast_kwh": {"state": round(result.load_forecast_kwh_total, 2)},
            "pv_surplus_triggered_today": {"state": "on" if pv_surplus else "off"},
        }

        for name, payload in states.items():
            self._client.publish(
                self._state_topic("sensor", name),
                json.dumps(payload),
                retain=True,
            )

        for key, state in self._switch_states.items():
            self._publish_switch_state(key, state)

    def publish_self_consumption(self, pct: float) -> None:
        self._client.publish(
            self._state_topic("sensor", "self_consumption_today"),
            json.dumps({"state": round(pct, 1)}),
            retain=True,
        )

    def publish_grid_import_avoided(self, kwh: float) -> None:
        """Publish kWh saved vs naive no-dispatch baseline."""
        self._client.publish(
            self._state_topic("sensor", "grid_import_avoided_kwh"),
            json.dumps({"state": round(kwh, 3)}),
            retain=True,
        )

    def publish_savings(self, savings_pln: float, cost_pln: float) -> None:
        self._client.publish(
            self._state_topic("sensor", "savings_pln"),
            json.dumps({"state": round(savings_pln, 2), "cost_pln": round(cost_pln, 2)}),
            retain=True,
        )

    def publish_morning_plan(
        self,
        result: "OptimizeResult",
        pv_forecast: list,
        base_load: list,
        load_starts: list,
        is_workday: bool,
        force_soc_pct: float = 0.0,
        vacation_mode: bool = False,
    ) -> None:
        """Publish a human-readable daily plan summary for push notifications."""
        from optimizer import PEAK_PRICE, OFFPEAK_PRICE
        pv_total = round(sum(pv_forecast), 1)
        import_total = round(sum(result.grid_import_kwh), 2)
        soc_start = round(result.soc_trajectory[0], 0) if result.soc_trajectory else 0
        soc_end = round(result.soc_trajectory[-1], 0) if result.soc_trajectory else 0
        savings = round(result.savings_pln, 2)
        cost = round(result.optimized_cost_pln, 2)

        # Find DHW heating windows
        dhw_windows = []
        in_window, wstart = False, 0
        for t, kwh in enumerate(result.dhw_heat_energy):
            if kwh > 0.02 and not in_window:
                in_window, wstart = True, t
            elif kwh <= 0.02 and in_window:
                h1, m1 = divmod(wstart * 30, 60)
                h2, m2 = divmod(t * 30, 60)
                dhw_windows.append(f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}")
                in_window = False
        if in_window:
            h1, m1 = divmod(wstart * 30, 60)
            dhw_windows.append(f"{h1:02d}:{m1:02d}–23:30")

        day_type = "Dzień roboczy" if is_workday else "Weekend/święto"
        lines = [
            f"☀️ PV: {pv_total} kWh  🔋 Bat: {soc_start:.0f}→{soc_end:.0f}%  📥 Import: {import_total} kWh (~{cost} PLN)  💰 Oszczędność: ~{savings} PLN  [{day_type}]",
        ]
        if dhw_windows:
            lines.append("🚿 CWU: " + ", ".join(dhw_windows))
        for name, start in load_starts:
            lines.append(f"🔌 {name}: najlepiej o {start}")
        if force_soc_pct > 0:
            lines.append(f"⚡ Force charge: cel {force_soc_pct:.0f}%")
        if vacation_mode:
            lines.append("🏖️ Tryb wakacyjny aktywny")

        summary = "\n".join(lines)
        self._client.publish(
            self._state_topic("sensor", "morning_plan"),
            json.dumps({"state": summary[:255]}),
            retain=True,
        )

    def publish_deferrable_load(self, name: str, start_time: str) -> None:
        """Publish recommended start time for a deferrable load."""
        slug = name.lower().replace(" ", "_").replace("-", "_")
        sensor_name = f"load_{slug}_start"
        # Register sensor via discovery on first publish
        topic_config = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}_{sensor_name}/config"
        device = {"identifiers": [NODE_ID], "name": "Solar Optimizer",
                  "model": "ha-solar-optimizer", "manufacturer": "Custom"}
        config = {
            "unique_id": f"{NODE_ID}_{sensor_name}",
            "name": f"Optimizer {name} start time",
            "icon": "mdi:clock-start",
            "state_topic": self._state_topic("sensor", sensor_name),
            "value_template": "{{ value_json.state }}",
            "device": device,
        }
        self._client.publish(topic_config, json.dumps(config), retain=True)
        self._client.publish(
            self._state_topic("sensor", sensor_name),
            json.dumps({"state": start_time, "load": name}),
            retain=True,
        )

    def publish_forecast_error(self, mape: float) -> None:
        self._client.publish(
            self._state_topic("sensor", "load_forecast_error_24h"),
            json.dumps({"state": round(mape, 2)}),
            retain=True,
        )

    def publish_current_slot(self, result: OptimizeResult, slot: int, dhw_cop: float = 3.0) -> None:
        """Publish per-slot planned values as individual HA sensor entities.

        These update every 30 min so HA's statistics engine builds a time-series
        that can be overlaid against actual sensor readings in ApexCharts.
        """
        def _kwh_to_w(kwh: float) -> float:
            return round(kwh / 0.5 * 1000, 1)

        pv_w = _kwh_to_w(result.pv_forecast_kwh[slot]) if result.pv_forecast_kwh else 0.0
        dhw_elec_kwh = (result.dhw_heat_energy[slot] / dhw_cop) if result.dhw_heat_energy else 0.0
        base_kwh = result.base_load_kwh[slot] if result.base_load_kwh else 0.0
        load_w = _kwh_to_w(base_kwh + dhw_elec_kwh)
        grid_import_w = _kwh_to_w(result.grid_import_kwh[slot]) if result.grid_import_kwh else 0.0
        soc_pct = round(result.soc_trajectory[slot], 1) if result.soc_trajectory else 0.0
        dhw_temp = round(result.dhw_temp_trajectory[slot], 1) if result.dhw_temp_trajectory else 0.0

        for name, value in [
            ("planned_pv_w", pv_w),
            ("planned_load_w", load_w),
            ("planned_grid_import_w", grid_import_w),
            ("planned_soc_pct", soc_pct),
            ("planned_dhw_temp_c", dhw_temp),
        ]:
            self._client.publish(
                self._state_topic("sensor", name),
                json.dumps({"state": value}),
                retain=True,
            )

    def is_enabled(self) -> bool:
        return self._switch_states["enabled"]

    def is_battery_enabled(self) -> bool:
        return self._switch_states["battery_control"]

    def is_dhw_enabled(self) -> bool:
        return self._switch_states["dhw_control"]

    def is_ac_enabled(self) -> bool:
        return self._switch_states["ac_control"]

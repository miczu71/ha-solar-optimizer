"""MQTT discovery and state publishing — 8 entities total.

Sensors  (5): status, plan_summary, savings_today, savings_month, mode
Switches (3): battery_live, dhw_live, ac_live

All chart data is served via FastAPI /api/timeline instead of HA-recorded
sensors — this eliminates 30+ recorder writes per replan.
"""
import json
import logging
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from config import Config

log = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "solar_optimizer"

SENSOR_CONFIGS: dict[str, dict] = {
    "status": {
        "name": "Status",
        "icon": "mdi:solar-power-variant",
        "value_template": "{{ value_json.state }}",
    },
    "plan_summary": {
        "name": "Plan Summary",
        "icon": "mdi:battery-clock",
        "value_template": "{{ value_json.state }}",
    },
    "savings_today": {
        "name": "Savings Today",
        "unit_of_measurement": "PLN",
        "icon": "mdi:cash-plus",
        "state_class": "measurement",
        "value_template": "{{ value_json.state }}",
    },
    "savings_month": {
        "name": "Savings Month",
        "unit_of_measurement": "PLN",
        "icon": "mdi:cash-multiple",
        "state_class": "total_increasing",
        "value_template": "{{ value_json.state }}",
    },
    "mode": {
        "name": "Mode",
        "icon": "mdi:eye-outline",
        "value_template": "{{ value_json.state }}",
    },
}

SWITCH_CONFIGS: dict[str, str] = {
    "battery_live": "Battery Live",
    "dhw_live":     "DHW Live",
    "ac_live":      "AC Live",
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
            "battery_live": False,
            "dhw_live":     False,
            "ac_live":      False,
        }

    def connect(self) -> None:
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()
        time.sleep(1)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            log.error("MQTT connect failed rc=%d", rc)
            return
        log.info("MQTT connected rc=%d", rc)
        self._publish_discovery()
        for key in SWITCH_CONFIGS:
            client.subscribe(f"{DISCOVERY_PREFIX}/switch/{NODE_ID}_{key}/set")

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode().strip().lower()
        for key in SWITCH_CONFIGS:
            if f"_{key}/set" in msg.topic:
                state = payload in ("on", "true", "1")
                self._switch_states[key] = state
                self._publish_switch_state(key, state)
                log.info("Switch %s -> %s", key, "ON" if state else "OFF")

    def _state_topic(self, kind: str, name: str) -> str:
        return f"{DISCOVERY_PREFIX}/{kind}/{NODE_ID}_{name}/state"

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": [NODE_ID],
            "name": "Solar Optimizer",
            "model": "ha-solar-optimizer v0.4",
            "manufacturer": "Custom",
        }
        for name, extra in SENSOR_CONFIGS.items():
            config = {
                "unique_id": f"so_{name}",
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
                "unique_id": f"so_{name}",
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
        log.info("MQTT discovery published (5 sensors, 3 switches)")

    def _publish_switch_state(self, name: str, state: bool) -> None:
        self._client.publish(
            self._state_topic("switch", name),
            "ON" if state else "OFF",
            retain=True,
        )

    def _pub(self, name: str, value) -> None:
        self._client.publish(
            self._state_topic("sensor", name),
            json.dumps({"state": value}),
            retain=True,
        )

    def publish_status(self, status_text: str, last_run: datetime, rule: str) -> None:
        self._pub("status", f"{status_text} | rule={rule} | {last_run.strftime('%H:%M')}")

    def publish_plan_summary(self, summary: str) -> None:
        self._pub("plan_summary", summary[:255])

    def publish_savings(self, today_pln: float, month_pln: float) -> None:
        self._pub("savings_today", round(today_pln, 2))
        self._pub("savings_month", round(month_pln, 2))

    def publish_mode(self, shadow: bool) -> None:
        self._pub("mode", "shadow" if shadow else "live")
        for key, state in self._switch_states.items():
            self._publish_switch_state(key, state)

    def is_battery_live(self) -> bool:
        return self._switch_states["battery_live"]

    def is_dhw_live(self) -> bool:
        return self._switch_states["dhw_live"]

    def is_ac_live(self) -> bool:
        return self._switch_states["ac_live"]

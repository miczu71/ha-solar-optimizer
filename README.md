# ha-solar-optimizer

EMHASS-like energy optimizer for Home Assistant, built for a Huawei solar+battery stack with a Heiko heat pump and Midea ACs on the Polish Tauron G12W two-tier tariff.

## What it does

- Runs every 30 minutes, computing a 24-hour (48-slot) optimal dispatch schedule
- Maximizes solar self-consumption by shifting flexible loads into PV-surplus windows
- Minimizes grid import cost using the G12W peak/off-peak price signal (1.23 / 0.63 PLN/kWh)
- Ships disabled by default (**shadow mode**) — logs what it *would* do without touching any controls

## Controlled loads

| Load | Control |
|------|----------|
| Heat pump DHW | Raises setpoint to 58°C + tight hysteresis during PV surplus; coasts at 48°C otherwise |
| Huawei battery | Optional off-peak forcible pre-charge when PV forecast won't cover peak demand |
| 3× Midea AC | ±2°C setpoint pre-conditioning before peak tariff windows |

## Installation

1. Add this repository to HA Supervisor → Add-on store
2. Install **Solar Optimizer**
3. Configure your InfluxDB and MQTT credentials in the add-on options
4. Start the add-on — it runs in shadow mode by default
5. Review `sensor.optimizer_*` entities in HA for plan quality before enabling live control

## Rollout stages

| Stage | What's live | Enable when |
|-------|-------------|-------------|
| 0 — Shadow | Plans published, no writes | Default; 2 weeks minimum |
| 1 — DHW | Heat pump setpoint/hysteresis writes | After shadow mode validates DHW plan |
| 2 — Battery | Off-peak forcible pre-charge | After DHW stage is stable |
| 3 — AC | ±2°C setpoint adjustments | After battery stage is stable |
| 4 — ML | LightGBM replaces rolling-mean forecast | After ≥30 days of operational data |

## MQTT entities

| Entity | Description |
|--------|-------------|
| `sensor.optimizer_status` | Last run time, phase, solver status |
| `sensor.optimizer_self_consumption_today` | % of PV consumed locally |
| `sensor.optimizer_grid_import_avoided_kwh` | kWh saved vs. baseline |
| `sensor.optimizer_battery_plan` | 48-slot forcible-charge plan (W) |
| `sensor.optimizer_dhw_next_window` | Next scheduled DHW heating slot |
| `sensor.optimizer_load_forecast_kwh` | Predicted total load next 24h |
| `sensor.optimizer_load_forecast_error_24h` | 24h load forecast MAPE % |
| `switch.optimizer_enabled` | Global on/off |
| `switch.optimizer_battery_control` | Enable battery dispatch |
| `switch.optimizer_dhw_control` | Enable DHW dispatch |
| `switch.optimizer_ac_control` | Enable AC pre-conditioning |

## Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `shadow_mode` | `true` | Publish plans without writing to HA |
| `replan_interval_minutes` | `30` | How often to recompute |
| `battery_capacity_kwh` | `5.0` | Huawei LUNA capacity |
| `dhw_comfort_min` | `45` | Minimum DHW tank temp (°C) |
| `dhw_solar_setpoint` | `58` | Setpoint when heating from surplus |
| `dhw_baseline_setpoint` | `48` | Setpoint when coasting |
| `ml_enabled` | `true` | Use LightGBM after 30+ days of data |

## Version

Current: **0.1.0** — shadow mode only, Phase-1 rolling-mean load forecast.

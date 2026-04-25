# ha-solar-optimizer

EMHASS-like energy optimizer for Home Assistant, built for a Huawei solar+battery stack with a
Heiko heat pump and Midea ACs on the Polish Tauron G12W two-tier tariff.

## What it does

- Runs every 30 minutes, computing a 24-hour (48-slot) optimal dispatch schedule
- Maximizes solar self-consumption by shifting flexible loads into PV-surplus windows
- Minimizes grid import cost using the G12W peak/off-peak price signal (1.23 / 0.63 PLN/kWh)
- Rewards keeping battery full at end-of-day to buffer overnight discharge
- Ships in **shadow mode** by default — logs what it would do without touching any controls
- Dashboard compares the optimizer plan against the existing JIT battery automation in real time

## Controlled loads

| Load | Control |
|------|----------|
| Heat pump DHW | Raises setpoint to 58°C + tight hysteresis during PV surplus; coasts at 48°C otherwise |
| Huawei battery | Optional off-peak forcible pre-charge when PV forecast won't cover peak demand |
| 3× Midea AC | ±2°C setpoint pre-conditioning before peak tariff windows |

## Installation

1. Add this repository to HA Supervisor → Add-on store
2. Install **Solar Optimizer**
3. Configure InfluxDB and MQTT credentials in the add-on options
4. Start the add-on — shadow mode is on by default
5. Review `sensor.optimizer_*` entities and the dashboard for plan quality

## Dashboard

Access via HA ingress (sidebar) or `http://addon-host:8099`.

| Tab | Contents |
|-----|----------|
| **Status** | Last replan, solver status, PV/load forecast; collapsible assumptions & objective panel |
| **Today's Plan** | 48-slot energy-flow chart, SoC + DHW temperature chart, full slot table |
| **History** | Per-day self-consumption and grid import (accumulates over time) |
| **Compare** | Live side-by-side: JIT automation state vs. optimizer plan + SoC trajectory chart |

## LP objective

```
minimize:
  1.0 × Σ grid_import[t]                    # maximize self-consumption
  + 0.3 × Σ price[t] × grid_import[t]       # prefer off-peak for unavoidable imports
  + 0.05 × Σ |dhw[t] − dhw[t−1]|           # anti-thrash
  + 0.02 × Σ (pv_to_bat[t] + bat_to_load[t]) # battery wear
  − 0.15 × soc[midnight]                    # reward full battery before overnight drain
```

## Rollout stages

| Stage | What's live | Enable when |
|-------|-------------|-------------|
| 0 — Shadow | Plans published, no writes | Default; 2+ weeks recommended |
| 1 — DHW | Heat pump setpoint/hysteresis writes | After shadow mode validates DHW plan |
| 2 — Battery | Off-peak forcible pre-charge | After DHW stage is stable |
| 3 — AC | ±2°C setpoint adjustments | After battery stage is stable |
| 4 — ML | LightGBM replaces rolling-mean forecast | After ≥30 days of operational data |

## MQTT entities

| Entity | Description |
|--------|-------------|
| `sensor.optimizer_status` | Last run time, phase, solver status |
| `sensor.optimizer_self_consumption_today` | % of PV consumed locally |
| `sensor.optimizer_grid_import_avoided_kwh` | kWh saved vs. naive baseline |
| `sensor.optimizer_battery_plan` | 48-slot forcible-charge plan (W) |
| `sensor.optimizer_dhw_next_window` | Next scheduled DHW heating slot |
| `sensor.optimizer_load_forecast_kwh` | Predicted total load next 24h |
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
| `soc_min_percent` | `10` | Hard battery floor |
| `dhw_comfort_min` | `45` | Minimum DHW tank temp (°C) |
| `dhw_solar_setpoint` | `58` | Setpoint when heating from surplus |
| `dhw_baseline_setpoint` | `48` | Setpoint when coasting |
| `ml_enabled` | `true` | Use LightGBM after ≥30 days of data |

## Version history

See [CHANGELOG.md](CHANGELOG.md) for full release notes.

Current version: **0.3.1**

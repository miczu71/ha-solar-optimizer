# ha-solar-optimizer

Rule-based energy optimizer for Home Assistant, built for a Huawei solar+battery stack with a
Heiko heat pump and Midea ACs on the Polish Tauron G12W two-tier tariff.

## What it does

Runs every 30 minutes and answers one question: **"What should the battery, heat pump, and ACs do right now to make the most of today's solar forecast?"**

- Evaluates five explicit battery rules (see below) using the Solcast PV forecast and a rolling load estimate
- Schedules off-peak grid pre-charging when tomorrow's peak demand will exceed PV production
- Shifts DHW heating into PV-surplus windows (free solar hot water)
- Pre-cools ACs in the 30 minutes before peak tariff to reduce peak-hour consumption
- Ships in **shadow mode** by default — computes and logs plans without issuing any HA service calls
- Dashboard shows live flows, the active rule, the plan for the next 48h, and hypothetical savings

## Dashboard

Single-page ingress UI with two panels:

**Top strip** — updates every 30 s:
```
Battery 67% (3.35/5.0 kWh)  |  PV 1.8 kW  |  Load 1.2 kW  |  Grid -0.3 kW (importing)
Plan: grid-charge tonight 23:00–04:00 → 92%
Rule: R3  |  Reason: Tomorrow peak load 14 kWh, PV during peak 6 kWh, shortfall 8 kWh
Mode: SHADOW  |  Today hypothetical savings*: 4.20 PLN  |  This month: 87 PLN
```

**Bottom chart** — 48-hour timeline:
- PV forecast (dashed yellow) and PV actual (solid yellow)
- Load forecast (dashed grey) and load actual (solid grey)
- Planned SoC trajectory (dashed blue) and actual SoC (solid blue)
- Peak-tariff hours: light red background shading
- Grid-charge windows: light blue background shading
- "now" vertical marker

*Shadow savings are hypothetical and approximate — see tooltip on the dashboard.

## Battery rules

| Rule | Trigger | Action |
|------|---------|--------|
| **R0 SAFETY** | SoC < 16% reserve | Idle — protect battery |
| **R1 PV charge** | PV > load AND SoC < 95% | Natural charging logged — no service call needed |
| **R2 Peak guard** | Peak tariff now active | Idle — never grid-charge during peak |
| **R3 Pre-peak top-up** | Off-peak window AND shortfall forecast | `forcible_charge` until target SoC reached |
| **R4 Export day** | PV surplus covers load + free battery | Idle — export surplus |
| **R5 Idle** | None of the above | No action |

## Entities (MQTT-discovered)

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.solar_optimizer_status` | sensor | Live status summary |
| `sensor.solar_optimizer_plan_summary` | sensor | One-line plan sentence |
| `sensor.solar_optimizer_savings_today` | sensor | Hypothetical savings today (PLN)* |
| `sensor.solar_optimizer_savings_month` | sensor | Hypothetical savings this month (PLN)* |
| `sensor.solar_optimizer_mode` | sensor | "shadow" or "live" |
| `switch.solar_optimizer_battery_live` | switch | Enable live battery dispatch |
| `switch.solar_optimizer_dhw_live` | switch | Enable live DHW dispatch |
| `switch.solar_optimizer_ac_live` | switch | Enable live AC dispatch |

## Installation

1. Add this repository to HA Supervisor → Add-on store
2. Install **Solar Optimizer**
3. Configure MQTT credentials in the add-on options (InfluxDB optional)
4. Start the add-on — **shadow mode is on by default**
5. Copy `packages/solar_optimizer.yaml` into your HA `packages/` directory and reload

## Rollout

| Stage | Action | How long |
|-------|--------|----------|
| 0 Shadow | All switches OFF; watch chart and savings | 1–2 weeks |
| 1 Battery live | Flip `switch.solar_optimizer_battery_live` ON; disable existing JIT battery automation | 1 week |
| 2 DHW live | Flip `switch.solar_optimizer_dhw_live` ON; disable DHW surplus automations | 1 week |
| 3 AC live | Flip `switch.solar_optimizer_ac_live` ON | ongoing |

Live mode requires *both* `config.shadow_mode: false` in add-on options AND the relevant switch ON.

## Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `battery_capacity_kwh` | 5.0 | Battery capacity (kWh) |
| `battery_max_charge_power_w` | 2500 | Max charge power (W) |
| `soc_reserve_pct` | 16 | Hardware backup floor (%) |
| `soc_max_percent` | 95 | Charge ceiling (%) |
| `load_history_days` | 14 | Rolling-mean window for load forecast |
| `shadow_mode` | true | Compute but don't issue service calls |
| `replan_interval_minutes` | 30 | How often to replan |
| `dhw_*` | various | DHW setpoints and hysteresis |
| `workday_entity` | `binary_sensor.workday` | G12W calendar — peak hours only on workdays |

# Changelog

All notable changes to Solar Optimizer are documented here.
Versions follow [Semantic Versioning](https://semver.org/).

---

## [0.3.8] — 2026-04-26

### Added
- **G12W Polish public holiday awareness**: `g12w_peak_vector` now accepts an `is_workday` flag read from HA's workday binary sensor (`binary_sensor.workday_sensor` by default). All Polish public holidays (fixed + Easter-based) are automatically excluded from peak tariff windows — no manual calendar maintenance required.
- **PLN cost savings sensor** (`sensor.optimizer_savings_pln`): published after every replan. Compares optimised grid cost vs. naive baseline (no battery / no DHW dispatch) using per-slot G12W prices. Visible on Lovelace and in the morning notification.
- **Daily morning plan notification**: `sensor.optimizer_morning_plan` publishes a rich text summary after each replan (PV forecast, battery trajectory, import, PLN savings, day type, DHW windows, deferrable load recommendations). HA automation in the package sends it to `notify.family` at 06:35.
- **Deferrable load scheduling** (advisory): configure appliances (e.g. dishwasher, washing machine) in `deferrable_loads` option. After each replan a sliding-window algorithm finds the slot that maximises PV coverage and publishes `sensor.optimizer_load_{name}_start_time`. No changes to LP solver — purely advisory.
- **Manual force-charge override**: set `input_number.optimizer_force_soc_target` (0 = disabled, 5–100%) and `input_number.optimizer_force_soc_deadline_hour`. Adds a hard LP constraint `soc[deadline] ≥ target`. Auto-cleared by setting back to 0. Notification sent on activation.
- **Vacation mode**: `input_boolean.optimizer_vacation_mode` + `input_number.optimizer_vacation_dhw_setpoint` (default 55°C). Passes an elevated `dhw_comfort_min` to the LP, keeping tank hotter while away.
- **Comfort profiles** (weekday vs. weekend/holiday DHW): `dhw_demand_hour_weekday` (default 07:00) and `dhw_demand_hour_weekend` (default 09:00) shift the DHW comfort-floor enforcement window automatically on non-workdays.
- **Auto-tune thermal parameters** (`thermal_calibrator.py`): runs weekly (Sunday 04:00). Queries 30 days of InfluxDB data, fits `dhw_loss_rate_c_per_hour` from idle-period temperature decay and `dhw_cop` from heating-period electrical/thermal ratios. Results saved to `/data/learned_params.json` and used in subsequent replans. Falls back to config defaults when insufficient data.

### Changed
- `run_optimizer()` now accepts `is_workday`, `force_soc_pct`, `force_soc_deadline_hour`, `vacation_dhw_setpoint`, `learned_dhw_loss_rate`, `learned_dhw_cop` — all optional with safe defaults.
- `OptimizeResult` gains `savings_pln`, `optimized_cost_pln`, `naive_cost_pln`.
- DHW demand window is now always set from the comfort profile (not only when bath-request is active).

---

## [0.3.7] — 2026-04-26

### Added
- **Battery charge source breakdown chart** (Today's Plan tab): new stacked bar chart below the SoC/DHW chart showing, per 30-min slot, how much energy flows from PV surplus into the battery (green), from forced grid precharge (yellow), and back out as discharge to load (red, negative). Uses exact LP solution values (`pv_to_battery_kwh`, `bat_to_load_kwh`) now exposed via `/schedule`.
- **Head-to-head comparison table** (Compare tab): new H2H table above the JIT/Optimizer detail cards comparing both systems side-by-side on 8 key metrics (SoC now, EOD SoC, this-slot charge power, PV forecast, load forecast, grid import, precharge total, DHW heat). Rows with comparable numeric values show a coloured "▲ OPT / ▲ JIT" winner badge with a 3% tolerance band for near-equal values.
- `OptimizeResult` fields `pv_to_battery_kwh` and `bat_to_load_kwh` (exact LP solution values, per slot); both exposed in `/schedule` endpoint.

---

## [0.3.6] — 2026-04-25

### Added
- **Plan-vs-reality overlay in the add-on web UI (Today's Plan tab)**:
  - Both charts (Energy flows and Battery SoC / DHW) now show dashed plan lines alongside solid actual-sensor lines for PV, load, grid import, SoC, and DHW temperature.
  - **Current-slot strip** (`#now-strip`) appears at the top of the plan panel showing Plan / Actual / Δ for all 5 metrics (PV, Load, Grid import, Battery SoC, DHW temperature) for the current 30-min slot.
  - Actual data is fetched from the new `/actual-today` endpoint which reads HA's history API and resamples to 48 half-hour averages.
- **`/actual-today` REST endpoint**: returns today's resampled 48-slot arrays for PV power (W), house load (W), grid import (W, sign-corrected), battery SoC (%), and DHW temperature (°C). Called client-side on every plan panel load.

---

## [0.3.5] — 2026-04-25

### Added
- **5 per-slot planned-value sensors** published to HA via MQTT every 30 min:
  `sensor.optimizer_planned_pv_w`, `sensor.optimizer_planned_load_w`,
  `sensor.optimizer_planned_grid_import_w`, `sensor.optimizer_planned_soc_pct`,
  `sensor.optimizer_planned_dhw_temp_c`. All have `state_class: measurement` so
  HA's statistics engine builds a history that ApexCharts can overlay against
  actual sensor readings for plan-vs-reality comparison.
- `MQTTPublisher.publish_current_slot()` method populates the above from the
  current-slot values of the latest OptimizeResult on every successful replan.

---

## [0.3.4] — 2026-04-25

### Fixed
- **Today's Plan tab blank** — `/schedule` API endpoint crashed with
  `AttributeError: 'OptimizeResult' object has no attribute 'pv_forecast_kwh'`
  (also `base_load_kwh` and `is_peak`). These per-slot input arrays were referenced
  in `api.py` but never added to the `OptimizeResult` dataclass. Added all three as
  optional fields (default empty list) and populate them from the optimizer inputs
  on every successful solve.

---

## [0.3.3] — 2026-04-25

### Fixed
- **LP Infeasible when DHW control is disabled** — when `switch.optimizer_dhw_control` is OFF,
  the DHW temperature LP variables (`dhwt_0..48`) have no heating term but still have
  `lowBound = dhw_comfort_min − 5 = 40°C`. The forced loss of `0.4°C/slot` means by slot 13
  the temperature hits the lower bound → infeasible. Fix: lower bound is relaxed to `0` when
  `enable_dhw=False`, and `dhw_temp_init` is clamped to the valid range (same pattern already
  used for `soc_init`). Previously the optimizer produced "Infeasible" on every replan and
  self-disabled after 3 consecutive failures.

---

## [0.3.2] — 2026-04-25

### Fixed
- **PuLP division crash** — `LpVariable / float` raises `TypeError` in the PuLP version
  installed in the Docker image. All three occurrences replaced with the equivalent
  `(1.0 / constant) * LpVariable` form that uses `LpVariable.__rmul__`:
  - Battery SoC dynamics: `bat_to_load[t] / ETA_DISCHARGE`
  - DHW electrical demand: `dhw[t] / cfg.dhw_cop`
  - DHW tank temperature: `dhw[t] / tm`
  Previously every replan failed with `TypeError: unsupported operand type(s) for /:
  'LpVariable' and 'float'` and the optimizer returned no schedule.

---

## [0.3.1] — 2026-04-25

### Added
- **Compare tab** in the dashboard — side-by-side real-time view of the existing JIT Battery
  Control automation vs. the optimizer shadow plan:
  - JIT card: replicates the Jinja2 template logic (workday calendar, net load, solar forecast,
    target SoC, required charge power, analysis text) using live HA entity reads
  - Optimizer card: EOD SoC, 24h PV/load forecast, planned grid import, precharge/DHW totals
  - 3-line SoC trajectory chart: Optimizer (blue) / JIT simulation (yellow dashed) /
    No-action naive (grey dashed)
  - 48-slot decision table with per-slot JIT charge W, optimizer precharge W, DHW kWh, grid import
- `/compare` REST endpoint (sync, FastAPI threadpool) that reads ~10 HA entities live and
  returns comparison JSON including simulated SoC trajectories
- `ha_client.py`: `get_state_str()` utility method

### Changed
- `main.py`: `set_state("ha", ha)` so `/compare` can read live sensors
- `main.py`: per-replan PV and base-load forecasts stored in state (`last_pv_forecast`,
  `last_base_load`) so `/compare` can compute trajectories without re-running InfluxDB queries

---

## [0.3.0] — 2026-04-25

### Added
- **End-of-day SoC incentive** (`w_eod_soc = 0.15`): LP objective now subtracts
  `0.15 × soc[SLOTS]` (kWh at midnight), rewarding a full battery at end of planning horizon.
  Prevents the wear penalty (`w_wear = 0.02`) from discouraging useful charging when PV surplus
  is plentiful. Overnight base-load discharge (3–5 kWh) is already modelled via `bat_to_load[t]`;
  this term ensures the LP also wants to *pre-fill* the battery before that drain happens.
- **Assumptions panel** (collapsible, Status tab): shows planning method, battery params,
  G12W tariff windows and prices, full objective formula with all weights, DHW config, and
  an explanation of the EOD-SoC term. Values are live from the running config.

### Changed
- `api.py`: `/status` now includes a `cfg` key with battery, DHW, and tariff parameters
- `api.py`: Shadow/Live badge colour now driven by `cfg.shadow_mode` from API response

---

## [0.2.9] — 2026-04-24

### Fixed
- **Real LightGBM lag features**: Phase-2 now fetches actual per-slot 1-day and 7-day rolling
  means from InfluxDB as lag features; previously hardcoded to 0.3 kWh/slot
- **Bath request → DHW demand constraints**: `input_boolean.temperatura_do_kapieli = on` marks
  the next 4 slots (2 h) as hard DHW comfort-floor constraints in the LP
- **Grid import avoided metric**: computed as naive baseline (load minus PV, no dispatch) minus
  optimised import; published to `sensor.optimizer_grid_import_avoided_kwh` via MQTT
- `influx_client.py`: `pv_total_yesterday()` method for LightGBM `pv_yesterday` feature

---

## [0.2.8] — 2026-04-23

### Fixed
- **Dashboard blank after HA ingress** (critical): all `fetch()` calls and `href` links changed
  from absolute paths (`/status`) to relative paths (`status`). Absolute paths resolve to HA's
  own frontend HTML when proxied through the Supervisor ingress token URL, causing JSON parse
  errors and blank panels
- Status panel now shows "Starting up… retrying in 5 s" and auto-retries until first replan

---

## [0.2.7] — 2026-04-22

### Fixed
- **Startup timing**: uvicorn now starts in a daemon thread *before* ML training and the first
  replan, making the dashboard available immediately instead of after 60–90 s warm-up
- Phase indicator set in state immediately after `_try_train()`, not only after Optimal solve

---

## [0.2.0] — 2026-04-21

### Added
- Full 3-tab web dashboard: Status, Today's Plan, History
- `/history` endpoint backed by `/data/plan_history.jsonl`
- MQTT discovery for all 8 sensors and 4 switches
- `/force-replan` POST endpoint wired to dashboard button

### Fixed
- Battery SoC infeasibility: initial SoC now clamped to `[soc_min_kwh, soc_max_kwh]`
- Solcast `pv_estimate` is kW not kWh — multiply by 0.5 to get kWh/slot

---

## [0.1.0] — 2026-04-20

### Added
- Initial release. Shadow mode only.
- Phase-1 LP optimizer with rolling-mean base-load forecast and G12W cost weighting
- Phase-2 LightGBM load forecaster (trains from InfluxDB or HA long-term statistics SQLite)
- APScheduler: replan every 30 min, retrain every Sunday 03:00
- FastAPI ingress API: `/status`, `/schedule`
- MQTT discovery
- HA config package `packages/solar_optimizer.yaml`: heartbeat watchdog, SoC safety guard,
  and failsafe-restore automations

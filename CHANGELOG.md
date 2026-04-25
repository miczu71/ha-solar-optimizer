# Changelog

All notable changes to Solar Optimizer are documented here.
Versions follow [Semantic Versioning](https://semver.org/).

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

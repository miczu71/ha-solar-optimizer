"""FastAPI ingress API: /, /status, /schedule, /force-replan."""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

log = logging.getLogger(__name__)

app = FastAPI(title="Solar Optimizer", version="0.2.2")

_state: dict[str, Any] = {
    "last_result": None,
    "last_run": None,
    "phase": 1,
    "replan_fn": None,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    last_run: Optional[datetime] = _state.get("last_run")
    result = _state.get("last_result")
    phase = _state.get("phase", 1)
    phase_label = "Phase 2 &#8212; LightGBM ML" if phase == 2 else "Phase 1 &#8212; Rolling Mean"
    solver_color = "#4ade80" if result and result.status == "Optimal" else "#f87171"

    rows = ""
    if result:
        rows = f"""
        <tr><td>Solver</td><td style="color:{solver_color}">{result.status}</td></tr>
        <tr><td>Objective</td><td>{result.objective_value:.4f}</td></tr>
        <tr><td>PV forecast today</td><td>{result.pv_forecast_kwh_total:.2f} kWh</td></tr>
        <tr><td>Load forecast today</td><td>{result.load_forecast_kwh_total:.2f} kWh</td></tr>
        """
    else:
        rows = "<tr><td colspan=2 style='color:#94a3b8'>No plan yet &mdash; waiting for first run</td></tr>"

    last_run_str = last_run.strftime("%Y-%m-%d %H:%M:%S") if last_run else "&mdash;"
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar Optimizer</title>
<style>
  body{{font-family:monospace;padding:20px;background:#0f172a;color:#e2e8f0;max-width:600px;margin:0 auto}}
  h1{{color:#4ade80;margin-bottom:4px}}h1 span{{font-size:0.55em;color:#64748b}}
  .badges{{margin:8px 0 16px}}.badge{{display:inline-block;padding:3px 10px;border-radius:12px;
    background:#1e293b;border:1px solid #334155;font-size:0.82em;margin-right:6px}}
  table{{border-collapse:collapse;width:100%;margin:12px 0}}
  td{{padding:7px 10px;border-bottom:1px solid #1e293b}}
  td:first-child{{color:#94a3b8;width:55%}}
  .links{{margin-top:16px;font-size:0.88em}}
  a{{color:#60a5fa;text-decoration:none}}a:hover{{text-decoration:underline}}
  .sep{{color:#334155;margin:0 6px}}
</style>
</head><body>
<h1>Solar Optimizer <span>v0.2.2</span></h1>
<div class="badges">
  <span class="badge">&#9679; Shadow Mode</span>
  <span class="badge">{phase_label}</span>
</div>
<table>
  <tr><td>Last replan</td><td>{last_run_str}</td></tr>
  {rows}
</table>
<div class="links">
  <a href="/status">JSON status</a><span class="sep">|</span>
  <a href="/schedule">JSON schedule</a><span class="sep">|</span>
  <a href="/force-replan" onclick="fetch('/force-replan',{{method:'POST'}});return false">Force replan</a>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/status")
async def status() -> JSONResponse:
    last_run: Optional[datetime] = _state.get("last_run")
    result = _state.get("last_result")
    return JSONResponse({
        "status": "ok",
        "phase": _state.get("phase", 1),
        "last_run": last_run.isoformat() if last_run else None,
        "solver_status": result.status if result else None,
        "objective": result.objective_value if result else None,
        "pv_forecast_kwh": result.pv_forecast_kwh_total if result else None,
        "load_forecast_kwh": result.load_forecast_kwh_total if result else None,
    })


@app.get("/schedule")
async def schedule() -> JSONResponse:
    result = _state.get("last_result")
    if result is None:
        raise HTTPException(status_code=503, detail="No schedule available yet")
    return JSONResponse({
        "dhw_heat_energy_kwh": result.dhw_heat_energy,
        "offpeak_precharge_w": result.offpeak_precharge_w,
        "soc_trajectory_pct": result.soc_trajectory,
        "dhw_temp_trajectory_c": result.dhw_temp_trajectory,
        "grid_import_kwh": result.grid_import_kwh,
        "grid_export_kwh": result.grid_export_kwh,
        "ac_deltas": result.ac_deltas,
    })


@app.post("/force-replan")
async def force_replan() -> JSONResponse:
    fn = _state.get("replan_fn")
    if fn is None:
        raise HTTPException(status_code=503, detail="Replan function not registered")
    try:
        fn()
        return JSONResponse({"status": "replan triggered"})
    except Exception as exc:
        log.error("Force replan failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

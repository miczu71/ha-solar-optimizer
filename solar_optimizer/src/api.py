"""FastAPI ingress API: /status, /schedule, /force-replan."""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

app = FastAPI(title="Solar Optimizer", version="0.1.0")

_state: dict[str, Any] = {
    "last_result": None,
    "last_run": None,
    "phase": 1,
    "replan_fn": None,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


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

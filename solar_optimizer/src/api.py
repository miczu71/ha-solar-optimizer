"""FastAPI ingress — two-panel dashboard.

Panel 1 (top):  status strip — battery/PV/grid/DHW/tariff + plan + mode/savings.
Panel 2 (bottom): rolling 48-hour timeline chart (PV, load, SoC, peak shading).

API endpoints:
  GET /             HTML dashboard
  GET /api/status   live status for the top strip (polled every 30 s)
  GET /api/timeline full 96-slot dataset for the chart (fetched on load + every 5 min)
  POST /api/replan  force an immediate replan
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from planner import format_plan_text
from tariff import datetime_to_slot

log = logging.getLogger(__name__)
app = FastAPI(title="Solar Optimizer")

_state: dict[str, Any] = {
    "last_plan":        None,
    "last_run":         None,
    "version":          "?",
    "cfg":              None,
    "ha":               None,
    "pv_96":            None,
    "load_96":          None,
    "is_peak_96":       None,
    "savings_today":    0.0,
    "savings_month":    0.0,
    "replan_fn":        None,
    # cached sensor values (updated by replan loop — no live HA reads in /api/status)
    "soc_pct":          0.0,
    "pv_kw":            0.0,
    "load_kw":          0.0,
    "grid_kw":          0.0,
    "battery_kw":       0.0,
    "dhw_plan":         None,
    "dhw_temp":         0.0,
    "is_peak_now":      False,
    "tariff_price":     0.63,
    "workday_today":    True,
    "workday_tomorrow": True,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


def _next_tariff_event(
    now: datetime, is_peak_now: bool, workday_today: bool, workday_tomorrow: bool
) -> str:
    """Return a short string: 'Peak ends 13:00 (1h 05m)' or 'Peak starts 15:00 (0h 45m)'."""
    h = now.hour
    if is_peak_now:
        end_h = 13 if h < 13 else 22
        end_dt = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
        dm = max(0, int((end_dt - now).total_seconds() / 60))
        return f"Peak ends {end_dt.strftime('%H:%M')} ({dm // 60}h {dm % 60:02d}m)"
    if workday_today:
        if h < 6:
            s = now.replace(hour=6, minute=0, second=0, microsecond=0)
            dm = max(0, int((s - now).total_seconds() / 60))
            return f"Peak starts {s.strftime('%H:%M')} ({dm // 60}h {dm % 60:02d}m)"
        if 13 <= h < 15:
            s = now.replace(hour=15, minute=0, second=0, microsecond=0)
            dm = max(0, int((s - now).total_seconds() / 60))
            return f"Peak starts {s.strftime('%H:%M')} ({dm // 60}h {dm % 60:02d}m)"
    if workday_tomorrow:
        s = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        dm = max(0, int((s - now).total_seconds() / 60))
        return f"Peak starts {s.strftime('%H:%M')}+1 ({dm // 60}h {dm % 60:02d}m)"
    return "Off-peak (no peak soon)"


# ------------------------------------------------------------------
# /api/status  — reads from cached _state; no live HA calls
# ------------------------------------------------------------------

@app.get("/api/status")
def api_status() -> JSONResponse:
    ha   = _state.get("ha")
    cfg  = _state.get("cfg")
    plan = _state.get("last_plan")
    last_run: Optional[datetime] = _state.get("last_run")
    tz   = ha.tz if ha else None

    soc         = _state.get("soc_pct", 0.0)
    pv_kw       = _state.get("pv_kw", 0.0)
    load_kw     = _state.get("load_kw", 0.0)
    grid_kw     = _state.get("grid_kw", 0.0)
    battery_kw  = _state.get("battery_kw", 0.0)
    dhw_plan    = _state.get("dhw_plan")
    dhw_temp    = _state.get("dhw_temp", 0.0)
    is_peak_now      = _state.get("is_peak_now", False)
    tariff_price     = _state.get("tariff_price", 0.63)
    workday_today    = _state.get("workday_today", True)
    workday_tomorrow = _state.get("workday_tomorrow", True)

    mode = "shadow" if (cfg and cfg.shadow_mode) else "live"

    plan_text   = "Waiting for first replan…"
    plan_reason = ""
    rule        = "?"

    if plan:
        bat = plan.battery
        rule = bat.rule
        plan_text = format_plan_text(bat, soc)
        plan_reason = bat.reason

    def _local_str(dt: datetime) -> str:
        return (dt.astimezone(tz) if tz else dt).strftime("%H:%M:%S")

    grid_dir = "exporting" if grid_kw > 0.05 else ("importing" if grid_kw < -0.05 else "balanced")
    bat_dir  = "charging" if battery_kw > 0.05 else ("discharging" if battery_kw < -0.05 else "idle")

    now_local = ha.local_now if ha else None
    tariff_event = (
        _next_tariff_event(now_local, is_peak_now, workday_today, workday_tomorrow)
        if now_local else "—"
    )

    return JSONResponse({
        "soc_pct":         round(soc, 1),
        "pv_kw":           round(pv_kw, 2),
        "load_kw":         round(load_kw, 2),
        "grid_kw":         round(grid_kw, 2),
        "grid_dir":        grid_dir,
        "battery_kw":      round(battery_kw, 2),
        "battery_dir":     bat_dir,
        "bat_cap_kwh":     cfg.battery_capacity_kwh if cfg else 5.0,
        "tariff_price":    tariff_price,
        "is_peak_now":     is_peak_now,
        "tariff_event":    tariff_event,
        "plan_text":       plan_text,
        "plan_reason":     plan_reason,
        "rule":            rule,
        "mode":            mode,
        "dhw_type":        dhw_plan.type if dhw_plan else "unknown",
        "dhw_reason":      dhw_plan.reason if dhw_plan else "—",
        "dhw_temp":        round(dhw_temp, 1),
        "savings_today":   round(_state.get("savings_today", 0.0), 2),
        "savings_month":   round(_state.get("savings_month", 0.0), 2),
        "last_run":        _local_str(last_run) if last_run else "—",
        "version":         _state.get("version", "?"),
    })


# ------------------------------------------------------------------
# /api/timeline — 96-slot data for the chart
# ------------------------------------------------------------------

@app.get("/api/timeline")
def api_timeline() -> JSONResponse:
    ha       = _state.get("ha")
    plan     = _state.get("last_plan")
    pv_96    = _state.get("pv_96") or ([0.0] * 96)
    load_96  = _state.get("load_96") or ([0.3] * 96)
    peak_96  = _state.get("is_peak_96") or ([False] * 96)

    labels = []
    for s in range(48):
        h, m = divmod(s * 30, 60)
        labels.append(f"{h:02d}:{m:02d}")
    for s in range(48):
        h, m = divmod(s * 30, 60)
        labels.append(f"{h:02d}:{m:02d}+1")

    current_slot = datetime_to_slot(ha.local_now) if ha else 0

    actual_pv: list[Optional[float]] = [None] * 96
    actual_load: list[Optional[float]] = [None] * 96
    actual_soc: list[Optional[float]] = [None] * 96

    if ha:
        try:
            hist = ha.get_history_today_30min([
                "sensor.inverter_input_power",
                "sensor.house_consumption_power",
                "sensor.battery_state_of_capacity",
            ])

            def _ffill(vals: list) -> list:
                out, last = list(vals), None
                for i, v in enumerate(out):
                    if v is not None:
                        last = v
                    elif last is not None:
                        out[i] = last
                return out

            pv_slots   = _ffill(hist.get("sensor.inverter_input_power", [None] * 48))
            load_slots = _ffill(hist.get("sensor.house_consumption_power", [None] * 48))
            soc_slots  = _ffill(hist.get("sensor.battery_state_of_capacity", [None] * 48))

            for s in range(current_slot + 1):
                if pv_slots[s] is not None:
                    actual_pv[s] = round(pv_slots[s] / 1000, 3)
                if load_slots[s] is not None:
                    actual_load[s] = round(load_slots[s] / 1000, 3)
                if soc_slots[s] is not None:
                    actual_soc[s] = round(soc_slots[s], 1)
        except Exception as exc:
            log.warning("Timeline history fetch failed: %s", exc)

    planned_soc: list[Optional[float]] = [None] * 96
    if plan and plan.soc_trajectory:
        for i, val in enumerate(plan.soc_trajectory):
            slot_idx = current_slot + i
            if slot_idx < 48:
                planned_soc[slot_idx] = val

    charge_windows = []
    if plan and plan.battery.type == "grid_charge" and plan.battery.grid_charge_start:
        bat = plan.battery
        if ha:
            midnight = ha.local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_slot = int((bat.grid_charge_start - midnight).total_seconds() / 1800)
            end_slot   = int((bat.grid_charge_end   - midnight).total_seconds() / 1800)
            if start_slot < 0:
                start_slot += 48
                end_slot   += 48
            charge_windows.append({
                "start_slot": start_slot,
                "end_slot":   end_slot,
                "target_soc": bat.target_soc_pct,
                "reason":     bat.reason,
            })

    return JSONResponse({
        "labels":         labels,
        "current_slot":   current_slot,
        "pv_forecast":    [round(v, 3) for v in pv_96],
        "load_forecast":  [round(v, 3) for v in load_96],
        "actual_pv":      actual_pv,
        "actual_load":    actual_load,
        "actual_soc":     actual_soc,
        "planned_soc":    planned_soc,
        "is_peak":        peak_96,
        "charge_windows": charge_windows,
        "plan_rule":      (plan.battery.rule if plan else "?"),
        "plan_reason":    (plan.battery.reason if plan else ""),
    })


# ------------------------------------------------------------------
# /api/replan — force immediate replan
# ------------------------------------------------------------------

@app.post("/api/replan")
def api_replan() -> JSONResponse:
    fn = _state.get("replan_fn")
    if fn:
        try:
            fn()
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": False, "error": "replan not yet wired"}, status_code=503)


# ------------------------------------------------------------------
# / — HTML dashboard
# ------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Optimizer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0e1117; color: #e0e0e0; font-family: monospace; height: 100vh;
         display: flex; flex-direction: column; overflow: hidden; }

  /* ---- Status strip ---- */
  #strip { background: #161b22; border-bottom: 1px solid #30363d; padding: 8px 16px;
           flex-shrink: 0; }
  .row { display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: center;
         font-size: 13px; line-height: 1.6; }
  .row + .row { margin-top: 3px; }
  .lbl  { color: #8b949e; font-size: 11px; text-transform: uppercase; margin-right: 4px; }
  .val  { color: #c9d1d9; font-weight: bold; }
  .val.pos  { color: #3fb950; }
  .val.neg  { color: #f85149; }
  .val.warn { color: #d29922; }
  .val.info { color: #58a6ff; }
  .sep { color: #30363d; }
  .reason { color: #8b949e; font-size: 11px; font-style: italic; }
  #mode-tag { display: inline-block; padding: 1px 7px; border-radius: 9px;
              font-size: 11px; font-weight: bold; background: #1c2128;
              border: 1px solid #30363d; }
  #mode-tag.shadow { color: #d29922; border-color: #d29922; }
  #mode-tag.live   { color: #3fb950; border-color: #3fb950; }
  #replan-btn {
    background: #1c2128; border: 1px solid #30363d; color: #c9d1d9;
    padding: 2px 10px; border-radius: 4px; cursor: pointer;
    font-family: monospace; font-size: 11px;
    transition: border-color 0.15s, color 0.15s;
  }
  #replan-btn:hover:not(:disabled) { border-color: #58a6ff; color: #58a6ff; }
  #replan-btn:disabled { cursor: wait; opacity: 0.65; }

  /* ---- Chart panel ---- */
  #chart-panel { flex: 1; position: relative; min-height: 0; padding: 8px 12px; }
  #chart-panel canvas { display: block; width: 100% !important; height: 100% !important; }

  /* ---- Loading overlay ---- */
  #loading { position: absolute; inset: 0; background: rgba(14,17,23,0.85);
             display: flex; align-items: center; justify-content: center;
             font-size: 14px; color: #8b949e; z-index: 10; }
</style>
</head>
<body>

<!-- Panel 1: Status strip -->
<div id="strip">
  <!-- Row 1: Live power readings -->
  <div class="row">
    <span><span class="lbl">Battery</span><span id="soc" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">PV</span><span id="pv" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Load</span><span id="load" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Grid</span><span id="grid" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Updated</span><span id="updated" class="val">&#8212;</span></span>
  </div>
  <!-- Row 2: Plan -->
  <div class="row">
    <span><span class="lbl">Plan</span><span id="plan-text" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Rule</span><span id="plan-rule" class="val">&#8212;</span></span>
    <span id="plan-reason" class="reason"></span>
  </div>
  <!-- Row 3: DHW + Tariff -->
  <div class="row">
    <span><span class="lbl">DHW</span><span id="dhw" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <span id="tariff" class="val">&#8212;</span>
    <span class="sep">|</span>
    <span id="tariff-event" class="val info">&#8212;</span>
  </div>
  <!-- Row 4: Mode + Savings + Replan -->
  <div class="row">
    <span><span id="mode-tag" class="shadow">SHADOW</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Today*</span><span id="sav-today" class="val pos" title="Positive = optimizer would save vs. actual. Negative = actual was cheaper (shadow plan not optimal yet).">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Month*</span><span id="sav-month" class="val pos" title="Cumulative 30-day hypothetical savings.">&#8212;</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">v</span><span id="version" class="val">&#8212;</span></span>
    <span class="sep">|</span>
    <button id="replan-btn" onclick="forceReplan()">Replan</button>
    <span style="color:#555;font-size:10px">* hypothetical; assumes perfect execution</span>
  </div>
</div>

<!-- Panel 2: Timeline chart -->
<div id="chart-panel">
  <div id="loading">Loading chart&#8230;</div>
  <canvas id="timeline"></canvas>
</div>

<script>
const $ = id => document.getElementById(id);
function cls(el, ...classes) { el.className = classes.join(' '); }

// ---- Status strip (polled every 30 s) ----
async function refreshStatus() {
  try {
    const s = await fetch('api/status').then(r => r.json());

    // Battery: SoC% + direction arrow + kWh
    const kwh    = (s.soc_pct / 100 * s.bat_cap_kwh).toFixed(2);
    const bkw    = Math.abs(s.battery_kw).toFixed(2);
    const arrow  = s.battery_dir === 'charging' ? '↑' : (s.battery_dir === 'discharging' ? '↓' : '→');
    const socEl  = $('soc');
    socEl.textContent = `${s.soc_pct.toFixed(1)}% ${arrow}${bkw}kW (${kwh}/${s.bat_cap_kwh}kWh)`;
    cls(socEl, 'val', s.battery_dir === 'charging' ? 'pos' : (s.battery_dir === 'discharging' ? 'warn' : ''));

    $('pv').textContent   = `${s.pv_kw.toFixed(2)} kW`;
    $('load').textContent = `${s.load_kw.toFixed(2)} kW`;

    const gridEl = $('grid');
    if (s.grid_kw > 0.05) {
      gridEl.textContent = `+${s.grid_kw.toFixed(2)} kW (exporting)`;
      cls(gridEl, 'val', 'pos');
    } else if (s.grid_kw < -0.05) {
      gridEl.textContent = `${s.grid_kw.toFixed(2)} kW (importing)`;
      cls(gridEl, 'val', 'neg');
    } else {
      gridEl.textContent = `≈0 kW (balanced)`;
      cls(gridEl, 'val');
    }

    $('updated').textContent = s.last_run;

    $('plan-text').textContent = s.plan_text;
    $('plan-rule').textContent = s.rule;
    $('plan-reason').textContent = s.plan_reason !== s.plan_text ? s.plan_reason : '';

    // DHW
    const dhwLabels = {heat_solar: 'solar heat', heat_comfort: 'comfort heat', coast: 'coasting', unknown: '—'};
    $('dhw').textContent = `${s.dhw_temp.toFixed(1)}°C → ${dhwLabels[s.dhw_type] || s.dhw_type}`;

    // Tariff
    const tariffEl = $('tariff');
    tariffEl.textContent = `${s.is_peak_now ? 'PEAK' : 'OFF-PEAK'} ${s.tariff_price.toFixed(2)} PLN/kWh`;
    cls(tariffEl, 'val', s.is_peak_now ? 'neg' : 'pos');
    $('tariff-event').textContent = s.tariff_event;

    // Savings (negative = actual was cheaper)
    const fmtSav = v => v >= 0 ? `+${v.toFixed(2)} PLN` : `${v.toFixed(2)} PLN`;
    $('sav-today').textContent = fmtSav(s.savings_today);
    cls($('sav-today'), 'val', s.savings_today >= 0 ? 'pos' : 'neg');
    $('sav-month').textContent = fmtSav(s.savings_month);
    cls($('sav-month'), 'val', s.savings_month >= 0 ? 'pos' : 'neg');

    const modeTag = $('mode-tag');
    modeTag.textContent = s.mode === 'live' ? 'LIVE' : 'SHADOW';
    cls(modeTag, s.mode === 'live' ? 'live' : 'shadow');

    $('version').textContent = s.version;
  } catch(e) { console.warn('status error', e); }
}

// ---- Force replan ----
async function forceReplan() {
  const btn = $('replan-btn');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Running...';
  try {
    const r = await fetch('api/replan', {method: 'POST'});
    const j = await r.json();
    if (j.ok) {
      btn.textContent = 'Done';
      btn.style.color = '#3fb950';
      btn.style.borderColor = '#3fb950';
      setTimeout(() => { refreshStatus(); buildChart(); }, 3000);
    } else {
      btn.textContent = 'Failed';
      btn.style.color = '#f85149';
      btn.style.borderColor = '#f85149';
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.style.color = '#f85149';
  } finally {
    setTimeout(() => {
      btn.textContent = orig;
      btn.disabled = false;
      btn.style.color = '';
      btn.style.borderColor = '';
    }, 5000);
  }
}

// ---- Chart ----
let chart = null;

// "now" vertical line plugin
const nowPlugin = {
  id: 'nowLine',
  afterDraw(chart) {
    const pos = chart.config._nowPos;
    if (pos == null) return;
    const xs = chart.scales.x;
    if (!xs) return;
    const x = xs.getPixelForValue(pos);
    const {top, bottom} = chart.chartArea, ctx = chart.ctx;
    ctx.save();
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom);
    ctx.lineWidth = 1.5; ctx.strokeStyle = 'rgba(255,255,255,0.45)';
    ctx.setLineDash([4, 4]); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '9px monospace'; ctx.textAlign = 'center';
    ctx.fillText('now', x, top - 3);
    ctx.restore();
  }
};

// Peak-shading background plugin
const peakPlugin = {
  id: 'peakShading',
  beforeDatasetsDraw(chart) {
    const peaks = chart.config._peaks;
    if (!peaks || !peaks.length) return;
    const xs = chart.scales.x;
    const {top, bottom} = chart.chartArea, ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = 'rgba(255, 80, 80, 0.08)';
    let inPeak = false, peakStart = 0;
    for (let i = 0; i < peaks.length; i++) {
      if (peaks[i] && !inPeak)       { inPeak = true; peakStart = i; }
      else if (!peaks[i] && inPeak)  { inPeak = false;
        ctx.fillRect(xs.getPixelForValue(peakStart), top,
                     xs.getPixelForValue(i) - xs.getPixelForValue(peakStart), bottom - top);
      }
    }
    if (inPeak) {
      ctx.fillRect(xs.getPixelForValue(peakStart), top,
                   xs.getPixelForValue(peaks.length - 1) - xs.getPixelForValue(peakStart), bottom - top);
    }
    ctx.restore();
  }
};

// Charge-window shading plugin
const chargePlugin = {
  id: 'chargeShading',
  beforeDatasetsDraw(chart) {
    const windows = chart.config._chargeWindows;
    if (!windows || !windows.length) return;
    const xs = chart.scales.x;
    const {top, bottom} = chart.chartArea, ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = 'rgba(88, 166, 255, 0.12)';
    for (const w of windows) {
      ctx.fillRect(xs.getPixelForValue(w.start_slot), top,
                   xs.getPixelForValue(w.end_slot) - xs.getPixelForValue(w.start_slot), bottom - top);
    }
    ctx.restore();
  }
};

async function buildChart() {
  try {
    const d = await fetch('api/timeline').then(r => r.json());
    $('loading').style.display = 'none';

    // Rolling window: 8 h history + 36 h future
    const winStart = Math.max(0, d.current_slot - 16);
    const winEnd   = Math.min(95, d.current_slot + 72);
    const nowPos   = d.current_slot - winStart;
    const sl = arr => (arr || []).slice(winStart, winEnd + 1);

    const winLabels  = sl(d.labels);
    const tickLabels = winLabels.map((l, i) => ((winStart + i) % 4 === 0 ? l : ''));

    // Adjust charge windows to windowed indices
    const chargeWindows = (d.charge_windows || [])
      .map(w => ({...w, start_slot: w.start_slot - winStart, end_slot: w.end_slot - winStart}))
      .filter(w => w.end_slot >= 0 && w.start_slot <= winEnd - winStart);

    const YELLOW      = 'rgba(255, 200, 60, 0.9)';
    const YELLOW_DARK = 'rgba(200, 150, 30, 0.9)';
    const GREY        = 'rgba(140, 140, 160, 0.7)';
    const GREY_DARK   = 'rgba(100, 100, 120, 0.9)';
    const BLUE_DASH   = 'rgba(88, 166, 255, 0.7)';
    const BLUE_SOLID  = 'rgba(88, 166, 255, 1.0)';

    const datasets = [
      {
        label: 'PV forecast (kW)', yAxisID: 'y', data: sl(d.pv_forecast),
        borderColor: YELLOW, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, tension: 0.3,
      },
      {
        label: 'PV actual (kW)', yAxisID: 'y', data: sl(d.actual_pv),
        borderColor: YELLOW_DARK, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3, spanGaps: false,
      },
      {
        label: 'Load forecast (kW)', yAxisID: 'y', data: sl(d.load_forecast),
        borderColor: GREY, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, tension: 0.3,
      },
      {
        label: 'Load actual (kW)', yAxisID: 'y', data: sl(d.actual_load),
        borderColor: GREY_DARK, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3, spanGaps: false,
      },
      {
        label: 'Planned SoC (%)', yAxisID: 'y2', data: sl(d.planned_soc),
        borderColor: BLUE_DASH, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [5, 3], pointRadius: 0, tension: 0.3, spanGaps: false,
      },
      {
        label: 'Actual SoC (%)', yAxisID: 'y2', data: sl(d.actual_soc),
        borderColor: BLUE_SOLID, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3, spanGaps: false,
      },
    ];

    if (chart) { chart.destroy(); chart = null; }

    chart = new Chart($('timeline').getContext('2d'), {
      type: 'line',
      _nowPos:        nowPos,
      _peaks:         sl(d.is_peak),
      _chargeWindows: chargeWindows,
      data: { labels: tickLabels, datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            labels: { color: '#8b949e', font: { size: 11 }, boxWidth: 20, padding: 12 },
          },
          tooltip: {
            backgroundColor: 'rgba(22,27,34,0.95)',
            borderColor: '#30363d', borderWidth: 1,
            titleColor: '#c9d1d9', bodyColor: '#8b949e',
            callbacks: {
              title: items => {
                const gi = winStart + items[0].dataIndex;
                const price = d.is_peak[gi] ? '1.23 PLN/kWh' : '0.63 PLN/kWh';
                return `${d.labels[gi]}  ${d.is_peak[gi] ? 'PEAK' : 'off-peak'} • ${price}`;
              },
              afterBody: items => {
                const gi = winStart + items[0].dataIndex;
                const lines = [];
                if (gi === d.current_slot)
                  lines.push(`Rule: ${d.plan_rule}  —  ${d.plan_reason}`);
                const cw = d.charge_windows.find(w => gi >= w.start_slot && gi <= w.end_slot);
                if (cw) lines.push(`Grid-charge window → ${cw.target_soc}%`);
                return lines;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#8b949e', font: { size: 10 }, maxRotation: 0 },
            grid:  { color: 'rgba(48,54,61,0.5)' },
          },
          y: {
            position: 'left',
            title: { display: true, text: 'kW', color: '#8b949e', font: { size: 11 } },
            ticks: { color: '#8b949e', font: { size: 10 } },
            grid:  { color: 'rgba(48,54,61,0.5)' },
            min: 0,
          },
          y2: {
            position: 'right',
            title: { display: true, text: 'SoC %', color: '#8b949e', font: { size: 11 } },
            ticks: { color: '#8b949e', font: { size: 10 } },
            grid:  { drawOnChartArea: false },
            min: 0, max: 100,
          },
        },
      },
      plugins: [nowPlugin, peakPlugin, chargePlugin],
    });

  } catch(e) {
    console.error('chart error', e);
    $('loading').textContent = 'Chart error: ' + e;
    $('loading').style.display = 'flex';
  }
}

// boot
refreshStatus();
buildChart();
setInterval(refreshStatus, 30_000);
setInterval(buildChart, 5 * 60_000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_HTML)

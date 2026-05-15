"""FastAPI ingress — two-panel dashboard.

Panel 1 (top):  status strip — live battery/PV/grid values + plan sentence + mode/savings.
Panel 2 (bottom): single 48-hour timeline chart (PV, load, SoC, peak shading).

API endpoints:
  GET /           HTML dashboard
  GET /api/status  live status for the top strip (polled every 30 s)
  GET /api/timeline  full 96-slot dataset for the chart (fetched on load + every 30 min)
  POST /api/replan   force an immediate replan
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tariff import is_peak, PEAK_PRICE, OFFPEAK_PRICE

log = logging.getLogger(__name__)
app = FastAPI(title="Solar Optimizer")

_state: dict[str, Any] = {
    "last_plan":     None,
    "last_run":      None,
    "version":       "?",
    "cfg":           None,
    "ha":            None,
    "pv_96":         None,
    "load_96":       None,
    "is_peak_96":    None,
    "savings_today": 0.0,
    "savings_month": 0.0,
    "replan_fn":     None,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


# ------------------------------------------------------------------
# /api/status  — live sensor snapshot (polled every 30 s by the UI)
# ------------------------------------------------------------------

@app.get("/api/status")
def api_status() -> JSONResponse:
    ha  = _state.get("ha")
    cfg = _state.get("cfg")
    plan = _state.get("last_plan")
    last_run: Optional[datetime] = _state.get("last_run")

    soc = pv_kw = load_kw = grid_kw = 0.0
    mode = "shadow"

    if ha:
        try:
            soc     = ha.soc_percent
            pv_kw   = ha.pv_power_w / 1000
            load_kw = ha.house_load_w / 1000
            grid_kw = (ha.grid_export_w - ha.grid_import_w) / 1000
        except Exception:
            pass

    if cfg:
        mode = "shadow" if cfg.shadow_mode else "live"

    plan_text   = "Waiting for first replan…"
    plan_reason = ""
    rule        = "?"
    if plan:
        bat = plan.battery
        rule = bat.rule
        if bat.type == "grid_charge" and bat.grid_charge_start:
            plan_text = (
                f"Grid-charge {bat.grid_charge_start.strftime('%H:%M')}–"
                f"{bat.grid_charge_end.strftime('%H:%M')} → {bat.target_soc_pct:.0f}%"
            )
        elif bat.type == "pv_charge":
            plan_text = f"Charging from PV → battery at {soc:.0f}%"
        else:
            plan_text = bat.reason
        plan_reason = bat.reason

    last_run_str = last_run.strftime("%H:%M:%S") if last_run else "—"

    grid_dir = "exporting" if grid_kw > 0.05 else ("importing" if grid_kw < -0.05 else "balanced")

    return JSONResponse({
        "soc_pct":         round(soc, 1),
        "pv_kw":           round(pv_kw, 2),
        "load_kw":         round(load_kw, 2),
        "grid_kw":         round(grid_kw, 2),
        "grid_dir":        grid_dir,
        "bat_cap_kwh":     cfg.battery_capacity_kwh if cfg else 5.0,
        "plan_text":       plan_text,
        "plan_reason":     plan_reason,
        "rule":            rule,
        "mode":            mode,
        "savings_today":   round(_state.get("savings_today", 0.0), 2),
        "savings_month":   round(_state.get("savings_month", 0.0), 2),
        "last_run":        last_run_str,
        "version":         _state.get("version", "?"),
    })


# ------------------------------------------------------------------
# /api/timeline — 96-slot data for the chart
# ------------------------------------------------------------------

@app.get("/api/timeline")
def api_timeline() -> JSONResponse:
    ha       = _state.get("ha")
    cfg      = _state.get("cfg")
    plan     = _state.get("last_plan")
    pv_96    = _state.get("pv_96") or ([0.0] * 96)
    load_96  = _state.get("load_96") or ([0.3] * 96)
    peak_96  = _state.get("is_peak_96") or ([False] * 96)

    # Build slot labels: "HH:MM" for today, "HH:MM+1" for tomorrow
    labels = []
    for s in range(48):
        h, m = divmod(s * 30, 60)
        labels.append(f"{h:02d}:{m:02d}")
    for s in range(48):
        h, m = divmod(s * 30, 60)
        labels.append(f"{h:02d}:{m:02d}+1")

    # Current slot (index into today's 48-slot array)
    current_slot = 0
    if ha:
        nl = ha.local_now
        current_slot = nl.hour * 2 + nl.minute // 30

    # Actual values for past slots from HA history (today only, slots 0 → current_slot)
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
                    actual_pv[s] = round(pv_slots[s] / 1000, 3)   # W → kW
                if load_slots[s] is not None:
                    actual_load[s] = round(load_slots[s] / 1000, 3)
                if soc_slots[s] is not None:
                    actual_soc[s] = round(soc_slots[s], 1)
        except Exception as exc:
            log.warning("Timeline history fetch failed: %s", exc)

    # Planned SoC trajectory (49 points starting at current_slot)
    planned_soc: list[Optional[float]] = [None] * 96
    if plan and plan.soc_trajectory:
        for i, val in enumerate(plan.soc_trajectory):
            slot_idx = current_slot + i
            if slot_idx < 48:
                planned_soc[slot_idx] = val

    # Grid-charge windows from the plan (for chart annotation)
    charge_windows = []
    if plan and plan.battery.type == "grid_charge" and plan.battery.grid_charge_start:
        bat = plan.battery
        if ha:
            nl = ha.local_now
            midnight = nl.replace(hour=0, minute=0, second=0, microsecond=0)
            start_slot = int((bat.grid_charge_start - midnight).total_seconds() / 1800)
            end_slot   = int((bat.grid_charge_end   - midnight).total_seconds() / 1800)
            # Clamp to 0-47; if overnight wrap to tomorrow (48-95)
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
        "labels":        labels,
        "current_slot":  current_slot,
        "pv_forecast":   [round(v, 3) for v in pv_96],
        "load_forecast": [round(v, 3) for v in load_96],
        "actual_pv":     actual_pv,
        "actual_load":   actual_load,
        "actual_soc":    actual_soc,
        "planned_soc":   planned_soc,
        "is_peak":       peak_96,
        "charge_windows": charge_windows,
        "plan_rule":     (plan.battery.rule if plan else "?"),
        "plan_reason":   (plan.battery.reason if plan else ""),
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
  #strip { background: #161b22; border-bottom: 1px solid #30363d; padding: 10px 16px;
           flex-shrink: 0; }
  .row { display: flex; flex-wrap: wrap; gap: 6px 20px; align-items: center;
         font-size: 13px; line-height: 1.6; }
  .row + .row { margin-top: 4px; }
  .lbl  { color: #8b949e; font-size: 11px; text-transform: uppercase; margin-right: 4px; }
  .val  { color: #c9d1d9; font-weight: bold; }
  .val.pos  { color: #3fb950; }   /* exporting / savings */
  .val.neg  { color: #f85149; }   /* importing */
  .val.warn { color: #d29922; }   /* caution */
  .val.info { color: #58a6ff; }   /* shadow mode / info */
  .sep { color: #30363d; }
  #plan-row .reason { color: #8b949e; font-size: 11px; font-style: italic; }
  #mode-tag { display: inline-block; padding: 1px 7px; border-radius: 9px;
              font-size: 11px; font-weight: bold; background: #1c2128;
              border: 1px solid #30363d; }
  #mode-tag.shadow { color: #d29922; border-color: #d29922; }
  #mode-tag.live   { color: #3fb950; border-color: #3fb950; }

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
  <div id="live-row" class="row">
    <span><span class="lbl">Battery</span><span id="soc" class="val">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">PV</span><span id="pv" class="val">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Load</span><span id="load" class="val">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Grid</span><span id="grid" class="val">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Updated</span><span id="updated" class="val">—</span></span>
  </div>
  <div id="plan-row" class="row">
    <span><span class="lbl">Plan</span><span id="plan-text" class="val">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Rule</span><span id="plan-rule" class="val">—</span></span>
    <span id="plan-reason" class="reason"></span>
  </div>
  <div id="mode-row" class="row">
    <span><span id="mode-tag" class="shadow">SHADOW</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">Today hypothetical savings*</span><span id="sav-today" class="val pos">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">This month</span><span id="sav-month" class="val pos">—</span></span>
    <span class="sep">|</span>
    <span><span class="lbl">v</span><span id="version" class="val">—</span></span>
    <span style="color:#555;font-size:10px">* assuming perfect execution; approximate</span>
  </div>
</div>

<!-- Panel 2: Timeline chart -->
<div id="chart-panel">
  <div id="loading">Loading chart…</div>
  <canvas id="timeline"></canvas>
</div>

<script>
// ---- helpers ----
const $ = id => document.getElementById(id);
function cls(el, ...classes) { el.className = classes.join(' '); }

// ---- status strip polling ----
async function refreshStatus() {
  try {
    const s = await fetch('api/status').then(r => r.json());

    const socKwh = (s.soc_pct / 100 * s.bat_cap_kwh).toFixed(2);
    $('soc').textContent = `${s.soc_pct.toFixed(1)}% (${socKwh}/${s.bat_cap_kwh} kWh)`;

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

    const savings_t = s.savings_today.toFixed(2);
    const savings_m = s.savings_month.toFixed(2);
    $('sav-today').textContent = `${savings_t} PLN`;
    $('sav-month').textContent = `${savings_m} PLN`;
    cls($('sav-today'), 'val', s.savings_today >= 0 ? 'pos' : 'neg');
    cls($('sav-month'), 'val', s.savings_month >= 0 ? 'pos' : 'neg');

    const modeTag = $('mode-tag');
    if (s.mode === 'live') {
      modeTag.textContent = 'LIVE';
      cls(modeTag, 'live');
    } else {
      modeTag.textContent = 'SHADOW';
      cls(modeTag, 'shadow');
    }

    $('version').textContent = s.version;
  } catch(e) { console.warn('status error', e); }
}

// ---- chart ----
let chart = null;

// "now" vertical line plugin
const nowPlugin = {
  id: 'nowLine',
  afterDraw(chart) {
    const nowSlot = chart.config._nowSlot;
    if (nowSlot == null) return;
    const xs = chart.scales.x;
    if (!xs) return;
    const x = xs.getPixelForValue(nowSlot);
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
      if (peaks[i] && !inPeak) { inPeak = true; peakStart = i; }
      else if (!peaks[i] && inPeak) {
        inPeak = false;
        const x1 = xs.getPixelForValue(peakStart);
        const x2 = xs.getPixelForValue(i);
        ctx.fillRect(x1, top, x2 - x1, bottom - top);
      }
    }
    if (inPeak) {
      const x1 = xs.getPixelForValue(peakStart);
      const x2 = xs.getPixelForValue(peaks.length - 1);
      ctx.fillRect(x1, top, x2 - x1, bottom - top);
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
      const x1 = xs.getPixelForValue(w.start_slot);
      const x2 = xs.getPixelForValue(w.end_slot);
      ctx.fillRect(x1, top, x2 - x1, bottom - top);
    }
    ctx.restore();
  }
};

async function buildChart() {
  try {
    const d = await fetch('api/timeline').then(r => r.json());
    $('loading').style.display = 'none';

    const canvas = $('timeline');
    const ctx = canvas.getContext('2d');

    // Tick every 2h (every 4th 30-min slot)
    const tickLabels = d.labels.map((l, i) => (i % 4 === 0 ? l : ''));

    const YELLOW      = 'rgba(255, 200, 60, 0.9)';
    const YELLOW_DARK = 'rgba(200, 150, 30, 0.9)';
    const GREY        = 'rgba(140, 140, 160, 0.7)';
    const GREY_DARK   = 'rgba(100, 100, 120, 0.9)';
    const BLUE_DASH   = 'rgba(88, 166, 255, 0.7)';
    const BLUE_SOLID  = 'rgba(88, 166, 255, 1.0)';

    const datasets = [
      // Left axis — kW
      {
        label: 'PV forecast (kW)', yAxisID: 'y', data: d.pv_forecast,
        borderColor: YELLOW, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, tension: 0.3,
      },
      {
        label: 'PV actual (kW)', yAxisID: 'y', data: d.actual_pv,
        borderColor: YELLOW_DARK, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3,
        spanGaps: false,
      },
      {
        label: 'Load forecast (kW)', yAxisID: 'y', data: d.load_forecast,
        borderColor: GREY, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, tension: 0.3,
      },
      {
        label: 'Load actual (kW)', yAxisID: 'y', data: d.actual_load,
        borderColor: GREY_DARK, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3,
        spanGaps: false,
      },
      // Right axis — SoC %
      {
        label: 'Planned SoC (%)', yAxisID: 'y2', data: d.planned_soc,
        borderColor: BLUE_DASH, backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [5, 3], pointRadius: 0, tension: 0.3,
        spanGaps: false,
      },
      {
        label: 'Actual SoC (%)', yAxisID: 'y2', data: d.actual_soc,
        borderColor: BLUE_SOLID, backgroundColor: 'transparent',
        borderWidth: 2.5, pointRadius: 0, tension: 0.3,
        spanGaps: false,
      },
    ];

    if (chart) { chart.destroy(); chart = null; }

    chart = new Chart(ctx, {
      type: 'line',
      _nowSlot:      d.current_slot,
      _peaks:        d.is_peak,
      _chargeWindows: d.charge_windows,
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
            borderColor: '#30363d',
            borderWidth: 1,
            titleColor: '#c9d1d9',
            bodyColor: '#8b949e',
            callbacks: {
              title: items => {
                const i = items[0].dataIndex;
                return `Slot ${i}: ${d.labels[i]}  ${d.is_peak[i] ? '⚡ peak tariff' : ''}`;
              },
              afterBody: () => {
                return [`Rule: ${d.plan_rule}  ${d.plan_reason}`];
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
setInterval(buildChart, 30 * 60_000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_HTML)

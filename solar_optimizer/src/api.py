"""FastAPI ingress API with tabbed shadow-mode dashboard."""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

from optimizer import g12w_peak_vector

log = logging.getLogger(__name__)
app = FastAPI(title="Solar Optimizer")

_state: dict[str, Any] = {
    "last_result": None,
    "last_run": None,
    "phase": 1,
    "version": "?",
    "cfg": None,
    "ha": None,
    "last_pv_forecast": None,
    "last_base_load": None,
    "replan_fn": None,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


# ---- JIT comparison helpers -------------------------------------------------

def _compute_naive_soc(pv_forecast: list[float], base_load: list[float],
                        soc_init_pct: float, cfg) -> list[float]:
    """SoC trajectory with no optimizer intervention."""
    cap = cfg.battery_capacity_kwh
    soc_min = cap * cfg.soc_min_percent / 100
    soc_max = cap * cfg.soc_max_percent / 100
    soc = max(soc_min, min(soc_max, soc_init_pct / 100 * cap))
    traj = [round(soc / cap * 100, 1)]
    for t in range(48):
        surplus = max(pv_forecast[t] - base_load[t], 0.0)
        deficit = max(base_load[t] - pv_forecast[t], 0.0)
        soc = min(soc + surplus * 0.95, soc_max)
        soc = max(soc - deficit / 0.95, soc_min)
        traj.append(round(soc / cap * 100, 1))
    return traj


def _compute_jit_soc(pv_forecast: list[float], base_load: list[float],
                     soc_init_pct: float, is_peak: list[bool],
                     target_soc_pct: float, req_power_w: float,
                     cfg) -> tuple[list[float], list[float]]:
    """SoC trajectory simulating JIT: charge at req_power during off-peak until target."""
    cap = cfg.battery_capacity_kwh
    soc_min = cap * cfg.soc_min_percent / 100
    soc_max = cap * cfg.soc_max_percent / 100
    soc = max(soc_min, min(soc_max, soc_init_pct / 100 * cap))
    target_kwh = min(target_soc_pct / 100 * cap, soc_max)
    traj = [round(soc / cap * 100, 1)]
    charges: list[float] = []
    for t in range(48):
        surplus = max(pv_forecast[t] - base_load[t], 0.0)
        deficit = max(base_load[t] - pv_forecast[t], 0.0)
        charge_kwh = 0.0
        if not is_peak[t] and soc < target_kwh:
            avail = max(0.0, (target_kwh - soc) / 0.95)
            charge_kwh = min(req_power_w / 1000 * 0.5, avail)
        charges.append(round(charge_kwh / 0.5 * 1000, 0))  # back to W
        soc = soc + surplus * 0.95 + charge_kwh * 0.95 - deficit / 0.95
        soc = max(soc_min, min(soc_max, soc))
        traj.append(round(soc / cap * 100, 1))
    return traj, charges


def _compute_jit_status(ha, cfg) -> dict:
    """Replicate the JIT battery automation Jinja2 template logic in Python."""
    capacity = cfg.battery_capacity_kwh
    backup_reserve = 16  # % — hardcoded in the existing JIT automation

    try:
        soc = ha.soc_percent
        house_now_kw = ha.house_load_w / 1000
        house_avg_kw = ha.get_state_value("sensor.srednie_zuzycie_domu_1h", 1500.0) / 1000
        pv_now_kw = ha.pv_power_w / 1000
        net_load_kw = max(house_avg_kw - pv_now_kw, 0.1)
        threshold = ha.get_state_value("input_number.prog_prognozy_slonca", 8.0)

        try:
            is_wd = ha.get_state("binary_sensor.workday")["state"] == "on"
        except Exception:
            is_wd = True
        try:
            is_wd_tom = ha.get_state("binary_sensor.workday_tomorrow")["state"] == "on"
        except Exception:
            is_wd_tom = True

        now_local = ha.local_now
        h = now_local.hour

        f_rem = ha.get_state_value("sensor.solcast_pv_forecast_forecast_remaining_today", 0.0)
        f_tom = ha.get_state_value("sensor.solcast_pv_forecast_forecast_tomorrow", 0.0)
        forecast = f_tom if h >= 15 else f_rem

        # PV takeover = sunrise + 90 min
        try:
            sun_attrs = ha.get_state("sun.sun").get("attributes", {})
            nr_str = sun_attrs.get("next_rising", "")
            nr = datetime.fromisoformat(nr_str.replace("Z", "+00:00"))
            sunrise = nr.astimezone(ha.tz)
        except Exception:
            sunrise = now_local.replace(hour=6, minute=0, second=0, microsecond=0)
        takeover = sunrise + timedelta(minutes=90)

        if h < 6:
            t_end = now_local.replace(hour=6, minute=0, second=0, microsecond=0)
            active_wd = is_wd
        elif h < 15:
            t_end = now_local.replace(hour=15, minute=0, second=0, microsecond=0)
            active_wd = is_wd
        else:
            t_end = (now_local + timedelta(days=1)).replace(
                hour=6, minute=0, second=0, microsecond=0)
            active_wd = is_wd_tom

        gap_h = max((takeover - t_end).total_seconds() / 3600, 0) if t_end.hour == 6 else 0
        gap_soc = (gap_h * net_load_kw / capacity) * 100

        if 6 <= h < 15 and is_wd:
            t_goal: float = 100 if forecast < 5.0 else backup_reserve
        elif (h < 6 and is_wd) or (h >= 15 and is_wd_tom):
            if forecast < threshold:
                t_goal = 100
            else:
                t_goal = min(max(backup_reserve + 15, gap_soc + 20), 100)
            t_goal = round(t_goal)
        else:
            t_goal = backup_reserve

        energy_needed = max((t_goal - soc) / 100 * capacity, 0.0)
        time_left_h = max((t_end - now_local).total_seconds() / 3600, 0.01)
        req_power_w = int(energy_needed / time_left_h * 1000 * 1.15)

        try:
            status_text = ha.get_state("input_text.ostatni_status_ladowania")["state"]
        except Exception:
            status_text = "—"

        if not active_wd and h < 15:
            analysis = "Day off — cheap tariff all day"
        elif h >= 15 and not is_wd_tom:
            analysis = "Tomorrow is a day off — no overnight precharge needed"
        elif soc >= t_goal:
            analysis = "Battery reached target SoC ✔"
        elif req_power_w < 500:
            analysis = f"Required power ({req_power_w} W) too low — waiting to reach ≥500 W"
        else:
            analysis = "Active charging in progress"

        return {
            "status_text": status_text,
            "calendar_today": "workday" if is_wd else "weekend",
            "calendar_tomorrow": "workday" if is_wd_tom else "weekend",
            "house_now_kw": round(house_now_kw, 3),
            "house_avg_kw": round(house_avg_kw, 3),
            "pv_now_kw": round(pv_now_kw, 3),
            "net_load_kw": round(net_load_kw, 3),
            "forecast_kwh": round(forecast, 1),
            "forecast_threshold": threshold,
            "pv_takeover": takeover.strftime("%H:%M"),
            "target_soc_pct": int(t_goal),
            "target_time": t_end.strftime("%H:%M"),
            "time_left_h": round(time_left_h, 2),
            "req_power_w": req_power_w,
            "soc_now": round(soc, 1),
            "analysis": analysis,
        }
    except Exception as exc:
        log.error("JIT status computation failed: %s", exc)
        return {
            "status_text": f"Error: {exc}",
            "calendar_today": "?", "calendar_tomorrow": "?",
            "house_now_kw": 0.0, "house_avg_kw": 0.0,
            "pv_now_kw": 0.0, "net_load_kw": 0.0,
            "forecast_kwh": 0.0, "forecast_threshold": 8.0,
            "pv_takeover": "—", "target_soc_pct": 16,
            "target_time": "—", "time_left_h": 0.0,
            "req_power_w": 0, "soc_now": 0.0,
            "analysis": "Read error — check add-on logs",
        }


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar Optimizer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0f172a;--card:#1e293b;--b:#334155;--t:#e2e8f0;--m:#94a3b8;
  --g:#4ade80;--r:#f87171;--o:#fb923c;--bl:#60a5fa;--p:#a78bfa;--y:#fbbf24}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:monospace;background:var(--bg);color:var(--t);padding:16px;max-width:1100px;margin:0 auto}
h1{color:var(--g);margin-bottom:4px}h1 span{font-size:.55em;color:var(--m)}
.badges{margin:8px 0 12px}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;background:var(--card);border:1px solid var(--b);font-size:.82em;margin-right:6px}
.tabs{display:flex;border-bottom:1px solid var(--b);margin-bottom:16px;flex-wrap:wrap}
.tab{background:none;border:none;color:var(--m);padding:8px 16px;cursor:pointer;font:inherit;font-size:.9em;border-bottom:2px solid transparent}
.tab.active,.tab:hover{color:var(--t)}.tab.active{border-bottom-color:var(--bl)}
.panel{display:none}.panel.active{display:block}
table{border-collapse:collapse;width:100%;font-size:.78em;margin-top:8px}
th{text-align:left;padding:6px 8px;color:var(--m);border-bottom:1px solid var(--b);font-weight:normal;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid #1a2640;white-space:nowrap}
tr.pk{background:rgba(248,113,113,.04)}tr.pk td:first-child{color:var(--r)}
tr.now{outline:1px solid var(--o);outline-offset:-1px}
.cw{margin:12px 0;background:var(--card);border-radius:8px;padding:12px}
.ct{color:var(--m);font-size:.76em;margin-bottom:8px}
.links{margin-top:20px;font-size:.85em}
a{color:var(--bl);text-decoration:none}a:hover{text-decoration:underline}
.sep{color:var(--b);margin:0 8px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px;font-size:.76em}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.msg{color:var(--m);font-size:.84em;padding:12px 0}
details{margin-top:14px}
details summary{cursor:pointer;color:var(--bl);font-size:.82em;user-select:none;padding:4px 0}
details summary:hover{color:var(--t)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:680px){.grid2{grid-template-columns:1fr}}
.btn-sm{background:var(--card);border:1px solid var(--b);color:var(--bl);padding:4px 12px;border-radius:6px;cursor:pointer;font:inherit;font-size:.82em}
.btn-sm:hover{color:var(--t);border-color:var(--bl)}
</style>
</head>
<body>
<h1>Solar Optimizer <span id="ver"></span></h1>
<div class="badges">
  <span class="badge" id="mode-b">&#9679; Shadow Mode</span>
  <span class="badge" id="phase-b"></span>
</div>
<div class="tabs">
  <button class="tab active" onclick="showTab('status',this)">Status</button>
  <button class="tab" onclick="showTab('plan',this)">Today&#39;s Plan</button>
  <button class="tab" onclick="showTab('history',this)">History</button>
  <button class="tab" onclick="showTab('compare',this)">Compare</button>
</div>

<div id="panel-status" class="panel active">
  <div id="st-wrap"><p class="msg">&#8987; Connecting&hellip;</p></div>
  <details id="assumptions-details">
    <summary>&#9432; System assumptions &amp; objective</summary>
    <div id="assumptions-wrap"></div>
  </details>
</div>

<div id="panel-plan" class="panel">
  <div id="now-strip" style="display:none;background:#0f1f36;border:1px solid #1e3a5f;border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:.8em">
    <div style="color:#94a3b8;margin-bottom:6px">&#9654; Current slot: <span id="now-slot-label" style="color:#e2e8f0;font-weight:600"></span></div>
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:4px;text-align:center">
      <div><div style="color:#94a3b8;font-size:.75em">PV</div><div id="ns-pv" style="color:#4ade80"></div></div>
      <div><div style="color:#94a3b8;font-size:.75em">Load</div><div id="ns-load" style="color:#fb923c"></div></div>
      <div><div style="color:#94a3b8;font-size:.75em">Grid import</div><div id="ns-grid" style="color:#f87171"></div></div>
      <div><div style="color:#94a3b8;font-size:.75em">Battery SoC</div><div id="ns-soc" style="color:#60a5fa"></div></div>
      <div><div style="color:#94a3b8;font-size:.75em">DHW</div><div id="ns-dhw" style="color:#fb923c"></div></div>
    </div>
  </div>
  <div class="cw">
    <div class="ct">Energy flows per 30-min slot</div>
    <div class="legend">
      <span style="color:#64748b;font-size:.75em;margin-right:6px">&#8213; Plan &nbsp; &#8212; Actual</span>
      <span><span class="dot" style="background:#4ade80"></span>PV</span>
      <span><span class="dot" style="background:#fb923c"></span>Load</span>
      <span><span class="dot" style="background:#f87171;opacity:.7"></span>Grid import</span>
    </div>
    <canvas id="ce" height="130"></canvas>
  </div>
  <div class="cw">
    <div class="ct">Battery SoC &amp; DHW temperature</div>
    <div class="legend">
      <span style="color:#64748b;font-size:.75em;margin-right:6px">&#8213; Plan &nbsp; &#8212; Actual</span>
      <span><span class="dot" style="background:#60a5fa"></span>SoC</span>
      <span><span class="dot" style="background:#fb923c"></span>DHW</span>
    </div>
    <canvas id="ct" height="95"></canvas>
  </div>
  <div id="plan-msg"></div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Time</th><th>Tariff</th><th>PV kWh</th><th>Load kWh</th>
        <th>Grid kWh</th><th>SoC %</th><th>DHW &deg;C</th><th>Decision</th>
      </tr></thead>
      <tbody id="pt"></tbody>
    </table>
  </div>
</div>

<div id="panel-history" class="panel">
  <p style="color:var(--m);font-size:.82em;margin-bottom:8px">One row per day (last replan of the day). Accumulates over time.</p>
  <table>
    <thead><tr>
      <th>Date</th><th>Phase</th><th>PV kWh</th><th>Load kWh</th>
      <th>Grid import kWh</th><th>Self-cons %</th>
    </tr></thead>
    <tbody id="ht"></tbody>
  </table>
</div>

<div id="panel-compare" class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="color:var(--m);font-size:.82em">Live data &mdash; reads HA sensors on every open</span>
    <button class="btn-sm" onclick="loadCompare()">&#8635; Refresh</button>
  </div>
  <div id="compare-msg"><p class="msg">&#9432; Loading&hellip;</p></div>
  <div class="grid2">
    <div class="cw"><div class="ct">JIT Battery Control (existing automation)</div><div id="jit-card"></div></div>
    <div class="cw"><div class="ct">Solar Optimizer (shadow plan)</div><div id="opt-card"></div></div>
  </div>
  <div class="cw" id="cc-wrap" style="display:none">
    <div class="ct">SoC trajectory comparison</div>
    <div class="legend">
      <span><span class="dot" style="background:#60a5fa"></span>Optimizer</span>
      <span><span class="dot" style="background:#fbbf24"></span>JIT simulation</span>
      <span><span class="dot" style="background:#475569"></span>No action (naive)</span>
    </div>
    <canvas id="cc" height="100"></canvas>
  </div>
  <div style="overflow-x:auto;margin-top:12px">
    <table>
      <thead><tr>
        <th>Time</th><th>Tariff</th><th>PV kWh</th><th>Load kWh</th>
        <th style="color:#fbbf24">JIT chg W</th>
        <th style="color:#60a5fa">Opt prechg W</th>
        <th style="color:#a78bfa">Opt DHW kWh</th>
        <th style="color:#f87171">Opt import</th>
      </tr></thead>
      <tbody id="ctab"></tbody>
    </table>
  </div>
</div>

<div class="links">
  <a href="status">JSON status</a><span class="sep">|</span>
  <a href="schedule">JSON schedule</a><span class="sep">|</span>
  <a href="compare">JSON compare</a><span class="sep">|</span>
  <a href="#" onclick="triggerReplan();return false">Force replan</a>
</div>

<script>
let eChart=null,tChart=null,cChart=null,planLoaded=false,histLoaded=false,_retryTimer=null;
const SL=Array.from({length:48},(_,i)=>`${String(i>>1).padStart(2,'0')}:${i&1?'30':'00'}`);
const CO={responsive:true,interaction:{intersect:false,mode:'index'},
  plugins:{legend:{display:false},
    tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${c.parsed.y!=null?c.parsed.y.toFixed(2):'-'}`}}},
  scales:{x:{ticks:{color:'#64748b',font:{size:10},maxTicksLimit:13},grid:{color:'#1a2640'}}}};

function showTab(n,b){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+n).classList.add('active');b.classList.add('active');
  if(n==='plan'&&!planLoaded)loadPlan();
  if(n==='history'&&!histLoaded)loadHistory();
  if(n==='compare')loadCompare();
}

async function loadStatus(){
  try{
    const d=await fetch('status').then(r=>{
      if(!r.ok)throw new Error('HTTP '+r.status);
      return r.json();
    });
    if(_retryTimer){clearInterval(_retryTimer);_retryTimer=null;}
    document.getElementById('ver').textContent='v'+(d.version||'?');
    document.getElementById('phase-b').textContent=d.phase===2?'Phase 2 — LightGBM ML':'Phase 1 — Rolling Mean';
    if(d.cfg){
      const mb=document.getElementById('mode-b');
      mb.textContent=d.cfg.shadow_mode?'● Shadow Mode':'● Live Mode';
      mb.style.color=d.cfg.shadow_mode?'#fbbf24':'#4ade80';
    }
    const sc=d.solver_status==='Optimal'?'#4ade80':'#f87171';
    const rows=[
      ['Last replan',d.last_run?new Date(d.last_run).toLocaleString():'—'],
      ['Solver',`<span style="color:${sc}">${d.solver_status||'—'}</span>`],
      ['Objective',d.objective!=null?d.objective.toFixed(4):'—'],
      ['PV forecast today',d.pv_forecast_kwh!=null?d.pv_forecast_kwh.toFixed(2)+' kWh':'—'],
      ['Load forecast today',d.load_forecast_kwh!=null?d.load_forecast_kwh.toFixed(2)+' kWh':'—'],
    ];
    document.getElementById('st-wrap').innerHTML='<table><tbody>'+rows.map(([k,v])=>`<tr><td style="color:#94a3b8;width:55%">${k}</td><td>${v}</td></tr>`).join('')+'</tbody></table>';
    if(d.cfg)renderAssumptions(d);
  }catch(e){
    document.getElementById('st-wrap').innerHTML='<p class="msg">&#8987; Starting up… retrying in 5 s</p>';
    if(!_retryTimer){_retryTimer=setInterval(loadStatus,5000);}
  }
}

function renderAssumptions(d){
  const c=d.cfg;
  const modeStr=c.shadow_mode
    ?'<span style="color:#fbbf24">Shadow — optimizer runs, no service calls sent to HA</span>'
    :'<span style="color:#4ade80">Live — service calls active</span>';
  const phaseStr=d.phase===2
    ?'Phase 2 — LightGBM ML (trained on historical load data)'
    :'Phase 1 — rolling mean of last 7 days (ML not yet trained)';
  const rows=[
    ['Mode',modeStr],
    ['Load forecast',phaseStr],
    ['PV source','Solcast detailedForecast (30-min slots × 0.5 = kWh)'],
    ['Planning horizon','24 h · 48 × 30-min slots starting at midnight'],
    ['Battery',`${c.battery_capacity_kwh} kWh · SoC ${c.soc_min_percent}–${c.soc_max_percent}%`],
    ['G12W peak hours','Mon–Fri 06:00–13:00 &amp; 15:00–22:00 — 1.23 PLN/kWh'],
    ['G12W off-peak hours','All other hours (incl. weekends) — 0.63 PLN/kWh'],
    ['Objective','↓ import ×1.0 + ↓ peak cost ×0.3 + ↓ DHW thrash ×0.05 + ↓ bat wear ×0.02 − ↑ SoC@midnight ×0.15'],
    ['Why SoC@midnight?','Rewards topping up battery before overnight discharge (base load drains 3–5 kWh nightly)'],
    ['DHW tank',`${c.dhw_tank_liters} L · comfort ${c.dhw_comfort_min}–${c.dhw_max_temp}°C · COP ${c.dhw_cop}`],
    ['Replan interval',`every ${c.replan_interval_minutes} min`],
  ];
  document.getElementById('assumptions-wrap').innerHTML=
    '<table><tbody>'+
    rows.map(([k,v])=>`<tr><td style="color:#94a3b8;width:42%;padding:4px 8px">${k}</td><td style="padding:4px 8px">${v}</td></tr>`).join('')+
    '</tbody></table>';
}

async function loadPlan(){
  try{
    const [d, act] = await Promise.all([
      fetch('schedule').then(r=>r.json()),
      fetch('actual-today').then(r=>r.json()).catch(()=>null)
    ]);
    if(!d.slots||!d.slots.length){
      document.getElementById('plan-msg').innerHTML='<p class="msg">No plan yet — waiting for first replan</p>';
      return;
    }
    planLoaded=true;
    document.getElementById('plan-msg').innerHTML='';
    const s=d.slots;
    const yAx=(id,pos,clr,mn,mx,lbl)=>({
      type:'linear',position:pos,min:mn,max:mx,
      ticks:{color:clr,font:{size:10}},grid:{color:pos==='left'?'#1a2640':undefined,drawOnChartArea:pos==='left'},
      title:{display:true,text:lbl,color:clr,font:{size:10}}
    });
    const w2k=v=>v!=null?Math.round(v/2)/1000:null;

    if(eChart)eChart.destroy();
    const eDS=[
      {type:'bar',label:'PV (plan)',data:s.map(x=>x.pv_kwh),backgroundColor:'rgba(74,222,128,.35)',borderColor:'#4ade80',borderWidth:.5,order:5},
      {type:'bar',label:'Load (plan)',data:s.map(x=>x.total_load_kwh),backgroundColor:'rgba(251,146,60,.3)',borderColor:'#fb923c',borderWidth:.5,order:6},
      {type:'line',label:'Grid import (plan)',data:s.map(x=>x.grid_import_kwh),borderColor:'rgba(248,113,113,.5)',borderDash:[4,3],tension:.3,pointRadius:0,borderWidth:1,order:3},
      {type:'line',label:'Grid export',data:s.map(x=>x.grid_export_kwh),borderColor:'rgba(74,222,128,.4)',borderDash:[3,3],tension:.3,pointRadius:0,borderWidth:1,order:4},
    ];
    if(act){
      eDS.push({type:'line',label:'PV (actual)',data:act.pv_w.map(w2k),borderColor:'#4ade80',tension:.3,pointRadius:0,borderWidth:2,order:2});
      eDS.push({type:'line',label:'Load (actual)',data:act.load_w.map(w2k),borderColor:'#fb923c',tension:.3,pointRadius:0,borderWidth:2,order:2});
      eDS.push({type:'line',label:'Grid import (actual)',data:act.grid_import_w.map(w2k),borderColor:'#f87171',backgroundColor:'rgba(248,113,113,.15)',fill:true,tension:.3,pointRadius:0,borderWidth:2,order:1});
    }
    eChart=new Chart(document.getElementById('ce').getContext('2d'),{
      data:{labels:SL,datasets:eDS},
      options:{...CO,scales:{...CO.scales,y:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'#1a2640'},title:{display:true,text:'kWh',color:'#64748b',font:{size:10}}}}}
    });

    if(tChart)tChart.destroy();
    const tDS=[
      {label:'SoC % (plan)',data:s.map(x=>x.soc_pct),borderColor:'rgba(96,165,250,.45)',borderDash:[4,3],tension:.4,pointRadius:0,borderWidth:1.5,yAxisID:'soc'},
      {label:'DHW °C (plan)',data:s.map(x=>x.dhw_temp_c),borderColor:'rgba(251,146,60,.45)',borderDash:[4,3],tension:.4,pointRadius:0,borderWidth:1.5,yAxisID:'dhw'},
    ];
    if(act){
      tDS.push({label:'SoC % (actual)',data:act.soc_pct,borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true,tension:.4,pointRadius:0,borderWidth:2,yAxisID:'soc'});
      tDS.push({label:'DHW °C (actual)',data:act.dhw_temp_c,borderColor:'#fb923c',tension:.4,pointRadius:0,borderWidth:2,yAxisID:'dhw'});
    }
    tChart=new Chart(document.getElementById('ct').getContext('2d'),{
      type:'line',
      data:{labels:SL,datasets:tDS},
      options:{...CO,plugins:{...CO.plugins,legend:{display:true,labels:{color:'#e2e8f0',font:{family:'monospace',size:11},boxWidth:10}}},
        scales:{...CO.scales,soc:yAx('soc','left','#60a5fa',0,105,'SoC %'),dhw:yAx('dhw','right','#fb923c',35,65,'DHW °C')}}
    });

    const now=new Date();
    const cur=now.getHours()*2+(now.getMinutes()>=30?1:0);

    if(act){
      const sl=act.current_slot,ps=s[sl]||{};
      const fW=v=>v!=null?Math.round(v)+'W':'—';
      const fP=v=>v!=null?v.toFixed(1)+'%':'—';
      const fT=v=>v!=null?v.toFixed(1)+'°':'—';
      const delta=(a,p,hi)=>{
        if(a==null||p==null)return '';
        const d=a-p,clr=(hi?d>0:d<0)?'#4ade80':'#f87171';
        return ` <span style="color:${clr}">${d>=0?'+':''}${Math.round(d)}${hi?'W':'W'}</span>`;
      };
      const deltaP=(a,p)=>{
        if(a==null||p==null)return '';
        const d=a-p,clr=Math.abs(d)<3?'#94a3b8':d>0?'#4ade80':'#f87171';
        return ` <span style="color:${clr}">${d>=0?'+':''}${d.toFixed(1)}pp</span>`;
      };
      const deltaT=(a,p)=>{
        if(a==null||p==null)return '';
        const d=a-p,clr=Math.abs(d)<1?'#94a3b8':d>0?'#4ade80':'#f87171';
        return ` <span style="color:${clr}">${d>=0?'+':''}${d.toFixed(1)}°</span>`;
      };
      const planPvW=ps.pv_kwh!=null?ps.pv_kwh*2000:null;
      const planLdW=ps.total_load_kwh!=null?ps.total_load_kwh*2000:null;
      const planGiW=ps.grid_import_kwh!=null?ps.grid_import_kwh*2000:null;
      const h=Math.floor(sl/2),m=sl%2===0?'00':'30';
      document.getElementById('now-slot-label').textContent=`${String(h).padStart(2,'0')}:${m}`;
      document.getElementById('ns-pv').innerHTML=`P:${fW(planPvW)}<br>A:${fW(act.pv_w[sl])}${delta(act.pv_w[sl],planPvW,true)}`;
      document.getElementById('ns-load').innerHTML=`P:${fW(planLdW)}<br>A:${fW(act.load_w[sl])}${delta(act.load_w[sl],planLdW,false)}`;
      document.getElementById('ns-grid').innerHTML=`P:${fW(planGiW)}<br>A:${fW(act.grid_import_w[sl])}${delta(act.grid_import_w[sl],planGiW,false)}`;
      document.getElementById('ns-soc').innerHTML=`P:${fP(ps.soc_pct)}<br>A:${fP(act.soc_pct[sl])}${deltaP(act.soc_pct[sl],ps.soc_pct)}`;
      document.getElementById('ns-dhw').innerHTML=`P:${fT(ps.dhw_temp_c)}<br>A:${fT(act.dhw_temp_c[sl])}${deltaT(act.dhw_temp_c[sl],ps.dhw_temp_c)}`;
      document.getElementById('now-strip').style.display='block';
    }

    document.getElementById('pt').innerHTML=s.map((x,i)=>{
      const gi=x.grid_import_kwh>.002?`<span style="color:#f87171">+${x.grid_import_kwh.toFixed(3)}</span>`:
               x.grid_export_kwh>.002?`<span style="color:#4ade80">−${x.grid_export_kwh.toFixed(3)}</span>`:'—';
      const actLbl=x.dhw_heat_kwh>.01?'<span style="color:#a78bfa">DHW heat</span>':
                x.precharge_w>10?'<span style="color:#fbbf24">Bat chg</span>':'—';
      return `<tr class="${x.is_peak?'pk':''}${i===cur?' now':''}">
        <td>${x.time}</td>
        <td style="color:${x.is_peak?'#f87171':'#4ade80'}">${x.is_peak?'PEAK':'off'}</td>
        <td style="color:#4ade80">${x.pv_kwh.toFixed(3)}</td>
        <td>${x.total_load_kwh.toFixed(3)}</td>
        <td>${gi}</td>
        <td style="color:#60a5fa">${x.soc_pct.toFixed(0)}</td>
        <td style="color:#fb923c">${x.dhw_temp_c.toFixed(1)}</td>
        <td>${actLbl}</td></tr>`;
    }).join('');
    const rows=document.getElementById('pt').querySelectorAll('tr');
    if(rows[cur])rows[cur].scrollIntoView({block:'center'});
  }catch(e){
    document.getElementById('plan-msg').innerHTML='<p class="msg">&#8987; Waiting for first replan…</p>';
    planLoaded=false;
  }
}

async function loadHistory(){
  histLoaded=true;
  try{
    const data=await fetch('history').then(r=>r.json());
    if(!data.length){
      document.getElementById('ht').innerHTML='<tr><td colspan=6 style="color:#94a3b8">No history yet — accumulates after first day</td></tr>';return;
    }
    document.getElementById('ht').innerHTML=[...data].reverse().map(r=>{
      const pv=r.pv_total_kwh||0,ex=r.grid_export_total_kwh||0;
      const sc=pv>0?((pv-ex)/pv*100).toFixed(0)+'%':'—';
      return `<tr><td>${r.date}</td><td>P${r.phase||1}</td>
        <td style="color:#4ade80">${pv.toFixed(2)}</td>
        <td>${(r.load_total_kwh||0).toFixed(2)}</td>
        <td style="color:#f87171">${(r.grid_import_total_kwh||0).toFixed(2)}</td>
        <td>${sc}</td></tr>`;
    }).join('');
  }catch(e){
    document.getElementById('ht').innerHTML='<tr><td colspan=6 style="color:#94a3b8">&#8987; Server starting up — try again in a moment</td></tr>';
    histLoaded=false;
  }
}

async function loadCompare(){
  document.getElementById('compare-msg').innerHTML='<p class="msg">&#8987; Reading live HA sensors…</p>';
  document.getElementById('jit-card').innerHTML='';
  document.getElementById('opt-card').innerHTML='';
  document.getElementById('cc-wrap').style.display='none';
  document.getElementById('ctab').innerHTML='';
  try{
    const d=await fetch('compare').then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();});
    if(d.error){document.getElementById('compare-msg').innerHTML=`<p class="msg">⚠ ${d.error}</p>`;return;}
    const j=d.jit,o=d.optimizer,cur=d.current_slot;
    document.getElementById('compare-msg').innerHTML='';

    function mkT(rows){
      return '<table><tbody>'+rows.map(([k,v])=>`<tr><td style="color:#94a3b8;width:52%;padding:4px 8px;white-space:normal">${k}</td><td style="padding:4px 8px">${v}</td></tr>`).join('')+'</tbody></table>';
    }
    const calT=j.calendar_today==='workday'?'&#127970; Work':'&#127958; Off';
    const calTom=j.calendar_tomorrow==='workday'?'&#127970; Work':'&#127958; Off';
    document.getElementById('jit-card').innerHTML=mkT([
      ['Status',j.status_text||'—'],
      ['Calendar',`Today: ${calT} &nbsp;|&nbsp; Tomorrow: ${calTom}`],
      ['SoC now',`<strong>${j.soc_now}%</strong>`],
      ['House (now / avg 1h)',`${j.house_now_kw.toFixed(2)} / ${j.house_avg_kw.toFixed(2)} kW`],
      ['PV now',`<span style="color:#4ade80">${j.pv_now_kw.toFixed(2)} kW</span>`],
      ['Net load',`${j.net_load_kw.toFixed(2)} kW`],
      ['Solar forecast',`${j.forecast_kwh} kWh &nbsp;<span style="color:#475569">(threshold: ${j.forecast_threshold})</span>`],
      ['PV takeover',j.pv_takeover],
      ['Target SoC',`<strong>${j.target_soc_pct}%</strong> by ${j.target_time}`],
      ['Time left / req. power',`${j.time_left_h.toFixed(1)} h &nbsp;/ &nbsp;<span style="color:#fbbf24">${j.req_power_w} W</span>`],
      ['Analysis',`<em style="color:#e2e8f0">${j.analysis}</em>`],
    ]);
    const sc=o.solver_status==='Optimal'?'#4ade80':'#f87171';
    document.getElementById('opt-card').innerHTML=mkT([
      ['Solver / Phase',`<span style="color:${sc}">${o.solver_status}</span> &nbsp;/ P${o.phase}`],
      ['SoC now',`<strong>${o.soc_now}%</strong>`],
      ['EOD SoC (midnight)',`<strong style="color:#60a5fa">${o.soc_eod_pct}%</strong>`],
      ['PV forecast 24h',`<span style="color:#4ade80">${o.pv_forecast_24h_kwh} kWh</span>`],
      ['Load forecast 24h',`${o.load_forecast_24h_kwh} kWh`],
      ['Grid import planned',o.grid_import_total_kwh!=null?`<span style="color:#f87171">${o.grid_import_total_kwh} kWh</span>`:'—'],
      ['Battery precharge today',`${o.precharge_total_kwh} kWh`],
      ['DHW heat today',`<span style="color:#a78bfa">${o.dhw_heat_total_kwh} kWh</span>`],
      ['This slot: precharge',`<span style="color:#60a5fa">${o.current_slot_precharge_w} W</span>`],
      ['This slot: DHW heat',`<span style="color:#a78bfa">${o.current_slot_dhw_kwh} kWh</span>`],
    ]);

    document.getElementById('cc-wrap').style.display='block';
    if(cChart)cChart.destroy();
    cChart=new Chart(document.getElementById('cc').getContext('2d'),{
      type:'line',
      data:{labels:SL,datasets:[
        {label:'Optimizer',data:d.soc_trajectory_optimizer.slice(0,48),borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.12)',fill:true,tension:.4,pointRadius:0,borderWidth:2},
        {label:'JIT simulation',data:d.soc_trajectory_jit.slice(0,48),borderColor:'#fbbf24',borderDash:[5,3],tension:.4,pointRadius:0,borderWidth:1.5},
        {label:'No action',data:d.soc_trajectory_naive.slice(0,48),borderColor:'#475569',borderDash:[2,4],tension:.4,pointRadius:0,borderWidth:1},
      ]},
      options:{...CO,plugins:{...CO.plugins,legend:{display:true,labels:{color:'#e2e8f0',font:{family:'monospace',size:11},boxWidth:10}}},
        scales:{...CO.scales,y:{min:0,max:105,ticks:{color:'#64748b',font:{size:10}},grid:{color:'#1a2640'},title:{display:true,text:'SoC %',color:'#64748b',font:{size:10}}}}}
    });

    document.getElementById('ctab').innerHTML=d.slots.map((x,i)=>{
      const jcw=x.jit_charge_w>10?`<span style="color:#fbbf24">${Math.round(x.jit_charge_w)}</span>`:'—';
      const ocw=x.optimizer_precharge_w>10?`<span style="color:#60a5fa">${Math.round(x.optimizer_precharge_w)}</span>`:'—';
      const odhw=x.optimizer_dhw_kwh>.01?`<span style="color:#a78bfa">${x.optimizer_dhw_kwh.toFixed(3)}</span>`:'—';
      const ogi=x.optimizer_grid_import_kwh>.002?`<span style="color:#f87171">${x.optimizer_grid_import_kwh.toFixed(3)}</span>`:'—';
      return `<tr class="${x.is_peak?'pk':''}${i===cur?' now':''}">
        <td>${x.time}</td>
        <td style="color:${x.is_peak?'#f87171':'#4ade80'}">${x.is_peak?'PEAK':'off'}</td>
        <td style="color:#4ade80">${x.pv_kwh.toFixed(3)}</td>
        <td>${x.base_load_kwh.toFixed(3)}</td>
        <td>${jcw}</td><td>${ocw}</td><td>${odhw}</td><td>${ogi}</td></tr>`;
    }).join('');
    const rows2=document.getElementById('ctab').querySelectorAll('tr');
    if(rows2[cur])rows2[cur].scrollIntoView({block:'center'});
  }catch(e){
    document.getElementById('compare-msg').innerHTML=`<p class="msg">&#9888; ${e.message||'Failed to load'} — check add-on logs</p>`;
  }
}

async function triggerReplan(){
  await fetch('force-replan',{method:'POST'}).catch(()=>{});
  setTimeout(()=>{loadStatus();planLoaded=false;},3000);
  setTimeout(()=>{if(document.getElementById('panel-plan').classList.contains('active'))loadPlan();},3500);
}

loadStatus();
setInterval(loadStatus,60000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/status")
async def status() -> JSONResponse:
    last_run: Optional[datetime] = _state.get("last_run")
    result = _state.get("last_result")
    cfg = _state.get("cfg")
    cfg_data = None
    if cfg is not None:
        cfg_data = {
            "shadow_mode": cfg.shadow_mode,
            "battery_capacity_kwh": cfg.battery_capacity_kwh,
            "soc_min_percent": cfg.soc_min_percent,
            "soc_max_percent": cfg.soc_max_percent,
            "dhw_tank_liters": cfg.dhw_tank_liters,
            "dhw_comfort_min": cfg.dhw_comfort_min,
            "dhw_max_temp": cfg.dhw_max_temp,
            "dhw_cop": cfg.dhw_cop,
            "replan_interval_minutes": cfg.replan_interval_minutes,
            "ml_enabled": cfg.ml_enabled,
        }
    return JSONResponse({
        "status": "ok",
        "version": _state.get("version", "?"),
        "phase": _state.get("phase", 1),
        "last_run": last_run.isoformat() if last_run else None,
        "solver_status": result.status if result else None,
        "objective": result.objective_value if result else None,
        "pv_forecast_kwh": result.pv_forecast_kwh_total if result else None,
        "load_forecast_kwh": result.load_forecast_kwh_total if result else None,
        "cfg": cfg_data,
    })


@app.get("/schedule")
async def schedule() -> JSONResponse:
    result = _state.get("last_result")
    if result is None:
        raise HTTPException(status_code=503, detail="No schedule available yet")
    cop = _state["cfg"].dhw_cop if _state.get("cfg") else 3.0
    slots = []
    for t in range(48):
        h, m = divmod(t * 30, 60)
        pv = result.pv_forecast_kwh[t] if result.pv_forecast_kwh else 0.0
        base = result.base_load_kwh[t] if result.base_load_kwh else 0.0
        dhw_heat = result.dhw_heat_energy[t]
        total_load = base + dhw_heat / cop
        slots.append({
            "slot": t,
            "time": f"{h:02d}:{m:02d}",
            "is_peak": result.is_peak[t] if result.is_peak else False,
            "pv_kwh": round(pv, 4),
            "base_load_kwh": round(base, 4),
            "dhw_heat_kwh": round(dhw_heat, 4),
            "total_load_kwh": round(total_load, 4),
            "grid_import_kwh": round(result.grid_import_kwh[t], 4),
            "grid_export_kwh": round(result.grid_export_kwh[t], 4),
            "soc_pct": round(result.soc_trajectory[t], 1),
            "dhw_temp_c": round(result.dhw_temp_trajectory[t], 1),
            "precharge_w": round(result.offpeak_precharge_w[t], 0),
        })
    return JSONResponse({"slots": slots})


@app.get("/actual-today")
def actual_today() -> JSONResponse:
    """Return today's actual sensor data resampled to 48 half-hour slots. Sync: HA history API call."""
    ha = _state.get("ha")
    if ha is None:
        raise HTTPException(status_code=503, detail="Not ready")

    entity_ids = [
        "sensor.inverter_input_power",
        "sensor.house_consumption_power",
        "sensor.battery_state_of_capacity",
        "sensor.power_meter_active_power",
        "sensor.heiko_hot_water_dhw_temperature",
    ]
    hist = ha.get_history_today_30min(entity_ids)

    # Sign-correct grid: power_meter_active_power positive=export, negative=import
    raw_grid = hist.get("sensor.power_meter_active_power", [None] * 48)
    grid_import_w = [round(max(0.0, -v), 1) if v is not None else None for v in raw_grid]

    now_slot = ha.local_now.hour * 2 + ha.local_now.minute // 30

    return JSONResponse({
        "current_slot": now_slot,
        "pv_w": hist.get("sensor.inverter_input_power", [None] * 48),
        "load_w": hist.get("sensor.house_consumption_power", [None] * 48),
        "soc_pct": hist.get("sensor.battery_state_of_capacity", [None] * 48),
        "grid_import_w": grid_import_w,
        "dhw_temp_c": hist.get("sensor.heiko_hot_water_dhw_temperature", [None] * 48),
    })


@app.get("/history")
async def history() -> JSONResponse:
    try:
        with open("/data/plan_history.jsonl") as f:
            lines = f.readlines()
        records = [json.loads(line) for line in lines if line.strip()]
        by_date: dict[str, Any] = {}
        for r in records:
            d = r.get("date", "")
            if d:
                by_date[d] = r
        return JSONResponse(sorted(by_date.values(), key=lambda x: x["date"])[-30:])
    except FileNotFoundError:
        return JSONResponse([])
    except Exception as exc:
        log.warning("History read error: %s", exc)
        return JSONResponse([])


@app.get("/compare")
def compare() -> JSONResponse:
    """Live comparison: JIT automation state vs optimizer plan. Sync: runs in FastAPI threadpool."""
    ha = _state.get("ha")
    cfg = _state.get("cfg")
    result = _state.get("last_result")
    pv_forecast: list[float] = _state.get("last_pv_forecast") or [0.0] * 48
    base_load: list[float] = _state.get("last_base_load") or [0.3] * 48

    if ha is None or cfg is None:
        return JSONResponse({"error": "Not ready — waiting for first replan"}, status_code=503)

    jit = _compute_jit_status(ha, cfg)

    now_local = ha.local_now
    is_peak = g12w_peak_vector(now_local)
    soc_now = jit["soc_now"]
    current_slot = now_local.hour * 2 + now_local.minute // 30

    naive_traj = _compute_naive_soc(pv_forecast, base_load, soc_now, cfg)
    jit_traj, jit_charges = _compute_jit_soc(
        pv_forecast, base_load, soc_now, is_peak,
        float(jit["target_soc_pct"]), float(jit["req_power_w"]), cfg,
    )
    optimizer_traj = result.soc_trajectory if result else naive_traj

    precharge_total = sum(w / 1000 * 0.5 for w in result.offpeak_precharge_w) if result else 0.0

    slots = []
    for t in range(48):
        h, m = divmod(t * 30, 60)
        slots.append({
            "slot": t,
            "time": f"{h:02d}:{m:02d}",
            "is_peak": is_peak[t],
            "pv_kwh": round(pv_forecast[t], 3),
            "base_load_kwh": round(base_load[t], 3),
            "jit_charge_w": round(jit_charges[t], 0),
            "optimizer_precharge_w": round(result.offpeak_precharge_w[t], 0) if result else 0,
            "optimizer_dhw_kwh": round(result.dhw_heat_energy[t], 3) if result else 0,
            "optimizer_grid_import_kwh": round(result.grid_import_kwh[t], 3) if result else 0,
        })

    return JSONResponse({
        "jit": jit,
        "optimizer": {
            "soc_now": round(soc_now, 1),
            "soc_eod_pct": round(optimizer_traj[-1], 1),
            "pv_forecast_24h_kwh": round(sum(pv_forecast), 2),
            "load_forecast_24h_kwh": round(sum(base_load), 2),
            "grid_import_total_kwh": round(sum(result.grid_import_kwh), 2) if result else None,
            "precharge_total_kwh": round(precharge_total, 2),
            "dhw_heat_total_kwh": round(sum(result.dhw_heat_energy), 2) if result else 0.0,
            "solver_status": result.status if result else "No plan yet",
            "phase": _state.get("phase", 1),
            "current_slot_precharge_w": round(result.offpeak_precharge_w[current_slot], 0) if result else 0,
            "current_slot_dhw_kwh": round(result.dhw_heat_energy[current_slot], 3) if result else 0.0,
        },
        "soc_trajectory_optimizer": [round(v, 1) for v in optimizer_traj],
        "soc_trajectory_naive": [round(v, 1) for v in naive_traj],
        "soc_trajectory_jit": [round(v, 1) for v in jit_traj],
        "current_slot": current_slot,
        "slots": slots,
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

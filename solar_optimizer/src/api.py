"""FastAPI ingress API with tabbed shadow-mode dashboard."""
import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

log = logging.getLogger(__name__)
app = FastAPI(title="Solar Optimizer")

_state: dict[str, Any] = {
    "last_result": None,
    "last_run": None,
    "phase": 1,
    "version": "?",
    "replan_fn": None,
}


def set_state(key: str, value: Any) -> None:
    _state[key] = value


# All fetch() calls and href links use RELATIVE paths (no leading slash).
# When served through HA ingress the page URL is:
#   https://ha:8123/api/hassio_ingress/{token}/
# Absolute paths like /status resolve to https://ha:8123/status (HA frontend).
# Relative paths like 'status' resolve to .../api/hassio_ingress/{token}/status
# which the supervisor proxy correctly forwards to the add-on.
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
body{font-family:monospace;background:var(--bg);color:var(--t);padding:16px;max-width:1024px;margin:0 auto}
h1{color:var(--g);margin-bottom:4px}h1 span{font-size:.55em;color:var(--m)}
.badges{margin:8px 0 12px}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;background:var(--card);border:1px solid var(--b);font-size:.82em;margin-right:6px}
.tabs{display:flex;border-bottom:1px solid var(--b);margin-bottom:16px}
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
</style>
</head>
<body>
<h1>Solar Optimizer <span id="ver"></span></h1>
<div class="badges">
  <span class="badge">&#9679; Shadow Mode</span>
  <span class="badge" id="phase-b"></span>
</div>
<div class="tabs">
  <button class="tab active" onclick="showTab('status',this)">Status</button>
  <button class="tab" onclick="showTab('plan',this)">Today&#39;s Plan</button>
  <button class="tab" onclick="showTab('history',this)">History</button>
</div>

<div id="panel-status" class="panel active">
  <div id="st-wrap"><p class="msg">&#8987; Connecting&hellip;</p></div>
</div>

<div id="panel-plan" class="panel">
  <div class="cw">
    <div class="ct">Energy flows per 30-min slot</div>
    <div class="legend">
      <span><span class="dot" style="background:#4ade80"></span>PV generation</span>
      <span><span class="dot" style="background:#fb923c"></span>Total load</span>
      <span><span class="dot" style="background:#f87171;opacity:.7"></span>Grid import</span>
      <span><span class="dot" style="background:#4ade80;opacity:.4"></span>Grid export</span>
    </div>
    <canvas id="ce" height="130"></canvas>
  </div>
  <div class="cw">
    <div class="ct">Battery SoC &amp; DHW temperature</div>
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

<div class="links">
  <a href="status">JSON status</a><span class="sep">|</span>
  <a href="schedule">JSON schedule</a><span class="sep">|</span>
  <a href="#" onclick="triggerReplan();return false">Force replan</a>
</div>

<script>
let eChart=null,tChart=null,planLoaded=false,histLoaded=false,_retryTimer=null;
const SL=Array.from({length:48},(_,i)=>`${String(i>>1).padStart(2,'0')}:${i&1?'30':'00'}`);
const CO={responsive:true,interaction:{intersect:false,mode:'index'},
  plugins:{legend:{display:false},
    tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${c.parsed.y!=null?c.parsed.y.toFixed(3):'-'}`}}},
  scales:{x:{ticks:{color:'#64748b',font:{size:10},maxTicksLimit:13},grid:{color:'#1a2640'}}}};

function showTab(n,b){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+n).classList.add('active');b.classList.add('active');
  if(n==='plan'&&!planLoaded)loadPlan();
  if(n==='history'&&!histLoaded)loadHistory();
}

async function loadStatus(){
  try{
    // Relative path: resolves correctly both at direct port and via HA ingress proxy
    const d=await fetch('status').then(r=>{
      if(!r.ok)throw new Error('HTTP '+r.status);
      return r.json();
    });
    if(_retryTimer){clearInterval(_retryTimer);_retryTimer=null;}
    document.getElementById('ver').textContent='v'+(d.version||'?');
    document.getElementById('phase-b').textContent=d.phase===2?'Phase 2 — LightGBM ML':'Phase 1 — Rolling Mean';
    const sc=d.solver_status==='Optimal'?'#4ade80':'#f87171';
    const rows=[
      ['Last replan',d.last_run?new Date(d.last_run).toLocaleString():'—'],
      ['Solver',`<span style="color:${sc}">${d.solver_status||'—'}</span>`],
      ['Objective',d.objective!=null?d.objective.toFixed(4):'—'],
      ['PV forecast today',d.pv_forecast_kwh!=null?d.pv_forecast_kwh.toFixed(2)+' kWh':'—'],
      ['Load forecast today',d.load_forecast_kwh!=null?d.load_forecast_kwh.toFixed(2)+' kWh':'—'],
    ];
    document.getElementById('st-wrap').innerHTML='<table><tbody>'+rows.map(([k,v])=>`<tr><td style="color:#94a3b8;width:55%">${k}</td><td>${v}</td></tr>`).join('')+'</tbody></table>';
  }catch(e){
    document.getElementById('st-wrap').innerHTML='<p class="msg">&#8987; Starting up… retrying in 5 s</p>';
    if(!_retryTimer){_retryTimer=setInterval(loadStatus,5000);}
  }
}

async function loadPlan(){
  try{
    const d=await fetch('schedule').then(r=>r.json());
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

    if(eChart)eChart.destroy();
    eChart=new Chart(document.getElementById('ce').getContext('2d'),{
      data:{labels:SL,datasets:[
        {type:'bar',label:'PV',data:s.map(x=>x.pv_kwh),backgroundColor:'rgba(74,222,128,.5)',borderColor:'#4ade80',borderWidth:.5,order:3},
        {type:'bar',label:'Load',data:s.map(x=>x.total_load_kwh),backgroundColor:'rgba(251,146,60,.4)',borderColor:'#fb923c',borderWidth:.5,order:4},
        {type:'line',label:'Grid import',data:s.map(x=>x.grid_import_kwh),borderColor:'#f87171',backgroundColor:'rgba(248,113,113,.15)',fill:true,tension:.3,pointRadius:0,borderWidth:1.5,order:1},
        {type:'line',label:'Grid export',data:s.map(x=>x.grid_export_kwh),borderColor:'#4ade80',borderDash:[3,3],tension:.3,pointRadius:0,borderWidth:1,order:2},
      ]},
      options:{...CO,scales:{...CO.scales,y:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'#1a2640'},title:{display:true,text:'kWh',color:'#64748b',font:{size:10}}}}}
    });

    if(tChart)tChart.destroy();
    tChart=new Chart(document.getElementById('ct').getContext('2d'),{
      type:'line',
      data:{labels:SL,datasets:[
        {label:'SoC %',data:s.map(x=>x.soc_pct),borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true,tension:.4,pointRadius:0,yAxisID:'soc'},
        {label:'DHW °C',data:s.map(x=>x.dhw_temp_c),borderColor:'#fb923c',borderDash:[4,3],tension:.4,pointRadius:0,borderWidth:1.5,yAxisID:'dhw'},
      ]},
      options:{...CO,plugins:{...CO.plugins,legend:{display:true,labels:{color:'#e2e8f0',font:{family:'monospace',size:11},boxWidth:10}}},
        scales:{...CO.scales,soc:yAx('soc','left','#60a5fa',0,105,'SoC %'),dhw:yAx('dhw','right','#fb923c',35,65,'DHW °C')}}
    });

    const now=new Date();
    const cur=now.getHours()*2+(now.getMinutes()>=30?1:0);
    document.getElementById('pt').innerHTML=s.map((x,i)=>{
      const gi=x.grid_import_kwh>.002?`<span style="color:#f87171">+${x.grid_import_kwh.toFixed(3)}</span>`:
               x.grid_export_kwh>.002?`<span style="color:#4ade80">−${x.grid_export_kwh.toFixed(3)}</span>`:'—';
      const act=x.dhw_heat_kwh>.01?'<span style="color:#a78bfa">DHW heat</span>':
                x.precharge_w>10?'<span style="color:#fbbf24">Bat chg</span>':'—';
      return `<tr class="${x.is_peak?'pk':''}${i===cur?' now':''}">
        <td>${x.time}</td>
        <td style="color:${x.is_peak?'#f87171':'#4ade80'}">${x.is_peak?'PEAK':'off'}</td>
        <td style="color:#4ade80">${x.pv_kwh.toFixed(3)}</td>
        <td>${x.total_load_kwh.toFixed(3)}</td>
        <td>${gi}</td>
        <td style="color:#60a5fa">${x.soc_pct.toFixed(0)}</td>
        <td style="color:#fb923c">${x.dhw_temp_c.toFixed(1)}</td>
        <td>${act}</td></tr>`;
    }).join('');
    const rows=document.getElementById('pt').querySelectorAll('tr');
    if(rows[cur])rows[cur].scrollIntoView({block:'center'});
  }catch(e){
    document.getElementById('plan-msg').innerHTML='<p class="msg">&#8987; Waiting for first replan…</p>';
    planLoaded=false;
    console.error('Plan load error:',e);
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
    document.getElementById('ht').innerHTML='<tr><td colspan=6 style="color:#94a3b8">&#8987; Server starting up — click History again in a moment</td></tr>';
    histLoaded=false;
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
    return JSONResponse({
        "status": "ok",
        "version": _state.get("version", "?"),
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

    cop = 3.0
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


@app.get("/history")
async def history() -> JSONResponse:
    try:
        with open("/data/plan_history.jsonl") as f:
            lines = f.readlines()
        records = [json.loads(l) for l in lines if l.strip()]
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

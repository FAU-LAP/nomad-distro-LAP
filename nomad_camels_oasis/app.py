"""The OASIS-side, authenticated proxy and setup UI for CAMELS."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from nomad.app.v1.routers.auth import get_current_user
from nomad.auth.scopes import Scope
from nomad_camels_oasis.measurement_dashboard import AUTHENTICATION_REQUIRED_PAGE
from nomad_camels_oasis import remote


# This is a presentation limit only. The complete, lossless measurement remains
# in CAMELS/NOMAD; it prevents a long live run from producing unbounded browser
# responses for every Dashboard refresh.
BROWSER_PLOT_MAX_POINTS = 2000


def _downsample_plot_values(values: list[Any], limit: int = BROWSER_PLOT_MAX_POINTS) -> list[Any]:
    """Evenly sample a JSON plot array while retaining its endpoints."""
    if len(values) <= limit:
        return values
    return [
        values[round(index * (len(values) - 1) / (limit - 1))]
        for index in range(limit)
    ]


def _limit_browser_plot_payload(payload: Any) -> Any:
    """Bound top-level traces and stored fit traces returned by Dash /data."""
    if not isinstance(payload, dict):
        return payload
    limited: dict[str, Any] = {}
    for name, value in payload.items():
        if isinstance(value, list):
            limited[name] = _downsample_plot_values(value)
        elif isinstance(value, dict) and isinstance(value.get("fit_result"), list):
            fit = dict(value)
            fit["fit_result"] = _downsample_plot_values(fit["fit_result"])
            limited[name] = fit
        else:
            limited[name] = value
    return limited


class RunRequest(BaseModel):
    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None


class ProtocolParameters(BaseModel):
    """Parameter values to persist as the CAMELS defaults for one protocol."""

    variables: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    """A user-owned CAMELS Action started from the Dashboard."""

    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)


class ConnectionSettings(BaseModel):
    camels_base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    name: str = Field(default="")
    connection_id: str | None = None


class RemoteDeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CredentialStore:
    """Persist the CAMELS key encrypted in the Oasis filesystem volume."""

    def __init__(self, path: str, encryption_secret: str | None):
        if not encryption_secret:
            raise RuntimeError("NOMAD_SERVICES_API_SECRET is required for CAMELS setup.")
        key = base64.urlsafe_b64encode(
            hashlib.sha256(encryption_secret.encode("utf-8")).digest()
        )
        self._fernet = Fernet(key)
        self._path = Path(path)

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            encrypted = self._path.read_bytes()
            return json.loads(self._fernet.decrypt(encrypted).decode("utf-8"))
        except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail="Saved CAMELS connection settings cannot be read.",
            ) from exc

    def save(self, data: dict[str, Any]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._fernet.encrypt(
            json.dumps(data).encode("utf-8")
        )
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_bytes(encrypted)
        os.replace(temporary_path, self._path)


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS connections setup</title><style>body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:1rem}input{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.3rem}button{padding:.5rem .8rem;margin:.5rem}#saved{list-style:disc;padding-left:1.6rem;margin:.5rem 0}#saved li{display:flex;align-items:center;gap:.5rem;min-height:2.2rem;padding:.2rem .4rem;border-bottom:1px solid #ddd}#saved .delete{float:none;margin:0 0 0 auto;padding:.25rem .55rem;line-height:1.2;white-space:nowrap}pre{white-space:pre-wrap}</style></head><body><h1>CAMELS connections setup</h1><p>Paste one CAMELS API key. CAMELS decides which protocols are visible: only protocols with <em>Expose this protocol to OASIS remote control</em> enabled are listed and controllable.</p><form id="form"><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label><label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label><button type="button" id="test">Test connection</button><button type="submit">Save connection</button><a href="./dashboard">Go to dashboard</a></form><pre id="result"></pre><h2>Saved CAMELS connections</h2><ul id="saved"><li>Loading...</li></ul><script>const result=document.getElementById('result'),form=document.getElementById('form'),saved=document.getElementById('saved');function values(){return{camels_base_url:document.getElementById('url').value,api_key:document.getElementById('key').value}}function show(data,ok=true){result.textContent=(ok?'Success: ':'Error: ')+JSON.stringify(data,null,2)}async function request(path,options={}){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw data;return data}async function refresh(){try{const data=await request('./settings/connections');saved.innerHTML='';const entries=data.connections||[];if(!entries.length){saved.innerHTML='<li>No CAMELS API key saved.</li>';return}for(const entry of entries){const li=document.createElement('li');li.textContent=entry.name;const button=document.createElement('button');button.className='delete';button.textContent='Delete';button.onclick=async()=>{if(!confirm('Delete the saved CAMELS key for '+entry.name+'?'))return;try{await request('./settings/connections/'+encodeURIComponent(entry.connection_id),{method:'DELETE'});refresh()}catch(e){show(e,false)}};li.append(button);saved.append(li)}}catch(e){show(e,false)}}document.getElementById('test').addEventListener('click',async()=>{try{show(await request('./settings/connection/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())}))}catch(e){show(e,false)}});form.addEventListener('submit',async e=>{e.preventDefault();try{const data=await request('./settings/connection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});show(data);document.getElementById('key').value='';refresh()}catch(e){show(e,false)}});refresh();</script></body></html>"""

DASHBOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS measurement dashboard</title><style>body{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto;line-height:1.45}select,input,button{padding:.55rem;margin:.35rem 0}select{min-width:300px}button{margin-left:.4rem}#parameters label{display:block;font-weight:600;margin-top:.7rem}#parameters input{display:block;box-sizing:border-box;width:100%;font-weight:400}pre{background:#f5f5f5;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}.hint{color:#555}</style></head><body><h1>CAMELS measurement dashboard</h1><p><a href="./setup">CAMELS connections setup</a></p><label><strong>Protocol</strong><br><select id="protocol"><option>Loading protocols...</option></select></label><button id="load">View protocol</button><p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p><h2>Measurement parameters</h2><div id="parameters">Choose a protocol, then select View protocol.</div><button id="run" disabled>Start measurement</button><h2>Sequence</h2><pre id="sequence">Choose a protocol to show its configured sequence.</pre><h2>Run status</h2><pre id="status">No measurement has been started.</pre><script>const protocol=document.getElementById('protocol'),params=document.getElementById('parameters'),sequence=document.getElementById('sequence'),status=document.getElementById('status'),run=document.getElementById('run');let timer=null;async function api(path,options={}){const r=await fetch('./dashboard/'+path,options),text=await r.text();let data;try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||text);return data}function stepsOf(v){if(Array.isArray(v))return v;if(!v||typeof v!=='object')return[];for(const k of ['loop_steps','sequence','steps','sub_steps','children'])if(Array.isArray(v[k]))return v[k];return[]}function sequenceText(steps,l=0){return steps.map(s=>{if(typeof s==='string')return '  '.repeat(l)+'- '+s;const label=s.name||s.full_name||s.step_type||s.type||s.description||'Step',children=stepsOf(s);return '  '.repeat(l)+'- '+label+(children.length?'\\n'+sequenceText(children,l+1):'')}).join('\\n')}function value(v){if(v&&typeof v==='object'&&'default'in v)v=v.default;return typeof v==='object'?JSON.stringify(v):v??''}async function loadProtocols(){try{const data=await api('protocols');protocol.innerHTML='';for(const n of data.Protocols||[]){const o=document.createElement('option');o.value=o.textContent=n;protocol.append(o)}if(!protocol.options.length)protocol.innerHTML='<option>No saved protocols</option>'}catch(e){protocol.innerHTML='<option>Could not load protocols</option>';status.textContent='Error: '+e.message}}async function viewProtocol(){try{const n=protocol.value,[schema,definition]=await Promise.all([api('protocols/'+encodeURIComponent(n)+'/schema'),api('protocols/'+encodeURIComponent(n)+'/definition')]),defs=schema.parameters||schema.variables||{};params.innerHTML='';for(const[k,v]of Object.entries(defs)){const label=document.createElement('label'),input=document.createElement('input');label.textContent=k;input.name=k;input.value=value(v);label.append(input);params.append(label)}if(!Object.keys(defs).length)params.textContent='This protocol does not declare user parameters.';sequence.textContent=sequenceText(stepsOf(definition))||'No sequence steps configured.';run.disabled=false}catch(e){status.textContent='Error: '+e.message;run.disabled=true}}function variables(){const result={};for(const i of params.querySelectorAll('input')){let v=i.value;try{v=JSON.parse(v)}catch{}result[i.name]=v}return result}function statusInfo(r){const raw=String(r.status||r.state||'').trim(),v=raw.toLowerCase();if(raw.includes('/')||raw.includes('\\\\')||v.includes('finished')||v.includes('complete')||v.includes('success'))return['Finished',true];if(v.includes('queue'))return['Queued',false];if(v.includes('running')||!v)return['Running',false];if(v.includes('failed')||v.includes('error')||v.includes('abort')||v.includes('stopped'))return['Failed',true];return['Running',false]}async function poll(id,connectionId){clearTimeout(timer);try{const r=await api('runs/'+encodeURIComponent(id)+'?connection_id='+encodeURIComponent(connectionId)),[text,done]=statusInfo(r);status.textContent=text;if(done)run.disabled=false;else timer=setTimeout(()=>poll(id,connectionId),1500)}catch(e){status.textContent='Status check failed: '+e.message;run.disabled=false}}async function startRun(){try{run.disabled=true;status.textContent='Starting measurement...';const r=await api('runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:protocol.value,variables:variables()})}),id=r.run_id||r.uuid||r.id;if(id){status.textContent='Running';poll(id,r.connection_id)}else{status.textContent='Started';run.disabled=false}}catch(e){status.textContent='Failed: '+e.message;run.disabled=false}}document.getElementById('load').addEventListener('click',viewProtocol);run.addEventListener('click',startRun);loadProtocols();</script></body></html>"""

# Browser plot definitions are read from the selected CAMELS protocol.  The
# browser communicates with each local Dash service directly, so that each
# configured plot may use its own port.
DASHBOARD_PAGE += """<script>
document.body.insertAdjacentHTML('beforeend','<section id="browser-plots"><h2>Browser plots</h2><label>Browser URL: <input id="browser-plot-url" value="http://localhost:" style="width:250px"></label><div><label>Plot name: <input id="manual-plot-name" placeholder="Plot name" style="width:180px"></label><label>Port: <input id="manual-plot-port" type="number" min="1" max="65535" placeholder="8050" style="width:90px"></label><button id="add-manual-plot" type="button">Add plot</button><span id="manual-plot-status" class="hint"></span></div><table style="width:100%;border-collapse:collapse"><thead><tr><th align="left">Plot name</th><th align="left">Port</th><th align="left">Display below</th></tr></thead><tbody id="browser-plot-rows"><tr><td colspan="3">Choose View protocol to load its plots.</td></tr></tbody></table><div id="browser-plot-frames"></div></section>');
const browserPlotRows=document.getElementById('browser-plot-rows'),browserPlotFrames=document.getElementById('browser-plot-frames'),browserPlotUrlInput=document.getElementById('browser-plot-url'),manualPlotName=document.getElementById('manual-plot-name'),manualPlotPort=document.getElementById('manual-plot-port'),manualPlotStatus=document.getElementById('manual-plot-status');let browserPlots=[];
function plotKey(plot){return plot.name+'@'+plot.port}
function walkForPlots(value,result){if(!value||typeof value!=='object')return;if(Array.isArray(value)){for(const item of value)walkForPlots(item,result);return}if(value.checkbox_show_in_browser!==undefined&&value.browser_port!==undefined)result.push(value);for(const key of ['loop_steps','sequence','steps','sub_steps','children','plots'])if(value[key])walkForPlots(value[key],result)}
function collectBrowserPlots(definition){const found=[];walkForPlots(definition,found);const used=new Set();return found.map((item,index)=>({name:String(item.name||item.title||('Plot '+(index+1))),port:Number(item.browser_port),enabled:item.checkbox_show_in_browser===true,shown:item.checkbox_show_in_browser===true,clearMode:'automatic'})).filter(item=>Number.isInteger(item.port)&&item.port>0).filter(item=>{const key=plotKey(item);if(used.has(key))return false;used.add(key);return true})}
function plotBaseUrl(port){let base=browserPlotUrlInput.value.trim();while(base.endsWith('/'))base=base.slice(0,-1);return (base.endsWith(':')?base:base+':')+port}
function plotUrl(plot){return plotBaseUrl(plot.port)+'/?run='+Date.now()}
function renderPlotTable(){browserPlotRows.innerHTML='';if(!browserPlots.length){browserPlotRows.innerHTML='<tr><td colspan="3">This protocol has no configured browser plots.</td></tr>';return}for(const plot of browserPlots){const row=document.createElement('tr'),name=document.createElement('td'),port=document.createElement('td'),display=document.createElement('td'),box=document.createElement('input');name.textContent=plot.name;port.textContent=String(plot.port);box.type='checkbox';box.checked=plot.shown;box.disabled=!plot.enabled;box.title=plot.enabled?'':'Enable Show plot in browser for this plot in CAMELS first.';box.addEventListener('change',()=>{plot.shown=box.checked;renderPlotFrames()});display.append(box);row.append(name,port,display);browserPlotRows.append(row)}}
function renderPlotFrames(){
  browserPlotFrames.innerHTML='';
  for(const plot of browserPlots.filter(item=>item.shown)){
    const section=document.createElement('section'),heading=document.createElement('h3'),label=document.createElement('label'),select=document.createElement('select'),manual=document.createElement('button'),message=document.createElement('span'),connection=document.createElement('small'),frame=document.createElement('iframe');
    heading.textContent=plot.name+' (port '+plot.port+')';
    label.textContent='Clear mode ';
    select.innerHTML='<option value="automatic">Automatic</option><option value="manual">Manual</option>';
    select.value=plot.clearMode;
    manual.type='button';manual.textContent='Clear plot';manual.style.display=plot.clearMode==='manual'?'inline-block':'none';
    message.className='hint';message.style.marginLeft='.5rem';
    connection.className='browser-plot-connect-status hint';connection.style.display='none';
    frame.src=plotUrl(plot);frame.title=plot.name;frame.dataset.browserPlotKey=plotKey(plot);frame.style.cssText='width:100%;height:620px;border:0;margin-top:1rem';
    select.addEventListener('change',()=>{plot.clearMode=select.value;manual.style.display=plot.clearMode==='manual'?'inline-block':'none'});
    manual.addEventListener('click',async()=>{await clearPlot(plot,message);frame.src=plotUrl(plot)});
    label.append(select);section.append(heading,label,manual,message,connection,frame);browserPlotFrames.append(section);
  }
}
async function clearPlot(plot,message){if(message)message.textContent=' Clearing...';try{const response=await fetch(plotBaseUrl(plot.port)+'/clear_data',{method:'POST'});if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);if(message)message.textContent=' Cleared'}catch(error){if(message)message.textContent=' Could not clear plot: '+error.message}}
async function loadBrowserPlots(){try{const definition=await api('protocols/'+encodeURIComponent(protocol.value)+'/definition');browserPlots=collectBrowserPlots(definition);renderPlotTable();renderPlotFrames()}catch(error){browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML='';status.textContent='Error loading browser plots: '+error.message}}
document.getElementById('load').addEventListener('click',loadBrowserPlots);
browserPlotUrlInput.addEventListener('change',()=>renderPlotFrames());
document.getElementById('add-manual-plot').addEventListener('click',()=>{const name=manualPlotName.value.trim(),port=Number(manualPlotPort.value);if(!name||!Number.isInteger(port)||port<1||port>65535){manualPlotStatus.textContent=' Enter a plot name and a valid port (1-65535).';return}const key=name+'@'+port;if(browserPlots.some(plot=>plotKey(plot)===key)){manualPlotStatus.textContent=' This plot is already in the list.';return}browserPlots.push({name,port,enabled:true,shown:true,clearMode:'automatic'});manualPlotName.value='';manualPlotPort.value='';manualPlotStatus.textContent=' Added.';renderPlotTable();renderPlotFrames()});
protocol.addEventListener('change',()=>{browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML=''});
run.addEventListener('click',()=>{const automatic=browserPlots.filter(plot=>plot.shown&&plot.clearMode==='automatic');Promise.all(automatic.map(plot=>clearPlot(plot))).finally(()=>{if(browserPlots.some(plot=>plot.shown))setTimeout(renderPlotFrames,800)})});
</script>"""

# CAMELS main-window plot state is separate from browser/Dash plots.  This panel
# mirrors the native "Close plots" button through the authenticated bridge.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="camels-plot-window-status"><h2>CAMELS plot windows</h2><p><strong id="camels-plot-window-count">Loading...</strong> native CAMELS plot window(s) open.</p><button id="close-camels-plots" type="button">Close plots</button><span id="camels-plot-window-message" class="hint" style="margin-left:.5rem"></span></section>');
const camelsPlotWindowCount=document.getElementById('camels-plot-window-count'),closeCamelsPlotsButton=document.getElementById('close-camels-plots'),camelsPlotWindowMessage=document.getElementById('camels-plot-window-message');
function selectedPlotProtocol(){const name=protocol.value;return name&&name!=='Loading protocols...'&&name!=='No saved protocols'?name:''}
async function refreshCamelsPlotWindows(){const name=selectedPlotProtocol();if(!name){camelsPlotWindowCount.textContent='-';closeCamelsPlotsButton.disabled=true;return}try{const data=await api('plots?protocol_name='+encodeURIComponent(name));const count=Number(data.open_plot_count||0);camelsPlotWindowCount.textContent=String(count);closeCamelsPlotsButton.disabled=count===0;camelsPlotWindowMessage.textContent=''}catch(error){camelsPlotWindowCount.textContent='Unavailable';closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' '+error.message}}
closeCamelsPlotsButton.addEventListener('click',async()=>{const name=selectedPlotProtocol();if(!name)return;closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' Closing...';try{const data=await api('plots/close?protocol_name='+encodeURIComponent(name),{method:'POST'});camelsPlotWindowMessage.textContent=' Close requested for '+String(data.closed_plot_count||0)+' plot(s).';setTimeout(refreshCamelsPlotWindows,300)}catch(error){camelsPlotWindowMessage.textContent=' Could not close plots: '+error.message}finally{setTimeout(refreshCamelsPlotWindows,700)}});
protocol.addEventListener('change',refreshCamelsPlotWindows);const nativePlotWindowInterval=setInterval(refreshCamelsPlotWindows,2000);setTimeout(refreshCamelsPlotWindows,500);
</script>"""

# PID controller plots are produced inside the driver (not as Bluesky/Dash
# plots).  The API supplies thread-safe data snapshots and Plotly renders them
# in OASIS without exposing CAMELS credentials to the browser.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="pid-controller-plots"><h2>PID controller plots</h2><p class="hint" id="pid-plot-status">Choose a protocol to inspect active PID controllers.</p><div id="pid-plot-frames"></div></section>');
const pidPlotStatus=document.getElementById('pid-plot-status'),pidPlotFrames=document.getElementById('pid-plot-frames');let pidPlotlyPromise=null;
function ensurePlotly(){if(window.Plotly)return Promise.resolve(window.Plotly);if(pidPlotlyPromise)return pidPlotlyPromise;pidPlotlyPromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';script.onload=()=>resolve(window.Plotly);script.onerror=()=>reject(new Error('Could not load Plotly.'));document.head.append(script)});return pidPlotlyPromise}
function pidPlotId(name){return 'pid-plot-'+name.replace(/[^a-zA-Z0-9_-]/g,'_')}
function ensurePidFrame(plot){const id=pidPlotId(plot.device_name);let frame=document.getElementById(id);if(frame)return frame;const section=document.createElement('section');section.id=id;const title=document.createElement('h3');title.textContent=plot.device_name+' - '+(plot.title||'PID plot');frame=document.createElement('div');frame.className='pid-plot-frame';frame.style.cssText='width:100%;height:260px;max-height:260px;overflow:hidden;margin-bottom:1rem';section.append(title,frame);pidPlotFrames.append(section);return frame}
async function refreshPidPlots(){const name=selectedPlotProtocol();if(!name){pidPlotStatus.textContent='Choose a protocol to inspect active PID controllers.';pidPlotFrames.innerHTML='';return}try{const data=await api('pid-plots?protocol_name='+encodeURIComponent(name)),plots=data.pid_plots||[];if(!plots.length){pidPlotStatus.textContent='No active PID controller plot is available for this protocol.';pidPlotFrames.innerHTML='';return}pidPlotStatus.textContent=plots.length+' active PID controller plot(s).';const active=new Set(plots.map(plot=>pidPlotId(plot.device_name)));for(const node of [...pidPlotFrames.children])if(!active.has(node.id))node.remove();const Plotly=await ensurePlotly();for(const plot of plots){const frame=ensurePidFrame(plot),series=plot.series||{},x=plot.x||[];const traces=['current value','setpoint','output','k','i','d'].filter(key=>Array.isArray(series[key])).map(key=>({x,y:series[key],name:key,mode:'lines',line:{width:key==='current value'||key==='setpoint'?3:1},visible:['current value','setpoint'].includes(key)?true:'legendonly',yaxis:['output','k','i','d'].includes(key)?'y2':'y'}));Plotly.react(frame,traces,{margin:{t:20,r:55,b:45,l:55},xaxis:{title:'time (s)'},yaxis:{title:'temperature / value'},yaxis2:{title:'PID values',overlaying:'y',side:'right'},legend:{orientation:'h'}},{responsive:true,displaylogo:false})}}catch(error){pidPlotStatus.textContent='Could not load PID plots: '+error.message}}
protocol.addEventListener('change',refreshPidPlots);
// Preserve the last PID frame after completion; only poll its live cache while
// CAMELS reports an active measurement.
const pidRefreshInterval=setInterval(()=>{if(window.camelsMeasurementRunning)refreshPidPlots()},1500);
document.addEventListener('camels-measurement-state',()=>{if(window.camelsMeasurementRunning)refreshPidPlots()});
setTimeout(refreshPidPlots,750);
</script>"""


# GUI v2's dashboard iframe intentionally does not grant ``allow-modals``.
# Replace the browser-native confirm() used by the setup page with a visible,
# two-click confirmation that works both embedded and as a standalone page.
# (The original text explicitly says "CAMELS key".)
SETUP_PAGE = SETUP_PAGE.replace(
    "if(!confirm('Delete the saved CAMELS key for '+entry.name+'?'))return;",
    "if(button.dataset.confirming!=='true'){button.dataset.confirming='true';button.textContent='Confirm delete';setTimeout(()=>{button.dataset.confirming='';button.textContent='Delete'},4000);return;}",
)

# Present the saved CAMELS machines as a table.  The credential itself is never
# returned to the browser; only non-secret service information is displayed.
SETUP_PAGE = SETUP_PAGE.replace(
    '<ul id="saved"><li>Loading...</li></ul>',
    '<table id="saved-table"><thead><tr><th>Connection</th><th>Host</th><th>Local IPv4</th><th>Port</th><th>Started</th><th>Action</th></tr></thead><tbody id="saved"><tr><td colspan="6">Loading...</td></tr></tbody></table>',
).replace(
    'pre{white-space:pre-wrap}',
    'pre{white-space:pre-wrap}#saved-table{width:100%;border-collapse:collapse;margin:.5rem 0}#saved-table th,#saved-table td{padding:.45rem .5rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:middle}#saved-table th{font-size:.9rem;color:#444}#saved-table .delete{margin:0;padding:.25rem .55rem;line-height:1.2;white-space:nowrap}',
).replace(
    "saved.innerHTML='<li>No CAMELS API key saved.</li>';",
    "saved.innerHTML='<tr><td colspan=\"6\">No CAMELS API key saved.</td></tr>';",
).replace(
    "const li=document.createElement('li');",
    "const li=document.createElement('tr');",
).replace(
    "li.textContent=entry.name;",
    "const info=entry.service_info||{},ips=(info.local_ipv4_addresses||[]).join(', ')||'-',host=info.hostname||'-',port=info.api_port||'-',started=info.started_at?new Date(info.started_at).toLocaleString():'-';for(const text of [entry.name,host,ips,port,started]){const cell=document.createElement('td');cell.textContent=text;li.append(cell);}",
).replace(
    "li.append(button);saved.append(li)",
    "const action=document.createElement('td');action.append(button);li.append(action);saved.append(li)",
)

# Keep the compact connection form centred inside the GUI v2 dashboard iframe.
SETUP_PAGE = SETUP_PAGE.replace(
    "body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}",
    "body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;min-height:calc(100vh - 6rem);display:flex;flex-direction:column;justify-content:center;line-height:1.45}",
)

# A successful connection with no exposed protocol is a useful state, not an
# error. Show its actionable message instead of a raw JSON response.
SETUP_PAGE = SETUP_PAGE.replace(
    "function show(data,ok=true){result.textContent=(ok?'Success: ':'Error: ')+JSON.stringify(data,null,2)}",
    "function show(data,ok=true){const message=data&&typeof data==='object'&&data.message?data.message:JSON.stringify(data,null,2);result.textContent=(ok?'Success: ':'Error: ')+message}",
)

# The two GUI v2 dashboards deliberately have separate responsibilities.
# Leave connection management in the setup dashboard and open the monitor
# dashboard in the parent GUI v2 window instead of navigating inside its iframe.
SETUP_PAGE = SETUP_PAGE.replace(
    '<a href="./dashboard">Go to dashboard</a>',
    '<a href="/nomad-oasis/gui/v2/dashboard_tools/camels-measurement-dashboard" target="_blank" rel="noopener">Go to dashboard</a>',
)

# Bulk deletion uses the same visible two-click confirmation pattern as each
# individual Delete button, which also works inside the GUI v2 iframe.
SETUP_PAGE = SETUP_PAGE.replace(
    "<h2>Saved CAMELS connections</h2>",
    '<div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap"><h2 style="margin:.8rem 0">Saved CAMELS connections</h2><button type="button" id="delete-all-protocols" class="delete">Delete all connections</button></div>',
).replace(
    "refresh();</script>",
    """document.getElementById('delete-all-protocols').onclick=async()=>{const button=document.getElementById('delete-all-protocols');if(button.dataset.confirming!=='true'){button.dataset.confirming='true';button.textContent='Confirm delete all';setTimeout(()=>{button.dataset.confirming='';button.textContent='Delete all connections'},4000);return;}try{await request('./settings/connections',{method:'DELETE'});button.dataset.confirming='';button.textContent='Delete all connections';refresh()}catch(e){show(e,false)}};refresh();</script>""",
)

# Keep protocol execution close to the protocol-scoped API-key management.  The
# browser talks only to the OASIS bridge: the corresponding CAMELS key remains
# in the server-side credential store.
SETUP_PAGE = SETUP_PAGE.replace(
    "</body></html>",
    """<section id="protocol-control">
<style>
#protocol-control{margin-top:2rem;padding-top:1.2rem;border-top:1px solid #bbb}
#protocol-control select{min-width:240px;padding:.45rem;margin:.4rem .35rem .4rem 0}
#protocol-control button{margin:.4rem .35rem .4rem 0}
#protocol-control label{display:block;font-weight:600;margin-top:.7rem}
#protocol-control input{display:block;box-sizing:border-box;width:100%;padding:.5rem;margin-top:.25rem;font-weight:400}
#protocol-control .hint{color:#555}
#protocol-control .run-status{white-space:pre-wrap;margin-top:.6rem}
</style>
<h2>Protocol control</h2>
<label>Protocol<br><select id="remote-protocol"><option>Loading saved protocols...</option></select></label>
<button type="button" id="view-remote-protocol">View protocol</button>
<p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p>
<h2>Measurement parameters</h2>
<div id="remote-parameters">Choose a protocol, then select View protocol.</div>
<button type="button" id="save-remote-parameters" disabled>Save parameters into protocol</button>
<button type="button" id="start-remote-measurement" disabled>Start measurement</button>
<div id="remote-run-status" class="run-status"></div>
<script>
const remoteProtocol=document.getElementById('remote-protocol');
const remoteParameters=document.getElementById('remote-parameters');
const remoteViewButton=document.getElementById('view-remote-protocol');
const remoteSaveButton=document.getElementById('save-remote-parameters');
const remoteStartButton=document.getElementById('start-remote-measurement');
const remoteRunStatus=document.getElementById('remote-run-status');
let remoteRunTimer=null;
async function remoteApi(path,options={}){
  const response=await fetch('./dashboard/'+path,options);
  const text=await response.text();
  let data;
  try{data=JSON.parse(text)}catch{data={detail:text}}
  if(!response.ok)throw new Error(data.detail||text);
  return data;
}
function remoteValue(value){
  if(value&&typeof value==='object'&&'default' in value)value=value.default;
  return typeof value==='object'?JSON.stringify(value):value??'';
}
async function loadRemoteProtocols(){
  try{
    const current=remoteProtocol.value;
    const data=await remoteApi('protocols');
    const names=data.Protocols||data.protocols||[];
    remoteProtocol.innerHTML='';
    for(const name of names){
      const option=document.createElement('option');
      option.value=option.textContent=name;
      remoteProtocol.append(option);
    }
    if(!names.length)remoteProtocol.innerHTML='<option>No saved protocols</option>';
    else if(names.includes(current))remoteProtocol.value=current;
  }catch(error){
    remoteProtocol.innerHTML='<option>Could not load protocols</option>';
    remoteRunStatus.textContent='Error: '+error.message;
  }
}
async function viewRemoteProtocol(){
  const name=remoteProtocol.value;
  if(!name||name==='No saved protocols'||name==='Could not load protocols')return;
  remoteViewButton.disabled=true;
  remoteStartButton.disabled=true;
  remoteParameters.textContent='Loading protocol parameters...';
  try{
    const schema=await remoteApi('protocols/'+encodeURIComponent(name)+'/schema');
    const definitions=schema.parameters||schema.variables||{};
    remoteParameters.innerHTML='';
    for(const [parameter,definition] of Object.entries(definitions)){
      const label=document.createElement('label');
      const input=document.createElement('input');
      label.textContent=parameter;
      input.name=parameter;
      input.value=remoteValue(definition);
      label.append(input);
      remoteParameters.append(label);
    }
    if(!Object.keys(definitions).length)remoteParameters.textContent='This protocol does not declare user parameters.';
    remoteRunStatus.textContent='';
    remoteSaveButton.disabled=false;
    remoteStartButton.disabled=false;
  }catch(error){
    remoteParameters.textContent='Could not load protocol parameters.';
    remoteRunStatus.textContent='Error: '+error.message;
  }finally{remoteViewButton.disabled=false;}
}
function remoteVariables(){
  const variables={};
  for(const input of remoteParameters.querySelectorAll('input')){
    let value=input.value;
    try{value=JSON.parse(value)}catch{}
    variables[input.name]=value;
  }
  return variables;
}
async function saveRemoteParameters(){
  const name=remoteProtocol.value;
  if(!name)return;
  remoteSaveButton.disabled=true;
  remoteRunStatus.textContent='Saving parameters into CAMELS protocol...';
  try{
    await remoteApi('protocols/'+encodeURIComponent(name)+'/parameters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({variables:remoteVariables()})});
    // Read the schema back from CAMELS. This is both visual confirmation and
    // a guard against presenting a successful save when CAMELS rejected it.
    const verified=await remoteApi('protocols/'+encodeURIComponent(name)+'/schema');
    const persisted=verified.parameters||verified.variables||{};
    for(const input of remoteParameters.querySelectorAll('input')){
      if(Object.prototype.hasOwnProperty.call(persisted,input.name))input.value=remoteValue(persisted[input.name]);
    }
    remoteRunStatus.textContent='Parameters saved and verified in CAMELS protocol.';
  }catch(error){
    remoteRunStatus.textContent='Could not save parameters: '+error.message;
  }finally{remoteSaveButton.disabled=false;}
}
function remoteStatusInfo(result){
  const raw=String(result.status||result.state||'').trim();
  const value=raw.toLowerCase();
  if(raw.includes('/')||raw.includes('\\\\')||value.includes('finished')||value.includes('complete')||value.includes('success'))return ['Finished',true];
  if(value.includes('queue'))return ['Queued',false];
  if(value.includes('running')||!value)return ['Running',false];
  if(value.includes('failed')||value.includes('error')||value.includes('abort')||value.includes('stopped'))return ['Failed',true];
  return ['Running',false];
}
async function pollRemoteRun(runId,connectionId){
  clearTimeout(remoteRunTimer);
  try{
    const result=await remoteApi('runs/'+encodeURIComponent(runId)+'?connection_id='+encodeURIComponent(connectionId||''));
    const [text,done]=remoteStatusInfo(result);
    remoteRunStatus.textContent=text;
    if(done)remoteStartButton.disabled=false;
    else remoteRunTimer=setTimeout(()=>pollRemoteRun(runId,connectionId),1500);
  }catch(error){
    remoteRunStatus.textContent='Status check failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
async function startRemoteMeasurement(){
  const name=remoteProtocol.value;
  if(!name)return;
  remoteStartButton.disabled=true;
  remoteRunStatus.textContent='Starting measurement...';
  try{
    const result=await remoteApi('actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:name,variables:remoteVariables()})});
    const workflowId=result.workflow_id;
    if(workflowId){remoteRunStatus.textContent='Action queued';pollRemoteAction(workflowId)}
    else{remoteRunStatus.textContent='Action created';remoteStartButton.disabled=false;}
  }catch(error){
    remoteRunStatus.textContent='Failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
async function pollRemoteAction(workflowId){
  clearTimeout(remoteRunTimer);
  try{
    const result=await remoteApi('actions/'+encodeURIComponent(workflowId));
    const status=String(result.status||'PENDING').toUpperCase();
    remoteRunStatus.textContent='NOMAD Action: '+status;
    if(['COMPLETED','FAILED','CANCELLED','TERMINATED','TIMED_OUT'].includes(status))remoteStartButton.disabled=false;
    else remoteRunTimer=setTimeout(()=>pollRemoteAction(workflowId),1500);
  }catch(error){
    remoteRunStatus.textContent='Action status check failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
remoteViewButton.addEventListener('click',viewRemoteProtocol);
remoteSaveButton.addEventListener('click',saveRemoteParameters);
remoteStartButton.addEventListener('click',startRemoteMeasurement);
remoteProtocol.addEventListener('change',()=>{remoteSaveButton.disabled=true;remoteStartButton.disabled=true;remoteParameters.textContent='Choose a protocol, then select View protocol.';remoteRunStatus.textContent='';});
loadRemoteProtocols();
</script></section></body></html>""",
)

# Saving a new protocol-scoped key changes the catalogue used by the protocol
# control below. Refresh that selector immediately instead of requiring a page
# reload.
SETUP_PAGE = SETUP_PAGE.replace(
    "document.getElementById('key').value='';refresh()",
    "document.getElementById('key').value='';await refresh();await loadRemoteProtocols()",
)
DASHBOARD_PAGE = DASHBOARD_PAGE.replace(
    '<p><a href="./setup">CAMELS connections setup</a></p>',
    '',
)

# The setup page uses POST for the two destructive operations as requested by
# this bridge's API contract.  Keeping this replacement here also covers the
# dynamically inserted bulk-delete handler above.
SETUP_PAGE = SETUP_PAGE.replace("{method:'DELETE'}", "{method:'POST'}")
SETUP_PAGE = SETUP_PAGE.replace(
    "./settings/connections/'+encodeURIComponent(entry.connection_id),{method:'POST'}",
    "./settings/connections/'+encodeURIComponent(entry.connection_id)+'/delete',{method:'POST'}",
).replace(
    "request('./settings/connections',{method:'POST'})",
    "request('./settings/connections/delete',{method:'POST'})",
)

# A browser plot lives on the same CAMELS host as its API.  Keep the browser
# address in the saved OASIS connection and expose only that non-secret address
# to the authenticated dashboard; the API key never leaves the server.
DASHBOARD_PAGE = DASHBOARD_PAGE.replace(
    "status=document.getElementById('status'),run=document.getElementById('run');let timer=null;",
    "status=document.getElementById('status'),run=document.getElementById('run');let timer=null,protocolConnectionUrls={};",
).replace(
    "const data=await api('protocols');protocol.innerHTML='';",
    "const data=await api('protocols');protocolConnectionUrls=data.connection_base_urls||{};protocol.innerHTML='';",
).replace(
    "function plotBaseUrl(port){let base=browserPlotUrlInput.value.trim();",
    "function updateBrowserPlotUrl(){const apiUrl=protocolConnectionUrls[protocol.value];if(!apiUrl)return;try{const parsed=new URL(apiUrl);browserPlotUrlInput.value=parsed.protocol+'//'+parsed.hostname+':'}catch(error){}}\nfunction plotBaseUrl(port){let base=browserPlotUrlInput.value.trim();",
).replace(
    "protocol.addEventListener('change',()=>{browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML=''});",
    "protocol.addEventListener('change',()=>{updateBrowserPlotUrl();browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML=''});setTimeout(updateBrowserPlotUrl,500);",
).replace(
    "function plotUrl(plot){return plotBaseUrl(plot.port)+'/?run='+Date.now()}",
    "function browserPlotProxyUrl(plot,path=''){return './dashboard/browser-plots/'+encodeURIComponent(protocol.value)+'/'+encodeURIComponent(String(plot.port))+'/'+path}\nfunction plotUrl(plot){return browserPlotProxyUrl(plot)+'?run='+Date.now()}",
).replace(
    "async function clearPlot(plot,message){if(message)message.textContent=' Clearing...';try{const response=await fetch(plotBaseUrl(plot.port)+'/clear_data',{method:'POST'});if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);if(message)message.textContent=' Cleared'}catch(error){if(message)message.textContent=' Could not clear plot: '+error.message}}",
    "async function clearPlot(plot,message){if(message)message.textContent=' Clearing...';try{const response=await fetch(browserPlotProxyUrl(plot,'clear_data'),{method:'POST'});if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);if(message)message.textContent=' Cleared'}catch(error){if(message)message.textContent=' Could not clear plot: '+error.message}}",
)

# A CAMELS measurement can briefly make its local API unavailable while the
# protocol is starting or finishing. Keep polling such transient failures so a
# completed run is not incorrectly presented as a failed measurement.
DASHBOARD_PAGE += """<script>
let camelsStatusUnavailableRetries=0;
async function poll(id,connectionId){
  clearTimeout(timer);
  try{
    const result=await api('runs/'+encodeURIComponent(id)+'?connection_id='+encodeURIComponent(connectionId));
    const [text,done]=statusInfo(result);
    status.textContent=text;
    camelsStatusUnavailableRetries=0;
    if(done)run.disabled=false;
    else timer=setTimeout(()=>poll(id,connectionId),1500);
  }catch(error){
    const message=String(error.message||error);
    if(message.includes('CAMELS service is unavailable')&&camelsStatusUnavailableRetries<20){
      camelsStatusUnavailableRetries+=1;
      status.textContent='CAMELS connection temporarily unavailable; retrying ('+camelsStatusUnavailableRetries+'/20)...';
      timer=setTimeout(()=>poll(id,connectionId),2000);
      return;
    }
    status.textContent='Status check failed: '+message;
    run.disabled=false;
  }
}
</script>"""

# The monitoring dashboard intentionally has no execution controls.  Protocol
# parameters and Start measurement live in the connections dashboard; here a
# protocol selection immediately loads its sequence and the configured browser
# plots.  Detailed plot configuration remains available on demand.
DASHBOARD_PAGE += """<script>
(() => {
  const loadButton=document.getElementById('load');
  const parameterTitle=document.getElementById('parameters').previousElementSibling;
  const measurementHint=document.querySelector('p.hint');
  const runTitle=document.getElementById('status').previousElementSibling;
  loadButton.style.display='none';
  // The parameters are shown and editable; Start measurement uses them for
  // the next run. They can also be saved as the defaults of the protocol.
  document.getElementById('run').style.display='none';
  const saveDefaults=document.createElement('button');
  saveDefaults.type='button';
  saveDefaults.id='dashboard-save-parameters';
  saveDefaults.textContent='Save as defaults in CAMELS';
  const saveMessage=document.createElement('small');
  saveMessage.className='hint';
  saveMessage.style.marginLeft='.5rem';
  document.getElementById('parameters').after(saveDefaults,saveMessage);
  saveDefaults.addEventListener('click',async()=>{
    const name=protocol.value;
    if(!name||/^Loading|^No saved|^Could not load/.test(name))return;
    saveDefaults.disabled=true;saveMessage.textContent='Saving...';
    try{
      await api('protocols/'+encodeURIComponent(name)+'/parameters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({variables:variables()})});
      saveMessage.textContent='Saved in CAMELS as the new defaults of '+name+'.';
    }catch(error){saveMessage.textContent='Could not save: '+error.message}
    finally{saveDefaults.disabled=false}
  });
  runTitle.style.display='none';
  document.getElementById('status').style.display='none';

  const pidSection=document.getElementById('pid-controller-plots');
  if(pidSection){
    const pidHeading=pidSection.querySelector('h2');
    if(pidHeading)pidHeading.style.display='none';
    pidPlotStatus.style.display='none';
  }
  const nativePlotSection=document.getElementById('camels-plot-window-status');
  if(nativePlotSection)nativePlotSection.style.display='';

  const browserSection=document.getElementById('browser-plots');
  const browserHeading=browserSection.querySelector('h2');
  if(browserHeading)browserHeading.style.display='none';
  const plotTitle=document.createElement('h2');
  plotTitle.textContent='PLOT';
  plotTitle.style.marginBottom='.35rem';
  const advanced=document.createElement('section');
  advanced.id='advanced-plot-settings';
  advanced.style.cssText='margin:.8rem 0';
  const toggle=document.createElement('input');
  toggle.type='checkbox';
  toggle.id='advanced-plot-settings-toggle';
  const toggleLabel=document.createElement('label');
  toggleLabel.htmlFor=toggle.id;
  toggleLabel.textContent=' Advanced plot display settings';
  const settings=document.createElement('div');
  settings.hidden=true;
  settings.style.cssText='margin:.6rem 0;padding:.6rem;border:1px solid #ddd';
  const urlLabel=document.getElementById('browser-plot-url').closest('label');
  const manualControls=document.getElementById('manual-plot-name').closest('div');
  const plotTable=document.getElementById('browser-plot-rows').closest('table');
  settings.append(urlLabel,manualControls,plotTable);
  toggle.addEventListener('change',()=>{settings.hidden=!toggle.checked});
  function updateClearModeVisibility(){
    for(const label of browserPlotFrames.querySelectorAll('label')){
      if(!label.textContent.trim().startsWith('Clear mode'))continue;
      label.hidden=!toggle.checked;
      const clearButton=label.nextElementSibling;
      if(clearButton&&clearButton.tagName==='BUTTON')clearButton.hidden=!toggle.checked;
    }
  }
  toggle.addEventListener('change',updateClearModeVisibility);
  new MutationObserver(updateClearModeVisibility).observe(browserPlotFrames,{childList:true,subtree:true});
  updateClearModeVisibility();
  advanced.append(toggle,toggleLabel,settings);
  browserSection.insertBefore(plotTitle,document.getElementById('browser-plot-frames'));
  browserSection.insertBefore(advanced,document.getElementById('browser-plot-frames'));
  // Keep PID data plots, but present them as ordinary plots under PLOT rather
  // than as a separate controller-status panel.
  if(pidSection){
    browserSection.insertBefore(pidPlotFrames,document.getElementById('browser-plot-frames'));
    pidSection.style.display='none';
  }

  let lastLoadedProtocol='';
  async function automaticallyLoadProtocol(){
    const name=protocol.value;
    if(!name||name==='Loading protocols...'||name==='No saved protocols'||name==='Could not load protocols')return;
    if(name===lastLoadedProtocol)return;
    lastLoadedProtocol=name;
    await viewProtocol();
    await loadBrowserPlots();
  }
  protocol.addEventListener('change',()=>{lastLoadedProtocol='';automaticallyLoadProtocol()});
  const protocolObserver=new MutationObserver(()=>automaticallyLoadProtocol());
  protocolObserver.observe(protocol,{childList:true});
  setTimeout(automaticallyLoadProtocol,350);
})();
</script>"""

# Recreate CAMELS Browser Plots from their authenticated /data snapshots.
# The original Dash application remains local to CAMELS; OASIS receives the
# same x/y data and protocol plot configuration without proxying Dash's private
# callback machinery through an iframe.
DASHBOARD_PAGE += """<script>
let browserPlotDataTimer=null;
  // Browser plots are snapshots from CAMELS. Bound the browser-side payload;
  // CAMELS retains the complete raw acquisition data.
  const BROWSER_PLOT_MAX_POINTS=2000;
  window.camelsMeasurementRunning=false;
  let browserPlotlyPromise=null;
  function ensureBrowserPlotly(){
    if(window.Plotly)return Promise.resolve(window.Plotly);
    if(browserPlotlyPromise)return browserPlotlyPromise;
    browserPlotlyPromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';script.onload=()=>resolve(window.Plotly);script.onerror=()=>reject(new Error('Could not load Plotly.'));document.head.append(script)});
    return browserPlotlyPromise;
  }
  function collectBrowserPlots(definition){
    const found=[];walkForPlots(definition,found);const used=new Set();
    return found.map((item,index)=>({
      name:String(item.name||item.title||('Plot '+(index+1))),title:String(item.title||item.name||('Plot '+(index+1))),port:Number(item.browser_port),
      xName:String(item.x_axis||'time'),yNames:Array.isArray(item.y_axes&&item.y_axes.formula)?item.y_axes.formula.map(String):[],
      yAxes:Array.isArray(item.y_axes&&item.y_axes.axis)?item.y_axes.axis:[],xlabel:String(item.xlabel||item.x_axis||'time'),
      ylabel:String(item.ylabel||((item.y_axes&&item.y_axes.formula||[])[0]||'value')),ylabel2:String(item.ylabel2||''),
      logX:item.logX===true,logY:item.logY===true,logY2:item.logY2===true,
      enabled:item.checkbox_show_in_browser===true,shown:item.checkbox_show_in_browser===true,clearMode:'automatic'
    })).filter(item=>Number.isInteger(item.port)&&item.port>0).filter(item=>{const key=plotKey(item);if(used.has(key))return false;used.add(key);return true});
  }
  function downsamplePlotValues(xValues,yValues,limit=BROWSER_PLOT_MAX_POINTS){
    const count=Math.min(Array.isArray(xValues)?xValues.length:0,Array.isArray(yValues)?yValues.length:0);
    if(count<=limit)return {x:(xValues||[]).slice(0,count),y:(yValues||[]).slice(0,count)};
    const x=[],y=[];
    for(let point=0;point<limit;point++){
      const index=Math.round(point*(count-1)/(limit-1));
      x.push(xValues[index]);y.push(yValues[index]);
    }
    return {x,y};
  }
  async function updateBrowserPlotFigure(plot,frame,message){
    try{
      const response=await fetch(browserPlotProxyUrl(plot,'data'));
      if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);
      const data=await response.json();
      const xName=Object.prototype.hasOwnProperty.call(data,plot.xName)?plot.xName:Object.keys(data)[0];
      const yNames=(plot.yNames||[]).filter(name=>Array.isArray(data[name]));
      const names=yNames.length?yNames:Object.keys(data).filter(name=>name!==xName&&Array.isArray(data[name]));
      const traces=names.map((name,index)=>{const values=downsamplePlotValues(data[xName]||[],data[name]||[]);return {x:values.x,y:values.y,name,mode:'markers',marker:{size:10,color:index,opacity:.8},yaxis:(plot.yAxes[index]==='right'||plot.yAxes[index]===2)?'y2':'y'}});
      // CAMELS Browser Plot stores every completed live fit as fit_* with the
      // fitted y values and the y trace it belongs to. Use those exact values
      // instead of performing a second, potentially different fit in OASIS.
      for(const [fitName,fit] of Object.entries(data)){
        if(!fitName.startsWith('fit_') || !fit || !Array.isArray(fit.fit_result))continue;
        const target=String(fit.y_axis_name||''),targetIndex=names.indexOf(target),axisIndex=targetIndex<0?0:targetIndex;
        const sourceX=data[xName]||[],count=Math.min(sourceX.length,fit.fit_result.length);
        let fitX=sourceX.slice(-count),fitY=fit.fit_result.slice(-count);
        if(sourceX.length>1 && fit.fit_result.length!==sourceX.length){const first=Number(sourceX[0]),last=Number(sourceX[sourceX.length-1]);if(Number.isFinite(first)&&Number.isFinite(last))fitX=fit.fit_result.map((_,index)=>first+(last-first)*index/Math.max(1,fit.fit_result.length-1));fitY=fit.fit_result;}
        const values=downsamplePlotValues(fitX,fitY);
        traces.push({x:values.x,y:values.y,name:fitName.slice(4),mode:'lines',line:{color:'red',dash:'dash',width:2},yaxis:(plot.yAxes[axisIndex]==='right'||plot.yAxes[axisIndex]===2)?'y2':'y'});
      }
      const Plotly=await ensureBrowserPlotly();
      await Plotly.react(frame,traces,{title:plot.title,xaxis:{title:plot.xlabel,type:plot.logX?'log':'linear'},yaxis:{title:plot.ylabel,type:plot.logY?'log':'linear'},yaxis2:{title:plot.ylabel2,type:plot.logY2?'log':'linear',overlaying:'y',side:'right'},margin:{l:60,r:60,t:50,b:55},uirevision:plotKey(plot)},{responsive:true,displaylogo:false});
      if(message)message.textContent='';
    }catch(error){
      if(message)message.textContent=' Could not load plot data: '+String(error.message||error);
    }
  }
  function renderPlotFrames(){
    clearInterval(browserPlotDataTimer);browserPlotFrames.innerHTML='';
    const visible=browserPlots.filter(item=>item.shown);
    for(const plot of visible){
      const section=document.createElement('section'),heading=document.createElement('h3'),label=document.createElement('label'),select=document.createElement('select'),manual=document.createElement('button'),message=document.createElement('span'),frame=document.createElement('div');
      heading.textContent=plot.name+' (port '+plot.port+')';label.textContent='Clear mode ';select.innerHTML='<option value="automatic">Automatic</option><option value="manual">Manual</option>';select.value=plot.clearMode;manual.type='button';manual.textContent='Clear plot';manual.style.display=plot.clearMode==='manual'?'inline-block':'none';message.className='hint';message.style.marginLeft='.5rem';frame.style.cssText='width:100%;height:620px;margin-top:1rem';frame.dataset.browserPlotKey=plotKey(plot);
      select.addEventListener('change',()=>{plot.clearMode=select.value;manual.style.display=plot.clearMode==='manual'?'inline-block':'none'});
      manual.addEventListener('click',async()=>{await clearPlot(plot,message);await updateBrowserPlotFigure(plot,frame,message)});
      label.append(select);section.append(heading,label,manual,message,frame);browserPlotFrames.append(section);updateBrowserPlotFigure(plot,frame,message);
    }
    if(visible.length)browserPlotDataTimer=setInterval(refreshBrowserPlotFigures,1500);
  }
  let browserPlotRefreshInFlight=false;
  async function refreshBrowserPlotFigures(){
    if(!window.camelsMeasurementRunning || browserPlotRefreshInFlight)return;
    browserPlotRefreshInFlight=true;
    try{for(const plot of browserPlots.filter(item=>item.shown)){const frame=[...browserPlotFrames.querySelectorAll('[data-browser-plot-key]')].find(element=>element.dataset.browserPlotKey===plotKey(plot));if(frame)await updateBrowserPlotFigure(plot,frame,null)}}finally{browserPlotRefreshInFlight=false}
  }
  document.addEventListener('camels-measurement-state',refreshBrowserPlotFigures);
</script>"""

# On first load, prefer the protocol that CAMELS is actively executing.  If no
# measurement is active, the normal alphabetical first option remains selected.
DASHBOARD_PAGE += """<script>
(() => {
  let activeProtocolSelectionDone=false;
  async function selectActiveProtocol(){
    if(activeProtocolSelectionDone || !protocol.options.length)return;
    const names=[...protocol.options].map(option=>option.value);
    if(!names.length || /^Loading|^No saved|^Could not load/.test(names[0]))return;
    try{
      const active=await api('active-measurement');
      const name=String(active.protocol_name||'');
      activeProtocolSelectionDone=true;
      if(name && names.includes(name) && protocol.value!==name){
        protocol.value=name;
        protocol.dispatchEvent(new Event('change'));
      }
    }catch(error){
      // Keep the regular alphabetical selection if CAMELS is temporarily
      // unavailable while the dashboard first opens.
      activeProtocolSelectionDone=true;
    }
  }
  new MutationObserver(selectActiveProtocol).observe(protocol,{childList:true});
  setTimeout(selectActiveProtocol,700);
})();
</script>"""

# Mirror the progress bar from the CAMELS main window next to the selected
# protocol. The data is a plain-Python snapshot served by CAMELS, so this
# remains safe even while the GUI is executing a measurement.
DASHBOARD_PAGE += """<script>
(() => {
  const protocolSelect=document.getElementById('protocol');
  if(!protocolSelect || document.getElementById('camels-measurement-progress'))return;
  const protocolLabel=protocolSelect.closest('label');
  if(!protocolLabel)return;
  const row=document.createElement('div');
  // Align the action with the bottom of the protocol select, rather than the
  // label above it. The status panel is stretched back to the row top below.
  row.style.cssText='display:flex;align-items:flex-end;gap:1.2rem;flex-wrap:wrap;margin:.25rem 0 .8rem';
  protocolLabel.parentNode.insertBefore(row,protocolLabel);
  row.append(protocolLabel);
  const startButton=document.createElement('button');
  startButton.type='button';
  startButton.id='dashboard-start-measurement';
  startButton.textContent='Start measurement';
  row.append(startButton);
  const stopButton=document.createElement('button');
  stopButton.type='button';
  stopButton.id='dashboard-stop-measurement';
  stopButton.textContent='Stop measurement';
  stopButton.disabled=true;
  row.append(stopButton);
  const panel=document.createElement('section');
  panel.id='camels-measurement-progress';
  panel.style.cssText='align-self:stretch;min-width:250px;border-left:1px solid #ddd;padding-left:1rem';
  panel.innerHTML='<strong>Measurement status</strong><div id="camels-measurement-status-text" class="hint">Idle</div><div style="height:12px;background:#e6e6e6;border-radius:6px;overflow:hidden;margin:.3rem 0"><div id="camels-measurement-progress-bar" style="height:100%;width:0%;background:#1976d2;transition:width .2s"></div></div><small id="camels-measurement-progress-detail">0%</small>';
  row.append(panel);
  const text=document.getElementById('camels-measurement-status-text');
  const bar=document.getElementById('camels-measurement-progress-bar');
  const detail=document.getElementById('camels-measurement-progress-detail');
  let actionTimer=null;
  let actionPending=false;
  let measurementRunning=false;
  let remotePaused=false;
  function updateStartAvailability(){
    startButton.disabled=actionPending || measurementRunning || remotePaused;
    if(remotePaused && !actionPending){
      startButton.textContent='Remote control paused';
      startButton.title='Remote control is paused in CAMELS on the lab computer (Tools > OASIS Remote Control...).';
      stopButton.disabled=true;
      return;
    }
    startButton.title='';
    if(startButton.dataset.confirming==='true')startButton.textContent='Click again to start';
    else if(measurementRunning && !actionPending)startButton.textContent='Measurement running';
    else if(!actionPending)startButton.textContent='Start measurement';
    stopButton.disabled=!measurementRunning;
    if(!measurementRunning){stopButton.dataset.confirming='';stopButton.textContent='Stop measurement'}
  }
  // Starting and stopping change the instrument: both need a second click.
  function confirmClick(button,text){
    if(button.dataset.confirming==='true'){button.dataset.confirming='';return true}
    button.dataset.confirming='true';button.textContent=text;
    setTimeout(()=>{if(button.dataset.confirming==='true'){button.dataset.confirming='';if(button===stopButton)stopButton.textContent='Stop measurement';updateStartAvailability()}},4000);
    return false;
  }
  stopButton.addEventListener('click',async()=>{
    const name=protocolSelect.value;
    if(!name || /^Loading|^No saved|^Could not load/.test(name))return;
    if(!confirmClick(stopButton,'Click again to stop'))return;
    stopButton.disabled=true;stopButton.textContent='Stopping...';
    try{
      const result=await api('stop?protocol_name='+encodeURIComponent(name),{method:'POST'});
      detail.textContent=result.status==='idle'?'No measurement was running.':'Stop requested.';
    }catch(error){
      detail.textContent='Could not stop: '+String(error.message||error);
    }finally{stopButton.textContent='Stop measurement';updateStartAvailability()}
  });
  function duration(seconds){
    if(seconds===null || seconds===undefined || !Number.isFinite(Number(seconds)))return '-';
    seconds=Math.max(0,Math.round(Number(seconds)));
    const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;
    return h ? String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0') : String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  }
  async function refreshMeasurementProgress(){
    const selected=protocolSelect.value;
    if(!selected || /^Loading|^No saved|^Could not load/.test(selected))return;
    try{
      const data=await api('measurement-status?protocol_name='+encodeURIComponent(selected));
      const status=String(data.status||'Idle');
      const progress=Math.max(0,Math.min(100,Number(data.progress)||0));
      const normalized=status.toLowerCase();
      const wasMeasurementRunning=window.camelsMeasurementRunning===true;
      measurementRunning=['running','preparing','queued'].some(word=>normalized.includes(word));
      remotePaused=data.remote_control_paused===true;
      window.camelsMeasurementRunning=measurementRunning;
      // Start snapshot polling immediately when CAMELS enters a run and stop
      // it as soon as the run reaches a terminal state. The final rendered
      // Plotly frame intentionally remains visible.
      if(wasMeasurementRunning!==measurementRunning){
        document.dispatchEvent(new Event('camels-measurement-state'));
      }
      const activeProtocol=data.protocol_name ? String(data.protocol_name) : '';
      text.textContent=(measurementRunning && activeProtocol
        ? status+' - currently running: '+activeProtocol
        : status)+(remotePaused?' | Remote control paused on the lab computer':'');
      bar.style.width=progress.toFixed(1)+'%';
      updateStartAvailability();
      detail.textContent=progress.toFixed(0)+'% | '+duration(data.elapsed_seconds)+' elapsed | '+(status==='Finished' ? 'finished' : duration(data.remaining_seconds)+' remaining');
    }catch(error){
      text.textContent='Status unavailable';
      detail.textContent=String(error.message||error);
    }
  }
  async function pollDashboardAction(workflowId){
    clearTimeout(actionTimer);
    try{
      const result=await api('actions/'+encodeURIComponent(workflowId));
      const actionStatus=String(result.status||'PENDING').toUpperCase();
      startButton.textContent='Action: '+actionStatus;
      if(['COMPLETED','FAILED','CANCELLED','TERMINATED','TIMED_OUT'].includes(actionStatus)){
        if(actionStatus!=='COMPLETED')detail.textContent='The measurement Action ended with '+actionStatus+'. Check CAMELS on the lab computer.';
        actionPending=false;
        updateStartAvailability();
      }else actionTimer=setTimeout(()=>pollDashboardAction(workflowId),1500);
    }catch(error){
      actionPending=false;
      updateStartAvailability();
      detail.textContent='Action status unavailable: '+String(error.message||error);
    }
  }
  startButton.addEventListener('click',async()=>{
    const name=protocolSelect.value;
    if(!name || /^Loading|^No saved|^Could not load/.test(name))return;
    if(!confirmClick(startButton,'Click again to start'))return;
    actionPending=true;
    updateStartAvailability();
    startButton.textContent='Creating Action...';
    try{
      // A new measurement must never inherit points from a previous run.
      // Clear CAMELS' active Browser Plot caches before submitting the Action,
      // then clear the local Plotly views immediately as visual feedback.
      const visiblePlots=(typeof browserPlots==='undefined'?[]:browserPlots.filter(plot=>plot.shown));
      await Promise.all(visiblePlots.map(plot=>clearPlot(plot,null)));
      if(typeof renderPlotFrames==='function')renderPlotFrames();
      const values=typeof variables==='function' ? variables() : {};
      const result=await api('actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:name,variables:values})});
      if(result.workflow_id)pollDashboardAction(result.workflow_id);
      else{actionPending=false;updateStartAvailability();}
    }catch(error){
      actionPending=false;
      updateStartAvailability();
      detail.textContent='Could not create NOMAD Action: '+String(error.message||error);
    }
  });
  protocolSelect.addEventListener('change',refreshMeasurementProgress);
  setInterval(refreshMeasurementProgress,1000);
  setTimeout(refreshMeasurementProgress,500);
})();
</script>"""

# Replace browser/Dash iframes with direct snapshots of CAMELS' currently open
# native plot windows. This keeps all rendering in the OASIS page and updates
# figures in place, using the same data-snapshot pattern as PID monitoring.
DASHBOARD_PAGE += """<script>
(() => {
  // Native Qt window mirroring was intentionally withdrawn: it introduced
  // unstable cross-thread HTTP calls. Keep the proven Browser iframe and PID
  // snapshot paths defined above.
  return;
  clearInterval(nativePlotWindowInterval);
  clearInterval(pidRefreshInterval);
  const oldBrowserPlots=document.getElementById('browser-plots');
  const oldPidPlots=document.getElementById('pid-controller-plots');
  const oldNativePlots=document.getElementById('camels-plot-window-status');
  if(oldPidPlots)oldPidPlots.remove();
  if(oldNativePlots)oldNativePlots.remove();
  const nativePlots=document.createElement('section');
  nativePlots.id='native-live-plots';
  nativePlots.innerHTML='<div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap"><h2 style="margin:.6rem 0">PLOT</h2><small id="native-plot-count">0 CAMELS plot window(s) open.</small><button type="button" id="native-close-plots">Close plots</button><small id="native-plot-message" class="hint"></small></div><div id="native-plot-frames"></div>';
  oldBrowserPlots.replaceWith(nativePlots);
  const nativeCount=document.getElementById('native-plot-count');
  const nativeCloseButton=document.getElementById('native-close-plots');
  const nativeMessage=document.getElementById('native-plot-message');
  const nativeFrames=document.getElementById('native-plot-frames');
  let nativePlotlyPromise=null;
  function ensureNativePlotly(){
    if(window.Plotly)return Promise.resolve(window.Plotly);
    if(nativePlotlyPromise)return nativePlotlyPromise;
    nativePlotlyPromise=new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';
      script.onload=()=>resolve(window.Plotly);
      script.onerror=()=>reject(new Error('Could not load Plotly.'));
      document.head.append(script);
    });
    return nativePlotlyPromise;
  }
  function activeProtocolName(){
    const name=protocol.value;
    return name&&name!=='Loading protocols...'&&name!=='No saved protocols'&&name!=='Could not load protocols'?name:'';
  }
  function nativeFrameId(plot,index){return 'native-plot-'+String(plot.id||index).replace(/[^a-zA-Z0-9_-]/g,'_');}
  function nativeFrame(plot,index){
    const id=nativeFrameId(plot,index);
    let frame=document.getElementById(id);
    if(frame)return frame;
    const section=document.createElement('section');
    section.id=id;
    section.style.cssText='margin:1rem 0 2rem';
    const title=document.createElement('h3');
    title.className='native-plot-title';
    frame=document.createElement('div');
    frame.className='native-plot-frame';
    frame.style.cssText='width:100%;height:420px';
    section.append(title,frame);
    nativeFrames.append(section);
    return frame;
  }
  async function refreshNativePlots(){
    const protocolName=activeProtocolName();
    if(!protocolName){
      nativeCount.textContent='0 CAMELS plot window(s) open.';
      nativeCloseButton.disabled=true;
      nativeFrames.innerHTML='';
      return;
    }
    try{
      const data=await api('plot-snapshots?protocol_name='+encodeURIComponent(protocolName));
      const plots=data.plots||[];
      const count=Number(data.open_plot_count||0);
      nativeCount.textContent=count+' CAMELS plot window(s) open.';
      nativeCloseButton.disabled=count===0;
      nativeMessage.textContent='';
      const activeIds=new Set(plots.map((plot,index)=>nativeFrameId(plot,index)));
      for(const node of [...nativeFrames.children])if(!activeIds.has(node.id))node.remove();
      if(!plots.length){
        if(count)nativeMessage.textContent=' Some open windows do not provide a supported live data snapshot.';
        return;
      }
      const Plotly=await ensureNativePlotly();
      for(const [index,plot] of plots.entries()){
        const frame=nativeFrame(plot,index);
        frame.previousElementSibling.textContent=plot.title||('CAMELS plot '+(index+1));
        let traces,layout;
        if(plot.type==='scatter2d'){
          traces=[{x:plot.x||[],y:plot.y||[],mode:'markers',type:'scattergl',marker:{color:plot.z||[],colorscale:'Viridis',showscale:true,size:8,colorbar:{title:'z'}},name:plot.title||'data'}];
          layout={margin:{t:20,r:65,b:50,l:60},xaxis:{title:plot.x_label||'x'},yaxis:{title:plot.y_label||'y'}};
        }else{
          traces=(plot.traces||[]).map(trace=>({x:trace.x||[],y:trace.y||[],name:trace.name||'value',mode:'lines+markers',type:'scattergl',marker:{size:5},yaxis:trace.y_axis||'y'}));
          layout={margin:{t:20,r:60,b:50,l:60},xaxis:{title:plot.x_label||'x'},yaxis:{title:'value'},yaxis2:{title:'value (right axis)',overlaying:'y',side:'right'},legend:{orientation:'h'}};
        }
        Plotly.react(frame,traces,layout,{responsive:true,displaylogo:false});
      }
    }catch(error){
      nativeMessage.textContent=' Could not load CAMELS plot windows: '+error.message;
    }
  }
  nativeCloseButton.addEventListener('click',async()=>{
    const protocolName=activeProtocolName();
    if(!protocolName)return;
    nativeCloseButton.disabled=true;
    nativeMessage.textContent=' Closing plots...';
    try{
      await api('plots/close?protocol_name='+encodeURIComponent(protocolName),{method:'POST'});
      setTimeout(refreshNativePlots,300);
    }catch(error){
      nativeMessage.textContent=' Could not close plots: '+error.message;
    }
  });
  protocol.addEventListener('change',refreshNativePlots);
  setInterval(refreshNativePlots,1000);
  setTimeout(refreshNativePlots,500);
})();
</script>"""

# A Dash browser plot is launched only when CAMELS starts a run.  Browsers do
# not retry a failed iframe navigation once the Dash port later becomes ready.
# Probe the local port lightly and reload each iframe exactly once on an
# offline-to-online transition; this removes the need for a manual F5 without
# continuously rebuilding a live plot.
DASHBOARD_PAGE += """<script>
(() => {
  const browserPlotAvailability=new Map();
  async function browserPlotReachable(plot){
    try{
      await fetch(plotBaseUrl(plot.port)+'/',{mode:'no-cors',cache:'no-store'});
      return true;
    }catch(error){
      return false;
    }
  }
  async function retryBrowserPlotsWhenReady(){
    for(const plot of browserPlots.filter(item=>item.shown)){
      const key=plotKey(plot),available=await browserPlotReachable(plot),wasAvailable=browserPlotAvailability.get(key);
      browserPlotAvailability.set(key,available);
      const frame=browserPlotFrames.querySelector('iframe[data-browser-plot-key="'+CSS.escape(key)+'"]');
      if(available&&wasAvailable===false&&frame){
        frame.src=plotUrl(plot);
      }
    }
  }
  setInterval(retryBrowserPlotsWhenReady,1500);
  setTimeout(retryBrowserPlotsWhenReady,800);
})();
</script>"""

# Remote CAMELS computers connected through the CAMELS broker.  CAMELS opens an
# outbound connection to OASIS, so these computers need no open port.  The
# browser only sees pairing codes and connection states; device tokens never
# leave the broker.
SETUP_PAGE = SETUP_PAGE.replace(
    "</body></html>",
    """<section id="remote-devices">
<style>
#remote-devices{margin-bottom:1.5rem}
.tip{position:relative;display:inline-flex;align-items:center;justify-content:center;width:1.15rem;height:1.15rem;margin-left:.3rem;border-radius:50%;background:#2850dd;color:#fff;font-size:.75rem;font-weight:700;cursor:help;vertical-align:middle}
.tip .tip-text{visibility:hidden;opacity:0;position:absolute;z-index:10;left:50%;top:1.6rem;transform:translateX(-50%);width:min(24rem,80vw);padding:.7rem .9rem;border-radius:6px;background:#222;color:#fff;font-size:.85rem;font-weight:400;line-height:1.45;text-align:left;box-shadow:0 4px 14px rgba(0,0,0,.25);transition:opacity .15s}
.tip:hover .tip-text,.tip:focus .tip-text{visibility:visible;opacity:1}
.tip-text code{background:#444;color:#fff;border:0;padding:0 .2rem}
.tip-text ol{margin:.3rem 0 0 1.1rem;padding:0}
#remote-address-line label{display:inline;margin:0;font-weight:600}
#remote-address-line input{font-family:Consolas,monospace}
#remote-devices .hint{color:#555;font-size:.85rem;margin:.2rem 0 1rem}
#api-connection{margin-top:1rem;padding-top:.8rem;border-top:1px solid #bbb}
#api-connection>summary{cursor:pointer;color:#555;font-weight:600}
#remote-devices .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
#remote-devices .row input{flex:1;min-width:200px;margin:0}
#remote-devices table{width:100%;border-collapse:collapse;margin:.5rem 0}
#remote-devices th,#remote-devices td{padding:.4rem .5rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:middle}
#remote-devices th{font-size:.9rem;color:#444}
#remote-devices td button{margin:0 .25rem 0 0;padding:.25rem .55rem}
#remote-pairing{border:2px solid #2850dd;border-radius:8px;padding:1rem 1.2rem;margin:1rem 0;background:#f4f7ff}
#remote-pairing .code{font-family:Consolas,monospace;font-size:2rem;font-weight:700;letter-spacing:.15em;user-select:all}
#remote-pairing code{user-select:all;background:#fff;padding:.1rem .3rem;border:1px solid #ccd}
#remote-pairing ol{margin:.5rem 0 0 1.2rem;padding:0}
.remote-online{color:#1a7f37;font-weight:700}.remote-offline{color:#8a8f98}.remote-unpaired{color:#b26b00}
</style>
<h2>Remote CAMELS computers</h2>
<p>CAMELS connects to OASIS by itself, like remote-desktop software. The lab computer needs no open port and no API key. Add a computer, then enter the pairing code in CAMELS.</p>
<div id="remote-address-line" class="row"><label for="remote-address">OASIS address for CAMELS</label>__ADDRESS_TIP__<input id="remote-address" spellcheck="false" autocomplete="off" placeholder="https://&lt;address&gt;/nomad-oasis"><button type="button" id="remote-address-reset" hidden>Use detected</button></div>
<p class="hint" id="remote-address-hint">Detected automatically. Change it if the lab computer reaches OASIS under another address.</p>
<form id="remote-add" class="row"><input id="remote-name" maxlength="100" required placeholder="Name, e.g. Lab PC 1"><button type="submit">Add computer</button></form>
<div id="remote-pairing" hidden></div>
<table><thead><tr><th>Computer</th><th>State</th><th>Host</th><th>CAMELS</th><th>Last seen</th><th>Action</th></tr></thead><tbody id="remote-list"><tr><td colspan="6">Loading...</td></tr></tbody></table>
<pre id="remote-result" hidden></pre>
</section>
<script>
(() => {
  const list=document.getElementById('remote-list'),pairing=document.getElementById('remote-pairing'),result=document.getElementById('remote-result');
  const addressTip=document.querySelector('#remote-address-line .tip').outerHTML;
  const addressInput=document.getElementById('remote-address'),addressReset=document.getElementById('remote-address-reset'),addressHint=document.getElementById('remote-address-hint'),ADDRESS_KEY='camels-remote-address';
  let address='',countdown=null;
  function savedAddress(){try{return localStorage.getItem(ADDRESS_KEY)||''}catch{return ''}}
  function currentAddress(){return addressInput.value.trim().replace(/\\/+$/,'')||address}
  function addressChanged(){
    const value=addressInput.value.trim().replace(/\\/+$/,''),custom=value&&value!==address;
    try{custom?localStorage.setItem(ADDRESS_KEY,value):localStorage.removeItem(ADDRESS_KEY)}catch{}
    addressReset.hidden=!custom;
    addressHint.textContent=custom?'Changed by you. Detected address: '+address:'Detected automatically. Change it if the lab computer reaches OASIS under another address.';
    if(value&&!/^https:\\/\\//.test(value))addressHint.textContent='The address must start with https://';
    const shown=document.getElementById('remote-pairing-address');if(shown)shown.textContent=currentAddress();
  }
  addressInput.addEventListener('input',addressChanged);
  addressReset.onclick=()=>{addressInput.value=address;addressChanged()};
  async function call(path,options={}){
    const r=await fetch('./settings/remote-devices'+path,options),text=await r.text();
    let data;try{data=JSON.parse(text)}catch{data={detail:text}}
    if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));
    return data;
  }
  function say(text,ok=true){result.hidden=false;result.style.color=ok?'':'#c0392b';result.textContent=text}
  function cell(row,content){const td=document.createElement('td');if(content instanceof Node)td.append(content);else td.textContent=content??'-';row.append(td);return td}
  function when(seconds){return seconds?new Date(seconds*1000).toLocaleString():'-'}
  function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e}
  function showPairing(name,data){
    clearInterval(countdown);
    pairing.hidden=false;pairing.innerHTML='';
    pairing.append(el('div','Pairing code for '+name));
    pairing.append(el('div',data.pairing_code,'code'));
    const expiry=el('div');pairing.append(expiry);
    const steps=el('ol');
    const step1=el('li','In CAMELS on the lab computer, open Tools > OASIS Remote Control...');
    const step2=el('li','OASIS address: ');const shown=el('code',currentAddress());shown.id='remote-pairing-address';step2.append(shown);step2.insertAdjacentHTML('beforeend',addressTip);
    const step3=el('li','CA certificate: only needed for a self-signed OASIS certificate.');
    const step4=el('li','Enter the pairing code and select Pair. The state below changes to Online.');
    steps.append(step1,step2,step3,step4);pairing.append(steps);
    function tick(){const left=Math.round(data.pairing_code_expires_at-Date.now()/1000);
      if(left<=0){expiry.textContent='This code has expired. Select New code to create another one.';clearInterval(countdown);return}
      expiry.textContent='Valid once, for '+Math.floor(left/60)+':'+String(left%60).padStart(2,'0')+' min.'}
    tick();countdown=setInterval(tick,1000);
  }
  function confirmButton(label,action){
    const b=el('button',label);b.type='button';
    b.onclick=async()=>{if(b.dataset.confirming!=='true'){b.dataset.confirming='true';b.textContent='Confirm';setTimeout(()=>{b.dataset.confirming='';b.textContent=label},4000);return}await action()};
    return b;
  }
  async function refresh(){
    try{
      const data=await call('');const first=!address;address=data.oasis_address;if(first){addressInput.value=savedAddress()||address;addressChanged()}list.innerHTML='';
      if(!data.devices.length){list.innerHTML='<tr><td colspan="6">No remote computer added.</td></tr>';return}
      for(const d of data.devices){
        const row=document.createElement('tr');
        cell(row,d.name);
        const state=!d.paired?['Waiting for pairing','remote-unpaired']:d.online?['Online','remote-online']:['Offline','remote-offline'];
        cell(row,el('span',state[0],state[1]));
        cell(row,d.hostname);cell(row,d.camels_version);cell(row,d.online?'now':when(d.last_seen));
        const actions=cell(row,'');
        const test=el('button','Test');test.type='button';test.disabled=!d.online;
        test.onclick=async()=>{try{say('Asking '+d.name+' for its protocols ...');const r=await call('/'+encodeURIComponent(d.device_id)+'/test',{method:'POST'});
          if(r.status==='completed'){const names=(r.result&&r.result.Protocols)||[];say(names.length?d.name+' exposes: '+names.join(', '):d.name+' is connected but exposes no protocol. Enable OASIS remote control for a protocol in CAMELS.')}
          else say(d.name+': '+((r.error&&r.error.message)||r.status),false)}catch(e){say(e.message,false)}};
        const code=el('button','New code');code.type='button';
        code.onclick=async()=>{try{showPairing(d.name,await call('/'+encodeURIComponent(d.device_id)+'/pairing-code',{method:'POST'}))}catch(e){say(e.message,false)}};
        const remove=confirmButton('Delete',async()=>{try{await call('/'+encodeURIComponent(d.device_id)+'/delete',{method:'POST'});pairing.hidden=true;say(d.name+' was removed. Its CAMELS can no longer connect.');refresh()}catch(e){say(e.message,false)}});
        actions.append(test,code,remove);
        list.append(row);
      }
    }catch(e){list.innerHTML='';const row=document.createElement('tr');cell(row,e.message).colSpan=6;list.append(row)}
  }
  document.getElementById('remote-add').addEventListener('submit',async e=>{
    e.preventDefault();const input=document.getElementById('remote-name');
    try{const data=await call('',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:input.value})});
      showPairing(data.name,data);input.value='';refresh()}catch(err){say(err.message,false)}
  });
  refresh();setInterval(refresh,5000);
})();
</script></body></html>""",
)

# Hover help for the address that CAMELS needs.  Block-level content is written
# as spans so that it may sit inside the <p> and <li> elements that show it.
REMOTE_ADDRESS_TIP = (
    '<span class="tip" tabindex="0" aria-label="How to find the OASIS address">?'
    '<span class="tip-text" role="tooltip">'
    "<strong>How to find the OASIS address</strong><br>"
    "CAMELS on the lab computer connects to this address, so the lab computer "
    "must be able to reach it."
    '<span style="display:block;margin:.4rem 0 0">'
    "1. On this OASIS computer, open PowerShell and run <code>ipconfig</code>.<br>"
    "2. Take the <em>IPv4 Address</em> of the Ethernet or Wi-Fi adapter on the lab "
    "network, for example <code>10.188.x.x</code>. Do not use <code>127.0.0.1</code> "
    "or a Docker, WSL or vEthernet adapter (often <code>172.x.x.x</code>).<br>"
    "3. On a server, use its host name instead, for example "
    "<code>https://oasis.example.org/nomad-oasis</code>.</span>"
    '<span style="display:block;margin:.4rem 0 0">'
    "The address must be in the HTTPS certificate. To change the proposed address "
    "permanently, set <code>CAMELS_REMOTE_PUBLIC_URL=https://&lt;address&gt;/nomad-oasis</code> "
    "in <code>.env</code> and restart OASIS.</span>"
    '<span style="display:block;margin:.4rem 0 0">'
    "To check, open the address in a browser on the lab computer.</span>"
    "</span></span>"
)
SETUP_PAGE = SETUP_PAGE.replace("__ADDRESS_TIP__", REMOTE_ADDRESS_TIP)

# Remote computers are the main way to connect CAMELS.  Show them first and
# fold the direct API-key connection, and the protocol control that still uses
# it, into a closed section below.
_remote_start = SETUP_PAGE.index('<section id="remote-devices">')
_remote_end = SETUP_PAGE.index("</section>", _remote_start) + len("</section>")
_remote_section = SETUP_PAGE[_remote_start:_remote_end]
_heading = "<h1>CAMELS connections setup</h1>"
_heading_end = SETUP_PAGE.index(_heading) + len(_heading)
SETUP_PAGE = (
    SETUP_PAGE[:_heading_end]
    + _remote_section
    + '<details id="api-connection"><summary>Direct API connection (older method)</summary>'
    + '<p class="hint">Needs an open port and an API key on the lab computer. Use Remote CAMELS computers above instead.</p>'
    + SETUP_PAGE[_heading_end:_remote_start]
    + "</details>"
    + SETUP_PAGE[_remote_end:]
)

# Live plots: CAMELS sends the data of its open plot windows (only new points,
# using a cursor) and the browser draws them with Plotly in the same style.
# Works for direct API connections and for remote computers via the broker.
DASHBOARD_PAGE += """<script>
(() => {
  const protocolSelect=document.getElementById('protocol');
  if(!protocolSelect||document.getElementById('camels-live-plots'))return;
  const section=document.createElement('section');
  section.id='camels-live-plots';
  section.innerHTML='<style>#camels-live-plots .lp-head{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap}#camels-live-plots h2{margin:.6rem 0}#camels-live-plots .lp-frame{width:100%;height:440px;margin:.5rem 0 1.5rem}</style>'+
    '<div class="lp-head"><h2>Live plots</h2><small id="lp-status" class="hint">Choose a protocol.</small><button type="button" id="lp-close">Close plot windows in CAMELS</button></div><div id="lp-frames"></div>';
  const anchor=document.getElementById('browser-plots')||document.getElementById('native-live-plots');
  if(anchor)anchor.parentNode.insertBefore(section,anchor);else document.body.append(section);
  const statusText=document.getElementById('lp-status'),frames=document.getElementById('lp-frames'),closeButton=document.getElementById('lp-close');
  const plots=new Map();
  let source='',loading=false,lastIdleRefresh=0,plotlyPromise=null;
  function selectedProtocol(){
    const name=protocolSelect.value;
    return name&&!/^Loading|^No saved|^Could not load/.test(name)?name:'';
  }
  function ensurePlotly(){
    if(window.Plotly)return Promise.resolve(window.Plotly);
    if(!plotlyPromise)plotlyPromise=new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';
      script.onload=()=>resolve(window.Plotly);
      script.onerror=()=>{plotlyPromise=null;reject(new Error('Could not load Plotly.'))};
      document.head.append(script);
    });
    return plotlyPromise;
  }
  function reset(){plots.clear();frames.innerHTML=''}
  function cursor(){
    const result={};
    for(const [id,plot] of plots)result[id]={generation:plot.generation,since:plot.since,version:plot.version};
    return result;
  }
  function merge(reply){
    let plot=plots.get(reply.id);
    if(!plot||reply.reset){plot={x:[],ys:{},fits:[]};plots.set(reply.id,plot)}
    const before=plot.x.length;
    plot.x=plot.x.concat(reply.x||[]);
    for(const trace of reply.traces||[]){
      const old=plot.ys[trace.name]||new Array(before).fill(null);
      plot.ys[trace.name]=old.concat(trace.y||[]);
    }
    plot.traces=reply.traces||[];
    if(reply.fits)plot.fits=reply.fits;
    plot.meta=reply;
    plot.generation=reply.generation;plot.since=reply.end;plot.version=reply.version;
    plot.dirty=true;
  }
  function axis(title,log,color){
    // pyqtgraph look: axis lines with outside ticks, no grid.
    return {title:{text:title||''},type:log?'log':'linear',showgrid:false,zeroline:false,showline:true,linecolor:color,ticks:'outside',tickcolor:color,mirror:false};
  }
  function figure(plot){
    const meta=plot.meta,fg=meta.foreground||'#000000',bg=meta.background||'#ffffff';
    const type=plot.x.length>5000?'scattergl':'scatter';
    // Plotly fails on fields that are present but undefined: copy only set ones.
    function trace(base,style){
      const result={...base,mode:style.mode||'markers',yaxis:style.axis==='y2'?'y2':'y'};
      for(const key of ['line','marker','visible'])if(style[key]!==undefined&&style[key]!==null)result[key]=style[key];
      return result;
    }
    const data=plot.traces.map(item=>trace({type,name:item.name,x:plot.x,y:plot.ys[item.name]},item));
    for(const fit of plot.fits)data.push(trace({type:'scatter',name:fit.name,x:fit.x,y:fit.y},{mode:'lines',...fit}));
    const hasY2=data.some(trace=>trace.yaxis==='y2');
    const layout={title:{text:meta.title||''},paper_bgcolor:bg,plot_bgcolor:bg,font:{color:fg},
      margin:{t:45,r:hasY2?70:20,b:55,l:70},xaxis:axis(meta.x_label,meta.x_log,fg),yaxis:axis(meta.y_label,meta.y_log,fg),
      legend:{orientation:'h',y:-0.2},uirevision:plot.generation};
    if(hasY2)layout.yaxis2={...axis(meta.y2_label,meta.y2_log,fg),overlaying:'y',side:'right'};
    return {data,layout};
  }
  async function render(){
    const dirty=[...plots.entries()].filter(([,plot])=>plot.dirty);
    if(!dirty.length)return;
    const Plotly=await ensurePlotly();
    for(const [id,plot] of dirty){
      const frameId='lp-'+id.replace(/[^a-zA-Z0-9_-]/g,'_');
      let frame=document.getElementById(frameId);
      if(!frame){frame=document.createElement('div');frame.id=frameId;frame.className='lp-frame';frames.append(frame)}
      const {data,layout}=figure(plot);
      Plotly.react(frame,data,layout,{responsive:true,displaylogo:false});
      plot.dirty=false;
    }
  }
  async function refresh(){
    const name=selectedProtocol();
    if(loading)return;
    if(!name){statusText.textContent='Choose a protocol.';closeButton.disabled=true;reset();return}
    // Poll every second while measuring, otherwise every five seconds.
    const now=Date.now();
    if(window.camelsMeasurementRunning!==true&&source===name&&now-lastIdleRefresh<5000)return;
    lastIdleRefresh=now;
    if(source!==name){reset();source=name}
    loading=true;
    try{
      for(let round=0;round<20;round++){
        const reply=await api('plot-caches?protocol_name='+encodeURIComponent(name)+'&cursor='+encodeURIComponent(JSON.stringify(cursor())));
        if(source!==name)return;
        const open=new Set((reply.plots||[]).map(plot=>plot.id));
        for(const id of [...plots.keys()])if(!open.has(id)){
          plots.delete(id);
          const frame=document.getElementById('lp-'+id.replace(/[^a-zA-Z0-9_-]/g,'_'));
          if(frame)frame.remove();
        }
        (reply.plots||[]).forEach(merge);
        const count=Number(reply.open_plot_count||0);
        statusText.textContent=count?count+' plot window(s) open in CAMELS.':'No plot window open in CAMELS.';
        closeButton.disabled=count===0;
        if(!(reply.plots||[]).some(plot=>plot.more))break;
      }
      await render();
    }catch(error){
      statusText.textContent='Could not load the plots: '+error.message;
    }finally{loading=false}
  }
  closeButton.addEventListener('click',async()=>{
    const name=selectedProtocol();
    if(!name)return;
    if(closeButton.dataset.confirming!=='true'){
      closeButton.dataset.confirming='true';closeButton.textContent='Click again to close them';
      setTimeout(()=>{closeButton.dataset.confirming='';closeButton.textContent='Close plot windows in CAMELS'},4000);
      return;
    }
    closeButton.dataset.confirming='';closeButton.textContent='Close plot windows in CAMELS';
    try{await api('plots/close?protocol_name='+encodeURIComponent(name),{method:'POST'});lastIdleRefresh=0;setTimeout(refresh,500)}
    catch(error){statusText.textContent='Could not close the plots: '+error.message}
  });
  protocolSelect.addEventListener('change',()=>{lastIdleRefresh=0;refresh()});
  document.addEventListener('camels-measurement-state',()=>{lastIdleRefresh=0;refresh()});
  setInterval(refresh,1000);
  setTimeout(refresh,600);
})();
</script>"""


def create_app(
    *,
    camels_base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    settings_path: str,
    encryption_secret: str | None,
) -> FastAPI:
    app = FastAPI(
        title="OASIS CAMELS bridge",
        version="1.1.0",
        description="Authenticated proxy from NOMAD Oasis to CAMELS.",
    )

    @app.exception_handler(HTTPException)
    async def camels_page_authentication_error(request: Request, exc: HTTPException):
        """Keep redirected GUI v2 pages consistent with the Dashboard lock UI.

        The GUI v2 dashboard initially authenticates its own plugin mount, then
        redirects to these bridge pages.  A later logout used to produce raw
        FastAPI JSON here, because this is a separate FastAPI application.
        """
        page_path = request.url.path.rstrip("/")
        if exc.status_code == 401 and page_path.endswith(("/camels/setup", "/camels/dashboard")):
            return HTMLResponse(
                AUTHENTICATION_REQUIRED_PAGE,
                status_code=401,
                headers=exc.headers,
            )
        return Response(
            content=json.dumps({"detail": exc.detail}),
            status_code=exc.status_code,
            media_type="application/json",
            headers=exc.headers,
        )

    store = CredentialStore(settings_path, encryption_secret)
    default_url = camels_base_url.rstrip("/")

    def saved_connections(user_id: str) -> tuple[dict[str, dict[str, str]], str | None]:
        """Return only the encrypted CAMELS credentials belonging to ``user_id``.

        Older bridge versions used one installation-wide credential store.  It is
        deliberately *not* read here: otherwise the first authenticated user
        would gain access to another user's instrument key.
        """
        saved = store.load()
        users = saved.get("users")
        user_settings = users.get(user_id, {}) if isinstance(users, dict) else {}
        connections = user_settings.get("connections")
        if not isinstance(connections, dict):
            return {}, None
        cleaned = {
            connection_id: value
            for connection_id, value in connections.items()
            if isinstance(value, dict) and value.get("api_key")
        }
        return cleaned, user_settings.get("active_connection_id")

    def store_user_connections(
        user_id: str, connections: dict[str, dict[str, str]], active_connection_id: str | None
    ) -> None:
        """Update one user's bucket without exposing legacy global credentials."""
        saved = store.load()
        users = saved.get("users")
        users = dict(users) if isinstance(users, dict) else {}
        users[user_id] = {
            "connections": connections,
            "active_connection_id": active_connection_id,
        }
        payload: dict[str, Any] = {"users": users}
        # Keep existing pre-account data encrypted for an administrator to
        # migrate manually, but never use it for API requests.
        if "connections" in saved or "api_key" in saved:
            payload["legacy_unassigned"] = {
                key: saved[key]
                for key in ("connections", "active_connection_id", "api_key", "name", "camels_base_url")
                if key in saved
            }
        store.save(payload)

    def connection_summary(user_id: str) -> dict[str, Any]:
        connections, active_connection_id = saved_connections(user_id)
        return {
            "connections": [
                {
                    "connection_id": connection_id,
                    "name": value.get("name") or connection_id,
                    "camels_base_url": value.get("camels_base_url", default_url),
                    "service_info": value.get("service_info", {}),
                }
                for connection_id, value in connections.items()
            ],
            "active_connection_id": active_connection_id,
        }

    def current_connection(user_id: str, connection_id: str | None = None) -> tuple[str, str]:
        if connection_id and connection_id.startswith(remote.CONNECTION_PREFIX):
            device_id = connection_id[len(remote.CONNECTION_PREFIX):]
            remote.owned_device(user_id, device_id)
            return remote.connection(device_id, user_id)
        connections, active_connection_id = saved_connections(user_id)
        selected_id = connection_id or active_connection_id
        selected = connections.get(selected_id) if selected_id else None
        if selected:
            return selected.get("camels_base_url", default_url).rstrip("/"), selected["api_key"]
        raise HTTPException(
            status_code=503,
            detail="No CAMELS connection is saved for this OASIS account. Open /camels/setup and save a connection.",
        )

    def persist_connection(user_id: str, settings: ConnectionSettings) -> dict[str, Any]:
        connections, active_connection_id = saved_connections(user_id)
        connection_id = settings.connection_id if settings.connection_id in connections else uuid.uuid4().hex
        connections[connection_id] = {
            "name": settings.name.strip() or settings.camels_base_url.rstrip("/"),
            "camels_base_url": settings.camels_base_url.rstrip("/"),
            "api_key": settings.api_key,
        }
        store_user_connections(user_id, connections, connection_id or active_connection_id)
        return {"connection_id": connection_id, **connection_summary(user_id)}

    def save_protocol_connections(
        user_id: str,
        settings: ConnectionSettings,
        protocol_response: dict[str, Any],
        service_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Store one global CAMELS key for all protocols exposed by CAMELS.

        CAMELS is the authority for exposure: its per-protocol
        ``oasis_remote_control`` checkbox determines what this key can list or
        control. OASIS stores the credential once instead of duplicating the
        same secret for every returned protocol.
        """
        protocol_names = [
            str(name)
            for name in protocol_response.get("Protocols", [])
            if isinstance(name, str) and name
        ]
        connections, _ = saved_connections(user_id)
        # Replace the prior global key for this CAMELS server. Protocol-scoped
        # records from older bridge versions remain available for migration.
        connections = {
            connection_id: value
            for connection_id, value in connections.items()
            if value.get("protocol_name")
            or value.get("camels_base_url", "").rstrip("/")
            != settings.camels_base_url.rstrip("/")
        }
        connection_id = uuid.uuid4().hex
        hostname = str(service_info.get("hostname") or "CAMELS")
        connections[connection_id] = {
            "name": f"{hostname} ({settings.camels_base_url.rstrip('/')})",
            "camels_base_url": settings.camels_base_url.rstrip("/"),
            "api_key": settings.api_key,
            "service_info": service_info,
        }
        store_user_connections(user_id, connections, connection_id)
        summary = connection_summary(user_id)
        summary["message"] = (
            "Connection saved. This CAMELS connection does not currently expose any protocol. "
            "Open the protocol in CAMELS and enable 'Expose this protocol to OASIS remote control', then refresh this page."
            if not protocol_names
            else f"Connection saved. {len(protocol_names)} exposed protocol(s) are available."
        )
        summary["exposed_protocol_count"] = len(protocol_names)
        return summary

    def protocol_catalog(user_id: str) -> tuple[list[str], dict[str, str]]:
        """Return all saved protocol names and the credential that owns each."""
        connections, _ = saved_connections(user_id)
        names: list[str] = []
        connection_ids: dict[str, str] = {}
        # Remote computers connected through the broker come first: they need
        # no API key and no open port on the lab computer.
        for device_id, device_protocols in remote.protocol_catalog(user_id):
            for discovered_name in device_protocols:
                if discovered_name not in connection_ids:
                    names.append(discovered_name)
                    connection_ids[discovered_name] = remote.CONNECTION_PREFIX + device_id
        # The current setup stores one CAMELS connection which discovers every
        # exposed protocol.  Older bridge releases stored protocol-scoped
        # records as well.  Query current connections first so an obsolete
        # per-protocol key cannot shadow a newly saved, working connection.
        ordered_connections = [
            (connection_id, value)
            for connection_id, value in connections.items()
            if not value.get("protocol_name")
        ] + [
            (connection_id, value)
            for connection_id, value in connections.items()
            if value.get("protocol_name")
        ]
        for connection_id, value in ordered_connections:
            protocol_name = value.get("protocol_name")
            if protocol_name:
                if protocol_name not in connection_ids:
                    names.append(protocol_name)
                connection_ids[protocol_name] = connection_id
                continue
            # CAMELS lists only the protocols whose "Expose this protocol to
            # OASIS remote control" checkbox is enabled.
            try:
                response = request_camels(
                    "GET",
                    "/api/v1/protocols",
                    connection=(value.get("camels_base_url", default_url).rstrip("/"), value["api_key"]),
                )
            except HTTPException:
                # An offline CAMELS or an outdated key must not hide the
                # protocols of the other connections.
                continue
            for discovered_name in response.get("Protocols", []):
                if discovered_name not in connection_ids:
                    names.append(discovered_name)
                    connection_ids[discovered_name] = connection_id
        return names, connection_ids

    def connection_for_protocol(user_id: str, protocol_name: str) -> tuple[str, str]:
        _, connection_ids = protocol_catalog(user_id)
        connection_id = connection_ids.get(protocol_name)
        if not connection_id:
            raise HTTPException(
                status_code=404,
                detail="This protocol is not available on a connected CAMELS computer.",
            )
        return current_connection(user_id, connection_id)

    def request_camels(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        connection: tuple[str, str] | None = None,
        require_key: bool = True,
    ) -> Any:
        if connection is None:
            if require_key:
                raise HTTPException(status_code=500, detail="A user CAMELS connection is required.")
            connection = (default_url, "")
        base_url, key = connection
        device_id = remote.device_id_of(base_url)
        if device_id is not None:
            # Through the broker; ``key`` holds the requesting user here.
            return remote.call(device_id, method, path, payload, key or None)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = UrlRequest(f"{base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read()
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                error_payload = {"detail": "CAMELS returned a non-JSON error."}
            detail = error_payload.get("detail", error_payload)
            if not isinstance(detail, str):
                detail = json.dumps(detail)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
        except URLError as exc:
            raise HTTPException(502, "CAMELS service is unavailable.") from exc
        try:
            return json.loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"detail": "CAMELS returned a non-JSON response."}

    write_user = get_current_user([Scope.ENTRIES_WRITE], allow_anonymous=False)
    read_user = get_current_user([Scope.ENTRIES_READ], allow_anonymous=False)

    @app.post("/logout", status_code=204)
    def clear_browser_authentication() -> Response:
        """Clear browser authentication state before GUI v2 leaves for Keycloak.

        This endpoint intentionally requires no user dependency: it must also
        work when an expired or half-logged-out browser is trying to discard
        stale state.  It never reads or changes server-side CAMELS connections.
        """
        response = Response(status_code=204)
        response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        # Fallback for clients without Clear-Site-Data support. The browser
        # frontend removes these paths too, covering cookies written by older
        # GUI versions.
        for cookie_path in (
            "/",
            "/nomad-oasis",
            "/nomad-oasis/api/v1",
            "/nomad-oasis/gui",
            "/nomad-oasis/gui/v2",
        ):
            response.delete_cookie("Authorization", path=cookie_path)
        return response

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(_user=Depends(read_user)):
        return SETUP_PAGE

    @app.get("/settings/connection")
    def connection_status(user=Depends(write_user)):
        return {"configured": bool(saved_connections(user.user_id)[0]), **connection_summary(user.user_id)}

    @app.get("/settings/connections")
    def list_connections(user=Depends(read_user)):
        return {"connections": connection_summary(user.user_id)["connections"]}

    @app.post("/settings/connection/test")
    def test_connection(settings: ConnectionSettings, _user=Depends(write_user)):
        return request_camels(
            "GET",
            "/api/v1/protocols",
            connection=(settings.camels_base_url.rstrip("/"), settings.api_key),
        )

    @app.post("/settings/connection")
    def save_connection(settings: ConnectionSettings, user=Depends(write_user)):
        protocols = request_camels(
            "GET",
            "/api/v1/protocols",
            connection=(settings.camels_base_url.rstrip("/"), settings.api_key),
        )
        try:
            service_info = request_camels(
                "GET",
                "/api/v1/info",
                connection=(settings.camels_base_url.rstrip("/"), settings.api_key),
            )
        except HTTPException as exc:
            # Existing CAMELS installations can still be linked; after CAMELS
            # is restarted with the new endpoint, re-saving enriches the card.
            if exc.status_code != 404:
                raise
            service_info = {}
        saved_connection = save_protocol_connections(
            user.user_id, settings, protocols, service_info
        )
        return {"saved": True, "protocols": protocols, **saved_connection}

    @app.post("/settings/connections/{connection_id}/delete")
    def delete_connection(connection_id: str, user=Depends(write_user)):
        connections, active_connection_id = saved_connections(user.user_id)
        if connection_id not in connections:
            raise HTTPException(status_code=404, detail="CAMELS connection not found.")
        del connections[connection_id]
        if active_connection_id == connection_id:
            active_connection_id = next(iter(connections), None)
        store_user_connections(user.user_id, connections, active_connection_id)
        return connection_summary(user.user_id)

    @app.post("/settings/connections/delete")
    def delete_all_connections(user=Depends(write_user)):
        """Remove only the current user's encrypted protocol keys."""
        store_user_connections(user.user_id, {}, None)
        return connection_summary(user.user_id)

    # ------------------------------------------------------------------
    # Remote CAMELS computers (outbound connection through the broker)
    # ------------------------------------------------------------------
    broker_request = remote.broker_request
    owned_device = remote.owned_device

    def remote_address(request: Request) -> str:
        # 1. Set by the administrator, 2. the host's network address detected
        # by the start script, 3. the address this page was opened with.
        configured = os.environ.get("CAMELS_REMOTE_PUBLIC_URL", "").strip()
        if configured:
            return configured.rstrip("/")
        path = request.url.path
        prefix = path.split("/camels/")[0] if "/camels/" in path else ""
        host = os.environ.get("CAMELS_REMOTE_DETECTED_HOST", "").strip()
        if not host:
            host = request.headers.get("host", "localhost").split(":")[0]
        return f"https://{host}{prefix}"

    def device_view(state: dict[str, Any]) -> dict[str, Any]:
        agent = state.get("agent") or {}
        return {
            "device_id": state.get("device_id"),
            "name": state.get("name"),
            "paired": bool(state.get("paired")),
            "online": bool(state.get("online")),
            "hostname": state.get("hostname") or agent.get("hostname"),
            "camels_version": agent.get("camels_version"),
            "last_seen": state.get("last_seen"),
        }

    @app.get("/settings/remote-devices")
    def list_remote_devices(request: Request, user=Depends(write_user)):
        data = broker_request("GET", f"/devices?owner={quote(user.user_id, safe='')}")
        return {
            "oasis_address": remote_address(request),
            "devices": [device_view(state) for state in data.get("devices", [])],
        }

    @app.post("/settings/remote-devices")
    def add_remote_device(body: RemoteDeviceCreate, request: Request, user=Depends(write_user)):
        created = broker_request("POST", "/devices", {"name": body.name.strip(), "owner": user.user_id})
        return {
            **device_view(created),
            "pairing_code": created["pairing_code"],
            "pairing_code_expires_at": created["pairing_code_expires_at"],
            "oasis_address": remote_address(request),
        }

    @app.post("/settings/remote-devices/{device_id}/pairing-code")
    def new_remote_pairing_code(device_id: str, request: Request, user=Depends(write_user)):
        owned_device(user.user_id, device_id)
        created = broker_request("POST", f"/devices/{quote(device_id, safe='')}/pairing-code")
        return {**created, "oasis_address": remote_address(request)}

    @app.post("/settings/remote-devices/{device_id}/delete")
    def delete_remote_device(device_id: str, user=Depends(write_user)):
        owned_device(user.user_id, device_id)
        return broker_request("DELETE", f"/devices/{quote(device_id, safe='')}")

    @app.post("/settings/remote-devices/{device_id}/test")
    def test_remote_device(device_id: str, user=Depends(write_user)):
        owned_device(user.user_id, device_id)
        return broker_request(
            "POST",
            f"/devices/{quote(device_id, safe='')}/commands",
            {"operation": "list_protocols", "requested_by": user.user_id, "timeout_seconds": 10},
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page(_user=Depends(read_user)):
        return DASHBOARD_PAGE

    @app.get("/dashboard/connections")
    def dashboard_connections(user=Depends(read_user)):
        return connection_summary(user.user_id)

    @app.get("/dashboard/protocols")
    def dashboard_protocols(connection_id: str | None = None, user=Depends(read_user)):
        protocol_names, connection_ids = protocol_catalog(user.user_id)
        connections, _ = saved_connections(user.user_id)
        return {
            "Protocols": protocol_names,
            "connection_ids": connection_ids,
            # This is intentionally only the API host address. The browser
            # needs it to embed a browser plot, while the secret stays in the
            # encrypted server-side connection record.
            "connection_base_urls": {
                name: connections[connection_ids[name]].get(
                    "camels_base_url", default_url
                )
                for name in protocol_names
                if connection_ids.get(name) in connections
            },
        }

    @app.get("/dashboard/protocols/{protocol_name}/schema")
    def dashboard_protocol_schema(protocol_name: str, connection_id: str | None = None, user=Depends(read_user)):
        return request_camels(
            "GET", f"/api/v1/protocols/{protocol_name}/schema", connection=connection_for_protocol(user.user_id, protocol_name)
        )

    @app.get("/dashboard/protocols/{protocol_name}/definition")
    def dashboard_protocol_definition(protocol_name: str, connection_id: str | None = None, user=Depends(read_user)):
        return request_camels(
            "GET", f"/api/v1/protocols/{protocol_name}/JSON", connection=connection_for_protocol(user.user_id, protocol_name)
        )

    @app.post("/dashboard/protocols/{protocol_name}/parameters")
    def dashboard_save_protocol_parameters(
        protocol_name: str, parameters: ProtocolParameters, user=Depends(write_user)
    ):
        """Persist form values as defaults in the selected CAMELS protocol."""
        return request_camels(
            "POST",
            f"/api/v1/protocols/{quote(protocol_name)}/parameters",
            payload={"variables": parameters.variables},
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.post("/dashboard/runs")
    def dashboard_start_run(request: RunRequest, user=Depends(write_user)):
        connection_ids = protocol_catalog(user.user_id)[1]
        response = request_camels(
            "POST",
            f"/api/v1/actions/run/protocols/{request.protocol_name}",
            payload={"variables": request.variables},
            connection=connection_for_protocol(user.user_id, request.protocol_name),
        )
        return {**response, "connection_id": connection_ids[request.protocol_name]}

    @app.post("/dashboard/actions")
    def dashboard_start_action(
        request: ActionRequest, user=Depends(write_user)
    ):
        """Create a real NOMAD Action record and immediately start its workflow."""
        from nomad.actions.manager import start_action
        from nomad_camels_oasis.actions.models import CAMELSMeasurementInput

        # Check the protocol/key mapping before scheduling the asynchronous
        # workflow, so invalid dashboard requests fail immediately.
        connection = connection_for_protocol(user.user_id, request.protocol_name)
        # Give the reason at once instead of a failed Action.
        try:
            state = request_camels("GET", "/api/v1/gui/measurement-status", connection=connection)
        except HTTPException:
            state = {}
        if state.get("remote_control_paused") is True:
            raise HTTPException(423, "Remote control is paused in CAMELS on the lab computer.")
        if str(state.get("status", "")).lower() in ("preparing", "running"):
            raise HTTPException(409, "Another measurement is running in CAMELS.")
        # The run id is fixed here, so a retried start command of the
        # workflow is recognised by CAMELS and starts nothing twice.
        run_id = str(uuid.uuid4())
        workflow_id = start_action(
            action_id="nomad_camels_oasis.entry_points:camels_measurement_action",
            data=CAMELSMeasurementInput(
                user_id=user.user_id,
                protocol_name=request.protocol_name,
                variables=request.variables,
                run_id=run_id,
            ),
        )
        return {"workflow_id": workflow_id, "status": "PENDING", "run_id": run_id}

    @app.get("/dashboard/actions/{workflow_id}")
    def dashboard_action_status(workflow_id: str, user=Depends(read_user)):
        """Return the status of the user-owned Action created by the dashboard."""
        from nomad.actions.manager import get_action_status

        return {
            "workflow_id": workflow_id,
            "status": get_action_status(workflow_id, user.user_id).name,
        }

    @app.get("/dashboard/runs/{run_id}")
    def dashboard_run_status(run_id: str, connection_id: str | None = None, user=Depends(read_user)):
        return request_camels(
            "GET", f"/api/v1/protocols/results/{run_id}", connection=current_connection(user.user_id, connection_id)
        )

    @app.get("/dashboard/plots")
    def dashboard_plot_windows(protocol_name: str, user=Depends(read_user)):
        """Return the live CAMELS GUI plot-window count for one saved protocol key."""
        return request_camels(
            "GET",
            "/api/v1/gui/plots",
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.get("/dashboard/plot-caches")
    def dashboard_plot_caches(protocol_name: str, cursor: str | None = None, user=Depends(read_user)):
        """Proxy only Qt-independent 1D plot caches from CAMELS.

        ``cursor`` (JSON) is the browser's position in each plot; then only
        new points are transferred.
        """
        path = "/api/v1/gui/plot-caches"
        if cursor:
            path += "?cursor=" + quote(cursor, safe="")
        return request_camels(
            "GET",
            path,
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.post("/dashboard/stop")
    def dashboard_stop_measurement(protocol_name: str, user=Depends(write_user)):
        """Stop the running measurement on the CAMELS computer of a protocol."""
        return request_camels(
            "POST",
            "/api/v1/actions/stop",
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.get("/dashboard/measurement-status")
    def dashboard_measurement_status(protocol_name: str, user=Depends(read_user)):
        """Proxy CAMELS' thread-safe main-window progress snapshot."""
        return request_camels(
            "GET",
            "/api/v1/gui/measurement-status",
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.api_route(
        "/dashboard/browser-plots/{protocol_name}/{port}/{plot_path:path}",
        methods=["GET", "POST"],
    )
    async def dashboard_browser_plot_proxy(
        protocol_name: str,
        port: int,
        plot_path: str,
        request: Request,
        user=Depends(read_user),
    ):
        """Serve a CAMELS localhost Dash plot through two authenticated proxies."""
        base_url, api_key = connection_for_protocol(user.user_id, protocol_name)
        if remote.device_id_of(base_url) is not None:
            raise HTTPException(
                status_code=501,
                detail="Browser plots are not available for remote computers; use Live plots.",
            )
        remote_path = (
            f"/api/v1/gui/browser-plots/{quote(protocol_name, safe='')}/"
            f"{port}/{plot_path.lstrip('/')}"
        )
        target = f"{base_url}{remote_path}"
        query = request.url.query
        if plot_path.strip("/") == "data":
            # Ask CAMELS to downsample before the response crosses the network.
            query = "&".join(
                value
                for value in (query, f"max_points={BROWSER_PLOT_MAX_POINTS}")
                if value
            )
        if query:
            target += "?" + query
        headers = {"Authorization": f"Bearer {api_key}"}
        if content_type := request.headers.get("content-type"):
            headers["Content-Type"] = content_type
        body = await request.body()
        remote_request = UrlRequest(
            target,
            data=body if body else None,
            headers=headers,
            method=request.method,
        )
        try:
            with urlopen(remote_request, timeout=timeout_seconds) as remote_response:
                content = remote_response.read()
                media_type = remote_response.headers.get_content_type()
                status_code = remote_response.status
        except HTTPError as error:
            content = error.read()
            media_type = error.headers.get_content_type()
            status_code = error.code
        except URLError as error:
            raise HTTPException(
                status_code=502,
                detail="CAMELS browser plot service is unavailable.",
            ) from error

        # The Dashboard requests only the Dash /data snapshot. Apply a hard
        # output bound before it reaches the user's browser. This is defence in
        # depth in addition to the JavaScript rendering limit.
        if plot_path.strip("/") == "data" and media_type == "application/json" and status_code < 400:
            try:
                content = json.dumps(_limit_browser_plot_payload(json.loads(content))).encode("utf-8")
            except (TypeError, ValueError, json.JSONDecodeError):
                # Keep the original response if a third-party plot has a
                # non-standard /data payload.
                pass

        if media_type == "text/html" and status_code < 400:
            # Dash emits root-relative component URLs. Rewrite its base and
            # request prefix so all assets/callbacks remain inside this OASIS
            # route instead of pointing at the user's localhost.
            prefix = (
                request.scope.get("root_path", "").rstrip("/")
                + f"/dashboard/browser-plots/{quote(protocol_name, safe='')}/{port}"
            )
            page = content.decode("utf-8", errors="replace")
            page = page.replace("<head>", f'<head><base href="{prefix}/">', 1)
            page = page.replace('src="/', f'src="{prefix}/')
            page = page.replace('href="/', f'href="{prefix}/')
            page = page.replace(
                '"requests_pathname_prefix":"/"',
                f'"requests_pathname_prefix":"{prefix}/"',
            )
            # Dash serializes the default slash as ``\/`` in its JSON config.
            # Without this variant its renderer requests ``/_dash-layout``
            # from OASIS' root rather than from the authenticated proxy.
            page = page.replace(
                '"requests_pathname_prefix":"\\/"',
                f'"requests_pathname_prefix":"{prefix}/"',
            )
            content = page.encode("utf-8")
        return Response(content=content, status_code=status_code, media_type=media_type)

    @app.get("/dashboard/active-measurement")
    def dashboard_active_measurement(user=Depends(read_user)):
        """Find the currently running exposed protocol, if CAMELS has one."""
        protocol_names, connection_ids = protocol_catalog(user.user_id)
        available_protocols = set(protocol_names)
        checked_connections: set[str] = set()
        for connection_id in connection_ids.values():
            if connection_id in checked_connections:
                continue
            checked_connections.add(connection_id)
            try:
                snapshot = request_camels(
                    "GET",
                    "/api/v1/gui/measurement-status",
                    connection=current_connection(user.user_id, connection_id),
                )
            except HTTPException:
                # One offline CAMELS computer must not prevent the Dashboard
                # from opening connections that are online.
                continue
            status = str(snapshot.get("status", "")).lower()
            name = str(snapshot.get("protocol_name") or "")
            if (
                name in available_protocols
                and any(word in status for word in ("running", "preparing", "queued"))
            ):
                return {"protocol_name": name, "connection_id": connection_id}
        return {"protocol_name": None}

    @app.post("/dashboard/plots/close")
    def dashboard_close_plot_windows(protocol_name: str, user=Depends(write_user)):
        """Request CAMELS to perform its main-window Close plots action."""
        return request_camels(
            "POST",
            "/api/v1/gui/plots/close",
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.get("/dashboard/pid-plots")
    def dashboard_pid_plots(protocol_name: str, user=Depends(read_user)):
        """Proxy the active PID controller traces for the selected protocol."""
        return request_camels(
            "GET",
            f"/api/v1/gui/pid-plots?protocol_name={quote(protocol_name)}",
            connection=connection_for_protocol(user.user_id, protocol_name),
        )

    @app.get("/health")
    def health(connection_id: str | None = None, user=Depends(read_user)):
        url, _ = current_connection(user.user_id, connection_id)
        return request_camels(
            "GET",
            "/api/v1/health",
            connection=(url, ""),
            require_key=False,
        )

    @app.get("/protocols")
    def list_protocols(user=Depends(read_user)):
        protocol_names, _ = protocol_catalog(user.user_id)
        return {"Protocols": protocol_names}

    @app.get("/protocols/{protocol_name}/schema")
    def get_protocol_schema(protocol_name: str, user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/{protocol_name}/schema", connection=connection_for_protocol(user.user_id, protocol_name))

    @app.get("/protocols/{protocol_name}/definition")
    def get_protocol_definition(protocol_name: str, user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/{protocol_name}/JSON", connection=connection_for_protocol(user.user_id, protocol_name))

    @app.post("/runs")
    def start_run(request: RunRequest, user=Depends(write_user)):
        return request_camels(
            "POST",
            f"/api/v1/actions/run/protocols/{request.protocol_name}",
            payload={"variables": request.variables},
            connection=connection_for_protocol(user.user_id, request.protocol_name),
        )

    @app.get("/runs/{run_id}")
    def get_run_status(run_id: str, user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/results/{run_id}", connection=current_connection(user.user_id))

    return app

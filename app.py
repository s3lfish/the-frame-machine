#!/usr/bin/env python3
"""
app.py — a tiny local web control panel for The Frame Machine.

Open it from any phone or laptop on your network (e.g. http://<host>.local:8080)
to choose what art shows, whether captions are real / made-up / off, how often the
art changes and when — plus Preview and "Change the art now" buttons.

It reads and writes ~/.config/frame/config.json (the same file frame_push.py reads
for its defaults) and, on macOS, manages the launchd schedule from the frequency/time
you pick. Run it with:  python3 app.py   (add --port 8080 to change the port)
"""

import argparse, json, os, platform, subprocess, sys, tempfile, time
from flask import Flask, request, jsonify, send_file, render_template_string

import frame_push as fp   # reuse CONFIG path, DEFAULTS, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "frame_push.py")
PYTHON = sys.executable
PREVIEW_PATH = os.path.join(tempfile.gettempdir(), "frame_preview.jpg")
LABEL = "com.frameart.daily"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = os.path.expanduser("~/Library/Logs/frameart.log")

app = Flask(__name__)

# ---------- config helpers ----------
def save_config(updates):
    cfg = fp.load_config()
    cfg.update(updates)
    os.makedirs(fp.CFG, exist_ok=True)
    with open(fp.CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg

def flags_from(cfg):
    """Turn a settings dict into frame_push.py CLI flags (so preview/now match the page)."""
    f = ["--theme", cfg["content"], "--mat", cfg["mat"], "--describe", cfg["description"]]
    if cfg.get("all_types"):
        f += ["--all-types"]
    else:
        f += ["--no-all-types"]
        if cfg.get("types"):
            f += ["--types", ",".join(cfg["types"])]
    f += ["--placard"] if cfg.get("placard", True) else ["--no-placard"]
    f += ["--qr"] if cfg.get("qr", True) else ["--no-qr"]
    f += ["--replace"] if cfg.get("replace", True) else ["--no-replace"]
    f += ["--fetch", str(cfg.get("fetch", 1))]
    if cfg.get("mac"):
        f += ["--mac", cfg["mac"]]
    return f

# ---------- schedule (macOS launchd) ----------
# frequency -> the hour offsets (from the anchor time) at which to fire each day
FREQ_OFFSETS = {"daily": [0], "twice-daily": [0, 12], "every-8h": [0, 8, 16], "every-6h": [0, 6, 12, 18]}

def _calendar_intervals(hh, mm, freq):
    return [{"Hour": (hh + off) % 24, "Minute": mm} for off in FREQ_OFFSETS.get(freq, [0])]

def write_schedule(cfg):
    """Regenerate + reload the launchd job from frequency/time. macOS only."""
    if platform.system() != "Darwin":
        return "Settings saved. (Auto-scheduling is macOS-only — set a cron job on this OS; see README.)"
    try:
        hh, mm = map(int, cfg.get("time", "07:30").split(":"))
    except Exception:
        hh, mm = 7, 30
    intervals = _calendar_intervals(hh, mm, cfg.get("frequency", "daily"))
    ivals = "".join(
        "<dict>" + "".join(f"<key>{k}</key><integer>{v}</integer>" for k, v in iv.items()) + "</dict>"
        for iv in intervals)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{PYTHON}</string><string>{SCRIPT}</string></array>
  <key>StartCalendarInterval</key><array>{ivals}</array>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
  <key>RunAtLoad</key><false/>
</dict></plist>
"""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], capture_output=True, text=True)
    when = ", ".join(f"{iv['Hour']:02d}:{iv['Minute']:02d}" for iv in intervals)
    if r.returncode != 0:
        return f"Settings saved, but scheduling failed: {r.stderr.strip()[:160]}"
    return f"Saved. The art will change daily at {when}."

# ---------- routes ----------
@app.route("/")
def index():
    return render_template_string(PAGE, cfg=fp.load_config(), types=list(fp.TYPE_FILTERS))

@app.route("/save", methods=["POST"])
def save():
    cfg = save_config(request.get_json(force=True))
    return jsonify(ok=True, message=write_schedule(cfg))

@app.route("/preview", methods=["POST"])
def preview():
    cfg = {**fp.load_config(), **request.get_json(force=True)}
    cmd = [PYTHON, SCRIPT, "--preview", PREVIEW_PATH] + flags_from(cfg)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if os.path.exists(PREVIEW_PATH) and r.returncode == 0:
        return jsonify(ok=True, image=f"/preview.jpg?t={int(time.time())}")
    return jsonify(ok=False, message=(r.stderr or r.stdout or "preview failed").strip()[-300:])

@app.route("/preview.jpg")
def preview_jpg():
    return send_file(PREVIEW_PATH, mimetype="image/jpeg")

@app.route("/change-now", methods=["POST"])
def change_now():
    cfg = {**fp.load_config(), **request.get_json(force=True)}
    cmd = [PYTHON, SCRIPT] + flags_from(cfg)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    if r.returncode == 0:
        return jsonify(ok=True, message="Done — " + " / ".join(tail))
    return jsonify(ok=False, message=(r.stderr or r.stdout or "failed").strip()[-300:])

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Frame Machine</title>
<style>
 :root{--bg:#1c1c1e;--panel:#262629;--ink:#ece9e2;--sub:#a8a49b;--line:#3a3a3d;--accent:#c9a24a}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
   font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:24px}
 .wrap{max-width:640px;margin:0 auto}
 h1{font-weight:700;font-size:24px;margin:0 0 4px} .muted{color:var(--sub);margin:0 0 24px;font-size:14px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px}
 label.f{display:block;font-weight:600;margin:0 0 8px} .sub{color:var(--sub);font-size:13px;font-weight:400}
 .seg{display:flex;gap:8px;flex-wrap:wrap} .seg button{flex:1;min-width:90px;background:#303033;color:var(--ink);
   border:1px solid var(--line);border-radius:10px;padding:10px;cursor:pointer;font-size:14px}
 .seg button.on{background:var(--accent);color:#1c1c1e;border-color:var(--accent);font-weight:600}
 select,input[type=time]{width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);
   border-radius:10px;padding:10px;font-size:15px}
 .row{display:flex;gap:14px;flex-wrap:wrap} .row>div{flex:1;min-width:160px}
 .chk{display:flex;align-items:center;gap:10px;cursor:pointer} .chk input{width:20px;height:20px}
 .typegrid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin-top:12px;
   padding-top:12px;border-top:1px solid var(--line)} .typegrid.hidden{display:none}
 .tk input{width:18px;height:18px} .tk span{font-size:14px}
 .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}
 .actions button{flex:1;min-width:130px;border-radius:10px;padding:13px;font-size:15px;font-weight:600;cursor:pointer;border:1px solid var(--line)}
 #save{background:#303033;color:var(--ink)} #prev{background:#303033;color:var(--ink)}
 #now{background:var(--accent);color:#1c1c1e;border-color:var(--accent)}
 #status{min-height:20px;margin:12px 2px;color:var(--sub);font-size:14px}
 img#pv{width:100%;border-radius:12px;margin-top:12px;display:none;border:1px solid var(--line)}
 details summary{cursor:pointer;color:var(--sub)} details input{margin-top:8px}
</style></head><body><div class="wrap">
<h1>The Frame Machine</h1>
<p class="muted">Choose what your Frame shows, and when it changes.</p>

<div class="card">
 <label class="f">Caption <span class="sub">— the story under each piece</span></label>
 <div class="seg" id="description">
   <button data-v="off">None</button>
   <button data-v="real">Real Met caption</button>
   <button data-v="made-up">Made-up tale</button>
 </div>
 <label class="chk" style="margin-top:14px"><input type="checkbox" id="qr">
   <span>Show a QR code <span class="sub">— links to the real Met page for this piece</span></span></label>
</div>

<div class="card">
 <label class="f">Content <span class="sub">— what art to pull</span></label>
 <select id="content">
   <option value="museum">Whole museum — a random surprise</option>
   <option value="cycle">Genre cycle — a different style each day</option>
   <option value="impressionist">Impressionist</option>
   <option value="ukiyo-e">Japanese woodblock (ukiyo-e)</option>
   <option value="old-masters">Old Masters</option>
   <option value="landscape">Landscapes</option>
   <option value="mix">Mixed classics</option>
 </select>
 <label class="chk" style="margin-top:14px"><input type="checkbox" id="all_types">
   <span>All object types <span class="sub">— everything the museum has</span></span></label>
 <div id="typegrid" class="typegrid">
   {% for t in types %}<label class="chk tk"><input type="checkbox" class="tcheck" data-type="{{t}}"><span>{{t}}</span></label>{% endfor %}
 </div>
</div>

<div class="card">
 <div class="row">
   <div><label class="f">Change how often</label>
     <select id="frequency">
       <option value="daily">Once a day</option>
       <option value="twice-daily">Twice a day</option>
       <option value="every-8h">Every 8 hours</option>
       <option value="every-6h">Every 6 hours</option>
     </select></div>
   <div><label class="f">At (first) time</label><input type="time" id="time"></div>
 </div>
 <div class="row" style="margin-top:14px">
   <div><label class="f">Mat colour</label>
     <select id="mat"><option value="off_white">Off-white</option><option value="linen">Linen</option>
       <option value="charcoal">Charcoal</option><option value="black">Black</option></select></div>
 </div>
</div>

<div class="actions">
 <button id="save">Save settings</button>
 <button id="prev">Preview</button>
 <button id="now">Change the art now</button>
</div>
<div id="status"></div>
<img id="pv">

<div class="card" style="margin-top:16px"><details><summary>Advanced</summary>
 <label class="f" style="margin-top:12px">TV wireless MAC address</label>
 <input type="text" id="mac" placeholder="AA:BB:CC:DD:EE:FF" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px">
 <p class="sub" style="margin-top:8px">Found on the TV under About This TV, or your router. Needed to find and control the Frame.</p>
</details></div>
</div>

<script>
const cfg = {{ cfg|tojson }};
const $ = id => document.getElementById(id);
const el = {content:$('content'), all_types:$('all_types'), typegrid:$('typegrid'), qr:$('qr'),
  frequency:$('frequency'), time:$('time'), mat:$('mat'), mac:$('mac'), status:$('status'), pv:$('pv'),
  save:$('save'), prev:$('prev'), now:$('now')};
function setSeg(val){document.querySelectorAll('#description button').forEach(b=>b.classList.toggle('on',b.dataset.v===val));}
document.querySelectorAll('#description button').forEach(b=>b.onclick=()=>setSeg(b.dataset.v));
function syncTypes(){el.typegrid.classList.toggle('hidden', el.all_types.checked);}
el.all_types.onchange=syncTypes;
// hydrate from config
setSeg(cfg.description);
el.content.value=cfg.content; el.all_types.checked=!!cfg.all_types; el.frequency.value=cfg.frequency;
el.time.value=cfg.time; el.mat.value=cfg.mat; el.mac.value=cfg.mac||''; el.qr.checked=cfg.qr!==false;
const chosen=new Set(cfg.types||[]);
document.querySelectorAll('.tcheck').forEach(c=>c.checked=chosen.has(c.dataset.type));
syncTypes();
function collect(){return {description:document.querySelector('#description button.on').dataset.v,
  content:el.content.value, all_types:el.all_types.checked,
  types:[...document.querySelectorAll('.tcheck')].filter(c=>c.checked).map(c=>c.dataset.type),
  qr:el.qr.checked, frequency:el.frequency.value, time:el.time.value, mat:el.mat.value,
  mac:el.mac.value.trim(), placard:true, replace:true};}
async function post(url,btn,label){el.status.textContent=label+'…';
  const old=btn.textContent; btn.disabled=true;
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
    const j=await r.json(); el.status.textContent=j.message||(j.ok?'Done.':'Something went wrong.');
    if(j.image){el.pv.src=j.image;el.pv.style.display='block';}
  }catch(e){el.status.textContent='Error: '+e;} btn.disabled=false;btn.textContent=old;}
el.save.onclick=()=>post('/save',el.save,'Saving');
el.prev.onclick=()=>post('/preview',el.prev,'Rendering a preview (can take ~20s)');
el.now.onclick=()=>post('/change-now',el.now,'Changing the art on your TV (can take a minute)');
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"The Frame Machine control panel on http://{platform.node()}.local:{a.port}  (Ctrl-C to stop)")
    app.run(host=a.host, port=a.port)

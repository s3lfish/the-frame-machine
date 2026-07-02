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
import secrets
from flask import Flask, request, jsonify, send_file, render_template_string, session, redirect

import frame_push as fp   # reuse CONFIG path, DEFAULTS, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "frame_push.py")
PYTHON = sys.executable
PREVIEW_PATH = os.path.join(tempfile.gettempdir(), "frame_preview.jpg")
LABEL = "com.frameart.daily"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = os.path.expanduser("~/Library/Logs/frameart.log")

app = Flask(__name__)

# stable secret for signed session cookies (generated once, stored locally)
_SK = os.path.join(fp.CFG, "secret_key.txt")
try:
    app.secret_key = open(_SK).read().strip() if os.path.exists(_SK) else None
    if not app.secret_key:
        app.secret_key = secrets.token_hex(16)
        os.makedirs(fp.CFG, exist_ok=True); open(_SK, "w").write(app.secret_key)
except Exception:
    app.secret_key = "frame-machine-dev-key"

LOGIN = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<body style="background:#1c1c1e;color:#ece9e2;font:16px -apple-system,sans-serif;display:flex;
min-height:90vh;align-items:center;justify-content:center">
<form method=post style="background:#262629;border:1px solid #3a3a3d;border-radius:14px;padding:28px;width:300px">
<h2 style="margin:0 0 14px">The Frame Machine</h2>
{% if error %}<p style="color:#e0704a">Wrong password.</p>{% endif %}
<input name=password type=password placeholder=Password autofocus
 style="width:100%;padding:11px;border-radius:10px;border:1px solid #3a3a3d;background:#303033;color:#ece9e2">
<button style="width:100%;margin-top:12px;padding:12px;border-radius:10px;border:0;background:#c9a24a;font-weight:600">Enter</button>
</form></body>"""

@app.before_request
def _auth():
    pw = fp.load_config().get("password") or ""
    if not pw or request.path == "/login" or session.get("auth"):
        return
    if request.method == "GET":
        return redirect("/login")
    return ("", 401)

@app.route("/login", methods=["GET", "POST"])
def login():
    pw = fp.load_config().get("password") or ""
    if request.method == "POST":
        if request.form.get("password") == pw:
            session["auth"] = True
            return redirect("/")
        return render_template_string(LOGIN, error=True)
    return render_template_string(LOGIN, error=False)

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
    f = ["--theme", cfg["content"], "--mat", cfg["mat"], "--describe", cfg["description"],
         "--source", cfg.get("source", "met")]
    tones = cfg.get("tone") or ["whimsical"]
    f += ["--tone", ",".join(tones if isinstance(tones, list) else [tones])]
    if cfg.get("all_types"):
        f += ["--all-types"]
    else:
        f += ["--no-all-types"]
        if cfg.get("types"):
            f += ["--types", ",".join(cfg["types"])]
    f += ["--placard"] if cfg.get("placard", True) else ["--no-placard"]
    f += ["--qr"] if cfg.get("qr", True) else ["--no-qr"]
    f += ["--replace"] if cfg.get("replace", True) else ["--no-replace"]
    f += ["--seasonal"] if cfg.get("seasonal") else ["--no-seasonal"]
    f += ["--holidays"] if cfg.get("holidays") else ["--no-holidays"]
    f += ["--hemisphere", cfg.get("hemisphere", "north")]
    if (cfg.get("subject") or "").strip():
        f += ["--subject", cfg["subject"].strip()]
    f += ["--fetch", str(cfg.get("fetch", 1))]
    if cfg.get("mac"):
        f += ["--mac", cfg["mac"]]
    return f

# ---------- schedule (macOS launchd / Linux cron) ----------
# frequency -> the hour offsets (from the anchor time) at which to fire each day
FREQ_OFFSETS = {"daily": [0], "twice-daily": [0, 12], "every-8h": [0, 8, 16], "every-6h": [0, 6, 12, 18]}

def write_schedule(cfg):
    """Regenerate + reload the recurring job from frequency/time (launchd on macOS, cron on Linux)."""
    try:
        hh, mm = map(int, cfg.get("time", "07:30").split(":"))
    except Exception:
        hh, mm = 7, 30
    offs = FREQ_OFFSETS.get(cfg.get("frequency", "daily"), [0])
    when = ", ".join(f"{(hh + o) % 24:02d}:{mm:02d}" for o in offs)
    sysname = platform.system()

    if sysname == "Darwin":
        ivals = "".join("<dict><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>"
                        % ((hh + o) % 24, mm) for o in offs)
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
        open(PLIST, "w").write(plist)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], capture_output=True, text=True)
        return f"Saved. The art will change at {when}." if r.returncode == 0 \
            else f"Settings saved, but scheduling failed: {r.stderr.strip()[:160]}"

    if sysname == "Linux":
        tag = "# frameart"
        lines = [f"{mm} {(hh + o) % 24} * * * {PYTHON} {SCRIPT} >> {LOG} 2>&1  {tag}" for o in offs]
        try:
            existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout.splitlines()
        except Exception:
            existing = []
        kept = [l for l in existing if tag not in l and l.strip()]
        cron = "\n".join(kept + lines) + "\n"
        r = subprocess.run(["crontab", "-"], input=cron, text=True, capture_output=True)
        return f"Saved. Cron will change the art at {when}." if r.returncode == 0 \
            else f"Settings saved, but cron update failed: {r.stderr.strip()[:160]}"

    return "Settings saved. (Automatic scheduling isn't supported on this OS — run frame_push.py on a timer yourself.)"

# ---------- routes ----------
@app.route("/")
def index():
    cfg = fp.load_config(); cfg.pop("password", None)   # never expose the password in the page
    return render_template_string(PAGE, cfg=cfg, types=list(fp.TYPE_FILTERS), tones=list(fp.TONES))

@app.route("/save", methods=["POST"])
def save():
    updates = request.get_json(force=True)
    if not updates.get("password"):        # blank field = leave the password unchanged
        updates.pop("password", None)
    cfg = save_config(updates)
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
    cmd = [PYTHON, SCRIPT, "--force"] + flags_from(cfg)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    if r.returncode == 0:
        return jsonify(ok=True, message="Done — " + " / ".join(tail))
    return jsonify(ok=False, message=(r.stderr or r.stdout or "failed").strip()[-300:])

def _read_status():
    try:
        return json.load(open(fp.STATUS))
    except Exception:
        return {}

@app.route("/state")
def state():
    cfg = fp.load_config()
    return jsonify(status=_read_status(), pinned=bool(cfg.get("pinned")),
                   has_image=os.path.exists(fp.CURRENT_IMG),
                   history=fp._load_list(fp.HISTORY)[-8:][::-1])

@app.route("/current.jpg")
def current_jpg():
    if os.path.exists(fp.CURRENT_IMG):
        return send_file(fp.CURRENT_IMG, mimetype="image/jpeg")
    return ("", 404)

@app.route("/pin", methods=["POST"])
def pin():
    newv = not fp.load_config().get("pinned")
    save_config({"pinned": newv})
    return jsonify(ok=True, pinned=newv,
                   message="Kept — this piece will stay up until you let it change." if newv
                           else "Off — the art will change on schedule again.")

@app.route("/ban", methods=["POST"])
def ban():
    pid = _read_status().get("id")
    if pid:
        bl = fp._load_list(fp.BLOCKLIST)
        if pid not in bl:
            bl.append(pid); fp._save_list(fp.BLOCKLIST, bl)
    r = subprocess.run([PYTHON, SCRIPT, "--force"] + flags_from(fp.load_config()),
                       capture_output=True, text=True, timeout=300)
    return jsonify(ok=(r.returncode == 0),
                   message="Banned and replaced with something new." if r.returncode == 0
                           else "Banned; the replacement failed: " + (r.stderr or "")[-160:])

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
 .nowrow{display:flex;gap:16px;align-items:flex-start}
 #cur{width:190px;border-radius:8px;border:1px solid var(--line);display:none}
 @media(max-width:520px){.nowrow{flex-direction:column}#cur{width:100%}}
</style></head><body><div class="wrap">
<h1>The Frame Machine</h1>
<p class="muted">Choose what your Frame shows, and when it changes.</p>

<div class="card" id="nowcard">
 <div class="nowrow">
  <img id="cur">
  <div style="flex:1;min-width:0">
   <div class="sub" id="laststatus">Loading…</div>
   <div id="nowtitle" style="font-weight:600;margin-top:4px"></div>
   <div class="sub" id="nowmeta"></div>
   <div class="actions" style="margin-top:12px">
    <button id="pin" style="background:#303033;color:var(--ink)">Keep this one</button>
    <button id="ban" style="background:#303033;color:var(--ink)">Never show again</button>
   </div>
  </div>
 </div>
</div>

<div class="card">
 <label class="chk"><input type="checkbox" id="placard">
   <span>Museum label <span class="sub">— artist, title &amp; details beside the art (off = just the artwork, no captions)</span></span></label>
 <div id="captionopts">
   <label class="f" style="margin-top:16px">Caption <span class="sub">— the story under each piece</span></label>
   <div class="seg" id="description">
     <button data-v="off">None</button>
     <button data-v="real">Real caption</button>
     <button data-v="made-up">Made-up tale</button>
   </div>
   <div id="tonerow" style="margin-top:14px"><label class="f">Made-up voice(s) <span class="sub">— one is picked at random each time</span></label>
     <div class="typegrid" id="tonegrid" style="border-top:none;padding-top:4px">
       {% for t in tones %}<label class="chk tk"><input type="checkbox" class="tonecheck" data-tone="{{t}}"><span>{{t|capitalize}}</span></label>{% endfor %}
     </div></div>
   <label class="chk" style="margin-top:14px"><input type="checkbox" id="qr">
     <span>Show a QR code <span class="sub">— links to the real museum page for this piece</span></span></label>
 </div>
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
 <label class="f" style="margin-top:14px">Source <span class="sub">— which museum</span></label>
 <select id="source">
   <option value="met">The Met</option>
   <option value="cleveland">Cleveland Museum of Art</option>
   <option value="any">Either — a random pick each time</option>
 </select>
 <label class="f" style="margin-top:16px">Only show art of… <span class="sub">— optional; combines with season/holidays (e.g. cats at Christmas → Christmas cats)</span></label>
 <input type="text" id="subject" placeholder="cats, dogs, dragons… — leave blank for none" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px">
 <label class="chk" style="margin-top:14px"><input type="checkbox" id="seasonal">
   <span>Match the season <span class="sub">— bias to snow, blossom, harvest…</span></span></label>
 <label class="chk" style="margin-top:14px"><input type="checkbox" id="holidays">
   <span>Celebrate holidays <span class="sub">— spooky art at Halloween, nativity at Christmas…</span></span></label>
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

<div class="card"><details><summary>Recent history</summary>
 <div id="historylist" class="sub" style="margin-top:10px">Loading…</div>
</details></div>

<div class="card" style="margin-top:16px"><details><summary>Advanced</summary>
 <label class="f" style="margin-top:12px">TV wireless MAC address</label>
 <input type="text" id="mac" placeholder="AA:BB:CC:DD:EE:FF" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px">
 <p class="sub" style="margin-top:8px">Found on the TV under About This TV, or your router. Needed to find and control the Frame.</p>
 <label class="f" style="margin-top:16px">Phone alerts — ntfy topic</label>
 <input type="text" id="ntfy_topic" placeholder="e.g. my-frame-alerts-8b3" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px">
 <p class="sub" style="margin-top:8px">Pick any hard-to-guess word, then subscribe to it in the free <b>ntfy</b> app to get a push if a run ever fails. Leave blank for none.</p>
 <label class="f" style="margin-top:16px">Hemisphere <span class="sub">— for “Match the season”</span></label>
 <select id="hemisphere"><option value="north">Northern</option><option value="south">Southern</option></select>
 <label class="f" style="margin-top:16px">Panel password</label>
 <input type="password" id="password" placeholder="leave blank to keep current" autocomplete="new-password" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px">
 <p class="sub" style="margin-top:8px">Requires a login to open this panel. Blank leaves it unchanged; to remove it, clear it in config.json.</p>
</details></div>
</div>

<script>
const cfg = {{ cfg|tojson }};
const $ = id => document.getElementById(id);
const el = {content:$('content'), source:$('source'), all_types:$('all_types'), typegrid:$('typegrid'),
  placard:$('placard'), captionopts:$('captionopts'), qr:$('qr'), tonerow:$('tonerow'),
  seasonal:$('seasonal'), holidays:$('holidays'), subject:$('subject'), hemisphere:$('hemisphere'),
  ntfy_topic:$('ntfy_topic'), password:$('password'),
  frequency:$('frequency'), time:$('time'), mat:$('mat'), mac:$('mac'), status:$('status'), pv:$('pv'),
  save:$('save'), prev:$('prev'), now:$('now'), historylist:$('historylist'),
  cur:$('cur'), laststatus:$('laststatus'), nowtitle:$('nowtitle'), nowmeta:$('nowmeta'), pin:$('pin'), ban:$('ban')};
function setSeg(val){document.querySelectorAll('#description button').forEach(b=>b.classList.toggle('on',b.dataset.v===val));
  el.tonerow.style.display = (val==='made-up') ? 'block' : 'none';}
document.querySelectorAll('#description button').forEach(b=>b.onclick=()=>setSeg(b.dataset.v));
function syncPlacard(){el.captionopts.style.display = el.placard.checked ? 'block' : 'none';}
el.placard.onchange=syncPlacard;
function syncTypes(){el.typegrid.classList.toggle('hidden', el.all_types.checked);}
el.all_types.onchange=syncTypes;
// hydrate from config
setSeg(cfg.description);
el.content.value=cfg.content; el.all_types.checked=!!cfg.all_types; el.frequency.value=cfg.frequency;
el.time.value=cfg.time; el.mat.value=cfg.mat; el.mac.value=cfg.mac||''; el.qr.checked=cfg.qr!==false;
el.source.value=cfg.source||'met'; el.ntfy_topic.value=cfg.ntfy_topic||'';
el.seasonal.checked=!!cfg.seasonal; el.hemisphere.value=cfg.hemisphere||'north';
el.holidays.checked=!!cfg.holidays; el.subject.value=cfg.subject||'';
el.placard.checked=cfg.placard!==false;
const tset=new Set(Array.isArray(cfg.tone)?cfg.tone:[cfg.tone||'whimsical']);
document.querySelectorAll('.tonecheck').forEach(c=>c.checked=tset.has(c.dataset.tone));
syncPlacard();
const chosen=new Set(cfg.types||[]);
document.querySelectorAll('.tcheck').forEach(c=>c.checked=chosen.has(c.dataset.type));
syncTypes();
function collect(){return {description:document.querySelector('#description button.on').dataset.v,
  content:el.content.value, source:el.source.value, all_types:el.all_types.checked,
  types:[...document.querySelectorAll('.tcheck')].filter(c=>c.checked).map(c=>c.dataset.type),
  qr:el.qr.checked, placard:el.placard.checked,
  tone:[...document.querySelectorAll('.tonecheck')].filter(c=>c.checked).map(c=>c.dataset.tone),
  seasonal:el.seasonal.checked, holidays:el.holidays.checked, subject:el.subject.value.trim(),
  hemisphere:el.hemisphere.value, ntfy_topic:el.ntfy_topic.value.trim(), password:el.password.value,
  frequency:el.frequency.value, time:el.time.value, mat:el.mat.value,
  mac:el.mac.value.trim(), replace:true};}
async function post(url,btn,label){el.status.textContent=label+'…';
  const old=btn.textContent; btn.disabled=true;
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
    const j=await r.json(); el.status.textContent=j.message||(j.ok?'Done.':'Something went wrong.');
    if(j.image){el.pv.src=j.image;el.pv.style.display='block';}
  }catch(e){el.status.textContent='Error: '+e;} btn.disabled=false;btn.textContent=old;}
el.save.onclick=()=>post('/save',el.save,'Saving');
el.prev.onclick=()=>post('/preview',el.prev,'Rendering a preview (can take ~20s)');
el.now.onclick=async()=>{await post('/change-now',el.now,'Changing the art on your TV (can take a minute)');loadState();};
async function loadState(){try{const j=await (await fetch('/state')).json(); const s=j.status||{};
  el.laststatus.textContent=(s.ok===false?'⚠ Last run failed: ':'Now showing · ')+(s.message||'');
  el.nowtitle.textContent=s.title?(s.title+(s.artist?(' — '+s.artist):'')):'';
  el.nowmeta.textContent=[s.source,s.when&&s.when.replace('T',' ')].filter(Boolean).join(' · ');
  if(j.has_image){el.cur.src='/current.jpg?t='+Date.now();el.cur.style.display='block';}
  el.pin.textContent=j.pinned?'Let it change':'Keep this one'; el.pin.classList.toggle('on',j.pinned);
  el.historylist.innerHTML=(j.history&&j.history.length)? j.history.map(h=>{
    const t=(h.url?`<a href="${h.url}" target="_blank" style="color:var(--ink)">${h.title||'?'}</a>`:(h.title||'?'));
    return `<div style="padding:5px 0;border-bottom:1px solid var(--line)">${t} <span style="opacity:.6">— ${h.source||''} · ${(h.when||'').replace('T',' ')}</span></div>`;
  }).join('') : 'Nothing yet.';
}catch(e){}}
el.pin.onclick=async()=>{const j=await (await fetch('/pin',{method:'POST'})).json();el.status.textContent=j.message;loadState();};
el.ban.onclick=async()=>{el.status.textContent='Banning & replacing…';const j=await (await fetch('/ban',{method:'POST'})).json();el.status.textContent=j.message;loadState();};
loadState();
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"The Frame Machine control panel on http://{platform.node()}.local:{a.port}  (Ctrl-C to stop)")
    app.run(host=a.host, port=a.port)

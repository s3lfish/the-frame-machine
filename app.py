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

import argparse, json, os, platform, shutil, subprocess, sys, tempfile, threading, time
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

# ---------- self-check: is our own code still readable? ----------
# Dropbox/iCloud can evict the project folder to "online-only", after which spawned
# processes (the scheduled job, the push subprocess, a watcher) can't read frame_push.py
# and silently fail. Detect that and alert, so it's obvious what to fix.
_files = {"alerted": False}

def _files_readable():
    try:
        with open(SCRIPT, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False

def _files_watchdog():
    while True:
        ok = _files_readable()
        if not ok and not _files["alerted"]:
            fp.ntfy_alert("Frame art: can't read its files",
                          "The project folder looks evicted to online-only (can't read frame_push.py). "
                          "Set the Dropbox/iCloud folder to 'Available offline' — scheduled art changes "
                          "will fail until then.")
            _files["alerted"] = True
        elif ok:
            _files["alerted"] = False
        time.sleep(600)

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
    if cfg.get("tone_weights"):
        f += ["--tone-weights", json.dumps(cfg["tone_weights"])]
    if cfg.get("all_types"):
        f += ["--all-types"]
    else:
        f += ["--no-all-types"]
        if cfg.get("types"):
            f += ["--types", ",".join(cfg["types"])]
    f += ["--placard"] if cfg.get("placard", True) else ["--no-placard"]
    f += ["--qr"] if cfg.get("qr", True) else ["--no-qr"]
    f += ["--replace"] if cfg.get("replace", True) else ["--no-replace"]
    # per-run "chance" modes — fall back to the legacy on/off bool if no chance is stored yet
    for cli, key in (("--seasonal-chance", "seasonal"), ("--holidays-chance", "holidays"),
                     ("--weather-chance", "weather"), ("--on-this-day-chance", "on_this_day"),
                     ("--googly-chance", "googly")):
        ch = cfg.get(key + "_chance")
        if ch is None:
            ch = 1.0 if cfg.get(key) else 0.0
        f += [cli, str(ch)]
    f += ["--watch-on-fail"] if cfg.get("watch_on_fail", True) else ["--no-watch-on-fail"]
    f += ["--hemisphere", cfg.get("hemisphere", "north")]
    if cfg.get("latitude") is not None and cfg.get("longitude") is not None:
        f += ["--latitude", str(cfg["latitude"]), "--longitude", str(cfg["longitude"])]
    if (cfg.get("subject") or "").strip():
        f += ["--subject", cfg["subject"].strip()]
    f += ["--fetch", str(cfg.get("fetch", 1))]
    if cfg.get("mac"):
        f += ["--mac", cfg["mac"]]
    return f

# ---------- schedule (macOS launchd / Linux cron) ----------
# The schedule is an arbitrary interval: `every` N `every_unit` (minutes/hours/days).
# Exactly "1 day" anchors at a chosen time; everything else fires on a rolling interval.
_UNIT_MIN = {"minutes": 1, "hours": 60, "days": 1440}
# Map configs saved before the flexible interval (old `frequency`) to (every, unit).
_LEGACY_FREQ = {"daily": (1, "days"), "twice-daily": (12, "hours"),
                "every-8h": (8, "hours"), "every-6h": (6, "hours")}

def schedule_of(cfg):
    """(interval_minutes, every, unit) for a config, honouring the legacy `frequency`."""
    if cfg.get("every"):
        n, unit = int(cfg["every"]), cfg.get("every_unit", "days")
    else:
        n, unit = _LEGACY_FREQ.get(cfg.get("frequency", "daily"), (1, "days"))
    n = max(1, n)
    unit = unit if unit in _UNIT_MIN else "days"
    return n * _UNIT_MIN[unit], n, unit

def _every_label(n, unit):
    return "once a day" if (n == 1 and unit == "days") else f"every {n} {unit[:-1] if n == 1 else unit}"

def write_schedule(cfg):
    """Regenerate + reload the recurring job for any interval (launchd on macOS, cron on Linux)."""
    interval, n, unit = schedule_of(cfg)
    try:
        hh, mm = map(int, cfg.get("time", "07:30").split(":"))
    except Exception:
        hh, mm = 7, 30
    daily_at_time = (interval == 1440)          # exactly once a day -> anchor at the chosen time
    when = f"once a day at {hh:02d}:{mm:02d}" if daily_at_time else f"{_every_label(n, unit)}, around the clock"
    sysname = platform.system()

    if sysname == "Darwin":
        if daily_at_time:
            sched = (f"<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hh}</integer>"
                     f"<key>Minute</key><integer>{mm}</integer></dict>")
        else:
            sched = f"<key>StartInterval</key><integer>{interval * 60}</integer>"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{PYTHON}</string><string>{SCRIPT}</string></array>
  {sched}
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
        return f"Saved. The art will change {when}." if r.returncode == 0 \
            else f"Settings saved, but scheduling failed: {r.stderr.strip()[:160]}"

    if sysname == "Linux":
        if daily_at_time:
            spec = f"{mm} {hh} * * *"
        elif interval < 60:
            spec = f"*/{interval} * * * *"
        elif interval % 60 == 0 and interval // 60 <= 23:
            spec = f"{mm} */{interval // 60} * * *"
        else:                                   # cron can't express it exactly — approximate
            spec = f"*/{min(59, interval)} * * * *" if interval < 1440 else f"{mm} {hh} * * *"
        tag = "# frameart"
        line = f"{spec} {PYTHON} {SCRIPT} >> {LOG} 2>&1  {tag}"
        try:
            existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout.splitlines()
        except Exception:
            existing = []
        kept = [l for l in existing if tag not in l and l.strip()]
        cron = "\n".join(kept + [line]) + "\n"
        r = subprocess.run(["crontab", "-"], input=cron, text=True, capture_output=True)
        return f"Saved. Cron will change the art {when}." if r.returncode == 0 \
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
                   has_image=os.path.exists(fp.CURRENT_IMG), watching=fp._watcher_running(),
                   files_ok=_files_readable(),
                   history=fp._load_list(fp.HISTORY)[-8:][::-1])

@app.route("/stop-watch", methods=["POST"])
def stop_watch():
    """Stop a background watcher that's waiting for the TV to wake."""
    if not fp._watcher_running():
        return jsonify(ok=False, message="No watcher is running.")
    try:
        pid = int(open(fp.WATCH_PID).read().strip())
        os.kill(pid, 15)
        os.remove(fp.WATCH_PID)
    except Exception as e:
        return jsonify(ok=False, message=f"Couldn't stop it: {str(e)[:120]}")
    fp.write_status(False, "Stopped waiting for the TV.")
    return jsonify(ok=True, message="Stopped waiting for the TV.")

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

def _navigate(step):
    """Step back (-1) / forward (+1) through the saved history images."""
    nav = fp.history_navlist()
    if len(nav) < 2:
        return jsonify(ok=False, message="No history to browse yet — change the art a few times first.")
    idx = fp.nav_get()
    if not (0 <= idx < len(nav)):
        idx = len(nav) - 1
    new = idx + step
    if new < 0:
        return jsonify(ok=False, message="You're already at the oldest piece.")
    if new >= len(nav):
        return jsonify(ok=False, message="You're already at the newest piece.")
    entry = nav[new]
    tmp = os.path.join(tempfile.gettempdir(), "frame_nav.jpg")
    shutil.copy(entry["file"], tmp)
    r = subprocess.run([PYTHON, SCRIPT, "--force", "--files", tmp, "--no-record"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return jsonify(ok=False, message="Couldn't switch: " + (r.stderr or "")[-160:])
    fp.nav_set(new)
    shutil.copy(entry["file"], fp.CURRENT_IMG)
    fp.write_status(True, f"Showing {entry.get('title','art')}",
                    {k: entry.get(k, "") for k in ("id", "title", "artist", "url", "source", "caption_style",
                                                    "caption", "date", "medium", "dimensions", "credit", "culture")})
    return jsonify(ok=True, message=f"{entry.get('title','art')} — {new + 1} of {len(nav)}")

@app.route("/back", methods=["POST"])
def back():
    return _navigate(-1)

@app.route("/forward", methods=["POST"])
def forward():
    return _navigate(+1)

@app.route("/drop-voice", methods=["POST"])
def drop_voice():
    """Remove the made-up voice used for the current piece from the rotation, so a
    voice you didn't like won't come up again."""
    style = _read_status().get("caption_style")
    tones = fp.load_config().get("tone") or []
    if isinstance(tones, str):
        tones = [tones]
    if not style or style not in tones:
        return jsonify(ok=False, message="No made-up voice to drop for this piece.")
    if len(tones) <= 1:
        return jsonify(ok=False, message=f"“{style}” is your only voice — turn a few others on first, then drop it.")
    weights = {k: v for k, v in (fp.load_config().get("tone_weights") or {}).items() if k != style}
    save_config({"tone": [t for t in tones if t != style], "tone_weights": weights})
    return jsonify(ok=True, dropped=style, message=f"Dropped “{style}” — it won't be used again.")

@app.route("/favourite", methods=["POST"])
def favourite():
    st = _read_status()
    if not st.get("id") or not os.path.exists(fp.CURRENT_IMG):
        return jsonify(ok=False, message="Nothing on the TV to favourite yet.")
    os.makedirs(fp.FAVS_DIR, exist_ok=True)
    dest = os.path.join(fp.FAVS_DIR, st["id"].replace(":", "_") + ".jpg")
    try:
        import shutil; shutil.copy(fp.CURRENT_IMG, dest)
    except Exception as e:
        return jsonify(ok=False, message=f"Couldn't save favourite: {str(e)[:120]}")
    favs = fp._load_list(fp.FAVOURITES)
    if not any(x.get("id") == st["id"] for x in favs):
        favs.append({"id": st["id"], "title": st.get("title", ""), "artist": st.get("artist", ""),
                     "url": st.get("url", ""), "source": st.get("source", ""), "file": dest})
        fp._save_list(fp.FAVOURITES, favs)
    return jsonify(ok=True, message="♥ Added to favourites — it'll turn up more often.")

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
 .voicerow{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
   padding:7px 0;border-bottom:1px solid var(--line)}
 .voicerow .vname{font-size:14px}
 .lvl{display:flex;gap:4px} .lvl button{background:#303033;color:var(--sub);border:1px solid var(--line);
   border-radius:8px;padding:5px 9px;font-size:12px;cursor:pointer}
 .lvl button.on{background:var(--accent);color:#1c1c1e;border-color:var(--accent);font-weight:600}
 .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}
 .actions button{flex:1;min-width:130px;border-radius:10px;padding:13px;font-size:15px;font-weight:600;cursor:pointer;border:1px solid var(--line)}
 #save{background:#303033;color:var(--ink)} #prev{background:#303033;color:var(--ink)}
 #now{background:var(--accent);color:#1c1c1e;border-color:var(--accent)}
 #status{min-height:20px;margin:12px 2px;color:var(--sub);font-size:14px}
 .spin{display:inline-block;width:1em;height:1em;border:2px solid currentColor;border-right-color:transparent;
   border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:7px;opacity:.85}
 @keyframes spin{to{transform:rotate(360deg)}}
 button:disabled{opacity:.7;cursor:progress}
 img#pv{width:100%;border-radius:12px;margin-top:12px;display:none;border:1px solid var(--line)}
 details summary{cursor:pointer;color:var(--sub)} details input{margin-top:8px}
 .nowrow{display:flex;gap:16px;align-items:flex-start}
 #cur{width:190px;border-radius:8px;border:1px solid var(--line);display:none}
 @media(max-width:520px){.nowrow{flex-direction:column}#cur{width:100%}}
</style></head><body><div class="wrap">
<h1>The Frame Machine</h1>
<p class="muted">Choose what your Frame shows, and when it changes.</p>
<div id="filewarn" style="display:none;background:#5a2d2d;border:1px solid #8a4a4a;color:#f3d6d6;padding:12px 14px;border-radius:10px;margin-bottom:16px;font-size:14px">
 ⚠ Can't read the app's own files — the project folder looks evicted to <b>online-only</b>. In Finder, set the Dropbox/iCloud folder to <b>“Available offline”</b>, or scheduled changes will keep failing.</div>

<div class="actions" style="margin-bottom:10px">
 <button id="back" style="background:#303033;color:var(--ink)">◀ Back</button>
 <button id="fwd" style="background:#303033;color:var(--ink)">Forward ▶</button>
</div>
<div class="actions" style="margin-bottom:16px">
 <button id="nowtop" style="background:var(--accent);color:#1c1c1e;border-color:var(--accent)">Change the art now</button>
</div>

<div class="card" id="nowcard">
 <div class="nowrow">
  <img id="cur">
  <div style="flex:1;min-width:0">
   <div class="sub" id="laststatus">Loading…</div>
   <div id="nowtitle" style="font-weight:600;margin-top:4px"></div>
   <div class="sub" id="nowmeta"></div>
   <div id="nowdetails" style="margin-top:8px;font-size:13px;line-height:1.5"></div>
   <div id="nowcaption" style="margin-top:10px;font-style:italic;font-size:14px;color:var(--ink);display:none"></div>
   <div class="sub" id="nowstyle" style="margin-top:8px"></div>
   <a id="nowlink" href="#" target="_blank" style="display:none;font-size:13px;color:var(--accent)">View at the museum ↗</a>
   <div class="actions" style="margin-top:12px">
    <button id="fav" style="background:#303033;color:var(--ink)">♥ Favourite</button>
    <button id="pin" style="background:#303033;color:var(--ink)">Stop this from changing</button>
    <button id="ban" style="background:#303033;color:var(--ink)">Never show again</button>
    <button id="dropvoice" style="background:#303033;color:var(--ink);display:none">Drop this voice</button>
    <button id="stopwatch" style="background:#303033;color:var(--ink);display:none">Stop waiting</button>
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
   <div id="tonerow" style="margin-top:14px"><label class="f">Made-up voice(s) <span class="sub">— one is picked each time; set how often each turns up</span></label>
     <div id="tonegrid" style="margin-top:4px">
       {% for t in tones %}<div class="voicerow" data-tone="{{t}}"><span class="vname">{{t|capitalize}}</span>
         <div class="lvl"><button data-w="0">Off</button><button data-w="0.35">Rarely</button><button data-w="1">Normal</button><button data-w="2.5">Often</button></div>
       </div>{% endfor %}
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
 <label class="f" style="margin-top:16px">How often to spice things up <span class="sub">— each rolls its own dice per change</span></label>
 <div class="voicerow chancerow" id="seasonal" data-w="0">
   <span class="vname">Match the season <span class="sub">— snow, blossom, harvest…</span></span>
   <div class="lvl"><button data-w="0">Never</button><button data-w="0.2">Rarely</button><button data-w="0.5">Sometimes</button><button data-w="1">Always</button></div>
 </div>
 <div class="voicerow chancerow" id="holidays" data-w="0">
   <span class="vname">Celebrate holidays <span class="sub">— spooky at Halloween, nativity at Christmas…</span></span>
   <div class="lvl"><button data-w="0">Never</button><button data-w="0.2">Rarely</button><button data-w="0.5">Sometimes</button><button data-w="1">Always</button></div>
 </div>
 <div class="voicerow chancerow" id="weather" data-w="0">
   <span class="vname">Match today's weather <span class="sub">— rain, snow or sunshine, from your forecast</span></span>
   <div class="lvl"><button data-w="0">Never</button><button data-w="0.2">Rarely</button><button data-w="0.5">Sometimes</button><button data-w="1">Always</button></div>
 </div>
 <div class="voicerow chancerow" id="on_this_day" data-w="0">
   <span class="vname">On this day <span class="sub">— a historical event from today's date</span></span>
   <div class="lvl"><button data-w="0">Never</button><button data-w="0.2">Rarely</button><button data-w="0.5">Sometimes</button><button data-w="1">Always</button></div>
 </div>
 <div class="voicerow chancerow" id="googly" data-w="0">
   <span class="vname">Googly eyes <span class="sub">— cartoon eyes on any faces, just for fun</span></span>
   <div class="lvl"><button data-w="0">Never</button><button data-w="0.2">Rarely</button><button data-w="0.5">Sometimes</button><button data-w="1">Always</button></div>
 </div>
 <label class="chk" style="margin-top:14px"><input type="checkbox" id="all_types">
   <span>All object types <span class="sub">— everything the museum has</span></span></label>
 <div id="typegrid" class="typegrid">
   {% for t in types %}<label class="chk tk"><input type="checkbox" class="tcheck" data-type="{{t}}"><span>{{t}}</span></label>{% endfor %}
 </div>
</div>

<div class="card">
 <label class="f">Change the art…</label>
 <div class="row" style="align-items:flex-end">
   <div style="flex:0 0 84px"><input type="number" id="every" min="1" step="1" value="1"
     style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px"></div>
   <div style="flex:0 0 130px"><select id="every_unit">
     <option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option>
   </select></div>
   <div id="attimewrap"><label class="f" style="font-weight:400;color:var(--sub)">at</label><input type="time" id="time"></div>
 </div>
 <p class="sub" id="schedhint" style="margin-top:8px"></p>
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
 <label class="chk" style="margin-top:16px"><input type="checkbox" id="watch_on_fail">
   <span>Keep trying if the TV is asleep <span class="sub">— if a change can't reach the TV, keep watching in the background and push as soon as it wakes</span></span></label>
 <label class="f" style="margin-top:16px">Hemisphere <span class="sub">— for “Match the season”</span></label>
 <select id="hemisphere"><option value="north">Northern</option><option value="south">Southern</option></select>
 <label class="f" style="margin-top:16px">Location <span class="sub">— for “Match today's weather”</span></label>
 <div class="row">
   <div><input type="number" step="any" id="latitude" placeholder="latitude" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px"></div>
   <div><input type="number" step="any" id="longitude" placeholder="longitude" style="width:100%;background:#303033;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px"></div>
 </div>
 <p class="sub" style="margin-top:8px">Leave blank to auto-detect from your internet connection. Set them for a precise local forecast.</p>
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
  weather:$('weather'), on_this_day:$('on_this_day'), googly:$('googly'),
  latitude:$('latitude'), longitude:$('longitude'),
  ntfy_topic:$('ntfy_topic'), password:$('password'), watch_on_fail:$('watch_on_fail'),
  every:$('every'), every_unit:$('every_unit'), attimewrap:$('attimewrap'), schedhint:$('schedhint'),
  time:$('time'), mat:$('mat'), mac:$('mac'), status:$('status'), pv:$('pv'),
  save:$('save'), prev:$('prev'), now:$('now'), historylist:$('historylist'),
  cur:$('cur'), laststatus:$('laststatus'), nowtitle:$('nowtitle'), nowmeta:$('nowmeta'),
  nowdetails:$('nowdetails'), nowcaption:$('nowcaption'), nowlink:$('nowlink'),
  nowstyle:$('nowstyle'), dropvoice:$('dropvoice'), stopwatch:$('stopwatch'), filewarn:$('filewarn'),
  pin:$('pin'), ban:$('ban'), fav:$('fav'), nowtop:$('nowtop'), back:$('back'), fwd:$('fwd')};
function setSeg(val){document.querySelectorAll('#description button').forEach(b=>b.classList.toggle('on',b.dataset.v===val));
  el.tonerow.style.display = (val==='made-up') ? 'block' : 'none';}
document.querySelectorAll('#description button').forEach(b=>b.onclick=()=>setSeg(b.dataset.v));
function syncPlacard(){el.captionopts.style.display = el.placard.checked ? 'block' : 'none';}
el.placard.onchange=syncPlacard;
function syncTypes(){el.typegrid.classList.toggle('hidden', el.all_types.checked);}
el.all_types.onchange=syncTypes;
// hydrate from config
setSeg(cfg.description);
el.content.value=cfg.content; el.all_types.checked=!!cfg.all_types;
// schedule: any interval (every N minutes/hours/days), with legacy `frequency` fallback
const LEGACY={daily:[1,'days'],'twice-daily':[12,'hours'],'every-8h':[8,'hours'],'every-6h':[6,'hours']};
let _ev=cfg.every, _eu=cfg.every_unit;
if(!_ev){const m=LEGACY[cfg.frequency||'daily']||[1,'days']; _ev=m[0]; _eu=m[1];}
el.every.value=_ev; el.every_unit.value=_eu||'days';
function syncSched(){const n=Math.max(1,parseInt(el.every.value)||1), u=el.every_unit.value;
  const daily=(n===1&&u==='days');
  el.attimewrap.style.display=daily?'block':'none';
  el.schedhint.textContent=daily?('Once a day at '+(el.time.value||'07:30')+'.')
    :('Every '+n+' '+(n===1?u.slice(0,-1):u)+', around the clock.');}
el.every.oninput=syncSched; el.every_unit.onchange=syncSched;
el.time.value=cfg.time; el.mat.value=cfg.mat; el.mac.value=cfg.mac||''; el.qr.checked=cfg.qr!==false;
el.time.oninput=syncSched; syncSched();
el.source.value=cfg.source||'met'; el.ntfy_topic.value=cfg.ntfy_topic||'';
el.hemisphere.value=cfg.hemisphere||'north'; el.subject.value=cfg.subject||'';
el.watch_on_fail.checked=cfg.watch_on_fail!==false;
el.latitude.value=(cfg.latitude!=null?cfg.latitude:''); el.longitude.value=(cfg.longitude!=null?cfg.longitude:'');
el.placard.checked=cfg.placard!==false;
// per-voice frequency levels (Off/Rarely/Normal/Often -> weight 0/0.35/1/2.5)
const LVLS=[0,0.35,1,2.5];
function setVoice(row,w){row.dataset.w=w;row.querySelectorAll('.lvl button').forEach(b=>b.classList.toggle('on',parseFloat(b.dataset.w)===w));}
function voiceRow(t){return document.querySelector('.voicerow[data-tone="'+t+'"]');}
(function(){const tset=new Set(Array.isArray(cfg.tone)?cfg.tone:[cfg.tone||'whimsical']);
  const tw=cfg.tone_weights||{};
  document.querySelectorAll('#tonegrid .voicerow').forEach(row=>{const t=row.dataset.tone;
    let w = tset.has(t) ? (t in tw ? LVLS.reduce((a,b)=>Math.abs(b-tw[t])<Math.abs(a-tw[t])?b:a) : 1) : 0;
    setVoice(row,w);
    row.querySelectorAll('.lvl button').forEach(b=>b.onclick=()=>setVoice(row,parseFloat(b.dataset.w)));
  });})();
// per-run "chance" rows: Never/Rarely/Sometimes/Always -> 0/0.2/0.5/1
const CH=[0,0.2,0.5,1];
[['seasonal',cfg.seasonal_chance,cfg.seasonal],['holidays',cfg.holidays_chance,cfg.holidays],
 ['weather',cfg.weather_chance,cfg.weather],['on_this_day',cfg.on_this_day_chance,cfg.on_this_day],
 ['googly',cfg.googly_chance,cfg.googly]].forEach(([id,ch,legacy])=>{
  const c = ch!=null?ch:(legacy?1:0); const row=$(id);
  setVoice(row,CH.reduce((a,b)=>Math.abs(b-c)<Math.abs(a-c)?b:a));
  row.querySelectorAll('.lvl button').forEach(b=>b.onclick=()=>setVoice(row,parseFloat(b.dataset.w)));
});
syncPlacard();
const chosen=new Set(cfg.types||[]);
document.querySelectorAll('.tcheck').forEach(c=>c.checked=chosen.has(c.dataset.type));
syncTypes();
function collect(){return {description:document.querySelector('#description button.on').dataset.v,
  content:el.content.value, source:el.source.value, all_types:el.all_types.checked,
  types:[...document.querySelectorAll('.tcheck')].filter(c=>c.checked).map(c=>c.dataset.type),
  qr:el.qr.checked, placard:el.placard.checked,
  tone:[...document.querySelectorAll('#tonegrid .voicerow')].filter(r=>parseFloat(r.dataset.w)>0).map(r=>r.dataset.tone),
  tone_weights:Object.fromEntries([...document.querySelectorAll('#tonegrid .voicerow')].filter(r=>parseFloat(r.dataset.w)>0).map(r=>[r.dataset.tone,parseFloat(r.dataset.w)])),
  subject:el.subject.value.trim(),
  seasonal_chance:parseFloat(el.seasonal.dataset.w||'0'), holidays_chance:parseFloat(el.holidays.dataset.w||'0'),
  weather_chance:parseFloat(el.weather.dataset.w||'0'), on_this_day_chance:parseFloat(el.on_this_day.dataset.w||'0'),
  googly_chance:parseFloat(el.googly.dataset.w||'0'),
  latitude:el.latitude.value.trim()===''?null:parseFloat(el.latitude.value),
  longitude:el.longitude.value.trim()===''?null:parseFloat(el.longitude.value),
  hemisphere:el.hemisphere.value, watch_on_fail:el.watch_on_fail.checked,
  ntfy_topic:el.ntfy_topic.value.trim(), password:el.password.value,
  every:Math.max(1,parseInt(el.every.value)||1), every_unit:el.every_unit.value, time:el.time.value, mat:el.mat.value,
  mac:el.mac.value.trim(), replace:true};}
async function post(url,btn,label,working){el.status.textContent=label+'…';
  const old=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span>'+(working||'Working')+'…';
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
    const j=await r.json(); el.status.textContent=j.message||(j.ok?'Done.':'Something went wrong.');
    if(j.image){el.pv.src=j.image;el.pv.style.display='block';}
  }catch(e){el.status.textContent='Error: '+e;} btn.disabled=false;btn.innerHTML=old;}
el.save.onclick=()=>post('/save',el.save,'Saving','Saving');
el.prev.onclick=()=>post('/preview',el.prev,'Rendering a preview (can take ~20s)','Rendering');
el.now.onclick=async()=>{await post('/change-now',el.now,'Changing the art on your TV (can take a minute)','Changing');loadState();};
el.nowtop.onclick=async()=>{await post('/change-now',el.nowtop,'Changing the art on your TV (can take a minute)','Changing');loadState();};
async function nav(url,btn){el.status.textContent='Switching…';btn.disabled=true;const o=btn.innerHTML;
  btn.innerHTML='<span class="spin"></span>…';
  const j=await (await fetch(url,{method:'POST'})).json();el.status.textContent=j.message;btn.disabled=false;btn.innerHTML=o;loadState();}
el.back.onclick=()=>nav('/back',el.back);
el.fwd.onclick=()=>nav('/forward',el.fwd);
async function loadState(){try{const j=await (await fetch('/state')).json(); const s=j.status||{};
  el.filewarn.style.display = (j.files_ok===false) ? 'block' : 'none';
  const waiting = j.watching || s.waiting;
  el.laststatus.textContent = waiting ? ('⏳ Waiting for the TV to wake — will push as soon as it is on. '+(s.message||''))
    : (s.ok===false?'⚠ Last run failed: ':'Now showing · ')+(s.message||'');
  el.laststatus.style.color = waiting ? 'var(--accent)' : (s.ok===false ? '#e0704a' : 'var(--sub)');
  el.stopwatch.style.display = waiting ? 'inline-block' : 'none';
  el.nowtitle.textContent=s.title?(s.title+(s.artist?(' — '+s.artist):'')):'';
  el.nowmeta.textContent=[s.source,s.when&&s.when.replace('T',' ')].filter(Boolean).join(' · ');
  const esc=t=>String(t).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const rows=[['Date',s.date],['Medium',s.medium],['Dimensions',s.dimensions],['Culture',s.culture],['Credit',s.credit]]
    .filter(([k,v])=>v);
  el.nowdetails.innerHTML=rows.map(([k,v])=>`<div><span style="opacity:.55">${k}:</span> ${esc(v)}</div>`).join('');
  el.nowcaption.textContent=s.caption||''; el.nowcaption.style.display=s.caption?'block':'none';
  if(s.url){el.nowlink.href=s.url;el.nowlink.style.display='inline-block';}else{el.nowlink.style.display='none';}
  const style=s.caption_style||'';
  el.nowstyle.textContent=style?('Made-up voice: '+style):'';
  el.dropvoice.textContent=style?('Drop “'+style+'” voice'):'Drop this voice';
  el.dropvoice.style.display=style?'inline-block':'none';
  if(j.has_image){el.cur.src='/current.jpg?t='+Date.now();el.cur.style.display='block';}
  el.pin.textContent=j.pinned?'Let it change again':'Stop this from changing'; el.pin.classList.toggle('on',j.pinned);
  el.historylist.innerHTML=(j.history&&j.history.length)? j.history.map(h=>{
    const t=(h.url?`<a href="${h.url}" target="_blank" style="color:var(--ink)">${h.title||'?'}</a>`:(h.title||'?'));
    return `<div style="padding:5px 0;border-bottom:1px solid var(--line)">${t} <span style="opacity:.6">— ${h.source||''} · ${(h.when||'').replace('T',' ')}</span></div>`;
  }).join('') : 'Nothing yet.';
}catch(e){}}
el.pin.onclick=async()=>{const j=await (await fetch('/pin',{method:'POST'})).json();el.status.textContent=j.message;loadState();};
el.ban.onclick=async()=>{el.status.textContent='Finding a replacement…';const j=await (await fetch('/ban',{method:'POST'})).json();el.status.textContent=j.message;loadState();};
el.fav.onclick=async()=>{const j=await (await fetch('/favourite',{method:'POST'})).json();el.status.textContent=j.message;};
el.stopwatch.onclick=async()=>{const j=await (await fetch('/stop-watch',{method:'POST'})).json();el.status.textContent=j.message;loadState();};
el.dropvoice.onclick=async()=>{const j=await (await fetch('/drop-voice',{method:'POST'})).json();el.status.textContent=j.message;
  if(j.ok&&j.dropped){const row=voiceRow(j.dropped);if(row)setVoice(row,0);}
  loadState();};
loadState();
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    threading.Thread(target=_files_watchdog, daemon=True).start()   # alert if the folder gets evicted
    print(f"The Frame Machine control panel on http://{platform.node()}.local:{a.port}  (Ctrl-C to stop)")
    app.run(host=a.host, port=a.port)

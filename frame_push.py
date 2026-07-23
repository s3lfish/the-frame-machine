#!/usr/bin/env python3
"""
frame_push.py — fetch free public-domain art and push it to a Samsung The Frame.

Finds the TV by MAC (DHCP-proof), wakes it (Wake-on-LAN), and uploads one fresh piece,
optionally matted with a museum-style label + caption. Reads ~/.config/frame/config.json
for its defaults (written by the web panel, app.py); any flag below overrides it.

Examples:
    python3 frame_push.py                                          # uses config.json
    python3 frame_push.py --theme museum --describe made-up        # a random piece + a tall tale
    python3 frame_push.py --source cleveland --subject cats        # Cleveland, cats only
    python3 frame_push.py --preview /tmp/out.jpg --no-placard      # render only, don't touch the TV
"""

import argparse, io, os, re, json, math, random, socket, subprocess, sys, time, warnings, datetime, html, platform
import requests
from PIL import Image, ImageDraw, ImageFont
try:
    from wakeonlan import send_magic_packet
except Exception:
    send_magic_packet = None
try:
    import qrcode
except Exception:
    qrcode = None
try:                                    # optional: face detection for --googly (opencv)
    import cv2, numpy as np
except Exception:
    cv2 = None

warnings.filterwarnings("ignore")

CANVAS, MARGIN, JPEG_Q = (3840, 2160), 0.86, 90
# Met Museum Collection API — keyless, public-domain, images download cleanly.
# (Switched off the Art Institute of Chicago in 2026-06: its IIIF image host
#  started returning 403 to all programmatic requests, even with a browser UA.)
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
MET_OBJECTS = "https://collectionapi.metmuseum.org/public/collection/v1/objects"  # every object id
HEADERS = {"User-Agent": "frame-art/1.0 (https://github.com/s3lfish/the-frame-machine)"}
# Full browser-ish header set — the Met's public www site sits behind a Vercel bot
# check that 403/429s a bare User-Agent, but lets a complete header set through.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.metmuseum.org/art/collection",
    "Upgrade-Insecure-Requests": "1",
}
# Default "wall art" filter (used when no specific types are chosen and all_types is off).
CLASS_OK = ("painting", "print", "drawing", "woodblock", "watercolor", "pastel")
# Selectable object-type families -> classification keywords matched against the Met
# object's classification. The GUI shows one checkbox per label; --types picks a subset.
TYPE_FILTERS = {
    "Paintings":    ("painting",),
    "Prints":       ("print", "woodblock"),
    "Drawings":     ("drawing", "watercolor", "pastel"),
    "Photographs":  ("photograph",),
    "Sculpture":    ("sculpture",),
    "Ceramics":     ("ceramic", "porcelain"),
    "Glass":        ("glass",),
    "Metalwork":    ("metal",),
    "Textiles":     ("textile",),
    "Furniture":    ("furniture", "woodwork"),
    "Jewelry":      ("jewel",),
    "Arms & Armor": ("arms", "armor"),
}
MAT_COLORS = {"off_white": (242,240,234), "linen": (228,222,210), "charcoal": (38,38,40), "black": (12,12,12)}
TERM_POOL = ["Monet","van Gogh","Cezanne","Caillebotte","Seurat","Renoir","Pissarro","Sisley",
             "Degas","Manet","Gauguin","Cassatt","Morisot","Whistler","Homer","Turner","Constable",
             "Hiroshige","Hokusai","ukiyo-e","Rembrandt","Vermeer","landscape","still life",
             "Corot","Gericault","Delacroix","Bonnard","Vuillard","Klimt","Schiele","Bruegel"]
# Named style pools — artist surnames only. The Met's text search is fuzzy (a
# search for "Hiroshige" also returns Monet, tagged with Japanese influence), so
# for a themed run we ALSO require the fetched object's artist to be one of these
# names. That makes each genre clean. --theme cycle walks THEME_CYCLE one step per
# calendar day, so the Frame marches through a different genre each day.
THEMES = {
    "impressionist": ["Monet","Renoir","Pissarro","Sisley","Degas","Manet","Caillebotte",
                      "Morisot","Cassatt","Seurat","van Gogh","Cezanne","Gauguin","Bonnard",
                      "Vuillard","Signac"],
    "ukiyo-e":       ["Hiroshige","Hokusai","Utamaro","Kuniyoshi","Kunisada","Yoshitoshi",
                      "Kiyonaga","Toyokuni","Sharaku","Eisen"],
    "old-masters":   ["Rembrandt","Vermeer","Hals","Rubens","Titian","Ruisdael","Steen",
                      "Bruegel","Poussin","Velazquez","Murillo","El Greco"],
    "landscape":     ["Corot","Turner","Constable","Church","Bierstadt","Cole","Cropsey",
                      "Inness","Daubigny","Durand","Kensett","Rousseau"],
    "mix":           TERM_POOL,
}
THEME_CYCLE = ["impressionist", "ukiyo-e", "old-masters", "landscape"]
# Seasonal search bias (hemisphere-aware) — picked when --seasonal is on.
SEASON_TERMS = {"winter": ["snow", "winter", "ice", "frost"],
                "spring": ["blossom", "spring", "flowers", "cherry blossom"],
                "summer": ["summer", "sea", "beach", "garden"],
                "autumn": ["autumn", "harvest", "leaves", "moon"]}
def seasonal_terms(hemisphere="north"):
    m = datetime.date.today().month
    if hemisphere == "south":
        m = (m + 5) % 12 + 1
    season = ("winter", "spring", "summer", "autumn")[((m % 12) // 3)]
    return SEASON_TERMS[season]

# Date-ranged holiday search bias (month,day) inclusive; last range wraps the year end.
# The holiday's own name leads the list so a typed subject can combine into e.g. "cats christmas".
HOLIDAYS = [
    ((10, 24), (10, 31), ["halloween", "skeleton", "witch", "ghost", "skull"]),   # Halloween
    ((12, 20), (12, 27), ["christmas", "nativity", "angel", "snow"]),             # Christmas
    ((2, 10),  (2, 15),  ["valentine", "lovers", "cupid", "romance"]),            # Valentine's
    ((12, 30), (1, 2),   ["new year", "feast", "celebration", "fireworks"]),      # New Year
]
def holiday_terms():
    md = (datetime.date.today().month, datetime.date.today().day)
    for start, end, terms in HOLIDAYS:
        within = (start <= md <= end) if start <= end else (md >= start or md <= end)
        if within:
            return terms
    return None

# Live weather -> search bias. open-meteo is keyless; it reports a WMO weather code
# which we bucket into a handful of moods, each with its own art search terms.
WEATHER_TERMS = {
    "clear": ["sunshine", "sunny", "blue sky", "summer"],
    "cloud": ["clouds", "overcast", "grey sky"],
    "fog":   ["fog", "mist", "haze"],
    "rain":  ["rain", "storm", "umbrella", "rainy day"],
    "snow":  ["snow", "winter", "frost", "blizzard"],
    "storm": ["storm", "lightning", "tempest", "thunder"],
}
def _wmo_bucket(code):
    if code in (1, 2, 3):                       return "cloud"
    if code in (45, 48):                        return "fog"
    if code in (71, 73, 75, 77, 85, 86):        return "snow"
    if code in (95, 96, 99):                    return "storm"
    if 51 <= code <= 82:                        return "rain"   # drizzle/rain/showers
    return "clear"                              # 0 clear, and anything unexpected

def _geolocate():
    """Best-effort (lat, lon) from the machine's public IP, cached ~7 days. Keyless."""
    cache = os.path.join(CFG, "geo.json")
    try:
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7*86400:
            g = json.load(open(cache)); return g.get("lat"), g.get("lon")
    except Exception:
        pass
    j = met_json("https://ipapi.co/json/", timeout=10)
    lat, lon = j.get("latitude"), j.get("longitude")
    if lat is not None and lon is not None:
        try:
            os.makedirs(CFG, exist_ok=True); json.dump({"lat": lat, "lon": lon}, open(cache, "w"))
        except Exception:
            pass
        return lat, lon
    return None, None

def weather_terms(latitude=None, longitude=None):
    """Search terms matching the current weather at (lat,lon) via open-meteo (keyless).
    Falls back to IP geolocation when no coordinates are configured; None if it can't tell."""
    lat, lon = latitude, longitude
    if lat is None or lon is None:
        lat, lon = _geolocate()
    if lat is None or lon is None:
        return None
    j = met_json("https://api.open-meteo.com/v1/forecast",
                 params={"latitude": lat, "longitude": lon, "current_weather": "true"}, timeout=15)
    code = (j.get("current_weather") or {}).get("weathercode")
    if code is None:
        return None
    bucket = _wmo_bucket(int(code))
    print(f"  weather: code {code} -> {bucket}")
    return WEATHER_TERMS.get(bucket)

def on_this_day_terms():
    """Search terms from real historical events on today's calendar date (Wikipedia's
    'On this day', keyless). Prefers OLDER events (they map to museum collections far
    better than modern news) and returns several candidate titles, or None."""
    t = datetime.date.today()
    j = met_json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{t.month:02d}/{t.day:02d}",
                 timeout=15)
    events = j.get("events") or []
    if not events:
        return None
    events.sort(key=lambda e: e.get("year") or 9999)     # oldest first
    pool = events[:max(6, len(events)//2)]               # from the older half
    random.shuffle(pool)
    terms = []
    for ev in pool:
        for pg in (ev.get("pages") or []):               # one salient page title per event
            name = (pg.get("normalizedtitle") or pg.get("title") or "").replace("_", " ").strip()
            if name and not re.fullmatch(r"\d{1,4}", name) and "List of" not in name:
                terms.append(name); break
        if len(terms) >= 6:
            break
    if terms:
        print(f"  on this day: {'; '.join(terms)}")
    return terms or None

def bias_terms(subject="", holidays=False, seasonal=False, hemisphere="north",
               weather=False, on_this_day=False, latitude=None, longitude=None):
    """Search terms a special mode wants, or None for 'normal'. A typed subject COMBINES
    with an active bias (e.g. 'cats' in December -> 'cats christmas'). When several biases
    are on, the most specific wins: holiday > on-this-day > weather > season."""
    base = (holiday_terms() if holidays else None) \
        or (on_this_day_terms() if on_this_day else None) \
        or (weather_terms(latitude, longitude) if weather else None) \
        or (seasonal_terms(hemisphere) if seasonal else None)
    subject = (subject or "").strip()
    if subject and base:
        return [f"{subject} {t}" for t in base]
    if subject:
        return [subject]
    return base
TMP = "/tmp/frame_art"
# Your Frame's wireless MAC — set it here or via the FRAME_MAC env var / --mac.
# Find it on the TV: Settings > General/Support > About This TV (or your router).
FRAME_MAC = os.environ.get("FRAME_MAC", "")
HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".config/frame")
HEARTBEAT = os.path.join(CFG, "last_run.txt")
STATE = os.path.join(CFG, "uploaded.json")
CONFIG = os.path.join(CFG, "config.json")   # written by the web GUI, read here for defaults

# Defaults the GUI can override via config.json.
DEFAULTS = {"mac": "", "ip": None, "description": "made-up", "content": "museum",
            "all_types": True, "types": [], "placard": True, "qr": True, "mat": "charcoal",
            "fetch": 1, "replace": True, "frequency": "daily", "time": "07:30",
            "every": 1, "every_unit": "days",
            "ntfy_topic": "", "tone": ["whimsical"], "source": "met", "orientation": "landscape",
            "pinned": False, "seasonal": False, "hemisphere": "north",
            "subject": "", "holidays": False, "weather": False, "on_this_day": False,
            "seasonal_chance": 0.0, "holidays_chance": 0.0, "weather_chance": 0.0, "on_this_day_chance": 0.0,
            "googly": False, "googly_chance": 0.0, "googly_strictness": 0.5,
            "latitude": None, "longitude": None, "tone_weights": {},
            "watch_on_fail": True, "watch_interval": 60, "watch_timeout": 180}
_TONE_WEIGHTS = None   # per-run override for made-up-voice weights (set from --tone-weights), else config's
STATUS = os.path.join(CFG, "status.json")   # last-run outcome, for alerts + the dashboard
HISTORY = os.path.join(CFG, "history.json") # recently displayed pieces (for no-repeats + dashboard)
BLOCKLIST = os.path.join(CFG, "blocklist.json")  # ids the user has banned
CURRENT_IMG = os.path.join(CFG, "current.jpg")   # a copy of what's on the TV now (dashboard thumb)
HIST_IMG_DIR = os.path.join(CFG, "history_imgs") # recent renders, kept so you can step back/forward
NAV = os.path.join(CFG, "nav.json")              # where you currently are when browsing history
FAVS_DIR = os.path.join(CFG, "favourites")       # saved favourite images
FAVOURITES = os.path.join(CFG, "favourites.json")  # favourite metadata
HISTORY_MAX = 40                            # how many past pieces (and their images) to keep
LAST_PIECES = []                            # meta of pieces prepped this run (for status)

def history_navlist():
    """History entries whose saved image still exists — the pieces you can browse back to."""
    return [h for h in _load_list(HISTORY) if h.get("file") and os.path.exists(h["file"])]

def nav_get():
    try:
        return int(json.load(open(NAV)).get("index", -1))
    except Exception:
        return -1

def nav_set(i):
    _save_list(NAV, {"index": i})

def _load_list(path):
    try:
        return json.load(open(path)) if os.path.exists(path) else []
    except Exception:
        return []

def _save_list(path, data):
    try:
        os.makedirs(CFG, exist_ok=True); json.dump(data, open(path, "w"))
    except Exception:
        pass

def load_config():
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG):
            cfg.update({k: v for k, v in json.load(open(CONFIG)).items() if v is not None})
    except Exception as e:
        print(f"  ! config read: {str(e)[:80]}", file=sys.stderr)
    return cfg

def write_status(ok, message, extra=None):
    """Record the last run's outcome (the dashboard + alerts read this)."""
    d = {"ok": ok, "when": datetime.datetime.now().isoformat(timespec="seconds"), "message": message}
    if extra:
        d.update(extra)
    try:
        os.makedirs(CFG, exist_ok=True); json.dump(d, open(STATUS, "w"), indent=2)
    except Exception:
        pass

def ntfy_alert(title, message):
    """Push a phone notification via ntfy.sh if a topic is configured (headless-friendly)."""
    topic = load_config().get("ntfy_topic")
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                      headers={"Title": title, "Priority": "high", "Tags": "framed_picture"}, timeout=10)
    except Exception:
        pass

# ---------- polite, cached HTTP (avoids hammering / rate-limit 403s) ----------
CACHE_DIR = os.path.join(CFG, "cache")

def http_get(url, params=None, headers=None, tries=4, timeout=30):
    """GET with exponential backoff on 403/429/5xx and transient errors. Returns a
    Response or None."""
    delay = 1.5
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code not in (403, 429, 500, 502, 503, 504):
                return r
        except Exception:
            pass
        time.sleep(delay); delay *= 2
    return None

def met_json(url, params=None, timeout=30):
    r = http_get(url, params=params, timeout=timeout)
    try:
        return r.json() if r is not None else {}
    except Exception:
        return {}

def met_object(oid):
    """A single object record, cached on disk forever (records are immutable)."""
    p = os.path.join(CACHE_DIR, f"obj_{oid}.json")
    try:
        if os.path.exists(p):
            return json.load(open(p))
    except Exception:
        pass
    o = met_json(MET_OBJECT.format(id=oid))
    if o.get("objectID"):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True); json.dump(o, open(p, "w"))
        except Exception:
            pass
    return o

# ---------- locate the TV by MAC (verify reachability, avoid stale ARP) ----------
def _arp_ip_for_mac(mac):
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if mac.lower() in line.lower():
            m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
            if m:
                return m.group(1)
    return None

# 1-second timeout flag differs by OS: -t on macOS is seconds, but on Linux -t is TTL,
# so Linux needs -W. (Getting this wrong makes a /24 sweep hang on every dead IP.)
_PING = ["ping", "-c1"] + (["-t", "1"] if platform.system() == "Darwin" else ["-W", "1"])

def _reachable(ip):
    return subprocess.run(_PING + [ip], capture_output=True).returncode == 0

def resolve_frame_ip(preferred, mac):
    # trust preferred only if it actually answers and maps to the MAC
    if preferred and _reachable(preferred):
        if _arp_ip_for_mac(mac) == preferred:
            return preferred
    # otherwise sweep, then take a *reachable* ARP match
    base = ".".join((preferred or "192.168.1.1").split(".")[:3])
    procs = [subprocess.Popen(_PING + [f"{base}.{i}"],
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(1, 255)]
    for p in procs:
        p.wait()
    ip = _arp_ip_for_mac(mac)
    return ip

# ---------- wake + art-channel readiness ----------
def wake(mac):
    if send_magic_packet:
        try:
            send_magic_packet(mac)
        except Exception:
            pass

def ensure_art_ready(art, mac, wait, retries):
    """WoL, wait, then probe the art channel. Raises if it never answers."""
    last = None
    for i in range(1, retries + 1):
        wake(mac)
        time.sleep(wait)
        for probe in ("get_artmode", "get_current", "available"):
            fn = getattr(art, probe, None)
            if fn:
                try:
                    fn()
                    return
                except Exception as e:
                    last = e
                    break
        print(f"  art channel not ready ({i}/{retries}): {last}", file=sys.stderr)
    raise RuntimeError(f"Frame art channel unresponsive after {retries} wake attempts ({last}). "
                       "Enable 'Power On with Mobile' / Wake-on-LAN on the TV, or run when it's awake.")

# ---------- auto-watch: when the TV is asleep, retry the push once it wakes ----------
WATCH_PID = os.path.join(CFG, "watcher.pid")
WATCH_LOG = os.path.join(CFG, "watcher.log")

class _Unreachable(RuntimeError):
    """The Frame's art channel couldn't be reached (asleep / off the LAN)."""

def _port_open(ip, port=8002, timeout=3):
    """True if a TCP connection to ip:port succeeds — i.e. the art API is actually up
    (the TV can answer pings in standby while this port stays closed)."""
    if not ip:
        return False
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def tv_ready(preferred, mac, deep=False):
    """The TV's IP if its art channel (port 8002) is accepting connections, else None.
    Fast path probes the known IP; `deep` re-resolves by MAC in case the IP changed."""
    if _port_open(preferred):
        return preferred
    if deep:
        ip = resolve_frame_ip(preferred, mac)
        if ip and _port_open(ip):
            return ip
    return None

def _watcher_running():
    """True if a watcher process from a previous run is still alive (avoid duplicates)."""
    try:
        pid = int(open(WATCH_PID).read().strip())
        os.kill(pid, 0)                                  # signal 0 only checks the pid exists
        return True
    except Exception:
        return False

def _spawn_watcher(argv):
    """Relaunch this script in --watch mode, detached, so it outlives a quick
    scheduler/panel invocation and keeps polling for the TV."""
    cmd = [sys.executable, os.path.abspath(__file__), "--watch"] + [a for a in argv if a != "--watch"]
    try:
        os.makedirs(CFG, exist_ok=True)
        log = open(WATCH_LOG, "a")
        p = subprocess.Popen(cmd, start_new_session=True, stdout=log, stderr=subprocess.STDOUT)
        open(WATCH_PID, "w").write(str(p.pid))
        return True
    except Exception as e:
        print(f"  ! couldn't start watcher: {str(e)[:100]}", file=sys.stderr)
        return False

def _maybe_watch(args, reason):
    """Handle a 'TV unreachable' outcome: hand off to a background watcher that retries
    when the TV wakes, or (if we ARE the watcher, or it's disabled/interactive) fail."""
    if getattr(args, "_watching", False):
        raise _Unreachable(reason)                       # we're the watcher — keep polling
    if not getattr(args, "watch_on_fail", True) or getattr(args, "no_record", False) or getattr(args, "files", None):
        raise RuntimeError(reason)                       # auto-watch off, or interactive nav/files
    if _watcher_running():
        print("  a watcher is already waiting for the TV.")
        write_status(False, reason + " A watcher is already waiting to retry.", {"waiting": True})
        return
    if _spawn_watcher(getattr(args, "_argv", [])):
        msg = reason + " Watching for the TV to wake — will retry automatically."
        print("  " + msg); write_status(False, msg, {"waiting": True}); ntfy_alert("Frame art waiting", msg)
    else:
        raise RuntimeError(reason)

def _watch_loop(args):
    """Poll until the TV's art channel is up, then do the real push. Gives up after
    watch_timeout minutes."""
    args._watching = True
    args.watch = False
    # Once the TV wakes, be extra patient about the art channel warming up (and about
    # someone accepting the on-TV pairing prompt) — more than a snappy foreground run.
    args.retries = max(int(getattr(args, "retries", 4) or 4), 6)
    args.wake_wait = max(int(getattr(args, "wake_wait", 12) or 12), 15)
    interval = max(10, int(getattr(args, "watch_interval", 60) or 60))
    mins = max(1, int(getattr(args, "watch_timeout", 180) or 180))
    deadline = time.time() + mins * 60
    print(f"Watcher up (pid {os.getpid()}): polling every {interval}s for up to {mins} min.")
    try:
        os.makedirs(CFG, exist_ok=True); open(WATCH_PID, "w").write(str(os.getpid()))
    except Exception:
        pass
    write_status(False, "Waiting for the TV to wake, then it'll change the art.", {"waiting": True})
    i = 0
    try:
        while time.time() < deadline:
            ip = tv_ready(args.ip, args.mac, deep=(i % 5 == 0))
            if ip:
                args.ip = ip
                print("TV art channel is up — pushing now.")
                try:
                    return run(args)
                except _Unreachable:
                    pass                                 # slipped back to sleep; keep waiting
            i += 1
            time.sleep(interval)
        gave = "Gave up — the TV didn't wake within the watch window."
        print(gave); write_status(False, gave); ntfy_alert("Frame art gave up", gave)
    finally:
        try:
            os.remove(WATCH_PID)
        except Exception:
            pass

# ---------- image prep ----------
def slug(s, n=50):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:n] or "art"

def mat_image(art, mat_rgb):
    cw, ch = CANVAS
    aw, ah = art.size
    scale = min(int(cw*MARGIN)/aw, int(ch*MARGIN)/ah)
    art = art.resize((max(1,int(aw*scale)), max(1,int(ah*scale))), Image.LANCZOS)
    canvas = Image.new("RGB", CANVAS, mat_rgb)
    canvas.paste(art, ((cw-art.width)//2, (ch-art.height)//2))
    return canvas

# ---------- googly eyes (optional silliness) ----------
def _draw_googly(draw, box):
    """Draw one wobbly cartoon eye filling `box` (x, y, w, h) in image coords."""
    x, y, w, h = box
    cx, cy, r = x + w/2, y + h/2, max(w, h) * 0.55
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(255, 255, 255),
                 outline=(20, 20, 20), width=max(2, int(r*0.09)))
    pr, off, ang = r*0.42, r*0.4, random.uniform(0, 2*math.pi)   # pupil, offset for the "googly" wobble
    px, py = cx + off*math.cos(ang), cy + off*math.sin(ang)
    draw.ellipse([px-pr, py-pr, px+pr, py+pr], fill=(12, 12, 12))

def add_googly_eyes(img, strictness=0.5):
    """Stick cartoon googly eyes on every detected face. Uses opencv Haar cascades; returns
    the image unchanged if opencv isn't installed or nothing face-like is found.
    `strictness` (0..1) sets how fussy detection is: 0 = eyes on anything vaguely face-ish
    (false "faces" are half the fun), 1 = only clear, unmistakable faces."""
    if cv2 is None:
        print("  ! googly eyes need opencv — run: pip install opencv-python-headless", file=sys.stderr)
        return img
    try:
        s = min(1.0, max(0.0, strictness))
        gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        base = cv2.data.haarcascades
        # Strictness scales every knob the cascade has: more corroborating neighbours,
        # a bigger minimum face, and a harsher "speck vs biggest face" cut.
        min_div = int(round(30 - 18*s))             # min face = 1/30th of the image (lax) .. 1/12th (strict)
        faces = list(cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml").detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3 + int(round(9*s)),
            minSize=(max(24, img.width//min_div), max(24, img.height//min_div))))
        if len(faces) > 1:
            biggest = max(fw*fh for (fx, fy, fw, fh) in faces)
            faces = [f for f in faces if f[2]*f[3] >= (0.06 + 0.44*s)*biggest]
        eye_cc = cv2.CascadeClassifier(base + "haarcascade_eye.xml")
        draw, n = ImageDraw.Draw(img), 0
        for (fx, fy, fw, fh) in faces:
            eyes = eye_cc.detectMultiScale(gray[fy:fy+fh, fx:fx+fw], scaleFactor=1.1,
                                           minNeighbors=6, minSize=(max(8, fw//12),)*2)
            eyes = sorted(eyes, key=lambda e: e[2]*e[3], reverse=True)[:2]
            if eyes:
                boxes = [(fx+ex, fy+ey, ew, eh) for (ex, ey, ew, eh) in eyes]
            elif s >= 0.75:                         # strict: a "face" with no findable eyes is probably a smudge
                continue
            else:                                   # no eyes detected — fake a pair in the upper face
                ew = fw // 5; ey = fy + int(fh*0.32)
                boxes = [(fx + int(fw*0.24) - ew//2, ey, ew, ew),
                         (fx + int(fw*0.66) - ew//2, ey, ew, ew)]
            for b in boxes:
                _draw_googly(draw, b)
            n += 1
        print(f"  googly eyes: {n} face(s) (strictness {s:.2f})")
        return img
    except Exception as e:
        print(f"  ! googly eyes: {str(e)[:100]}", file=sys.stderr)
        return img

# ---------- museum-placard layout (artwork + caption panel) ----------
# Serif fonts by style, with cross-platform fallbacks (macOS Georgia, else common
# Linux serifs) so the placard renders in Docker / on a Pi too.
_MAC = "/System/Library/Fonts/Supplemental"
_LIN = "/usr/share/fonts/truetype"
_FONT_CANDIDATES = {
    "Georgia.ttf":             [f"{_MAC}/Georgia.ttf", f"{_LIN}/liberation/LiberationSerif-Regular.ttf", f"{_LIN}/dejavu/DejaVuSerif.ttf"],
    "Georgia Bold.ttf":        [f"{_MAC}/Georgia Bold.ttf", f"{_LIN}/liberation/LiberationSerif-Bold.ttf", f"{_LIN}/dejavu/DejaVuSerif-Bold.ttf"],
    "Georgia Italic.ttf":      [f"{_MAC}/Georgia Italic.ttf", f"{_LIN}/liberation/LiberationSerif-Italic.ttf", f"{_LIN}/dejavu/DejaVuSerif-Italic.ttf"],
    "Georgia Bold Italic.ttf": [f"{_MAC}/Georgia Bold Italic.ttf", f"{_LIN}/liberation/LiberationSerif-BoldItalic.ttf", f"{_LIN}/dejavu/DejaVuSerif-BoldItalic.ttf"],
}
_font_cache = {}
def _font(name, size):
    key = (name, size)
    if key not in _font_cache:
        f = None
        for path in _FONT_CANDIDATES.get(name, [f"{_MAC}/{name}"]):
            if os.path.exists(path):
                f = ImageFont.truetype(path, size); break
        _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]

def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in str(text).split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    return lines

def _truncate_prose(text, limit=620):
    """Trim to whole sentences up to ~limit chars, so the panel stays readable."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    out = ""
    for sent in re.split(r"(?<=[.!?]) ", text):
        if out and len(out) + len(sent) + 1 > limit:
            break
        out = (out + " " + sent).strip()
    return out or text[:limit].rsplit(" ", 1)[0] + "…"

def met_prose(object_url):
    """The Met's own curatorial description, scraped from the public object page
    (the JSON API doesn't carry it). Returns trimmed prose, or None if there's none."""
    if not object_url:
        return None
    r = http_get(object_url, headers=BROWSER_HEADERS)
    if r is None:
        return None
    h = r.text
    best = ""
    for m in re.finditer(r"read-more-wrapper[^>]*>", h):   # description lives in this block
        end = h.find("</div>", m.end())
        if end < 0:
            continue
        inner = re.sub(r"<br\s*/?>", "\n", h[m.end():end])
        inner = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if len(inner) > len(best):
            best = inner
    return _truncate_prose(best) if len(best) >= 40 else None

# Caption "tones/voices" for the made-up tale — one instruction each. The GUI lists
# them as checkboxes (add one here and it appears automatically); one is picked per run.
TONES = {
    "whimsical":    "a joyfully absurd, tongue-in-cheek tall tale (secret purposes, imagined former owners, unlikely adventures)",
    "noir":         "a hard-boiled film-noir vignette, all shadows, betrayal and cigarette smoke",
    "epic":         "a grandiose, mock-heroic legend in the breathless voice of an ancient epic",
    "haiku":        "exactly two lines of vivid, evocative imagery (haiku-like, no need to count syllables)",
    "limerick":     "a single cheeky limerick (five lines, AABBA rhyme) — you may exceed two sentences for this one",
    "conspiracy":   "a paranoid conspiracy theory connecting it to secret societies and cover-ups, delivered deadpan",
    "pirate":       "a swashbuckling pirate's yarn — 'arr', grog, cursed doubloons and buried treasure",
    "shakespeare":  "a dramatic mock-Shakespearean soliloquy, all thee, thou, forsooth and anguished asides",
    "corporate":    "soulless corporate-speak — synergy, deliverables, circling back, leveraging learnings",
    "genz":         "chaotic Gen-Z internet slang — no cap, unserious, it's giving, lowkey iconic, core",
    "attenborough": "a hushed David-Attenborough nature-documentary narration, observing it like rare wildlife",
    "sarcastic":    "a dry, deadpan, sarcastic remark — mock-unimpressed, heavy eye-roll, faux boredom",
    "topical":      "a wry, tongue-in-cheek link to current events",
    "firstperson":  "narrated in first person BY the main subject of the artwork, describing their day",
    "trailer":      "a booming movie-trailer voiceover — 'In a world…', dramatic pauses, high stakes",
    "gossip":       "breathless tabloid gossip about the artwork and everyone supposedly in it",
    "dadjoke":      "groan-worthy dad jokes and puns inspired by the artwork's subject",
    "fairytale":    "the opening of a storybook fairy tale — 'Once upon a time…' — cosy and enchanted",
}

def _headline():
    """A recent news headline (BBC RSS, keyless) so the 'topical' tone is actually topical."""
    r = http_get("https://feeds.bbci.co.uk/news/rss.xml", timeout=10)
    if not r:
        return None
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text)
    heads = [t.strip() for t in titles[1:] if t.strip()]   # [0] is the feed's own title
    return random.choice(heads[:10]) if heads else None

def _weighted_tone(tones):
    """Pick one voice from `tones`, honouring per-voice relative weights (from the
    --tone-weights override or config['tone_weights']; default 1.0 each). A weight of 0
    excludes a voice; if every weight is 0 or none are set, it's a plain uniform pick.
    Weights only bias which of the *enabled* voices comes up — turning a voice fully off
    is still done by removing it from the list, so there's no feedback loop."""
    tones = list(tones)
    if not tones:
        return "whimsical"
    tw = _TONE_WEIGHTS if _TONE_WEIGHTS is not None else (load_config().get("tone_weights") or {})
    weights = [max(0.0, float(tw.get(t, 1.0))) for t in tones]
    if sum(weights) <= 0:
        return random.choice(tones)
    return random.choices(tones, weights=weights, k=1)[0]

def ai_blurb(meta, tone="whimsical"):
    """Fallback when there's no real prose: a deliberately fake, self-evidently invented
    mini-tale from Claude. `tone` may be one tone or a weighted list — one is picked.
    Returns (text, tone_used) so the caller can show/act on which voice was chosen."""
    if isinstance(tone, (list, tuple)):
        tone = _weighted_tone(tone) if tone else "whimsical"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        kf = os.path.join(CFG, "anthropic_key.txt")
        if os.path.exists(kf):
            key = open(kf).read().strip()
    if not key:
        return None, None
    facts = ", ".join(f"{k}: {meta[k]}" for k in ("artist", "title", "date", "medium",
             "culture", "objectName") if meta.get(k))
    style = TONES.get(tone, TONES["whimsical"])
    if tone == "topical":
        head = _headline()
        if head:
            style = ("a witty, tongue-in-cheek connection between this artwork and a REAL recent "
                     f'news headline. The headline is: "{head}". Riff on it with a knowing wink')
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 220,
                  "messages": [{"role": "user", "content":
                    f"This museum object has no real description, so invent one as {style}. "
                    "Keep it warm and PG, nod to the object's name/date/material, then run wild. "
                    "Two sentences (unless the style says otherwise). Output only the text — no "
                    "title, heading, markdown, preamble, or disclaimer.\n" + facts}]},
            timeout=30)
        text = r.json()["content"][0]["text"].strip()
        text = re.sub(r"^\s*#+.*$", "", text, flags=re.M).strip()   # drop any stray heading line
        text = _truncate_prose(text) or None
        return text, (tone if text else None)
    except Exception as e:
        print(f"  ! ai_blurb: {str(e)[:80]}", file=sys.stderr)
        return None, None

def mat_with_placard(art, meta, mat_rgb, desc=None, link=None):
    """Fit the artwork on the left and render a gallery-label panel on the right,
    so photographed sculpture/objects (and paintings) all look intentional."""
    cw, ch = CANVAS
    M, TEXT_W, GAP = 165, 1040, 130
    canvas = Image.new("RGB", CANVAS, mat_rgb)
    draw = ImageDraw.Draw(canvas)

    # --- artwork, fitted into the left region, with a thin keyline ---
    region_w, region_h = cw - 2*M - TEXT_W - GAP, ch - 2*M
    aw, ah = art.size
    scale = min(region_w/aw, region_h/ah)
    nw, nh = max(1, int(aw*scale)), max(1, int(ah*scale))
    art_r = art.resize((nw, nh), Image.LANCZOS)
    ax, ay = M + (region_w - nw)//2, M + (region_h - nh)//2
    canvas.paste(art_r, (ax, ay))
    draw.rectangle([ax-1, ay-1, ax+nw, ay+nh], outline=(92, 92, 94), width=2)

    # --- caption text on the right ---
    ink, sub, dim = (236, 233, 226), (178, 174, 166), (140, 136, 129)
    tx = cw - M - TEXT_W
    maker = (meta.get("artist") or meta.get("culture") or meta.get("objectName") or "Unknown").strip()
    title = (meta.get("title") or "Untitled").strip()
    body = (198, 195, 187)
    # (font_file, size, colour, text, gap_below) — empties are skipped
    rows = [("Georgia Bold.ttf", 60, ink, maker, 6),
            ("Georgia Italic.ttf", 33, sub, meta.get("bio"), 44),
            ("Georgia Italic.ttf", 50, ink, title, 4),
            ("Georgia.ttf", 37, sub, meta.get("date"), 40),
            ("Georgia.ttf", 31, body, desc, 40),
            ("Georgia.ttf", 33, dim, meta.get("medium"), 6),
            ("Georgia.ttf", 33, dim, meta.get("culture_period"), 6),
            ("Georgia.ttf", 29, dim, meta.get("dimensions"), 40),
            ("Georgia Italic.ttf", 28, dim, meta.get("credit"), 26),
            ("Georgia.ttf", 26, dim, meta.get("museum") or "The Metropolitan Museum of Art", 0)]

    # measure, then vertically centre the whole block
    laid = []
    total = 0
    for fname, size, colour, text, gap in rows:
        if not text or not str(text).strip():
            continue
        font = _font(fname, size)
        lines = _wrap(draw, text, font, TEXT_W)
        lh = int(size * 1.32)
        laid.append((font, colour, lines, lh, gap))
        total += lh * len(lines) + gap
    y = max(M, (ch - total)//2)
    for font, colour, lines, lh, gap in laid:
        for ln in lines:
            draw.text((tx, y), ln, font=font, fill=colour)
            y += lh
        y += gap

    # QR to the real Met page (the tale above is invented), bottom-right of the panel
    if link and qrcode:
        try:
            qr = qrcode.QRCode(border=2, box_size=10)
            qr.add_data(link); qr.make(fit=True)
            S = 200
            q = qr.make_image(fill_color=(30, 30, 32), back_color=(214, 210, 202)).convert("RGB").resize((S, S), Image.NEAREST)
            qx, qy = cw - M - S, ch - M - S
            canvas.paste(q, (qx, qy))
        except Exception as e:
            print(f"  ! qr: {str(e)[:80]}", file=sys.stderr)
    return canvas

def _render_piece(art, meta, mat_rgb, path, placard, describe, qr, tone, page_url, real_text=None, scrape=False, googly_chance=0.0, googly_strict=0.5):
    """Render one artwork to `path`: plain art, or a placard with an optional caption + QR.
    'real' captions come from `real_text` (Cleveland) or by scraping the Met page (scrape=True).
    Googly eyes are applied at random with probability `googly_chance` (0..1); `googly_strict`
    sets how fussy the face detection is."""
    if googly_chance and random.random() < googly_chance:
        art = add_googly_eyes(art, googly_strict)
    desc, caption_style = None, None
    if placard and describe == "real":
        desc = _truncate_prose(real_text) if real_text else (met_prose(page_url) if scrape else None)
    elif placard and describe == "made-up":
        desc, caption_style = ai_blurb(meta, tone)
    if desc:
        print(f"    + {describe}{f' ({caption_style})' if caption_style else ''}: {desc[:60]}...")
    link = page_url if (placard and describe != "off" and qr) else None
    canvas = mat_with_placard(art, meta, mat_rgb, desc, link) if placard else mat_image(art, mat_rgb)
    canvas.save(path, "JPEG", quality=JPEG_Q)
    return caption_style, desc   # voice used (None for real/off) + the caption text, for the panel

def plan_search(query, theme):
    """Return (terms_to_search, required_artists). required_artists is a lowercased
    list the fetched object's artist must match (None = no artist filtering)."""
    if query:
        return [query], None
    if theme:
        name = theme
        if theme == "cycle":
            name = THEME_CYCLE[datetime.date.today().toordinal() % len(THEME_CYCLE)]
        pool = THEMES.get(name, TERM_POOL)
        print(f"  theme: {name}")
        terms = random.sample(pool, k=min(6, len(pool)))
        # For a named genre, keep only works actually by one of its artists.
        require = [a.lower() for a in pool] if name != "mix" else None
        return terms, require
    return random.sample(TERM_POOL, k=min(6, len(TERM_POOL))), None

def all_object_ids():
    """Every object id in the collection (~half a million), cached locally for a
    week. Used by --theme museum to pull a true random piece from the whole Met."""
    cache = os.path.join(CFG, "met_object_ids.json")
    try:
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7*86400:
            return json.load(open(cache))
    except Exception:
        pass
    ids = met_json(MET_OBJECTS, timeout=90).get("objectIDs") or []
    try:
        json.dump(ids, open(cache, "w"))
    except Exception:
        pass
    return ids

def fetch_matted(count, query, mat_rgb, theme=None, placard=False, all_types=False, describe="off", types=None, qr=True, tone="whimsical", avoid=None, seasonal=False, hemisphere="north", subject="", holidays=False, weather=False, on_this_day=False, latitude=None, longitude=None, googly_chance=0.0, googly_strict=0.5):
    os.makedirs(TMP, exist_ok=True)
    LAST_PIECES.clear()
    avoid = avoid or set()
    bias = bias_terms(subject, holidays, seasonal, hemisphere, weather, on_this_day, latitude, longitude)
    # Gather candidate object IDs, then pull each object's record and download its
    # public-domain image until we have enough.
    if bias and not query:
        print(f"  bias: {'/'.join(bias)}")
        require_artists = None
        oids = []
        for kw in bias:
            hits = met_json(MET_SEARCH, params={"q": kw, "hasImages": "true"}).get("objectIDs") or []
            random.shuffle(hits); oids.extend(hits[:60])
        random.shuffle(oids)
    elif theme == "museum" and types and not all_types:
        # Targeted: search the collection for the chosen type families (dense pool,
        # far fewer fetches than random-sampling 500k ids). The per-object
        # classification check below still confirms each hit really is that type.
        print(f"  source: whole collection, types: {', '.join(types)}")
        require_artists = None
        oids = []
        for kw in (k for t in types for k in TYPE_FILTERS.get(t, ())):
            hits = met_json(MET_SEARCH, params={"q": kw, "hasImages": "true"}).get("objectIDs") or []
            random.shuffle(hits); oids.extend(hits[:80])
        random.shuffle(oids)
    elif theme == "museum":
        print("  source: whole collection (surprise me)")
        require_artists = None               # no artist lock — but the type filter still applies
        pool = all_object_ids()
        oids = random.sample(pool, min(600, len(pool))) if pool else []
    else:
        terms, require_artists = plan_search(query, theme)
        oids = []
        for q in terms:
            hits = met_json(MET_SEARCH, params={"q": q, "hasImages": "true"}).get("objectIDs") or []
            random.shuffle(hits)
            oids.extend(hits[:60])           # cap per term so one term can't dominate
        random.shuffle(oids)
    seen = set()
    paths = []
    for oid in oids:
        if len(paths) >= count:
            break
        if oid in seen or f"met:{oid}" in avoid:
            continue
        seen.add(oid)
        try:
            o = met_object(oid)
            if not o.get("isPublicDomain"):
                continue
            cls = (o.get("classification") or "").lower()
            if not all_types:
                if types:
                    allowed = tuple(k for t in types for k in TYPE_FILTERS.get(t, ()))
                else:
                    allowed = CLASS_OK
                if not any(k in cls for k in allowed):
                    continue
            if require_artists:
                artist = (o.get("artistDisplayName") or "").lower()
                if not any(a in artist for a in require_artists):
                    continue
            img_url = o.get("primaryImage") or ""
            if not img_url:
                continue
            img = requests.get(img_url, headers=HEADERS, timeout=60)
            img.raise_for_status()
            art = Image.open(io.BytesIO(img.content)).convert("RGB")
            artist_s, culture, period = (o.get("artistDisplayName") or ""), (o.get("culture") or ""), (o.get("period") or "")
            cp = " · ".join(x for x in [culture, period] if x.strip()) if artist_s else period.strip()
            meta = {"title": o.get("title"), "artist": artist_s, "bio": o.get("artistDisplayBio"),
                    "date": o.get("objectDate"), "medium": o.get("medium"), "dimensions": o.get("dimensions"),
                    "culture": culture, "objectName": o.get("objectName"), "culture_period": cp,
                    "credit": o.get("creditLine"), "museum": "The Metropolitan Museum of Art"}
            p = os.path.join(TMP, f"{len(paths)+1:02d}_{slug(o.get('title','art'))}.jpg")
            caption_style, caption = _render_piece(art, meta, mat_rgb, p, placard, describe, qr, tone, o.get("objectURL"), scrape=True, googly_chance=googly_chance, googly_strict=googly_strict)
            paths.append(p)
            LAST_PIECES.append({"title": o.get("title") or "", "source": o.get("_source_name", "The Met"),
                                "artist": o.get("artistDisplayName") or o.get("culture") or "Unknown",
                                "url": o.get("objectURL") or "", "id": f"met:{o.get('objectID')}",
                                "caption_style": caption_style or "", "caption": caption or "",
                                "date": meta.get("date") or "", "medium": meta.get("medium") or "",
                                "dimensions": meta.get("dimensions") or "", "credit": meta.get("credit") or "",
                                "culture": meta.get("culture_period") or meta.get("culture") or ""})
            print(f"  prepped: {o.get('title','?')} — {o.get('artistDisplayName') or o.get('culture') or 'Unknown'}")
        except Exception as e:
            print(f"  ! skip {oid}: {str(e)[:120]}", file=sys.stderr)
    return paths

CLE_API = "https://openaccess-api.clevelandart.org/api/artworks/"
CLE_NAME = "Cleveland Museum of Art"

def fetch_cleveland(count, query, mat_rgb, theme=None, placard=False, describe="off", types=None, qr=True, tone="whimsical", avoid=None, seasonal=False, hemisphere="north", all_types=True, subject="", holidays=False, weather=False, on_this_day=False, latitude=None, longitude=None, googly_chance=0.0, googly_strict=0.5):
    """Second source: Cleveland Museum of Art open access (keyless, CC0). Its API carries
    a real 'description', so 'real' captions need no scraping."""
    os.makedirs(TMP, exist_ok=True); LAST_PIECES.clear()
    avoid = avoid or set()
    params = {"has_image": "1", "cc0": "1", "limit": "100", "indent": "1"}
    q = query
    _bias = bias_terms(subject, holidays, seasonal, hemisphere, weather, on_this_day, latitude, longitude)
    if not q and _bias:
        q = random.choice(_bias)
    elif not q and theme == "cycle":
        q = random.choice(THEMES[THEME_CYCLE[datetime.date.today().toordinal() % len(THEME_CYCLE)]])
    elif not q and theme in THEMES and theme != "mix":
        q = random.choice(THEMES[theme])
    elif not q and theme == "mix":
        q = random.choice(TERM_POOL)
    if q:
        params["q"] = q; print(f"  cleveland: {q}")
    else:                                        # museum: jump to a random page
        total = met_json(CLE_API, params={**params, "limit": "1"}).get("info", {}).get("total", 0)
        if total > 120:
            params["skip"] = str(random.randint(0, total - 100))
        print("  cleveland: whole collection")
    data = met_json(CLE_API, params=params).get("data") or []
    random.shuffle(data)
    paths = []
    for o in data:
        if len(paths) >= count:
            break
        try:
            if o.get("share_license_status") != "CC0" or f"cle:{o.get('id')}" in avoid:
                continue
            typ = (o.get("type") or "").lower()
            if not all_types:
                allowed = tuple(k for t in types for k in TYPE_FILTERS.get(t, ())) if types else CLASS_OK
                if not any(k in typ for k in allowed):
                    continue
            imgs = o.get("images") or {}
            img_url = (imgs.get("print") or imgs.get("web") or {}).get("url")
            if not img_url:
                continue
            cr = ((o.get("creators") or [{}])[0].get("description") or "")
            name, bio = cr, ""
            if "(" in cr:
                name, bio = cr.split("(")[0].strip(), cr[cr.find("(") + 1:].rstrip(")").strip()
            meta = {"title": o.get("title") or "Untitled", "artist": name, "bio": bio,
                    "date": o.get("creation_date"), "medium": o.get("technique"),
                    "dimensions": (o.get("measurements") or "").split(";")[0].strip(),
                    "culture": "; ".join(o.get("culture") or []), "objectName": o.get("type"),
                    "culture_period": ("; ".join(o.get("culture") or []) if not name else ""),
                    "credit": o.get("creditline"), "museum": CLE_NAME}
            r = http_get(img_url)
            if r is None:
                continue
            art = Image.open(io.BytesIO(r.content)).convert("RGB")
            p = os.path.join(TMP, f"{len(paths)+1:02d}_{slug(meta['title'])}.jpg")
            caption_style, caption = _render_piece(art, meta, mat_rgb, p, placard, describe, qr, tone, o.get("url"), real_text=o.get("description"), googly_chance=googly_chance, googly_strict=googly_strict)
            paths.append(p)
            LAST_PIECES.append({"title": meta["title"], "artist": name or meta["culture"] or "Unknown",
                                "url": o.get("url") or "", "source": CLE_NAME, "id": f"cle:{o.get('id')}",
                                "caption_style": caption_style or "", "caption": caption or "",
                                "date": meta.get("date") or "", "medium": meta.get("medium") or "",
                                "dimensions": meta.get("dimensions") or "", "credit": meta.get("credit") or "",
                                "culture": meta.get("culture_period") or meta.get("culture") or ""})
            print(f"  prepped: {meta['title']} — {name or meta['culture'] or 'Unknown'}")
        except Exception as e:
            print(f"  ! skip {o.get('id')}: {str(e)[:120]}", file=sys.stderr)
    return paths

def prep_local(files, mat_rgb, googly_chance=0.0, googly_strict=0.5):
    os.makedirs(TMP, exist_ok=True)
    out = []
    for f in files:
        im = Image.open(f).convert("RGB")
        googlied = bool(googly_chance) and random.random() < googly_chance
        if googlied:
            im = add_googly_eyes(im, googly_strict)
        if googlied or im.size != CANVAS:
            p = os.path.join(TMP, f"local_{slug(os.path.basename(f))}.jpg")
            mat_image(im, mat_rgb).save(p, "JPEG", quality=JPEG_Q); out.append(p)
        else:
            out.append(f)
    return out

def notify(msg):
    try:
        subprocess.run(["osascript", "-e",
            f'display notification "{msg}" with title "Frame art"'], capture_output=True)
    except Exception:
        pass

# ---------- main ----------
def favourite_pick():
    """A random saved favourite image path (and set it as the 'prepped' piece), or None."""
    favs = [f for f in _load_list(FAVOURITES) if os.path.exists(f.get("file", ""))]
    if not favs:
        return None
    fav = random.choice(favs)
    LAST_PIECES.clear()
    LAST_PIECES.append({k: fav.get(k, "") for k in ("title", "artist", "url", "source", "id")})
    return fav["file"]

def _roll(chance):
    """True with probability `chance` (0..1) — how a per-run bias mode fires."""
    return random.random() < (chance or 0.0)

def _fetch_source(args, mat_rgb, count):
    avoid = {str(x) for x in _load_list(BLOCKLIST)} | {str(h.get("id")) for h in _load_list(HISTORY)[-40:]}
    src = random.choice(["met", "cleveland"]) if args.source == "any" else args.source
    # Each bias mode is rolled once per run against its configured chance.
    seasonal    = _roll(args.seasonal_chance)
    holidays    = _roll(args.holidays_chance)
    weather     = _roll(args.weather_chance)
    on_this_day = _roll(args.on_this_day_chance)
    if src == "cleveland":
        return fetch_cleveland(count, args.query, mat_rgb, args.theme, args.placard,
                               args.describe, args.types, args.qr, args.tone, avoid,
                               seasonal, args.hemisphere, args.all_types, args.subject, holidays,
                               weather, on_this_day, args.latitude, args.longitude, args.googly_chance,
                               args.googly_strict)
    return fetch_matted(count, args.query, mat_rgb, args.theme, args.placard, args.all_types,
                        args.describe, args.types, args.qr, args.tone, avoid, seasonal,
                        args.hemisphere, args.subject, holidays,
                        weather, on_this_day, args.latitude, args.longitude, args.googly_chance,
                        args.googly_strict)

FAV_CHANCE = 0.2   # chance a scheduled run re-shows a favourite instead of fresh art

def _gather(args, mat_rgb, count):
    """Fetch `count` matted images (files / Met / Cleveland / any), with favourites mixed in
    and used as a graceful fallback if fresh art can't be fetched. Preview and an explicit
    'change now' (--force) always fetch fresh; only automatic runs re-show favourites."""
    if args.files:
        return prep_local(args.files, mat_rgb, args.googly_chance, args.googly_strict)
    if not args.preview and not args.force and random.random() < FAV_CHANCE:
        fav = favourite_pick()
        if fav:
            print("  ★ reshowing a favourite"); return [fav]
    paths = _fetch_source(args, mat_rgb, count)
    if not paths and not args.preview:         # couldn't get new art -> fall back to a favourite
        fav = favourite_pick()
        if fav:
            print("  ★ no fresh art — reshowing a favourite"); return [fav]
    return paths

def run(args):
    if getattr(args, "watch", False):                     # watcher mode: wait for the TV, then push
        return _watch_loop(args)
    mat_rgb = MAT_COLORS[args.mat]
    if getattr(args, "tone_weights", None) is not None:   # live weights from the panel override config's
        global _TONE_WEIGHTS
        _TONE_WEIGHTS = args.tone_weights

    # Preview: render one image to a file and stop — never touches the TV.
    if args.preview:
        os.makedirs(CFG, exist_ok=True)
        paths = _gather(args, mat_rgb, 1)
        if not paths:
            raise RuntimeError("No image to preview.")
        import shutil
        shutil.copy(paths[0], args.preview)
        print(f"Preview written: {args.preview}")
        return

    if load_config().get("pinned") and not args.force and not args.files:
        print("Kept — leaving the current art in place.")
        write_status(True, "Kept — art left unchanged", LAST_PIECES[0] if LAST_PIECES else {})
        return
    if not args.mac:
        raise RuntimeError("No TV MAC set. Pass --mac AA:BB:CC:DD:EE:FF, or set FRAME_MAC in "
                           "the environment. Find it on the Frame under About This TV, or your router.")
    print("Locating the Frame...")
    ip = resolve_frame_ip(args.ip, args.mac)
    if not ip:
        return _maybe_watch(args, f"Frame (MAC {args.mac}) not found on the network.")
    print(f"Frame at {ip}")

    os.makedirs(CFG, exist_ok=True)
    from samsungtvws import SamsungTVWS
    art = SamsungTVWS(host=ip, port=8002, token_file=args.token_file, timeout=args.timeout).art()

    if not args.no_wake:
        print("Waking the Frame and waiting for the art channel...")
        try:
            ensure_art_ready(art, args.mac, args.wake_wait, args.retries)
        except RuntimeError as e:
            return _maybe_watch(args, str(e))
        print("Art channel is up.")

    print("Preparing images...")
    paths = _gather(args, mat_rgb, args.fetch)
    if not paths:
        raise RuntimeError("No images to upload.")

    if args.replace and os.path.exists(STATE):
        try:
            old = json.load(open(STATE))
            if old:
                try:
                    art.delete_list(old)
                except Exception:
                    for cid in old:
                        try: art.delete(cid)
                        except Exception: pass
                print(f"Removed {len(old)} from the previous batch")
        except Exception as e:
            print(f"  ! prune skipped: {e}", file=sys.stderr)

    def fresh_art():
        return SamsungTVWS(host=ip, port=8002, token_file=args.token_file, timeout=args.timeout).art()

    print(f"Uploading {len(paths)} image(s)...")
    ids = []
    for p in paths:
        data = open(p, "rb").read()
        for attempt in range(1, args.upload_retries + 1):
            try:
                cid = art.upload(data, file_type="JPEG", matte="none")
                ids.append(cid)
                print(f"  uploaded {os.path.basename(p)} -> {cid}")
                break
            except Exception as e:
                print(f"  ! upload retry {attempt}/{args.upload_retries} for {os.path.basename(p)}: {str(e)[:120]}", file=sys.stderr)
                time.sleep(2)
                try:
                    art = fresh_art()
                except Exception:
                    pass
        time.sleep(0.5)
    if not ids:
        raise RuntimeError("All uploads failed (intermittent art-channel errors). Try re-running.")

    json.dump(ids, open(STATE, "w"))

    if ids and not args.no_select:
        art.select_image(ids[0], show=True)
        print(f"Displaying {ids[0]}")

    if args.slideshow is not None:
        ok = False
        for fn in ("set_slideshow_status", "set_auto_rotation_status"):
            try:
                getattr(art, fn)(duration=args.slideshow, type=True, category=2); ok = True; break
            except Exception:
                continue
        print("Slideshow " + (f"on, {args.slideshow}-min interval" if ok else "toggle failed — set in TV menu"))

    with open(HEARTBEAT, "w") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}  ip={ip}  uploaded={len(ids)}  replace={args.replace}\n")
    import shutil
    if args.no_record:                       # a history-browse display: don't extend history
        print(f"\nDone. {len(ids)} uploaded (browsing).")
        return
    piece = LAST_PIECES[0] if LAST_PIECES else {}
    histfile = ""
    try:
        os.makedirs(HIST_IMG_DIR, exist_ok=True)
        histfile = os.path.join(HIST_IMG_DIR, f"{int(time.time())}.jpg")
        shutil.copy(paths[0], histfile)
    except Exception:
        histfile = ""
    hist = _load_list(HISTORY)
    hist.append({**piece, "content_id": ids[0],
                 "when": datetime.datetime.now().isoformat(timespec="seconds"), "file": histfile})
    hist = hist[-HISTORY_MAX:]
    _save_list(HISTORY, hist)
    keep = {h.get("file") for h in hist}     # drop image files that aged out of history
    try:
        for f in os.listdir(HIST_IMG_DIR):
            if os.path.join(HIST_IMG_DIR, f) not in keep:
                os.remove(os.path.join(HIST_IMG_DIR, f))
    except Exception:
        pass
    nav_set(len(history_navlist()) - 1)      # cursor at the newest
    write_status(True, f"Displayed {piece.get('title','art')}", {"content_id": ids[0], **piece})
    try:
        shutil.copy(paths[0], CURRENT_IMG)
    except Exception:
        pass
    print(f"\nDone. {len(ids)} uploaded. Heartbeat: {HEARTBEAT}")

def main():
    cfg = load_config()   # config.json supplies defaults; CLI flags override
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=os.environ.get("FRAME_IP") or cfg["ip"],
                    help="optional IP hint; the TV is found by MAC regardless (DHCP-proof)")
    ap.add_argument("--mac", default=os.environ.get("FRAME_MAC") or cfg["mac"],
                    help="the Frame's wireless MAC (or set FRAME_MAC env / config); the TV is located by this")
    ap.add_argument("--token-file", default=os.path.join(CFG, "token.txt"))
    ap.add_argument("--fetch", type=int, default=cfg["fetch"])
    ap.add_argument("--query", default=None, help="freeform search term (overrides --theme)")
    ap.add_argument("--theme", default=cfg["content"], choices=list(THEMES) + ["cycle", "museum"],
                    help="named style, 'cycle' to rotate one genre/day, or 'museum' for a random piece from the whole collection")
    ap.add_argument("--placard", action=argparse.BooleanOptionalAction, default=cfg["placard"],
                    help="compose artwork + a gallery-label caption panel (title/artist/details)")
    ap.add_argument("--all-types", action=argparse.BooleanOptionalAction, default=cfg["all_types"],
                    help="allow every object type (sculpture, photos, ceramics...), not just wall art")
    ap.add_argument("--types", default=cfg.get("types") or [],
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    help="with --no-all-types, comma-separated families to allow, e.g. 'Paintings,Prints'")
    ap.add_argument("--describe", choices=["off", "real", "made-up"], default=cfg["description"],
                    help="caption: off, the Met's real prose, or an invented tale (needs a key)")
    ap.add_argument("--qr", action=argparse.BooleanOptionalAction, default=cfg["qr"],
                    help="show a QR code linking to the real museum page (when a caption is shown)")
    ap.add_argument("--source", choices=["met", "cleveland", "any"], default=cfg.get("source", "met"),
                    help="art source: the Met, the Cleveland Museum, or a random pick each run")
    _tone_default = cfg.get("tone") if isinstance(cfg.get("tone"), list) else [cfg.get("tone") or "whimsical"]
    ap.add_argument("--tone", default=_tone_default,
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip() in TONES],
                    help="voice(s) for made-up captions, comma-separated; one is picked at random")
    ap.add_argument("--tone-weights", dest="tone_weights", type=json.loads, default=None,
                    help="JSON map of voice->relative weight, e.g. '{\"pirate\":2.5,\"noir\":0.35}'")
    ap.add_argument("--seasonal", action=argparse.BooleanOptionalAction, default=None,
                    help="always/never bias art to the current season (shortcut for --seasonal-chance 1/0)")
    ap.add_argument("--seasonal-chance", dest="seasonal_chance", type=float,
                    default=cfg.get("seasonal_chance", 1.0 if cfg.get("seasonal") else 0.0),
                    help="probability 0..1 that a run leans seasonal")
    ap.add_argument("--hemisphere", choices=["north", "south"], default=cfg.get("hemisphere", "north"),
                    help="which hemisphere's seasons to use")
    ap.add_argument("--subject", default=cfg.get("subject", ""),
                    help="only show art of this subject, e.g. 'cats' (overrides content/season)")
    ap.add_argument("--holidays", action=argparse.BooleanOptionalAction, default=None,
                    help="always/never theme art around nearby holidays (shortcut for --holidays-chance 1/0)")
    ap.add_argument("--holidays-chance", dest="holidays_chance", type=float,
                    default=cfg.get("holidays_chance", 1.0 if cfg.get("holidays") else 0.0),
                    help="probability 0..1 that a run leans to a nearby holiday (Halloween, Christmas…)")
    ap.add_argument("--weather", action=argparse.BooleanOptionalAction, default=None,
                    help="always/never bias art to the live local weather (shortcut for --weather-chance 1/0)")
    ap.add_argument("--weather-chance", dest="weather_chance", type=float,
                    default=cfg.get("weather_chance", 1.0 if cfg.get("weather") else 0.0),
                    help="probability 0..1 that a run matches the live local weather (open-meteo, keyless)")
    ap.add_argument("--on-this-day", dest="on_this_day", action=argparse.BooleanOptionalAction, default=None,
                    help="always/never bias to a historical event from today's date (shortcut for --on-this-day-chance 1/0)")
    ap.add_argument("--on-this-day-chance", dest="on_this_day_chance", type=float,
                    default=cfg.get("on_this_day_chance", 1.0 if cfg.get("on_this_day") else 0.0),
                    help="probability 0..1 that a run ties art to a historical event today (Wikipedia)")
    ap.add_argument("--latitude", type=float, default=cfg.get("latitude"),
                    help="latitude for --weather (falls back to IP geolocation if unset)")
    ap.add_argument("--longitude", type=float, default=cfg.get("longitude"),
                    help="longitude for --weather (falls back to IP geolocation if unset)")
    ap.add_argument("--googly", action=argparse.BooleanOptionalAction, default=None,
                    help="always/never add googly eyes to faces (shortcut for --googly-chance 1/0; needs opencv)")
    ap.add_argument("--googly-chance", dest="googly_chance", type=float,
                    default=cfg.get("googly_chance", 1.0 if cfg.get("googly") else 0.0),
                    help="probability 0..1 that a piece gets cartoon googly eyes on its faces")
    ap.add_argument("--googly-strictness", dest="googly_strict", type=float,
                    default=cfg.get("googly_strictness", 0.5),
                    help="how fussy face detection is, 0..1 (0 = eyes on anything vaguely "
                         "face-ish, 1 = only clear faces with findable eyes)")
    ap.add_argument("--preview", default=None, metavar="PATH",
                    help="render one image to PATH and exit — does not touch the TV")
    ap.add_argument("--force", action="store_true", help="change the art even if pinned")
    ap.add_argument("--no-record", action="store_true",
                    help="display without adding to history (used when browsing back/forward)")
    ap.add_argument("--mat", choices=MAT_COLORS, default=cfg["mat"])
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--no-select", action="store_true")
    ap.add_argument("--slideshow", type=int, default=None)
    ap.add_argument("--replace", action=argparse.BooleanOptionalAction, default=cfg["replace"])
    ap.add_argument("--timeout", type=int, default=30, help="socket timeout (s) so it never hangs")
    ap.add_argument("--wake-wait", type=int, default=12, help="seconds to wait after WoL before probing")
    ap.add_argument("--retries", type=int, default=4, help="wake+probe attempts (the watcher is more patient still)")
    ap.add_argument("--no-wake", action="store_true", help="skip the WoL/wake step")
    ap.add_argument("--upload-retries", type=int, default=3, help="retries per image on transient errors")
    ap.add_argument("--watch", action="store_true",
                    help="poll until the TV's art channel is up, then push (used by auto-retry)")
    ap.add_argument("--watch-on-fail", dest="watch_on_fail", action=argparse.BooleanOptionalAction,
                    default=cfg.get("watch_on_fail", True),
                    help="if the TV is asleep, keep retrying in the background until it wakes")
    ap.add_argument("--watch-interval", dest="watch_interval", type=int, default=cfg.get("watch_interval", 60),
                    help="seconds between polls while waiting for the TV")
    ap.add_argument("--watch-timeout", dest="watch_timeout", type=int, default=cfg.get("watch_timeout", 180),
                    help="minutes to keep waiting for the TV before giving up")
    args = ap.parse_args()
    args._argv = sys.argv[1:]                          # preserved so a spawned watcher repeats this run
    for name in ("seasonal", "holidays", "weather", "on_this_day", "googly"):
        b = getattr(args, name)                       # the --x / --no-x boolean shortcut, if given
        if b is not None:
            setattr(args, name + "_chance", 1.0 if b else 0.0)
        setattr(args, name + "_chance", min(1.0, max(0.0, getattr(args, name + "_chance") or 0.0)))
    args.googly_strict = min(1.0, max(0.0, args.googly_strict if args.googly_strict is not None else 0.5))
    try:
        run(args)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        write_status(False, str(e)[:300])
        notify(str(e)[:200])
        ntfy_alert("Frame art failed", str(e)[:300])
        sys.exit(1)

if __name__ == "__main__":
    main()

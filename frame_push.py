#!/usr/bin/env python3
"""
frame_push.py — push art to a Samsung The Frame over the local WebSocket API.

Self-locating (finds the TV by MAC), token-persistent, heartbeat + macOS failure
notification. Wakes the Frame (Wake-on-LAN) and waits for the art channel to respond
before doing anything, with a hard timeout so it can never hang. With --replace it
deletes the batch it added last run before adding a fresh one, so a daily job rotates.

Examples:
    python3 frame_push.py                                   # 1 piece, proof
    python3 frame_push.py --fetch 12 --mat charcoal --slideshow 30 --replace
    python3 frame_push.py --fetch 12 --query Hiroshige      # themed batch
"""

import argparse, io, os, re, json, random, subprocess, sys, time, warnings, datetime, html, platform
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
HOLIDAYS = [
    ((10, 24), (10, 31), ["skeleton", "skull", "witch", "ghost", "death", "devil"]),   # Halloween
    ((12, 20), (12, 27), ["nativity", "angel", "snow", "adoration of the magi"]),       # Christmas
    ((2, 10),  (2, 15),  ["lovers", "cupid", "romance", "embrace"]),                    # Valentine's
    ((12, 30), (1, 2),   ["feast", "celebration", "fireworks"]),                        # New Year
]
def holiday_terms():
    md = (datetime.date.today().month, datetime.date.today().day)
    for start, end, terms in HOLIDAYS:
        within = (start <= md <= end) if start <= end else (md >= start or md <= end)
        if within:
            return terms
    return None

def bias_terms(subject="", holidays=False, seasonal=False, hemisphere="north"):
    """The search terms a special mode wants, in priority order, or None for 'normal'."""
    if subject and subject.strip():
        return [subject.strip()]
    if holidays:
        h = holiday_terms()
        if h:
            return h
    if seasonal:
        return seasonal_terms(hemisphere)
    return None
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
            "ntfy_topic": "", "tone": ["whimsical"], "source": "met", "orientation": "landscape",
            "pinned": False, "seasonal": False, "hemisphere": "north",
            "subject": "", "holidays": False}
STATUS = os.path.join(CFG, "status.json")   # last-run outcome, for alerts + the dashboard
HISTORY = os.path.join(CFG, "history.json") # recently displayed pieces (for no-repeats + dashboard)
BLOCKLIST = os.path.join(CFG, "blocklist.json")  # ids the user has banned
CURRENT_IMG = os.path.join(CFG, "current.jpg")   # a copy of what's on the TV now (dashboard thumb)
LAST_PIECES = []                            # meta of pieces prepped this run (for status)

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
    "roast":        "a savage but affectionate roast of the piece, like a snobby critic who's had quite enough",
    "compliment":   "a lavish, over-the-top compliment aimed at YOU, the viewer, inspired by the piece",
    "firstperson":  "narrated in first person BY the main subject of the artwork, describing their day",
    "trailer":      "a booming movie-trailer voiceover — 'In a world…', dramatic pauses, high stakes",
    "gossip":       "breathless tabloid gossip about the artwork and everyone supposedly in it",
    "dadjoke":      "groan-worthy dad jokes and puns inspired by the artwork's subject",
    "fairytale":    "the opening of a storybook fairy tale — 'Once upon a time…' — cosy and enchanted",
}

def ai_blurb(meta, tone="whimsical"):
    """Fallback when there's no real prose: a deliberately fake, self-evidently invented
    mini-tale from Claude. `tone` may be one tone or a list — a random one is picked."""
    if isinstance(tone, (list, tuple)):
        tone = random.choice(tone) if tone else "whimsical"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        kf = os.path.join(CFG, "anthropic_key.txt")
        if os.path.exists(kf):
            key = open(kf).read().strip()
    if not key:
        return None
    facts = ", ".join(f"{k}: {meta[k]}" for k in ("artist", "title", "date", "medium",
             "culture", "objectName") if meta.get(k))
    style = TONES.get(tone, TONES["whimsical"])
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
        return _truncate_prose(text) or None
    except Exception as e:
        print(f"  ! ai_blurb: {str(e)[:80]}", file=sys.stderr)
        return None

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

def fetch_matted(count, query, mat_rgb, theme=None, placard=False, all_types=False, describe="off", types=None, qr=True, tone="whimsical", avoid=None, seasonal=False, hemisphere="north", subject="", holidays=False):
    os.makedirs(TMP, exist_ok=True)
    LAST_PIECES.clear()
    avoid = avoid or set()
    bias = bias_terms(subject, holidays, seasonal, hemisphere)
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
            url = o.get("primaryImage") or ""
            if not url:
                continue
            img = requests.get(url, headers=HEADERS, timeout=60)
            img.raise_for_status()
            art = Image.open(io.BytesIO(img.content)).convert("RGB")
            p = os.path.join(TMP, f"{len(paths)+1:02d}_{slug(o.get('title','art'))}.jpg")
            if placard:
                artist_s, culture, period = (o.get("artistDisplayName") or ""), (o.get("culture") or ""), (o.get("period") or "")
                cp = " · ".join(x for x in [culture, period] if x.strip()) if artist_s else period.strip()
                meta = {"title": o.get("title"), "artist": artist_s, "bio": o.get("artistDisplayBio"),
                        "date": o.get("objectDate"), "medium": o.get("medium"), "dimensions": o.get("dimensions"),
                        "culture": culture, "objectName": o.get("objectName"), "culture_period": cp,
                        "credit": o.get("creditLine"), "museum": "The Metropolitan Museum of Art"}
                desc = None
                if describe == "real":
                    desc = met_prose(o.get("objectURL"))          # the Met's own caption (may be None)
                elif describe == "made-up":
                    desc = ai_blurb(meta, tone)                    # an invented tale (needs a key)
                if desc:
                    print(f"    + {describe}: {desc[:60]}...")
                link = o.get("objectURL") if (describe != "off" and qr) else None
                mat_with_placard(art, meta, mat_rgb, desc, link).save(p, "JPEG", quality=JPEG_Q)
            else:
                mat_image(art, mat_rgb).save(p, "JPEG", quality=JPEG_Q)
            paths.append(p)
            LAST_PIECES.append({"title": o.get("title") or "", "source": o.get("_source_name", "The Met"),
                                "artist": o.get("artistDisplayName") or o.get("culture") or "Unknown",
                                "url": o.get("objectURL") or "", "id": f"met:{o.get('objectID')}"})
            print(f"  prepped: {o.get('title','?')} — {o.get('artistDisplayName') or o.get('culture') or 'Unknown'}")
        except Exception as e:
            print(f"  ! skip {oid}: {str(e)[:120]}", file=sys.stderr)
    return paths

CLE_API = "https://openaccess-api.clevelandart.org/api/artworks/"
CLE_NAME = "Cleveland Museum of Art"

def fetch_cleveland(count, query, mat_rgb, theme=None, placard=False, describe="off", types=None, qr=True, tone="whimsical", avoid=None, seasonal=False, hemisphere="north", all_types=True, subject="", holidays=False):
    """Second source: Cleveland Museum of Art open access (keyless, CC0). Its API carries
    a real 'description', so 'real' captions need no scraping."""
    os.makedirs(TMP, exist_ok=True); LAST_PIECES.clear()
    avoid = avoid or set()
    params = {"has_image": "1", "cc0": "1", "limit": "100", "indent": "1"}
    q = query
    _bias = bias_terms(subject, holidays, seasonal, hemisphere)
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
            url = ((imgs.get("print") or imgs.get("web") or {}).get("url"))
            if not url:
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
            r = http_get(url)
            if r is None:
                continue
            art = Image.open(io.BytesIO(r.content)).convert("RGB")
            desc = None
            if placard and describe == "real":
                desc = _truncate_prose(o["description"]) if o.get("description") else None
            elif placard and describe == "made-up":
                desc = ai_blurb(meta, tone)
            if desc:
                print(f"    + {describe}: {desc[:60]}...")
            link = o.get("url") if (placard and describe != "off" and qr) else None
            p = os.path.join(TMP, f"{len(paths)+1:02d}_{slug(meta['title'])}.jpg")
            (mat_with_placard(art, meta, mat_rgb, desc, link) if placard else mat_image(art, mat_rgb)).save(p, "JPEG", quality=JPEG_Q)
            paths.append(p)
            LAST_PIECES.append({"title": meta["title"], "artist": name or meta["culture"] or "Unknown",
                                "url": o.get("url") or "", "source": CLE_NAME, "id": f"cle:{o.get('id')}"})
            print(f"  prepped: {meta['title']} — {name or meta['culture'] or 'Unknown'}")
        except Exception as e:
            print(f"  ! skip {o.get('id')}: {str(e)[:120]}", file=sys.stderr)
    return paths

def prep_local(files, mat_rgb):
    os.makedirs(TMP, exist_ok=True)
    out = []
    for f in files:
        im = Image.open(f).convert("RGB")
        if im.size != CANVAS:
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
def _gather(args, mat_rgb, count):
    """Fetch `count` matted images from the chosen source (files / Met / Cleveland / any)."""
    if args.files:
        return prep_local(args.files, mat_rgb)
    avoid = {str(x) for x in _load_list(BLOCKLIST)} | {str(h.get("id")) for h in _load_list(HISTORY)[-40:]}
    src = args.source
    if src == "any":
        src = random.choice(["met", "cleveland"])
    if src == "cleveland":
        return fetch_cleveland(count, args.query, mat_rgb, args.theme, args.placard,
                               args.describe, args.types, args.qr, args.tone, avoid,
                               args.seasonal, args.hemisphere, args.all_types, args.subject, args.holidays)
    return fetch_matted(count, args.query, mat_rgb, args.theme, args.placard, args.all_types,
                        args.describe, args.types, args.qr, args.tone, avoid, args.seasonal,
                        args.hemisphere, args.subject, args.holidays)

def run(args):
    mat_rgb = MAT_COLORS[args.mat]

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
        print("Pinned — leaving the current art in place.")
        write_status(True, "Pinned — art left unchanged", LAST_PIECES[0] if LAST_PIECES else {})
        return
    if not args.mac:
        raise RuntimeError("No TV MAC set. Pass --mac AA:BB:CC:DD:EE:FF, or set FRAME_MAC in "
                           "the environment. Find it on the Frame under About This TV, or your router.")
    print("Locating the Frame...")
    ip = resolve_frame_ip(args.ip, args.mac)
    if not ip:
        raise RuntimeError(f"Frame (MAC {args.mac}) not found on the network. Is it on the LAN?")
    print(f"Frame at {ip}")

    os.makedirs(CFG, exist_ok=True)
    from samsungtvws import SamsungTVWS
    art = SamsungTVWS(host=ip, port=8002, token_file=args.token_file, timeout=args.timeout).art()

    if not args.no_wake:
        print("Waking the Frame and waiting for the art channel...")
        ensure_art_ready(art, args.mac, args.wake_wait, args.retries)
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
    piece = LAST_PIECES[0] if LAST_PIECES else {}
    write_status(True, f"Displayed {piece.get('title','art')}",
                 {"content_id": ids[0], **piece})
    hist = _load_list(HISTORY)
    hist.append({**piece, "content_id": ids[0], "when": datetime.datetime.now().isoformat(timespec="seconds")})
    _save_list(HISTORY, hist[-200:])
    try:
        import shutil; shutil.copy(paths[0], CURRENT_IMG)
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
    ap.add_argument("--seasonal", action=argparse.BooleanOptionalAction, default=cfg.get("seasonal", False),
                    help="bias art to the current season")
    ap.add_argument("--hemisphere", choices=["north", "south"], default=cfg.get("hemisphere", "north"),
                    help="which hemisphere's seasons to use")
    ap.add_argument("--subject", default=cfg.get("subject", ""),
                    help="only show art of this subject, e.g. 'cats' (overrides content/season)")
    ap.add_argument("--holidays", action=argparse.BooleanOptionalAction, default=cfg.get("holidays", False),
                    help="theme the art around nearby holidays (Halloween, Christmas…)")
    ap.add_argument("--preview", default=None, metavar="PATH",
                    help="render one image to PATH and exit — does not touch the TV")
    ap.add_argument("--force", action="store_true", help="change the art even if pinned")
    ap.add_argument("--mat", choices=MAT_COLORS, default=cfg["mat"])
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--no-select", action="store_true")
    ap.add_argument("--slideshow", type=int, default=None)
    ap.add_argument("--replace", action=argparse.BooleanOptionalAction, default=cfg["replace"])
    ap.add_argument("--timeout", type=int, default=30, help="socket timeout (s) so it never hangs")
    ap.add_argument("--wake-wait", type=int, default=12, help="seconds to wait after WoL before probing")
    ap.add_argument("--retries", type=int, default=3, help="wake+probe attempts")
    ap.add_argument("--no-wake", action="store_true", help="skip the WoL/wake step")
    ap.add_argument("--upload-retries", type=int, default=3, help="retries per image on transient errors")
    args = ap.parse_args()
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

# The Frame Machine

Turn a **Samsung The Frame** TV into a self-refreshing museum wall — for free, with no Art
Store subscription. Every day it pulls a public-domain artwork from **The Metropolitan Museum
of Art's** open collection, lays it out as a gallery display (artwork + wall label), and pushes
it to the TV over the local network. Optionally, it captions each piece with a gleefully
made-up story and stamps on a QR code to the real museum page.

It runs entirely on your own machine (a Mac mini, a Raspberry Pi, any always-on box) and talks
to the TV directly over its local WebSocket API — no cloud, no account, no subscription.

<p align="center">
  <img src="docs/panel-desktop.png" alt="The web control panel" width="420">
</p>
<p align="center"><em>Everything runs from one phone-friendly web page.</em></p>

![example placard](docs/example.jpg)

> ⚠️ **A word of warning.** This is vibe-coded by someone who has no idea what they're doing.
> It will almost certainly break your TV, and there is a non-trivial chance it will run away
> with your significant other. No warranties are offered — express, implied, or marital. Use
> entirely at your own risk, and keep a spare spouse handy.

## Examples

| Made-up tale (a painting) | The Met's real caption (an object) | A sculpture, made-up |
|---|---|---|
| ![](docs/example.jpg) | ![](docs/example-real.jpg) | ![](docs/example-object.jpg) |

Every piece keeps its real artist, title and date up top; the story below is either the Met's
own words, a cheerfully invented tale, or nothing — your choice. A QR code (toggleable) links
to the genuine museum page.

## What it does

- **Free art, daily.** Fetches public-domain works from the keyless [Met Collection API](https://metmuseum.github.io/).
  The art is CC0 (public domain) — yours to display.
- **Finds the TV by MAC address.** Survives DHCP changes — no static IP needed. Wakes the TV
  (Wake-on-LAN) and waits for the art channel before uploading.
- **Museum-placard layout.** Fits the artwork with a keyline and renders a real gallery label
  beside it: artist, dates, title, year, medium, dimensions, credit line.
- **Genres or the whole museum.** Pick a style (`impressionist`, `ukiyo-e`, `old-masters`,
  `landscape`), cycle one genre per day, or `museum` mode for a random piece from the entire
  ~500,000-object collection — any type: paintings, prints, sculpture, ceramics, armour.
- **Made-up stories (optional).** With an Anthropic API key, each piece gets a two-sentence,
  deliberately absurd invented backstory. The real artist/title/date stay accurate up top; a
  **QR code** links to the genuine Met page so you can check the truth.
- **Rotates cleanly.** `--replace` prunes the previous day's upload so nothing piles up. Never
  touches art you added yourself.
- **Web control panel.** A phone-friendly page to pick the content, caption style, how often the
  art changes and when — with **Preview** and **Change now** buttons. No config-file editing.
- **Two museums.** The Met *and* the Cleveland Museum of Art (both keyless, CC0) — pick one or
  let it choose either at random. Resilient if one source ever changes.
- **Caption voices.** Made-up tales in ~18 tones — whimsical, noir, epic, haiku, limerick,
  conspiracy, pirate, Shakespearean, corporate, Gen-Z, Attenborough, sarcastic, **topical**
  (ties the piece to a real, current news headline), first-person, movie-trailer, tabloid
  gossip, dad-jokes, fairytale. Tick several; one is picked at random each time.
- **Favourites & graceful fallback.** ♥ a piece and it reappears more often — and if fresh art
  ever can't be fetched, the Frame quietly re-shows a favourite instead of going blank.
- **Subject & holiday modes.** Ask for "only show art of cats" (or dogs, dragons, anything),
  or let it get festive — spooky art near Halloween, nativities at Christmas, hearts for
  Valentine's.
- **Mission-control dashboard.** See what's on the TV now, when it last changed, and whether the
  last run worked — plus **Pin** (hold a piece), **Ban** (never show it again) and no-repeats.
- **Seasonal mode.** Optionally bias the art to the season (hemisphere-aware).
- **Phone alerts.** Get an [ntfy](https://ntfy.sh) push if a run ever fails. Optional panel password.

## Requirements

- A Samsung **The Frame** TV on your LAN (tested on a 2022 LS03B / firmware 1720; the art
  WebSocket on port 8002 must be reachable — it is on many 2020–2023 models).
- An always-on computer on the same network (macOS or Linux) with **Python 3.10+**.

## Install (the easy way)

**Step 1 — download it:** click **[⬇ Download The Frame Machine (ZIP)](https://github.com/s3lfish/the-frame-machine/archive/refs/heads/main.zip)**.
(That's the same as the green **Code ▾** button near the top of this page → **Download ZIP**.)
Double-click the downloaded file to unzip it.

**Step 2 — run the installer:** open **Terminal** (macOS: Applications → Utilities → Terminal),
then paste this and press Return:

```bash
cd ~/Downloads/the-frame-machine-main && bash install.sh
```

The script installs everything, asks for your TV's MAC address (it tells you where to find it),
and starts the control panel as a background service. When it finishes it prints a link — open
it on your phone or laptop, click **Change the art now** (accept the one-time "Allow" prompt on
the TV), pick how often it should change, and hit **Save**. Done.

<details><summary>Prefer git?</summary>

```bash
git clone https://github.com/s3lfish/the-frame-machine
cd the-frame-machine && ./install.sh
```
</details>

> On **Linux/Raspberry Pi** you can instead use Docker — see [Docker](#docker-linux--raspberry-pi) below.

<details>
<summary>Manual setup (if you'd rather not run the script)</summary>

```bash
pip install -r requirements.txt
export FRAME_MAC=AA:BB:CC:DD:EE:FF          # your TV's wireless MAC (About This TV, or your router)
python3 app.py --port 8080                  # the control panel, then open http://localhost:8080
# or drive it from the command line directly:
python3 frame_push.py --fetch 1 --placard --replace   # first run shows an "Allow" prompt on the TV
```
The pairing token is saved to `~/.config/frame/token.txt` (tied to the TV, not the IP).
</details>

## The control panel

When the installer finishes it prints a link — open it on any phone or laptop on your network
(e.g. `http://your-host.local:8080`). Everything lives here; no config files, no command line:

- **Change the art now**, or step **◀ Back / Forward ▶** through the last ~40 pieces, right at the top.
- **Caption:** none, the museum's own real caption, or a made-up tale — in ~18 voices (pirate,
  noir, Shakespearean, Attenborough, topical…), one picked at random.
- **Content:** the whole museum, a single genre, a genre-a-day cycle, or *"only show art of
  cats"*. Match the season or celebrate holidays. Pick the museum (the Met, Cleveland, or either).
- **Object types**, **mat colour**, and a **QR-code** toggle.
- **How often & when** the art changes — the panel builds the schedule for you (launchd on
  macOS, cron on Linux).
- **♥ Favourite** a piece (it returns more often), **Stop this from changing**, or **Never show
  again** — plus a **history** of recent pieces.

The panel writes `~/.config/frame/config.json`; you can also edit that by hand.

### Docker (Linux / Raspberry Pi)

```bash
FRAME_MAC=AA:BB:CC:DD:EE:FF docker compose up -d   # then open http://<host>:8080
```

Host networking lets the container discover and wake the TV (works on Linux/Pi; not on Docker
Desktop for Mac). `~/.config/frame` is mounted in, so the pairing token, settings and history
persist. For scheduled changes, set the schedule in the panel (writes cron inside the container).

## Command line (optional)

Prefer the terminal? `frame_push.py` does everything via flags (they override `config.json`):

```bash
python3 frame_push.py --theme museum --describe made-up     # a random piece + a tall tale
python3 frame_push.py --source cleveland --subject cats     # Cleveland, cats only
python3 frame_push.py --files a.jpg b.jpg                    # push your own images
```

Run `python3 frame_push.py --help` for every flag.

### Made-up captions & the Anthropic key

The made-up tales are written by Claude, so they need an Anthropic API key — **the installer
offers to set this up for you**. To do it by hand instead:

```bash
echo 'sk-ant-...' > ~/.config/frame/anthropic_key.txt   # or set ANTHROPIC_API_KEY
```

Get a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
(sign in, **Create Key**, copy it). It costs a fraction of a cent per run. Without a key,
captions fall back to the museum's own text (or nothing).

## Running it daily (without the panel)

If you'd rather not run the web app, schedule `frame_push.py` yourself.

**macOS (launchd).** Edit `com.example.frameart.plist` (fill in the `__PLACEHOLDERS__`: absolute
python path, script path, your MAC), then:

```bash
cp com.example.frameart.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.frameart.plist
# reload after edits:  launchctl bootout gui/$(id -u)/com.example.frameart && launchctl bootstrap ...
```

**Linux (cron).** `30 7 * * *  FRAME_MAC=AA:.. /usr/bin/python3 /path/frame_push.py --fetch 1 --theme museum --placard --all-types --describe --replace`

## Gotchas

- **The TV must be awake for the API to answer.** Deep standby drops it off the network. WoL
  wakes it from light standby reliably; from deep overnight standby, WoL over Wi-Fi is hit-or-
  miss — a **wired ethernet** connection makes it bulletproof.
- **One image, once a day** is the most reliable cadence: a single upload finishes before the
  TV can nod off, and it's displayed directly (no reliance on the TV's slideshow shuffle).
- Confirm the TV's IP by MAC if anything seems off: `arp -an | grep -i <mac-prefix>`.

## Credits & notes

- Art & metadata: [The Met Collection API](https://metmuseum.github.io/) (Open Access, CC0).
- TV control: [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api).
- Not affiliated with or endorsed by Samsung, The Metropolitan Museum of Art, or Anthropic.
  "The Frame" is a Samsung trademark. Respect the Met's [Open Access terms](https://www.metmuseum.org/about-the-met/policies-and-documents/open-access).

## License

MIT — see [LICENSE](LICENSE).

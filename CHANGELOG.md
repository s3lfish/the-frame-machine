# Changelog

All notable changes to The Frame Machine are recorded here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed
- **Fullscreen, modern pictures by default.** The default is now a *modern mix* —
  Wikimedia Commons featured pictures, NASA's image library and Unsplash — shown edge
  to edge: anything within 20% of the screen's shape is trimmed to fit, the rest sits
  on a blurred, dimmed copy of itself (new `blur` backdrop, `--mat blur`). The museum
  label is off by default. **An existing config is moved over once** (label off, smart
  fill on the blurred backdrop, and a museum source switched to the modern mix); pick a
  museum again in the panel to go back. (2026-11-09)
- **Modern sources ignore the season/holiday/weather/on-this-day modes.** Those were built
  for museum collections. With them at *Sometimes*, nearly every modern run searched a bias
  term instead of the curated scenery list: "grey sky" found a grim street photo and
  "1880 Zagreb earthquake" found nothing at NASA. Wikimedia, NASA and Unsplash now search
  their own terms, or your typed subject. Museums still use all four modes. (2026-11-09)
- **Screen-shaped pictures first.** Most Commons and NASA hits are portrait or square, so a
  modern run usually landed on the blurred backdrop. Candidates that fill the screen within
  the fill tolerance are now tried first; the backdrop is used only when none of them
  fits. (2026-11-09)
- **No NASA charts, diagrams or clean-room shots.** NASA hits titled as an illustration,
  graphic, infographic, diagram, chart, cutaway, schematic, artist's concept, map or
  labelled comparison are skipped, and so are ones whose description opens by calling them
  one ("This graphic compares…") or by describing engineers at work. A curated "Jupiter"
  search had picked a labelled Ceres cutaway, a four-panel mission graphic and a
  spacecraft in JPL's clean room. (2026-11-09)
- **White padding is trimmed.** Some NASA press images are a square picture padded with
  white to 16:9, which "filled" the screen with white bars. A flat near-white border is
  now cut off before the fit is judged, so the picture goes on the backdrop by its real
  shape. Black borders are kept. (2026-11-09)
- **NASA searches stay near the top of the results.** A run used a random page out of up
  to 100, and the deep pages of "Jupiter" or "Saturn" are wind-tunnel models, pool tests
  and team meetings. It now picks from the first 5 pages, where the science images
  are. (2026-11-09)

### Added
- **Leave out pictures of…** A list of words a modern source skips (default: church, temple,
  cathedral, mosque, protest, homeless, portrait). They're negated in the Wikimedia search,
  and every source's titles are checked against them. A word you type as the subject is
  never excluded. Edit it in the panel, `exclude_terms` in config.json, or `--exclude`
  (`--exclude ""` for none). (2026-11-09)
- **Favourites only** source (`--source favourites`): shows only the pieces you've ♥'d,
  each in turn, starting with the one shown longest ago. With none saved yet it uses the
  modern mix. (2026-11-09)
- Sources `wikimedia`, `nasa`, `unsplash`, `modern` (those three) and `everything`
  (museums + modern). Wikimedia and NASA are keyless; Unsplash needs a free access key,
  entered in the panel or the installer and kept in `~/.config/frame/unsplash_key.txt`
  (or `UNSPLASH_ACCESS_KEY`). NASA press photos and black-and-white frames are skipped;
  Wikimedia searches stick to scenery and leave out scanned paintings. (2026-11-09)

### Fixed
- **Re-running the installer keeps the panel's port.** `install.sh` always wrote port
  8080 into the panel's launchd/systemd service, so a re-run could move a panel that had
  been set to another port onto one something else already uses. It now keeps the port
  the installed service already has; a first install uses 8080, and `PORT=8137 ./install.sh`
  picks one. (2026-11-09)
- **No more blurry blow-ups.** Some museum "original" scans are tiny (one was 750px
  wide) and were being enlarged 4-5x to fill a 4K screen. Any picture that would need
  enlarging more than 1.6x in the current layout is now skipped (`--max-upscale`,
  config `max_upscale`). Cleveland is screened from its image metadata before
  download; the Met after. (2026-09-09)
- **A search that finds nothing no longer ends in a blank.** If the configured search
  comes up empty (e.g. an obscure "on this day" term the museum has no art for), the run
  retries: the same search without the date/weather terms, then the other museum, then
  anything from the whole collection — before the favourites fallback. (2026-09-08)
- **Automatic art changes no longer take over the TV while someone is watching it.**
  The daily job (and its background retry watcher) now checks whether Art Mode is
  active first; if the TV is on showing live TV or apps, the change waits until the
  TV is back in art mode or asleep. Explicit panel actions (Change the art now,
  Back/Forward) still act immediately. (2026-07-24)

### Added
- **Screen fit** setting: fill the screen with only wide, screen-shaped art (shown edge
  to edge, no mat) at a chosen trim tolerance of 10/20/30%, or keep matting any shape.
  Candidates are screened by the museums' dimension data before download and by the
  actual image after. `--fill` / `--fill-tolerance` on the CLI. (2026-09-08)
- Weather-reactive art, "on this day" art, and googly eyes on faces (opencv
  face detection), all with their own controls in the panel. (2026-07-02)
- Browse **back and forward** through recently shown art from the panel — a ring of
  recent renders, browsing never disturbs the history itself. (2026-07-02)
- Auto-watch: when a change can't reach the TV (asleep, off the network), a
  background watcher keeps retrying and pushes once the TV is available; its state
  shows in the panel with a "Stop waiting" button. (2026-07-08)
- Any-interval scheduling — every N hours/days, not just once a day — plus a
  self-check that warns if the install folder becomes unreadable (e.g. evicted to
  online-only by a syncing service). (2026-07-09)
- Captions name the made-up voice that wrote them, with a one-tap "drop this
  voice"; voices can be weighted Off/Rarely/Normal/Often. (2026-07-03)
- Full artwork details in the panel: date, medium, dimensions, credit, and the
  caption used. (2026-07-03)
- A face-detection fussiness slider for googly eyes. (2026-07-23)
- Panel shows a spinner while an action runs. (2026-07-03)

### Changed
- Googly eyes and the season/holidays/weather/on-this-day biases are all
  Never/Rarely/Sometimes/Always chances rather than on/off toggles. (2026-07-03)
- Daily job made more robust; panel layout refreshed (Back/Forward row, full-width
  "Change the art now", README leads with a panel screenshot). (2026-07-08)

## [1.0.0] — 2026-07-02

Initial public release: a Samsung The Frame as a self-refreshing museum wall,
free, with no Art Store subscription.

- Free public-domain art from the Met and the Cleveland Museum (keyless, CC0).
- Museum-style placard: artwork plus a real gallery label (artist, title, date,
  medium, credit), or plain full-bleed art.
- Captions: the museum's real text, or a made-up tale in ~18 voices (pirate, noir,
  Shakespearean, Attenborough, topical…), with an optional QR code to the real page.
- Phone-friendly web control panel: content, source, object types, mat, schedule,
  favourites, history, pin/ban, password — no config files.
- Whole-museum surprise, genre-a-day cycle, single genre, "only show art of cats",
  seasonal (hemisphere-aware) and holiday modes.
- Robust plumbing: MAC-based TV discovery, Wake-on-LAN, caching and backoff,
  phone failure alerts (ntfy), favourites as a graceful fallback.
- One-step installer for macOS and Linux, plus Docker for Raspberry Pi.

[Unreleased]: https://github.com/s3lfish/the-frame-machine/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/s3lfish/the-frame-machine/releases/tag/v1.0.0

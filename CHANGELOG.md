# Changelog

All notable changes to The Frame Machine are recorded here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- **Automatic art changes no longer take over the TV while someone is watching it.**
  The daily job (and its background retry watcher) now checks whether Art Mode is
  active first; if the TV is on showing live TV or apps, the change waits until the
  TV is back in art mode or asleep. Explicit panel actions (Change the art now,
  Back/Forward) still act immediately. (2026-07-24)

### Added
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

#!/usr/bin/env python3
"""Tests for the pure logic in frame_push.py and app.py — no network, no TV.

    python3 -m unittest -v test_frame          # or: python3 test_frame.py

Every case here corresponds to a bug that actually shipped, or to an invariant that
kept the fix honest. Stdlib unittest on purpose: no dev dependency to install, and
pytest runs this file unchanged if you prefer it.
"""

import argparse, os, tempfile, unittest, warnings

# Both modules derive their state paths from HOME at import time, so this has to happen
# before the imports or the tests would read and overwrite the real ~/.config/frame.
_HOME = tempfile.mkdtemp(prefix="frame-test-home-")
os.environ["HOME"] = _HOME

import frame_push as fp                                            # noqa: E402
import app                                                         # noqa: E402
from PIL import Image                                              # noqa: E402

# Anchor the fixture reads to the repo, not the caller's working directory.
HERE = os.path.dirname(os.path.abspath(__file__))

assert fp.CFG.startswith(_HOME), f"tests would touch the real config dir: {fp.CFG}"

# frame_push and app read state with the json.load(open(...)) idiom throughout. Tidying that
# up is a separate change; silence the handle warnings so results stay readable.
warnings.filterwarnings("ignore", category=ResourceWarning)


class TempState(unittest.TestCase):
    """Give each test its own state directory and scratch space."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="frame-test-")
        self._saved = {k: getattr(fp, k) for k in
                       ("CFG", "STATUS", "HISTORY", "BLOCKLIST", "FAVOURITES", "NAV",
                        "CURRENT_IMG", "HIST_IMG_DIR", "FAVS_DIR", "CONFIG", "TMP",
                        "STATE", "PENDING_DELETE")}
        fp.CFG = os.path.join(self.tmp, "cfg")
        fp.TMP = os.path.join(self.tmp, "art")
        for name, leaf in (("STATUS", "status.json"), ("HISTORY", "history.json"),
                           ("BLOCKLIST", "blocklist.json"), ("FAVOURITES", "favourites.json"),
                           ("STATE", "uploaded.json"), ("PENDING_DELETE", "pending_delete.json"),
                           ("NAV", "nav.json"), ("CURRENT_IMG", "current.jpg"),
                           ("CONFIG", "config.json")):
            setattr(fp, name, os.path.join(fp.CFG, leaf))
        fp.HIST_IMG_DIR = os.path.join(fp.CFG, "history_imgs")
        fp.FAVS_DIR = os.path.join(fp.CFG, "favourites")
        os.makedirs(fp.CFG, exist_ok=True)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(fp, k, v)


# --------------------------------------------------------------------------- cron_spec
class TestCronSpec(unittest.TestCase):
    """Bug: every interval over a day silently became daily on Linux, and the panel's
    confirmation still claimed the requested interval."""

    def test_exactly_daily_anchors_at_the_chosen_time(self):
        self.assertEqual(app.cron_spec(1440, 7, 30), ("30 7 * * *", 1440, "once a day at 07:30"))

    def test_multi_day_intervals_use_the_day_of_month_field(self):
        for days, spec in ((2, "30 7 */2 * *"), (3, "30 7 */3 * *"), (7, "30 7 */7 * *")):
            with self.subTest(days=days):
                got_spec, eff, _ = app.cron_spec(days * 1440, 7, 30)
                self.assertEqual(got_spec, spec)              # NOT "30 7 * * *" — the old bug
                self.assertEqual(eff, days * 1440)

    def test_sub_hour_divisors_are_exact(self):
        for mins in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30):
            with self.subTest(mins=mins):
                spec, eff, _ = app.cron_spec(mins, 7, 30)
                self.assertEqual(spec, f"*/{mins} * * * *")
                self.assertEqual(eff, mins)

    def test_hour_divisors_are_exact(self):
        for hrs in (1, 2, 3, 4, 6, 8, 12):
            with self.subTest(hrs=hrs):
                spec, eff, _ = app.cron_spec(hrs * 60, 7, 30)
                self.assertEqual(spec, f"30 */{hrs} * * *")
                self.assertEqual(eff, hrs * 60)

    def test_non_divisors_round_to_something_cron_can_express(self):
        # (requested minutes, expected spec, expected effective minutes)
        for req, spec, eff in ((45, "30 * * * *", 60),        # nearer an hour than 30 min
                               (50, "30 * * * *", 60),
                               (90, "30 */2 * * *", 120),     # was */59 * * * *
                               (7 * 60, "30 */8 * * *", 480)):  # was */7, a 3h gap at midnight
            with self.subTest(requested=req):
                self.assertEqual(app.cron_spec(req, 7, 30)[:2], (spec, eff))

    def test_effective_minutes_is_reported_honestly(self):
        """The whole point of the fix: effective_minutes differs from the request exactly
        when cron had to round, so the caller can say so."""
        for mins in (5, 30, 60, 120, 1440, 2880):
            self.assertEqual(app.cron_spec(mins, 7, 30)[1], mins, f"{mins} should be exact")
        for mins in (45, 90, 7 * 60):
            self.assertNotEqual(app.cron_spec(mins, 7, 30)[1], mins, f"{mins} should round")

    def test_every_step_is_a_valid_five_field_spec(self):
        for mins in (1, 5, 45, 90, 420, 1440, 2880, 10080, 44640):
            spec = app.cron_spec(mins, 7, 30)[0]
            with self.subTest(mins=mins):
                self.assertEqual(len(spec.split()), 5, spec)

    def test_ordinal_suffixes(self):
        self.assertEqual([app._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 31)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "31st"])
        self.assertIn("3rd", app.cron_spec(2880, 7, 30)[2])    # "on the 1st, 3rd, …"

    def test_tie_breaks_toward_the_longer_interval(self):
        """Firing less often than asked is a smaller surprise than firing twice as much."""
        self.assertEqual(app._nearest_step(25, (20, 30)), 30)
        self.assertEqual(app._nearest_step(7, (6, 8)), 8)


class TestScheduleOf(unittest.TestCase):
    def test_explicit_interval_wins(self):
        self.assertEqual(app.schedule_of({"every": 3, "every_unit": "hours"}), (180, 3, "hours"))

    def test_legacy_frequency_is_still_honoured(self):
        for freq, want in (("daily", (1440, 1, "days")), ("twice-daily", (720, 12, "hours")),
                           ("every-8h", (480, 8, "hours")), ("every-6h", (360, 6, "hours"))):
            with self.subTest(freq=freq):
                self.assertEqual(app.schedule_of({"frequency": freq}), want)

    def test_nonsense_is_clamped_not_crashed(self):
        self.assertEqual(app.schedule_of({"every": 0, "every_unit": "fortnights"}),
                         (1440, 1, "days"))
        self.assertEqual(app.schedule_of({"every": 5, "every_unit": "fortnights"})[2], "days")


# ------------------------------------------------------------------------------- MACs
class TestMacNormalisation(unittest.TestCase):
    """Bug: MAC matching was a substring test, but macOS/BSD arp prints octets unpadded
    via ether_ntoa, so a MAC with a leading-zero octet never matched."""

    def test_unpadded_and_padded_forms_are_equal(self):
        self.assertEqual(fp._norm_mac("a0:d0:5b:1:23:56"), "a0:d0:5b:01:23:56")
        self.assertEqual(fp._norm_mac("a0:d0:5b:1:23:56"), fp._norm_mac("A0:D0:5B:01:23:56"))

    def test_case_and_separator_insensitive(self):
        for form in ("A0:D0:5B:01:23:56", "a0-d0-5b-01-23-56", " a0:D0:5b:01:23:56 "):
            with self.subTest(form=form):
                self.assertEqual(fp._norm_mac(form), "a0:d0:5b:01:23:56")

    def test_rejects_non_macs(self):
        for bad in ("", None, "nope", "a0:d0:5b:01:23", "a0:d0:5b:01:23:56:78", "zz:d0:5b:01:23:56"):
            with self.subTest(bad=bad):
                self.assertIsNone(fp._norm_mac(bad))

    def _arp_returning(self, text):
        """Stub subprocess.run so _arp_ip_for_mac sees `text` as the arp table."""
        class Result:
            stdout = text
        return lambda *a, **k: Result()

    def test_finds_the_ip_in_macos_arp_output(self):
        """The exact case the old substring match got wrong: octet 01 printed as 1."""
        macos = ("? (192.168.1.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]\n"
                 "? (192.168.1.64) at a0:d0:5b:1:23:56 on en0 ifscope [ethernet]\n")
        real_run = fp.subprocess.run
        fp.subprocess.run = self._arp_returning(macos)
        try:
            self.assertEqual(fp._arp_ip_for_mac("a0:d0:5b:01:23:56"), "192.168.1.64")
            self.assertEqual(fp._arp_ip_for_mac("00:11:22:33:44:55"), "192.168.1.1")
            self.assertIsNone(fp._arp_ip_for_mac("de:ad:be:ef:00:01"))
        finally:
            fp.subprocess.run = real_run

    def test_finds_the_ip_in_linux_arp_output(self):
        linux = ("? (192.168.1.64) at a0:d0:5b:01:23:56 [ether] on wlan0\n"
                 "? (192.168.1.9) at <incomplete> on wlan0\n")
        real_run = fp.subprocess.run
        fp.subprocess.run = self._arp_returning(linux)
        try:
            self.assertEqual(fp._arp_ip_for_mac("A0:D0:5B:01:23:56"), "192.168.1.64")
            self.assertIsNone(fp._arp_ip_for_mac("de:ad:be:ef:00:01"))
        finally:
            fp.subprocess.run = real_run


class TestArpCandidateOrdering(unittest.TestCase):
    """Bug: one device often has two ARP entries — a routable lease and a self-assigned
    169.254.x link-local. Returning the table's first match handed back the address that
    cannot be connected to, and resolve_frame_ip returned it without the reachability
    check its own comment promised."""

    TABLE = ("? (169.254.35.135) at b6:e7:5c:d0:b:b2 on en1 [ethernet]\n"
             "? (192.168.4.23) at b6:e7:5c:d0:0b:b2 on en1 ifscope [ethernet]\n"
             "? (192.168.4.64) at a0:d0:5b:a0:e9:89 on en1 ifscope [ethernet]\n")

    def _with_table(self, text):
        class Result:
            stdout = text
        real = fp.subprocess.run
        fp.subprocess.run = lambda *a, **k: Result()
        self.addCleanup(lambda: setattr(fp.subprocess, "run", real))

    def test_a_routable_address_beats_a_link_local_one(self):
        self._with_table(self.TABLE)
        self.assertEqual(fp._arp_ips_for_mac("b6:e7:5c:d0:0b:b2"),
                         ["192.168.4.23", "169.254.35.135"])
        self.assertEqual(fp._arp_ip_for_mac("b6:e7:5c:d0:0b:b2"), "192.168.4.23")

    def test_a_single_entry_is_unaffected(self):
        self._with_table(self.TABLE)
        self.assertEqual(fp._arp_ips_for_mac("a0:d0:5b:a0:e9:89"), ["192.168.4.64"])

    def test_duplicate_ips_are_not_repeated(self):
        self._with_table(self.TABLE * 3)
        self.assertEqual(fp._arp_ips_for_mac("a0:d0:5b:a0:e9:89"), ["192.168.4.64"])

    def test_an_unknown_mac_yields_nothing(self):
        self._with_table(self.TABLE)
        self.assertEqual(fp._arp_ips_for_mac("de:ad:be:ef:00:01"), [])
        self.assertIsNone(fp._arp_ip_for_mac("de:ad:be:ef:00:01"))

    def test_resolve_skips_a_candidate_that_does_not_answer(self):
        """The check the old comment claimed but never did."""
        self._with_table(self.TABLE)
        fp.shutil.which = lambda t: None          # no ping binary: skip the sweep
        self.addCleanup(lambda: setattr(fp, "shutil", __import__("shutil")))
        reachable = {"192.168.4.23"}
        real_reach, real_port = fp._reachable, fp._port_open
        fp._reachable = lambda ip: ip in reachable
        fp._port_open = lambda ip, *a, **k: False
        self.addCleanup(lambda: (setattr(fp, "_reachable", real_reach),
                                 setattr(fp, "_port_open", real_port)))
        self.assertEqual(fp.resolve_frame_ip(None, "b6:e7:5c:d0:0b:b2"), "192.168.4.23")
        reachable.clear()                          # nothing answers -> still offer the best
        self.assertEqual(fp.resolve_frame_ip(None, "b6:e7:5c:d0:0b:b2"), "192.168.4.23")

    def test_a_preferred_ip_is_honoured_even_when_it_is_not_the_first_entry(self):
        self._with_table(self.TABLE)
        real = fp._reachable
        fp._reachable = lambda ip: True
        self.addCleanup(lambda: setattr(fp, "_reachable", real))
        self.assertEqual(fp.resolve_frame_ip("192.168.4.23", "b6:e7:5c:d0:0b:b2"),
                         "192.168.4.23")


class TestDiscoveryToolsMissing(unittest.TestCase):
    """Bug: a missing `ping` crashed with FileNotFoundError; a missing `arp` reported
    "Frame not found" forever."""

    def test_reachable_returns_false_without_ping(self):
        real = fp.subprocess.run
        fp.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ping"))
        try:
            self.assertIs(fp._reachable("192.168.1.50"), False)
        finally:
            fp.subprocess.run = real

    def test_arp_lookup_returns_none_without_arp(self):
        real = fp.subprocess.run
        fp.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("arp"))
        try:
            self.assertIsNone(fp._arp_ip_for_mac("a0:d0:5b:01:23:56"))
        finally:
            fp.subprocess.run = real

    def test_net_tools_missing_names_the_absent_commands(self):
        real = fp.shutil.which
        fp.shutil.which = lambda t: None
        try:
            self.assertEqual(fp._net_tools_missing(), ["ping", "arp"])
        finally:
            fp.shutil.which = real
        fp.shutil.which = lambda t: "/usr/bin/ping" if t == "ping" else None
        try:
            self.assertEqual(fp._net_tools_missing(), ["arp"])
        finally:
            fp.shutil.which = real

    def test_every_missing_tool_maps_to_an_installable_package(self):
        for tool in ("ping", "arp"):
            self.assertIn(tool, fp.APT_FOR)


# --------------------------------------------------------------------------- geometry
class TestGeometry(unittest.TestCase):
    def test_crop_loss_is_zero_for_an_exact_fit(self):
        self.assertAlmostEqual(fp.crop_loss(3840, 2160, 3840, 2160), 0.0)
        self.assertAlmostEqual(fp.crop_loss(1920, 1080, 3840, 2160), 0.0)   # same ratio

    def test_crop_loss_of_four_three_into_sixteen_nine(self):
        self.assertAlmostEqual(fp.crop_loss(4000, 3000, 3840, 2160), 0.25, places=3)

    def test_crop_loss_is_symmetric_in_orientation(self):
        self.assertAlmostEqual(fp.crop_loss(4000, 3000, 3840, 2160),
                               fp.crop_loss(3000, 4000, 2160, 3840), places=6)

    def test_crop_loss_handles_a_degenerate_size(self):
        self.assertEqual(fp.crop_loss(0, 100, 3840, 2160), 1.0)

    def test_fill_target_leaves_room_for_the_placard(self):
        self.assertEqual(fp.fill_target(False), fp.CANVAS)
        w, h = fp.fill_target(True)
        self.assertLess(w, fp.CANVAS[0])
        self.assertEqual((w, h), (fp.CANVAS[0] - 2 * fp.PLACARD_M - fp.PLACARD_TEXT_W - fp.PLACARD_GAP,
                                  fp.CANVAS[1] - 2 * fp.PLACARD_M))

    def test_display_scale_matches_the_help_text(self):
        """--max-upscale 1.6 is documented as "a 2400px-wide scan is the smallest that
        fills a 4K screen"."""
        self.assertAlmostEqual(fp.display_scale(2400, 1350, False, True), 1.6, places=2)

    def test_too_small_rejects_only_what_needs_enlarging(self):
        self.assertFalse(fp.too_small(4000, 3000, False, False, 1.6))
        self.assertTrue(fp.too_small(400, 300, False, False, 1.6))
        self.assertTrue(fp.too_small(0, 0, False, False, 1.6))
        self.assertTrue(fp.too_small(None, None, False, False, 1.6))

    def test_display_scale_agrees_with_what_the_renderer_actually_does(self):
        """If these drift, too_small() starts rejecting art that would have looked fine."""
        aw, ah = 2000, 2500
        rw, rh = fp.fill_target(True)
        self.assertAlmostEqual(fp.display_scale(aw, ah, True, False), min(rw / aw, rh / ah))
        art = Image.new("RGB", (aw, ah))
        scale = fp.display_scale(aw, ah, True, False)
        canvas = fp.mat_with_placard(art, {"title": "t"}, fp.MAT_COLORS["charcoal"])
        self.assertEqual(canvas.size, fp.CANVAS)
        self.assertLessEqual(int(aw * scale), rw + 1)
        self.assertLessEqual(int(ah * scale), rh + 1)

    def test_cover_crop_hits_the_target_exactly(self):
        for size in ((4000, 3000), (1000, 4000), (2160, 2160)):
            with self.subTest(size=size):
                out = fp.cover_crop(Image.new("RGB", size), 3840, 2160)
                self.assertEqual(out.size, (3840, 2160))

    def test_smart_fill_crops_near_fits_and_mats_the_rest(self):
        self.assertTrue(fp.fills_screen(4000, 2400, 3840, 2160, True, 0.15))    # 1.67:1, ~6% trimmed
        self.assertFalse(fp.fills_screen(4000, 3000, 3840, 2160, True, 0.15))   # 4:3 loses 25%
        self.assertFalse(fp.fills_screen(2000, 3000, 3840, 2160, True, 0.15))   # a portrait
        self.assertTrue(fp.fills_screen(2000, 3000, 3840, 2160, True, 1.0))     # "always" still crops
        self.assertFalse(fp.fills_screen(4000, 2400, 3840, 2160, False, 1.0))   # fill off

    @staticmethod
    def _with_detail(size, box):
        """A flat grey picture with a busy checkerboard 'subject' inside `box`."""
        im = Image.new("RGB", size, (120, 120, 120))
        x0, y0, x1, y1 = box
        for x in range(x0, x1, 8):
            for y in range(y0, y1, 8):
                if (x // 8 + y // 8) % 2:
                    im.paste((250, 250, 250), (x, y, x + 8, y + 8))
        return im

    def test_the_crop_keeps_an_off_centre_subject_in_a_portrait(self):
        art = self._with_detail((1200, 1800), (200, 150, 1000, 650))     # a head near the top
        keep = round(1200 / (3840 / 2160))                               # 675 px of 1800
        y0 = fp.crop_start(art, keep, horizontal=False)
        self.assertLessEqual(y0, 150)                                    # whole subject in shot
        self.assertGreaterEqual(y0 + keep, 650)

    def test_the_crop_follows_the_subject_sideways_too(self):
        art = self._with_detail((3000, 1000), (2300, 200, 2900, 800))    # subject at the right
        keep = round(1000 * 3840 / 2160)
        x0 = fp.crop_start(art, keep, horizontal=True)
        self.assertGreaterEqual(x0 + keep, 2900)

    def test_a_featureless_picture_is_cropped_from_the_centre(self):
        art = Image.new("RGB", (1200, 1800), (90, 90, 90))
        keep = 675
        self.assertAlmostEqual(fp.crop_start(art, keep, horizontal=False), (1800 - keep) / 2, delta=10)


# -------------------------------------------------------------------------- rendering
class TestPrepLocal(TempState):
    """Bug: browsing history re-matted an already-finished 4K render, shrinking it to
    86% inside a second mat (and drawing googly eyes onto the placard)."""

    def _img(self, size, colour, name):
        p = os.path.join(self.tmp, name)
        Image.new("RGB", size, colour).save(p)
        return p

    def test_a_canvas_sized_render_is_passed_through_untouched(self):
        src = self._img(fp.CANVAS, (10, 20, 30), "hist.jpg")
        self.assertEqual(fp.prep_local([src], fp.MAT_COLORS["charcoal"]), [src])

    def test_a_googly_roll_does_not_re_mat_a_finished_render(self):
        src = self._img(fp.CANVAS, (10, 20, 30), "hist.jpg")
        out = fp.prep_local([src], fp.MAT_COLORS["charcoal"], googly_chance=1.0)
        im = Image.open(out[0])
        self.assertEqual(im.size, fp.CANVAS)
        self.assertEqual(im.getpixel((5, 5)), (10, 20, 30),
                         "the corner is mat colour — the render was shrunk and re-matted")

    def test_a_smaller_file_is_still_matted_onto_the_canvas(self):
        src = self._img((1200, 900), (200, 50, 50), "small.jpg")
        im = Image.open(fp.prep_local([src], fp.MAT_COLORS["charcoal"])[0])
        self.assertEqual(im.size, fp.CANVAS)
        self.assertEqual(im.getpixel((5, 5)), fp.MAT_COLORS["charcoal"])

    def test_matting_is_not_cumulative_across_repeated_calls(self):
        """Stepping back and forth through history repeatedly must be idempotent."""
        src = self._img(fp.CANVAS, (10, 20, 30), "hist.jpg")
        out = src
        for _ in range(3):
            out = fp.prep_local([out], fp.MAT_COLORS["charcoal"])[0]
        self.assertEqual(Image.open(out).getpixel((5, 5)), (10, 20, 30))


class TestRender(unittest.TestCase):
    META = {"title": "The Harvesters", "artist": "Pieter Bruegel the Elder",
            "bio": "Netherlandish, ca. 1525-1569", "date": "1565", "medium": "Oil on wood",
            "dimensions": "119 x 162 cm", "credit": "Rogers Fund, 1919",
            "culture_period": "Netherlandish", "museum": "The Metropolitan Museum of Art"}

    def test_every_layout_produces_a_full_canvas(self):
        art = Image.new("RGB", (2400, 1600), (120, 90, 60))
        for placard in (True, False):
            for fill in (True, False):
                with self.subTest(placard=placard, fill=fill):
                    out = (fp.mat_with_placard(art, self.META, fp.MAT_COLORS["charcoal"],
                                               "A caption.", "https://example.org/x", fill)
                           if placard else fp.mat_image(art, fp.MAT_COLORS["charcoal"], fill))
                    self.assertEqual(out.size, fp.CANVAS)

    def test_a_long_caption_still_fits_beside_the_art(self):
        """The placard block is vertically centred with no overflow handling, so a
        maximal caption plus full metadata must not run past the canvas."""
        from PIL import ImageDraw
        draw = ImageDraw.Draw(Image.new("RGB", fp.CANVAS))
        desc = fp._truncate_prose("This piece matters to the history of art. " * 40)
        rows = [("Georgia Bold.ttf", 60, self.META["artist"], 6),
                ("Georgia Italic.ttf", 33, self.META["bio"], 44),
                ("Georgia Italic.ttf", 50, self.META["title"], 4),
                ("Georgia.ttf", 37, self.META["date"], 40),
                ("Georgia.ttf", 31, desc, 40),
                ("Georgia.ttf", 33, self.META["medium"], 6),
                ("Georgia.ttf", 33, self.META["culture_period"], 6),
                ("Georgia.ttf", 29, self.META["dimensions"], 40),
                ("Georgia Italic.ttf", 28, self.META["credit"], 26),
                ("Georgia.ttf", 26, self.META["museum"], 0)]
        total = sum(int(size * 1.32) * len(fp._wrap(draw, text, fp._font(name, size),
                                                    fp.PLACARD_TEXT_W)) + gap
                    for name, size, text, gap in rows)
        self.assertLessEqual(total, fp.CANVAS[1] - 2 * fp.PLACARD_M,
                             "placard text would overflow the canvas")

    def test_truncate_prose_keeps_whole_sentences_under_the_limit(self):
        text = fp._truncate_prose("One. Two. Three. " * 100, limit=60)
        self.assertLessEqual(len(text), 60)
        self.assertTrue(text.endswith(".") or text.endswith("…"))
        short = "Just the one sentence."
        self.assertEqual(fp._truncate_prose(short), short)

    def test_truncate_prose_handles_prose_with_no_sentence_breaks(self):
        """Bug: the loop accepted its first segment unconditionally, so prose with no
        sentence break was returned whole however long it was."""
        out = fp._truncate_prose("word " * 200, limit=50)
        self.assertLessEqual(len(out), 52)
        self.assertTrue(out.endswith("…"))

    def test_truncate_prose_caps_a_first_sentence_longer_than_the_budget(self):
        """The realistic case: one long comma-heavy curatorial sentence."""
        text = "This painting, executed over many months, comma after comma, " * 40
        out = fp._truncate_prose(text, limit=620)
        self.assertLessEqual(len(out), 621, "an over-long first sentence escaped the limit")

    def test_truncate_prose_never_exceeds_its_limit(self):
        for text in ("word " * 400,
                     "One giant clause, " * 100,
                     "Short. " * 300,
                     "No punctuation at all just words running on and on " * 30):
            for limit in (50, 200, 620):
                with self.subTest(limit=limit, text=text[:24]):
                    self.assertLessEqual(len(fp._truncate_prose(text, limit=limit)), limit + 1)


class TestFillTheScreen(unittest.TestCase):
    """"Fill the screen, no borders" — the tolerance is what decides whether an
    off-shape piece is cropped or skipped, and 1.0 means never skip for shape."""

    def test_tolerance_of_one_never_skips_for_shape(self):
        tw, th = fp.fill_target(False)
        for size in ((3840, 2160), (4000, 3000), (3000, 3000), (2000, 3000), (1000, 4000)):
            with self.subTest(size=size):
                self.assertLess(fp.crop_loss(*size, tw, th), 1.0,
                                "crop_loss must stay under 1.0 or a 1.0 tolerance would skip")

    def test_a_lower_tolerance_still_skips_off_shape_art(self):
        tw, th = fp.fill_target(False)
        self.assertGreater(fp.crop_loss(2000, 3000, tw, th), 0.3)   # portrait, skipped at 0.3
        self.assertLess(fp.crop_loss(3000, 2000, tw, th), 0.3)      # 3:2 landscape, kept

    def test_the_clamp_allows_one(self):
        """The old clamp was 0.5, so "crop whatever it takes" was unreachable."""
        import subprocess, sys
        captured = {}
        real_run, real_argv = fp.run, sys.argv
        fp.run = lambda a: captured.update(fill=a.fill, tol=a.fill_tolerance)
        sys.argv = ["frame_push.py", "--fill", "--fill-tolerance", "1.0",
                    "--mac", "a0:d0:5b:01:23:56"]
        try:
            fp.main()
        finally:
            fp.run, sys.argv = real_run, real_argv
        self.assertEqual(captured, {"fill": True, "tol": 1.0})

    def test_out_of_range_tolerances_are_still_clamped(self):
        import sys
        for given, want in (("5", 1.0), ("-1", 0.0)):
            captured = {}
            real_run, real_argv = fp.run, sys.argv
            fp.run = lambda a: captured.update(tol=a.fill_tolerance)
            sys.argv = ["frame_push.py", "--fill-tolerance", given, "--mac", "a0:d0:5b:01:23:56"]
            try:
                fp.main()
            finally:
                fp.run, sys.argv = real_run, real_argv
            with self.subTest(given=given):
                self.assertEqual(captured["tol"], want)

    def test_fill_renders_edge_to_edge_with_no_mat(self):
        """The actual "no borders" requirement: not one pixel of mat colour."""
        mat = fp.MAT_COLORS["charcoal"]
        for size in ((2000, 3000), (4000, 3000), (3000, 3000)):
            with self.subTest(size=size):
                out = fp.mat_image(Image.new("RGB", size, (200, 60, 40)), mat, fill=True)
                self.assertEqual(out.size, fp.CANVAS)
                self.assertNotIn(mat, set(out.getdata()), "mat colour is visible in fill mode")

    def test_the_museum_label_reintroduces_a_border(self):
        """Why the panel hint has to mention it: fill + placard is not edge to edge."""
        mat = fp.MAT_COLORS["charcoal"]
        out = fp.mat_with_placard(Image.new("RGB", (2000, 3000), (200, 60, 40)),
                                  {"title": "t"}, mat, None, None, True)
        self.assertEqual(out.getpixel((5, 5)), mat)

    def test_the_resolution_guard_still_applies_in_fill_mode(self):
        """Independent of shape: fill scales by the tighter axis, so a narrow scan is
        skipped however loose the tolerance. The hint says so."""
        self.assertFalse(fp.too_small(2400, 1350, False, True, 1.6))
        self.assertTrue(fp.too_small(2000, 3000, False, True, 1.6))
        self.assertTrue(fp.too_small(1600, 2400, False, True, 1.6))

    def test_the_panel_offers_and_round_trips_the_always_option(self):
        page_has = '<option value="1">' in app.PAGE
        self.assertTrue(page_has, "the Screen fit dropdown has no always-fill option")
        self.assertIn("'0.1','0.2','0.3','1'", app.PAGE,
                      "hydration would snap a saved 1.0 back to 0.3")
        flags = app.flags_from(dict(fp.DEFAULTS, fill=True, fill_tolerance=1.0, placard=False,
                                    mac="a0:d0:5b:01:23:56"))
        self.assertIn("--fill", flags)
        self.assertIn("--no-placard", flags)
        self.assertEqual(flags[flags.index("--fill-tolerance") + 1], "1.0")


# ------------------------------------------------------------------- fullscreen layout
class TestFullscreenLayout(TempState):
    """2026-11: the TV kept showing the old museum-label layout because a saved config
    still had placard on and fill off. A pre-v2 config is moved to fullscreen once."""

    def _write(self, d):
        import json
        with open(fp.CONFIG, "w") as f:
            json.dump(d, f)

    def _read(self):
        import json
        with open(fp.CONFIG) as f:
            return json.load(f)

    def test_an_old_config_is_moved_to_fullscreen_and_saved(self):
        self._write({"placard": True, "fill": False, "mat": "linen", "source": "met", "mac": "aa"})
        cfg = fp.load_config()
        for k, v in fp.FULLSCREEN_LAYOUT.items():
            self.assertEqual(cfg[k], v, k)
        self.assertEqual(cfg["source"], "modern")
        self.assertEqual(cfg["mac"], "aa", "unrelated settings must survive")
        self.assertEqual(self._read()["layout_version"], fp.LAYOUT_VERSION, "not persisted")

    def test_it_happens_once_so_a_later_choice_sticks(self):
        self._write({"placard": True, "source": "met", "layout_version": fp.LAYOUT_VERSION})
        cfg = fp.load_config()
        self.assertTrue(cfg["placard"])
        self.assertEqual(cfg["source"], "met")

    def test_a_modern_source_already_chosen_is_kept(self):
        self._write({"source": "nasa"})
        self.assertEqual(fp.load_config()["source"], "nasa")

    def test_a_hand_edited_version_still_migrates_instead_of_failing(self):
        """A raise here would leave load_config on bare DEFAULTS, and the next panel save
        would then overwrite the real config (password included)."""
        self._write({"layout_version": "abc", "password": "pw", "placard": True})
        cfg = fp.load_config()
        self.assertEqual(cfg["password"], "pw")
        self.assertFalse(cfg["placard"])

    def test_the_defaults_are_fullscreen(self):
        self.assertFalse(fp.DEFAULTS["placard"])
        self.assertTrue(fp.DEFAULTS["fill"] and fp.DEFAULTS["fill_mat"])
        self.assertEqual(fp.DEFAULTS["mat"], "blur")
        self.assertIn(fp.DEFAULTS["source"], fp.SOURCE_CHOICES)
        self.assertEqual(fp.DEFAULTS["layout_version"], fp.LAYOUT_VERSION)

    def test_a_3_2_photo_fills_at_the_default_tolerance(self):
        """Most camera photos are 3:2 (15.6% trim). At the old 0.15 they all fell back to
        the backdrop instead of filling the screen."""
        tw, th = fp.fill_target(False)
        self.assertTrue(fp.fills_screen(6000, 4000, tw, th, True, fp.DEFAULTS["fill_tolerance"]))


class TestBlurBackdrop(unittest.TestCase):
    def test_blur_puts_the_whole_piece_on_a_screen_sized_backdrop(self):
        art = Image.new("RGB", (2000, 3000), (200, 60, 40))
        out = fp.mat_image(art, fp.MAT_COLORS["blur"])
        self.assertEqual(out.size, fp.CANVAS)
        self.assertEqual(out.getpixel((fp.CANVAS[0] // 2, fp.CANVAS[1] // 2)), (200, 60, 40))
        corner = out.getpixel((5, 5))
        self.assertTrue(all(c < v for c, v in zip(corner, (200, 61, 41))), "backdrop isn't dimmed")
        self.assertNotIn(fp.MAT_COLORS["charcoal"], (corner,))

    def test_the_label_falls_back_to_a_flat_colour(self):
        out = fp.mat_with_placard(Image.new("RGB", (2000, 3000), (200, 60, 40)),
                                  {"title": "t"}, fp.MAT_COLORS["blur"])
        self.assertEqual(out.getpixel((5, 5)), fp.PLACARD_FALLBACK_MAT)

    def test_prep_local_accepts_blur(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "a.jpg")
        Image.new("RGB", (800, 1200), (10, 200, 10)).save(src)
        real = fp.TMP
        fp.TMP = os.path.join(tmp, "out")
        try:
            out = fp.prep_local([src], fp.MAT_COLORS["blur"])
        finally:
            fp.TMP = real
        self.assertEqual(Image.open(out[0]).size, fp.CANVAS)


# ------------------------------------------------------------------- modern sources
class Patch:
    """Temporarily replace attributes on a module."""
    def __init__(self, mod, **attrs):
        self.mod, self.attrs = mod, attrs

    def __enter__(self):
        self.saved = {k: getattr(self.mod, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)
        return False


class TestSourcePick(TempState):
    def setUp(self):
        super().setUp()
        self._env = os.environ.pop("UNSPLASH_ACCESS_KEY", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["UNSPLASH_ACCESS_KEY"] = self._env
        super().tearDown()

    def test_mixes_skip_unsplash_without_a_key(self):
        for _ in range(60):
            self.assertIn(fp.pick_source("modern"), ("wikimedia", "nasa"))
            self.assertNotEqual(fp.pick_source("everything"), "unsplash")

    def test_a_saved_key_brings_unsplash_into_the_mix(self):
        with open(os.path.join(fp.CFG, "unsplash_key.txt"), "w") as f:
            f.write("abc\n")
        self.assertEqual(fp.unsplash_key(), "abc")
        self.assertIn("unsplash", {fp.pick_source("modern") for _ in range(200)})

    def test_a_single_source_is_returned_as_is(self):
        for src in fp.MUSEUM_SOURCES + fp.MODERN_SOURCES:
            self.assertEqual(fp.pick_source(src), src)

    def test_every_source_has_a_fallback_and_a_name(self):
        for src in fp.MUSEUM_SOURCES + fp.MODERN_SOURCES + (fp.AERIAL_SOURCE,):
            self.assertIn(fp.SOURCE_FALLBACK[src], fp.SOURCE_NAMES)
            self.assertIn(src, fp.SOURCE_NAMES)


def _wm_page(pid, title, objname="", date="2021-05-01 10:00", mime="image/jpeg", w=6000, h=4000):
    return {"pageid": pid, "ns": 6, "title": title, "imageinfo": [{
        "width": w, "height": h, "mime": mime, "url": f"https://upload/{pid}.jpg",
        "thumburl": f"https://thumb/{pid}.jpg", "descriptionurl": f"https://commons/{pid}",
        "extmetadata": {"ObjectName": {"value": objname}, "DateTimeOriginal": {"value": date},
                        "Artist": {"value": "<bdi><a href='x'>Ann Example</a></bdi>"},
                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                        "ImageDescription": {"value": "A <i>misty</i> lake."}}}]}


class TestWikimediaCandidates(unittest.TestCase):
    def _run(self, pages):
        calls = []
        def fake(url, params=None, timeout=30):
            calls.append(params)
            return {"query": {"pages": {str(p["pageid"]): p for p in pages}}}
        with Patch(fp, met_json=fake):
            return fp.wikimedia_candidates("lake"), calls

    def test_parses_and_cleans_a_featured_photo(self):
        out, calls = self._run([_wm_page(1, "File:Misty lake.jpg", "Misty lake")])
        c = out[0]
        self.assertEqual(c["id"], "wm:1")
        self.assertEqual(c["img_url"], "https://thumb/1.jpg")
        self.assertEqual(c["meta"]["artist"], "Ann Example")
        self.assertEqual(c["meta"]["date"], "2021-05-01")
        self.assertEqual(c["real_text"], "A misty lake.")
        q = calls[0]["gsrsearch"]
        self.assertIn("incategory:Featured_pictures_on_Wikimedia_Commons", q)
        self.assertIn("-painting", q)

    def test_wikidata_markup_titles_become_readable(self):
        out, _ = self._run([_wm_page(2, "File:Glacier.jpg",
                                     'Glacier title QS:P1476,en:"Tasman Glacier" label QS:Len,"x"')])
        self.assertEqual(out[0]["meta"]["title"], "Tasman Glacier")

    def test_old_scans_and_non_photos_are_dropped(self):
        out, _ = self._run([_wm_page(3, "File:Old.jpg", date="1865 date QS:P571,+1865"),
                            _wm_page(4, "File:Map.svg", mime="image/svg+xml"),
                            _wm_page(5, "File:New.jpg", date="")])
        self.assertEqual([c["id"] for c in out], ["wm:5"])


class TestNasaCandidates(unittest.TestCase):
    def _item(self, nid, centre, href=None, w=6000, h=4000):
        href = href or f"http://images-assets.nasa.gov/image/{nid}/{nid}~orig.jpg"
        return {"data": [{"nasa_id": nid, "center": centre, "title": f"T {nid}",
                          "description": "<p>Stars</p>", "date_created": "2020-01-02T00:00:00Z",
                          "secondary_creator": "NASA/JPL"}],
                "links": [{"rel": "preview", "href": "x~thumb.jpg"},
                          {"rel": "canonical", "href": href, "width": w, "height": h}]}

    def test_press_photos_and_non_jpegs_are_dropped(self):
        items = [self._item("PIA1", "JPL"), self._item("KSC-1", "KSC"), self._item("HQ-1", "HQ"),
                 self._item("PIA2", "GSFC", href="http://x/PIA2~orig.tif")]
        def fake(url, params=None, timeout=30):
            return {"collection": {"metadata": {"total_hits": len(items)}, "items": items}}
        with Patch(fp, met_json=fake):
            out = fp.nasa_candidates("nebula")
        self.assertEqual([c["id"] for c in out], ["nasa:PIA1"])
        self.assertTrue(out[0]["img_url"].startswith("https://"))
        self.assertEqual((out[0]["w"], out[0]["h"]), (6000, 4000))
        self.assertEqual(out[0]["page_url"], "https://images.nasa.gov/details/PIA1")

    def test_charts_and_labelled_illustrations_are_dropped(self):
        items = [self._item("PIA1", "JPL"), self._item("PIA2", "JPL"), self._item("PIA3", "GSFC")]
        items[1]["data"][0]["title"] = "Flattening of Brown Dwarf, Jupiter and Saturn (Illustration)"
        items[2]["data"][0]["title"] = "Mimas Global Map - June 2017"
        def fake(url, params=None, timeout=30):
            return {"collection": {"metadata": {"total_hits": len(items)}, "items": items}}
        with Patch(fp, met_json=fake):
            self.assertEqual([c["id"] for c in fp.nasa_candidates("saturn")], ["nasa:PIA1"])

    def test_only_the_most_relevant_pages_are_used(self):
        """Bug: a random page of up to 100 reached page 20+ of "Jupiter", which is wind-tunnel
        models and team meetings."""
        pages = set()
        def fake(url, params=None, timeout=30):
            pages.add(int(params["page"]))
            return {"collection": {"metadata": {"total_hits": 9000}, "items": []}}
        with Patch(fp, met_json=fake):
            for _ in range(60):
                fp.nasa_candidates("jupiter")
        self.assertLessEqual(max(pages), fp.NASA_MAX_PAGE)
        self.assertGreater(len(pages), 1)

    def test_an_illustration_named_only_in_its_description_is_dropped(self):
        """Bug: "Fluid Flow in Ceres Due to Core Heating", a labelled cutaway, got through on
        its title; its description opens "This illustration depicts the interior..."."""
        self.assertTrue(fp.nasa_is_graphic("Fluid Flow in Ceres Due to Core Heating",
                                           "<p>This illustration depicts the interior of dwarf planet Ceres</p>"))
        self.assertTrue(fp.nasa_is_graphic("TRAPPIST-1e", "An artist's concept of the planet."))
        self.assertTrue(fp.nasa_is_graphic("Cutaway of Europa", ""))
        self.assertTrue(fp.nasa_is_graphic("Engine Schematic", None))
        self.assertTrue(fp.nasa_is_graphic("Atmospheric Circulation Cells on Earth and Jupiter",
                                           "This graphic compares the atmospheric circulations"))
        self.assertTrue(fp.nasa_is_graphic("NASA's Europa Clipper Takes Center Stage",
                                           "Engineers and technicians work on the towering main body"))

    def test_photographs_are_kept(self):
        self.assertFalse(fp.nasa_is_graphic("Jupiter's Great Red Spot",
                                            "Juno photographed the storm; scientists mapped its winds."))
        self.assertFalse(fp.nasa_is_graphic("Pillars of Creation", "A comparison of Hubble and Webb views."))
        self.assertFalse(fp.nasa_is_graphic("Saturn's Rings", "A photographic mosaic from Cassini."))
        self.assertFalse(fp.nasa_is_graphic("Saturn", "x" * fp.NASA_DESC_CHARS + " illustration"))

    def test_white_padding_is_trimmed_before_the_fit_is_judged(self):
        """Bug: a square infrared map padded with white to 16:9 'filled' the screen,
        white bars and all."""
        im = Image.new("RGB", (1600, 900), (255, 255, 255))
        im.paste(Image.new("RGB", (900, 900), (190, 10, 0)), (350, 0))
        out = fp.trim_white_border(im)
        self.assertLess(abs(out.width - 900), 10)
        self.assertEqual(out.height, 900)

    def test_black_space_and_unpadded_pictures_are_left_alone(self):
        im = Image.new("RGB", (1600, 900), (0, 0, 0))
        im.paste(Image.new("RGB", (400, 400), (200, 180, 150)), (600, 250))
        self.assertIs(fp.trim_white_border(im), im)
        sky = Image.new("RGB", (1600, 900), (250, 250, 252))
        sky.paste(Image.new("RGB", (1600, 300), (40, 90, 30)), (0, 600))
        self.assertIs(fp.trim_white_border(sky), sky)     # a white sky reaching the edge

    def test_grayscale_detection(self):
        self.assertTrue(fp.is_grayscale(Image.new("RGB", (100, 100), (120, 120, 120))))
        self.assertFalse(fp.is_grayscale(Image.new("RGB", (100, 100), (200, 60, 40))))


class TestUnsplashCandidates(TempState):
    PHOTO = {"id": "abc", "width": 6000, "height": 4000, "description": None,
             "alt_description": "a foggy forest", "created_at": "2024-03-01T00:00:00Z",
             "urls": {"raw": "https://images.unsplash.com/photo-1?ixid=x"},
             "links": {"html": "https://unsplash.com/photos/abc",
                       "download_location": "https://api.unsplash.com/photos/abc/download"},
             "user": {"name": "Jo Snap"}}

    class R:
        def __init__(self, body, code=200):
            self.status_code, self._body = code, body
        def json(self):
            return self._body

    def setUp(self):
        super().setUp()
        self._env = os.environ.pop("UNSPLASH_ACCESS_KEY", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["UNSPLASH_ACCESS_KEY"] = self._env
        super().tearDown()

    def test_no_key_means_no_request(self):
        calls = []
        with Patch(fp, http_get=lambda *a, **k: calls.append(a)):
            self.assertEqual(fp.unsplash_candidates("fog"), [])
        self.assertEqual(calls, [])

    def test_parses_photos_and_pings_the_download_endpoint_on_use(self):
        os.environ["UNSPLASH_ACCESS_KEY"] = "KEY"
        calls = []
        def fake(url, params=None, headers=None, tries=4, timeout=30):
            calls.append((url, params, headers))
            return self.R([self.PHOTO])
        with Patch(fp, http_get=fake):
            out = fp.unsplash_candidates("fog")
            self.assertEqual(calls[0][2]["Authorization"], "Client-ID KEY")
            self.assertEqual(calls[0][1]["query"], "fog")
            self.assertEqual(calls[0][1]["orientation"], "landscape")
            c = out[0]
            self.assertEqual(c["img_url"], "https://images.unsplash.com/photo-1?ixid=x&w=3840&fm=jpg&q=88")
            self.assertEqual(c["meta"]["credit"], "Photo by Jo Snap on Unsplash")
            self.assertEqual(c["meta"]["title"], "a foggy forest")
            c["on_use"]()
        self.assertEqual(calls[-1][0], "https://api.unsplash.com/photos/abc/download")

    def test_the_key_is_never_sent_to_another_host(self):
        os.environ["UNSPLASH_ACCESS_KEY"] = "KEY"
        photo = dict(self.PHOTO, links={"html": "h", "download_location": "https://evil.example/x"})
        calls = []
        def fake(url, params=None, headers=None, tries=4, timeout=30):
            calls.append(url)
            return self.R([photo])
        with Patch(fp, http_get=fake):
            fp.unsplash_candidates("fog")[0]["on_use"]()
        self.assertEqual(calls, [fp.UNSPLASH_API])

    def test_a_rejected_key_gives_no_candidates(self):
        os.environ["UNSPLASH_ACCESS_KEY"] = "BAD"
        with Patch(fp, http_get=lambda *a, **k: self.R({"errors": ["OAuth error"]}, 401)):
            self.assertEqual(fp.unsplash_candidates(None), [])


class TestFetchModern(TempState):
    def _cand(self, cid, w=6000, h=4000, **kw):
        return {"id": cid, "img_url": f"https://x/{cid}.jpg", "w": w, "h": h, "page_url": f"https://p/{cid}",
                "real_text": "", "meta": {"title": cid, "artist": "A", "date": "2020", "credit": "c"}, **kw}

    def test_renders_records_and_calls_on_use(self):
        used = []
        cands = [self._cand("nasa:1", on_use=lambda: used.append(1))]
        with Patch(fp, nasa_candidates=lambda term, exclude=(): cands,
                   download_image=lambda url, headers=None: Image.new("RGB", (6000, 4000), (200, 60, 40))):
            paths = fp.fetch_modern("nasa", 1, None, None, fill=True, fill_mat=True)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Image.open(paths[0]).size, fp.CANVAS)
        self.assertEqual(used, [1])
        self.assertEqual(fp.LAST_PIECES[0]["source"], "NASA")
        self.assertEqual(fp.LAST_PIECES[0]["id"], "nasa:1")

    def test_small_listed_images_are_never_downloaded(self):
        fetched = []
        with Patch(fp, wikimedia_candidates=lambda term, exclude=(): [self._cand("wm:1", 800, 600)],
                   download_image=lambda url, headers=None: fetched.append(url)):
            self.assertEqual(fp.fetch_modern("wikimedia", 1, None, None, fill=True, fill_mat=True), [])
        self.assertEqual(fetched, [])

    def test_gives_up_after_repeated_failed_downloads(self):
        tries = []
        cands = [self._cand(f"wm:{i}") for i in range(20)]
        with Patch(fp, wikimedia_candidates=lambda term, exclude=(): cands,
                   download_image=lambda url, headers=None: tries.append(url)):
            fp.fetch_modern("wikimedia", 1, None, None)
        self.assertEqual(len(tries), fp.MAX_FAILED_DOWNLOADS)

    def test_banned_and_recent_pieces_are_avoided(self):
        fetched = []
        with Patch(fp, nasa_candidates=lambda term, exclude=(): [self._cand("nasa:1")],
                   download_image=lambda url, headers=None: fetched.append(url)):
            fp.fetch_modern("nasa", 1, None, None, avoid={"nasa:1"})
        self.assertEqual(fetched, [])

    def test_an_api_that_changed_shape_gives_no_candidates_not_a_crash(self):
        def broken(url, params=None, timeout=30):
            return {"collection": {"metadata": {"total_hits": "lots"}, "items": []}}
        with Patch(fp, met_json=broken):
            self.assertEqual(fp.fetch_modern("nasa", 1, None, None), [])

    def test_black_and_white_nasa_frames_are_skipped(self):
        with Patch(fp, nasa_candidates=lambda term, exclude=(): [self._cand("nasa:1")],
                   download_image=lambda url, headers=None: Image.new("RGB", (6000, 4000), (90, 90, 90))):
            self.assertEqual(fp.fetch_modern("nasa", 1, None, None), [])


class TestAerial(TempState):
    """Apple TV aerial stills: one frame grabbed with ffmpeg from Apple's screen-saver films."""
    HOST = "https://sylvan.apple.com/Aerials/2x/Videos/"

    def _asset(self, aid, label, pois, **urls):
        urls = urls or {"url-4K-SDR": f"{self.HOST}{aid}_SDR_4K_HEVC.mov",
                        "url-4K-HDR": f"{self.HOST}{aid}_HDR_4K_HEVC.mov"}
        return {"id": aid, "accessibilityLabel": label,
                "pointsOfInterest": {str(k): v for k, v in pois.items()}, **urls}

    def _cands(self, assets, captions=None, term=None):
        with Patch(fp, ffmpeg_path=lambda: "/bin/ffmpeg",
                   aerial_manifest=lambda: (assets, captions or {})):
            return fp.aerial_candidates(term)

    def test_one_sdr_candidate_per_film(self):
        out = self._cands([self._asset("A1", "Greenland", {0: "G_0", 60: "G_60"}),
                           self._asset("A2", "London", {0: "L_0"})])
        self.assertEqual([c["id"] for c in out], ["aerial:A1", "aerial:A2"])
        self.assertTrue(all("_SDR_" in c["img_url"] for c in out))     # never the HDR film
        self.assertEqual((out[0]["w"], out[0]["h"]), fp.CANVAS)
        self.assertEqual(out[0]["meta"]["title"], "Greenland")

    def test_urls_off_apples_host_are_dropped(self):
        evil = self._asset("A3", "Evil", {0: "E_0"}, **{"url-4K-SDR": "file:///etc/passwd",
                                                         "url-1080-SDR": "https://evil.example/x.mov"})
        self.assertEqual(self._cands([evil]), [])

    def test_a_term_picks_the_matching_point_and_apples_nbsps_match(self):
        caps = {"S_0": "Over the city", "S_140": "San\xa0Francisco Bay and the Golden\xa0Gate Bridge"}
        a = [self._asset("A1", "San Francisco", {0: "S_0", 140: "S_140"}),
             self._asset("A2", "London", {0: "L_0"})]
        for _ in range(10):
            out = self._cands(a, caps, "golden gate")
            self.assertEqual([c["id"] for c in out], ["aerial:A1"])
            self.assertEqual(out[0]["real_text"], "San Francisco Bay and the Golden Gate Bridge")
        self.assertEqual([c["id"] for c in self._cands(a, caps, "london")], ["aerial:A2"])

    def test_no_ffmpeg_means_no_candidates_and_no_manifest_fetch(self):
        def boom():
            raise AssertionError("fetched the manifest without ffmpeg")
        with Patch(fp, ffmpeg_path=lambda: None, aerial_manifest=boom):
            self.assertEqual(fp.aerial_candidates(None), [])

    def _png(self, colour=(30, 90, 160), noisy=True):
        import io, random
        im = Image.new("RGB", (320, 180), colour)
        if noisy:
            for _ in range(4000):
                im.putpixel((random.randrange(320), random.randrange(180)), (250, 250, 250))
        buf = io.BytesIO(); im.save(buf, "PNG")
        return buf.getvalue()

    def test_the_grab_is_locked_to_apple_over_verified_tls(self):
        import subprocess
        seen = []
        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, self._png(), b"")
        with Patch(fp.subprocess, run=run):
            im = fp.aerial_frame(self.HOST + "x.mov", 68, "/bin/ffmpeg")
            self.assertIsNone(fp.aerial_frame("https://evil.example/x.mov", 1, "/bin/ffmpeg"))
        self.assertEqual(im.size, (320, 180))
        self.assertEqual(len(seen), 1)                       # the off-host URL never ran ffmpeg
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "https,tls,tcp")
        self.assertEqual(cmd[cmd.index("-tls_verify") + 1], "1")
        with open(cmd[cmd.index("-ca_file") + 1]) as f:
            self.assertEqual(f.read(), fp.APPLE_ROOT_CA)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "68")

    def test_a_fade_to_black_is_not_a_picture(self):
        import subprocess
        flat = self._png((2, 2, 2), noisy=False)
        with Patch(fp.subprocess, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, flat, b"")):
            self.assertIsNone(fp.aerial_frame(self.HOST + "x.mov", 8, "/bin/ffmpeg"))

    def test_ffmpeg_failing_gives_none(self):
        import subprocess
        with Patch(fp.subprocess, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"boom")):
            self.assertIsNone(fp.aerial_frame(self.HOST + "x.mov", 8, "/bin/ffmpeg"))

    def test_fetch_modern_renders_a_grabbed_frame(self):
        def never(url, headers=None):
            raise AssertionError("downloaded the film instead of grabbing a frame")
        cand = {"id": "aerial:A1", "img_url": self.HOST + "x.mov", "w": 3840, "h": 2160, "page_url": "",
                "real_text": "Over Greenland", "grab": lambda: Image.new("RGB", (3840, 2160), (40, 90, 160)),
                "meta": {"title": "Greenland", "artist": "Apple TV aerial", "date": "", "credit": "Apple"}}
        with Patch(fp, aerial_candidates=lambda term, exclude=(): [cand], download_image=never):
            paths = fp.fetch_modern(fp.AERIAL_SOURCE, 1, None, None, fill=True)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Image.open(paths[0]).size, fp.CANVAS)
        self.assertEqual(fp.LAST_PIECES[0]["source"], "Apple TV aerials")

    def test_aerials_are_opt_in_never_in_a_mix(self):
        self.assertIn(fp.AERIAL_SOURCE, fp.SOURCE_CHOICES)
        for pool in fp.SOURCE_POOLS.values():
            self.assertNotIn(fp.AERIAL_SOURCE, pool)

    def test_ffmpeg_is_found_under_launchds_bare_path(self):
        saved = os.environ.pop("FRAME_FFMPEG", None)
        try:
            with Patch(fp.shutil, which=lambda name: None), \
                 Patch(fp.os, access=lambda p, mode: p == "/opt/homebrew/bin/ffmpeg"):
                self.assertEqual(fp.ffmpeg_path(), "/opt/homebrew/bin/ffmpeg")
        finally:
            if saved is not None:
                os.environ["FRAME_FFMPEG"] = saved


def _run_args(**kw):
    """The argparse fields _fetch_source/_gather read, with every spice mode switched on."""
    base = dict(source="wikimedia", query=None, subject="", theme="museum", types=[], all_types=True,
                seasonal_chance=1.0, holidays_chance=1.0, weather_chance=1.0, on_this_day_chance=1.0,
                hemisphere="north", latitude=None, longitude=None, placard=False, describe="off",
                qr=True, tone=["whimsical"], googly_chance=0.0, googly_strict=0.5, fill=True,
                fill_tolerance=0.2, max_upscale=1.6, fill_mat=True, exclude=["church"],
                files=None, preview="/tmp/x.jpg", force=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestModernSourcesSkipMuseumBiases(TempState):
    """Bug: with the spice modes at 'Sometimes', ~94% of modern runs searched a bias term
    instead of the curated list: 'grey sky' found a street photo, '1880 Zagreb earthquake'
    found nothing at NASA."""

    def _fetch(self, **kw):
        seen = {}
        def fake_modern(src, count, query, mat_rgb, *a, **k):
            seen["bias"], seen["exclude"] = a[5], k.get("exclude")
            return []
        def boom(*a, **k):
            raise AssertionError("a museum bias was looked up for a modern source")
        with Patch(fp, fetch_modern=fake_modern, holiday_terms=boom, seasonal_terms=boom,
                   weather_terms=boom, on_this_day_terms=boom):
            fp._fetch_source(_run_args(**kw), None, 1)
        return seen

    def test_no_bias_term_reaches_a_modern_source(self):
        for src in fp.MODERN_SOURCES:
            with self.subTest(src=src):
                self.assertIsNone(self._fetch(source=src)["bias"])

    def test_the_typed_subject_still_does(self):
        self.assertEqual(self._fetch(subject=" lighthouse ")["bias"], ["lighthouse"])

    def test_the_exclusions_are_passed_through(self):
        self.assertEqual(self._fetch()["exclude"], ["church"])

    def test_museums_keep_their_biases(self):
        seen = []
        with Patch(fp, fetch_matted=lambda *a, **k: seen.append(a[14]) or [],
                   holiday_terms=lambda: ["christmas"]):
            fp._fetch_source(_run_args(source="met", seasonal_chance=0, weather_chance=0,
                                       on_this_day_chance=0), None, 1)
        self.assertEqual(seen, [True])           # holidays rolled on for the museum


class TestScreenShapedFirst(TempState):
    """Most Commons and NASA hits are portrait or square, so a plain shuffle usually put the
    piece on the blurred backdrop even when a 16:9 one was in the same results."""

    def _cand(self, cid, w, h, title=None):
        return {"id": cid, "img_url": f"https://x/{cid}.jpg", "w": w, "h": h, "page_url": "",
                "real_text": "", "meta": {"title": title or cid, "artist": "A", "date": "", "credit": ""}}

    def _pick(self, cands, **kw):
        fetched = []
        def dl(url, headers=None):
            fetched.append(url)
            c = next(c for c in cands if c["img_url"] == url)
            return Image.new("RGB", (c["w"], c["h"]), (200, 60, 40))
        with Patch(fp, wikimedia_candidates=lambda term, exclude=(): list(cands), download_image=dl):
            paths = fp.fetch_modern("wikimedia", 1, None, None, fill=True, fill_mat=True, **kw)
        return paths, fetched

    def test_fit_rank(self):
        tw, th = fp.CANVAS
        self.assertEqual(fp.fit_rank(3840, 2160, tw, th, 0.2), 0)
        self.assertEqual(fp.fit_rank(4000, 3000, tw, th, 0.2), 2)   # 4:3 loses 25%
        self.assertEqual(fp.fit_rank(None, 2000, tw, th, 0.2), 1)
        self.assertEqual(fp.fit_rank("x", 2000, tw, th, 0.2), 1)

    def test_a_screen_shaped_candidate_beats_portraits_and_squares(self):
        cands = [self._cand(f"wm:p{i}", 4000, 6000) for i in range(10)] + \
                [self._cand(f"wm:s{i}", 5000, 5000) for i in range(10)] + [self._cand("wm:wide", 6000, 3400)]
        for _ in range(5):
            paths, fetched = self._pick(cands)
            self.assertEqual(fetched, ["https://x/wm:wide.jpg"])
            self.assertEqual(fp.LAST_PIECES[0]["id"], "wm:wide")

    def test_the_backdrop_is_still_used_when_nothing_fits(self):
        paths, fetched = self._pick([self._cand("wm:p", 4000, 6000)])
        self.assertEqual(len(paths), 1)
        self.assertEqual(Image.open(paths[0]).size, fp.CANVAS)


class TestExclusions(TempState):
    def test_the_wikimedia_query_negates_each_word(self):
        q = fp.wikimedia_query("coast", ["church", "street art", 'say "x"'])
        self.assertTrue(q.startswith("coast -painting"))
        self.assertIn(" -church", q)
        self.assertIn(' -"street art"', q)
        self.assertIn(" -say x", q.replace('"', ""))
        self.assertTrue(q.endswith(f"incategory:{fp.WM_FEATURED}"))

    def test_the_query_stays_under_the_search_limit(self):
        q = fp.wikimedia_query("coast", [f"word{i}" for i in range(200)])
        self.assertLessEqual(len(q), fp.WM_MAX_QUERY)
        self.assertTrue(q.endswith(f"incategory:{fp.WM_FEATURED}"))

    def test_the_default_list_reaches_the_search(self):
        calls = []
        def fake(url, params=None, timeout=30):
            calls.append(params["gsrsearch"]); return {}
        with Patch(fp, met_json=fake, download_image=lambda *a, **k: None):
            fp.fetch_modern("wikimedia", 1, "lake", None, exclude=fp.DEFAULTS["exclude_terms"])
        for word in ("church", "temple", "mosque", "protest", "homeless", "portrait"):
            self.assertIn(f" -{word}", calls[0])

    def test_a_word_the_user_asks_for_is_not_excluded(self):
        self.assertEqual(fp.active_exclusions(["Cathedral", " ", "protest"], "gothic cathedral"), ["protest"])

    def test_titles_are_matched_on_whole_words_and_plurals(self):
        ex = ["temple", "church"]
        self.assertTrue(fp.is_excluded("Salt Lake Temple at dusk", ex))
        self.assertTrue(fp.is_excluded("Two churches", ex))
        self.assertFalse(fp.is_excluded("Templeton Lake", ex))
        self.assertFalse(fp.is_excluded("", ex))

    def test_an_excluded_title_is_never_downloaded(self):
        fetched = []
        cand = {"id": "nasa:1", "img_url": "u", "w": 6000, "h": 3400, "page_url": "", "real_text": "",
                "meta": {"title": "Protest outside the gate"}}
        with Patch(fp, nasa_candidates=lambda term, exclude=(): [cand],
                   download_image=lambda url, headers=None: fetched.append(url)):
            self.assertEqual(fp.fetch_modern("nasa", 1, None, None, exclude=["protest"]), [])
        self.assertEqual(fetched, [])

    def test_the_panel_sends_the_list_and_an_empty_one_means_none(self):
        f = app.flags_from(dict(fp.DEFAULTS, exclude_terms=["church", "street, art"]))
        self.assertEqual(f[f.index("--exclude") + 1], "church,street  art")
        f = app.flags_from(dict(fp.DEFAULTS, exclude_terms=[]))
        self.assertEqual(f[f.index("--exclude") + 1], "")
        self.assertIn('id="exclude_terms"', app.PAGE)


class TestFavouritesOnly(TempState):
    def _fav(self, fid):
        path = os.path.join(self.tmp, fid + ".jpg")
        Image.new("RGB", fp.CANVAS).save(path)
        return {"id": fid, "title": fid, "artist": "", "url": "", "source": "", "file": path}

    def test_cycles_through_them_never_shown_first_then_oldest(self):
        fp._save_list(fp.FAVOURITES, [self._fav("a"), self._fav("b"), self._fav("c")])
        fp._save_list(fp.HISTORY, [{"id": "b"}, {"id": "x"}, {"id": "a"}])
        self.assertTrue(fp.favourite_pick(cycle=True).endswith("c.jpg"))
        fp._save_list(fp.HISTORY, [{"id": "b"}, {"id": "a"}, {"id": "c"}])
        self.assertTrue(fp.favourite_pick(cycle=True).endswith("b.jpg"))
        self.assertEqual(fp.LAST_PIECES[0]["id"], "b")

    def test_the_source_shows_a_favourite_even_in_preview(self):
        fp._save_list(fp.FAVOURITES, [self._fav("a")])
        with Patch(fp, _fetch_with_retries=lambda *a: self.fail("fetched fresh art")):
            out = fp._gather(_run_args(source="favourites"), None, 1)
        self.assertTrue(out[0].endswith("a.jpg"))

    def test_with_none_saved_it_falls_back_to_the_modern_mix(self):
        seen = []
        with Patch(fp, _fetch_with_retries=lambda args, m, c: seen.append(args.source) or ["p"]):
            self.assertEqual(fp._gather(_run_args(source="favourites"), None, 1), ["p"])
        self.assertEqual(seen, ["modern"])

    def test_the_panel_and_the_pusher_both_offer_it(self):
        self.assertIn("favourites", fp.SOURCE_CHOICES)
        self.assertIn('value="favourites"', app.PAGE)


class TestInstallerPort(unittest.TestCase):
    """Bug: install.sh hard-coded PORT=8080 and rewrote the panel's service with it, so a
    re-run moved a panel on 8137 onto a port a Docker container already had."""

    def _saved_port(self, home):
        import re, subprocess, sys
        with open(os.path.join(HERE, "install.sh")) as f:
            fn = re.search(r"^saved_port\(\) \{.*?^\}$", f.read(), re.S | re.M).group(0)
        r = subprocess.run(["bash", "-c", fn + "\nsaved_port"], capture_output=True, text=True,
                           env={"HOME": home, "PY": sys.executable, "PATH": os.environ["PATH"]}, timeout=30)
        return r.stdout.strip()

    def test_keeps_the_port_from_an_installed_launch_agent(self):
        import plistlib
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "Library/LaunchAgents"))
        with open(os.path.join(home, "Library/LaunchAgents/com.frameart.gui.plist"), "wb") as f:
            plistlib.dump({"Label": "com.frameart.gui",
                           "ProgramArguments": ["/usr/bin/python3", "/x/app.py", "--port", "8137"]}, f)
        self.assertEqual(self._saved_port(home), "8137")

    def test_keeps_the_port_from_an_installed_systemd_unit(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".config/systemd/user"))
        with open(os.path.join(home, ".config/systemd/user/frameart-gui.service"), "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python3 /x/app.py --port 9000\n")
        self.assertEqual(self._saved_port(home), "9000")

    def test_nothing_installed_or_garbage_gives_no_port(self):
        home = tempfile.mkdtemp()
        self.assertEqual(self._saved_port(home), "")
        os.makedirs(os.path.join(home, ".config/systemd/user"))
        with open(os.path.join(home, ".config/systemd/user/frameart-gui.service"), "w") as f:
            f.write("ExecStart=/usr/bin/python3 /x/app.py --port 99999\n")
        self.assertEqual(self._saved_port(home), "")

    def test_the_port_is_no_longer_hard_coded(self):
        with open(os.path.join(HERE, "install.sh")) as f:
            src = f.read()
        self.assertNotRegex(src, r"(?m)^PORT=8080$")
        self.assertIn('if [ -n "${PORT:-}" ]', src)

class TestUnsplashKeyInThePanel(TempState):
    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def test_the_key_is_saved_to_its_own_file_not_the_page(self):
        import json, stat
        with NoServiceControl():
            r = self.client.post("/save", json={"source": "unsplash", "unsplash_key": "SECRETKEY"})
        self.assertEqual(r.status_code, 200)
        path = os.path.join(fp.CFG, "unsplash_key.txt")
        with open(path) as f:
            self.assertEqual(f.read().strip(), "SECRETKEY")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(fp.CONFIG) as f:
            self.assertNotIn("unsplash_key", json.load(f))
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("SECRETKEY", page)
        self.assertIn("saved ✓", page)

    def test_a_blank_field_keeps_the_saved_key(self):
        with open(os.path.join(fp.CFG, "unsplash_key.txt"), "w") as f:
            f.write("OLD\n")
        with NoServiceControl():
            self.client.post("/save", json={"unsplash_key": ""})
        with open(os.path.join(fp.CFG, "unsplash_key.txt")) as f:
            self.assertEqual(f.read().strip(), "OLD")

    def test_every_source_in_the_dropdown_is_one_the_pusher_accepts(self):
        import re
        offered = set(re.findall(r'<option value="([a-z]+)"', app.PAGE[app.PAGE.index('id="source"'):
                                                                       app.PAGE.index('id="unsplashrow"')]))
        self.assertEqual(offered, set(fp.SOURCE_CHOICES))


# ------------------------------------------------------------------------------ state
class TestStatusRoundTrip(TempState):
    """Bug: the pinned early return overwrote status.json with nothing, taking the
    panel's whole record of what's on the TV with it."""

    PIECE = {"id": "met:435809", "title": "The Harvesters", "artist": "Bruegel",
             "url": "https://metmuseum.org/1", "source": "The Met",
             "caption_style": "pirate", "caption": "Arr.", "date": "1565"}

    def test_write_then_read(self):
        fp.write_status(True, "Displayed The Harvesters", {"content_id": "x1", **self.PIECE})
        got = fp.read_status()
        self.assertTrue(got["ok"])
        for k, v in self.PIECE.items():
            self.assertEqual(got[k], v)

    def test_read_status_of_a_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(fp.read_status(), {})
        with open(fp.STATUS, "w") as f:
            f.write("{ not json")
        self.assertEqual(fp.read_status(), {})

    def test_the_pinned_carry_forward_preserves_the_piece(self):
        fp.write_status(True, "Displayed The Harvesters", dict(self.PIECE))
        keep = {k: v for k, v in fp.read_status().items()
                if k not in ("ok", "when", "message")}
        fp.write_status(True, "Kept — art left unchanged", keep)
        got = fp.read_status()
        self.assertEqual(got["message"], "Kept — art left unchanged")
        for k, v in self.PIECE.items():
            self.assertEqual(got[k], v, f"pinning lost {k!r}")


class TestWatcherPid(TempState):
    """Bug: a stale pid file whose pid had been recycled wedged every later run into
    "a watcher is already waiting"."""

    def setUp(self):
        super().setUp()
        fp.WATCH_PID = os.path.join(fp.CFG, "watcher.pid")

    def test_no_pid_file_means_no_watcher(self):
        self.assertFalse(fp._watcher_running())

    def test_a_dead_pid_means_no_watcher(self):
        with open(fp.WATCH_PID, "w") as f:
            f.write("999999")
        self.assertFalse(fp._watcher_running())

    def test_a_live_but_unrelated_pid_means_no_watcher(self):
        with open(fp.WATCH_PID, "w") as f:                # the test runner, not frame_push
            f.write(str(os.getpid()))
        self.assertFalse(fp._watcher_running())

    def test_garbage_in_the_pid_file_is_survivable(self):
        with open(fp.WATCH_PID, "w") as f:
            f.write("not-a-pid")
        self.assertFalse(fp._watcher_running())


# ----------------------------------------------------------------------- date & bias
class TestSeasonAndWeather(unittest.TestCase):
    def test_northern_seasons(self):
        import datetime as dt
        for month, season in ((1, "winter"), (2, "winter"), (3, "spring"), (5, "spring"),
                              (6, "summer"), (8, "summer"), (9, "autumn"), (11, "autumn"),
                              (12, "winter")):
            with self.subTest(month=month):
                self._assert_season(month, "north", season)

    def test_southern_seasons_are_six_months_offset(self):
        for month, season in ((1, "summer"), (4, "autumn"), (7, "winter"), (10, "spring")):
            with self.subTest(month=month):
                self._assert_season(month, "south", season)

    def _assert_season(self, month, hemisphere, want):
        import datetime as dt
        real = fp.datetime.date

        class FakeDate(dt.date):
            @classmethod
            def today(cls):
                return real(2026, month, 15)
        fp.datetime.date = FakeDate
        try:
            self.assertEqual(fp.seasonal_terms(hemisphere), fp.SEASON_TERMS[want])
        finally:
            fp.datetime.date = real

    def test_holiday_ranges_including_the_new_year_wrap(self):
        import datetime as dt
        real = fp.datetime.date
        cases = [((10, 28), "halloween"), ((12, 25), "christmas"), ((2, 14), "valentine"),
                 ((12, 31), "new year"), ((1, 1), "new year")]
        for (month, day), lead in cases:
            class FakeDate(dt.date):
                @classmethod
                def today(cls, _m=month, _d=day):
                    return real(2026, _m, _d)
            fp.datetime.date = FakeDate
            try:
                with self.subTest(date=(month, day)):
                    self.assertEqual((fp.holiday_terms() or [None])[0], lead)
            finally:
                fp.datetime.date = real

    def test_an_ordinary_date_has_no_holiday(self):
        import datetime as dt
        real = fp.datetime.date

        class FakeDate(dt.date):
            @classmethod
            def today(cls):
                return real(2026, 6, 15)
        fp.datetime.date = FakeDate
        try:
            self.assertIsNone(fp.holiday_terms())
        finally:
            fp.datetime.date = real

    def test_wmo_buckets(self):
        for code, bucket in ((0, "clear"), (2, "cloud"), (45, "fog"), (61, "rain"),
                             (75, "snow"), (86, "snow"), (95, "storm"), (999, "clear")):
            with self.subTest(code=code):
                self.assertEqual(fp._wmo_bucket(code), bucket)
        self.assertEqual(fp._wmo_bucket(80), "rain")     # showers, not snow


class TestBiasTerms(unittest.TestCase):
    def test_a_subject_alone_is_the_only_term(self):
        self.assertEqual(fp.bias_terms(subject="cats"), ["cats"])

    def test_nothing_on_means_no_bias(self):
        self.assertIsNone(fp.bias_terms())

    def test_a_subject_combines_with_an_active_bias(self):
        real = fp.holiday_terms
        fp.holiday_terms = lambda: ["christmas", "nativity"]
        try:
            self.assertEqual(fp.bias_terms(subject="cats", holidays=True),
                             ["cats christmas", "cats nativity"])
        finally:
            fp.holiday_terms = real

    def test_the_most_specific_bias_wins(self):
        real = (fp.holiday_terms, fp.seasonal_terms)
        fp.holiday_terms = lambda: ["christmas"]
        fp.seasonal_terms = lambda hemisphere="north": ["snow"]
        try:
            self.assertEqual(fp.bias_terms(holidays=True, seasonal=True), ["christmas"])
        finally:
            fp.holiday_terms, fp.seasonal_terms = real


class TestWeightedTone(unittest.TestCase):
    def test_a_zero_weight_voice_never_comes_up(self):
        fp._TONE_WEIGHTS = {"pirate": 0, "noir": 1}
        try:
            picks = {fp._weighted_tone(["pirate", "noir"]) for _ in range(200)}
            self.assertEqual(picks, {"noir"})
        finally:
            fp._TONE_WEIGHTS = None

    def test_all_zero_weights_fall_back_to_a_uniform_pick(self):
        fp._TONE_WEIGHTS = {"pirate": 0, "noir": 0}
        try:
            self.assertIn(fp._weighted_tone(["pirate", "noir"]), ("pirate", "noir"))
        finally:
            fp._TONE_WEIGHTS = None

    def test_an_empty_voice_list_still_returns_a_voice(self):
        self.assertEqual(fp._weighted_tone([]), "whimsical")

    def test_every_configurable_voice_has_an_instruction(self):
        for tone in fp.TONES:
            self.assertTrue(fp.TONES[tone].strip(), tone)


class TestLogPath(unittest.TestCase):
    """Bug: LOG was hardcoded to ~/Library/Logs, which exists only on macOS. Both
    schedules REDIRECT into it, and a redirect into a missing directory fails before the
    job starts — so on Linux the scheduled art change never ran at all."""

    def test_the_log_path_suits_the_platform(self):
        import platform
        if platform.system() == "Darwin":
            self.assertIn("Library/Logs", app.LOG)
        else:
            self.assertNotIn("Library/Logs", app.LOG,
                             "a macOS-only log directory on a non-macOS platform")

    def test_the_log_directory_is_created(self):
        self.assertTrue(app._log_dir_ready())
        self.assertTrue(os.path.isdir(os.path.dirname(app.LOG)))

    def test_a_redirect_into_the_log_path_succeeds(self):
        """Exactly what the cron line does, and what used to fail with exit 2."""
        import subprocess
        app._log_dir_ready()
        r = subprocess.run(["sh", "-c", f"echo probe >> {app.LOG}"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_scheduled_command_is_runnable_as_written(self):
        """Build the cron line the way write_schedule does and run its command, to catch a
        redirect or quoting problem that would stop the job before Python starts."""
        import subprocess
        app._log_dir_ready()
        cmd = f"{app.PYTHON} {app.SCRIPT} --help >> {app.LOG} 2>&1"
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, f"the scheduled command could not run: {r.stderr}")


class FakeArt:
    """A stand-in Frame. `available()` returns what the TV holds; `refuse` ids fail to
    delete, and `batch_raises` makes delete_list blow up so the per-item fallback runs."""

    def __init__(self, content_ids=(), refuse=(), batch_raises=False, echo_reordered=False):
        self.items = [{"content_id": c, "category_id": "MY-C0002" if c.upper().startswith("MY")
                       else "MY-C0004"} for c in content_ids]
        self.refuse, self.batch_raises, self.echo_reordered = set(refuse), batch_raises, echo_reordered
        self.batches, self.singles = [], []

    def available(self, category=None):
        return list(self.items)

    def _drop(self, cid):
        if cid in self.refuse:
            raise RuntimeError("cannot delete")
        self.items = [i for i in self.items if i["content_id"] != cid]

    def delete_list(self, content_ids):
        content_ids = list(content_ids)
        self.batches.append(content_ids)
        if self.batch_raises:
            raise RuntimeError("delete_image_list failed")
        for cid in content_ids:
            try:
                self._drop(cid)
            except RuntimeError:
                pass
        return False if self.echo_reordered else True

    def delete(self, content_id):
        self.singles.append(content_id)
        self._drop(content_id)
        return True


class TestUserUploadIds(unittest.TestCase):
    """Only art uploaded to the TV may be wiped. Samsung's own bundled pieces start SAM-
    and cannot be re-downloaded, so they must never be selected."""

    def test_samsung_art_is_excluded(self):
        art = FakeArt(["MY_F0001", "SAM-S0011", "MY-F0002", "SAM-F0099"])
        self.assertEqual(fp.user_upload_ids(art), ["MY_F0001", "MY-F0002"])

    def test_duplicates_collapse_and_blanks_are_ignored(self):
        art = FakeArt(["MY_F0001", "MY_F0001"])
        art.items.append({"category_id": "MY-C0002"})          # no content_id at all
        art.items.append(None)                                  # and a junk row
        self.assertEqual(fp.user_upload_ids(art), ["MY_F0001"])

    def test_an_empty_tv_yields_nothing(self):
        self.assertEqual(fp.user_upload_ids(FakeArt([])), [])

    def test_an_unreadable_list_is_an_error_not_a_silent_empty(self):
        """Returning [] here would make a wipe report success having deleted nothing."""
        class Broken:
            def available(self, category=None):
                raise RuntimeError("websocket closed")
        with self.assertRaises(RuntimeError):
            fp.user_upload_ids(Broken())


class TestWipeUploads(unittest.TestCase):
    def test_everything_uploaded_goes_and_samsung_art_stays(self):
        art = FakeArt(["MY_F0001", "MY_F0002", "SAM-S0011"])
        attempted, deleted = fp.wipe_uploads(art)
        self.assertEqual(sorted(attempted), ["MY_F0001", "MY_F0002"])
        self.assertEqual(sorted(deleted), ["MY_F0001", "MY_F0002"])
        self.assertEqual([i["content_id"] for i in art.items], ["SAM-S0011"])

    def test_success_is_measured_from_the_tv_not_from_delete_lists_return(self):
        """delete_list compares the echoed payload and reports False on a reordered reply,
        so trusting it would report a successful wipe as a total failure."""
        art = FakeArt(["MY_F0001", "MY_F0002"], echo_reordered=True)
        attempted, deleted = fp.wipe_uploads(art)
        self.assertEqual(sorted(deleted), ["MY_F0001", "MY_F0002"])

    def test_a_failed_batch_falls_back_to_deleting_one_at_a_time(self):
        art = FakeArt(["MY_F0001", "MY_F0002"], batch_raises=True)
        attempted, deleted = fp.wipe_uploads(art)
        self.assertEqual(art.singles, ["MY_F0001", "MY_F0002"])
        self.assertEqual(sorted(deleted), ["MY_F0001", "MY_F0002"])

    def test_one_undeletable_image_does_not_strand_the_rest(self):
        art = FakeArt(["MY_F0001", "MY_F0002", "MY_F0003"],
                      refuse=["MY_F0002"], batch_raises=True)
        attempted, deleted = fp.wipe_uploads(art)
        self.assertEqual(sorted(attempted), ["MY_F0001", "MY_F0002", "MY_F0003"])
        self.assertEqual(sorted(deleted), ["MY_F0001", "MY_F0003"])
        self.assertIn("MY_F0002", [i["content_id"] for i in art.items])


class TestPruneUploads(unittest.TestCase):
    """A replace push prunes against the TV's own list, not the one id in uploaded.json:
    trusting that file let 450+ strays pile up in the Frame's rotation (2026-10)."""

    def test_every_older_upload_goes_except_the_new_one_and_samsung_art(self):
        art = FakeArt(["MY_F0001", "MY_F0002", "MY_F0003", "SAM-S0011"])
        fp.prune_uploads(art, keep=["MY_F0003"])
        self.assertEqual(sorted(i["content_id"] for i in art.items), ["MY_F0003", "SAM-S0011"])

    def test_one_undeletable_stray_does_not_stop_the_rest(self):
        art = FakeArt(["MY_F0001", "MY_F0002", "MY_F0009"], refuse=["MY_F0001"], batch_raises=True)
        fp.prune_uploads(art, keep=["MY_F0009"])
        self.assertEqual(sorted(i["content_id"] for i in art.items), ["MY_F0001", "MY_F0009"])

    def test_an_unreadable_list_skips_the_prune_instead_of_failing_the_push(self):
        class Broken:
            def available(self, category=None):
                raise RuntimeError("websocket closed")
        fp.prune_uploads(Broken(), keep=["MY_F0009"])           # must not raise

    def test_recent_keeps_the_newest_few_for_the_tvs_own_slideshow(self):
        """Numbered, not listed, order decides 'newest': MY_F0100 is newer than MY_F0099."""
        art = FakeArt(["MY_F0100", "MY_F0007", "MY_F0099", "MY_F0098", "MY_F0097", "MY_F0096",
                       "SAM-S0011", "MY_F0101"])
        fp.prune_uploads(art, keep=["MY_F0101"], recent=5)
        self.assertEqual(sorted(i["content_id"] for i in art.items),
                         ["MY_F0097", "MY_F0098", "MY_F0099", "MY_F0100", "MY_F0101", "SAM-S0011"])

    def test_recent_never_drops_the_piece_just_uploaded(self):
        art = FakeArt(["MY_F0500", "MY_F0003"])                 # an oddly low new id
        fp.prune_uploads(art, keep=["MY_F0003"], recent=1)
        self.assertEqual([i["content_id"] for i in art.items], ["MY_F0003"])


class FakeSlideshow:
    def __init__(self, value="off", category_id=""):
        self.status = {"value": value, "category_id": category_id}
        self.sets = []

    def get_slideshow_status(self):
        return dict(self.status)

    def set_slideshow_status(self, duration=0, type=True, category=2):
        self.sets.append(duration)
        self.status = {"value": str(duration) if duration else "off", "category_id": f"MY-C000{category}"}


class TestEnsureSlideshow(unittest.TestCase):
    def test_turns_it_on_over_my_photos(self):
        art = FakeSlideshow()
        self.assertTrue(fp.ensure_slideshow(art, 60))
        self.assertEqual(art.status, {"value": "60", "category_id": "MY-C0002"})

    def test_leaves_it_alone_when_already_right_so_the_timer_isnt_reset(self):
        art = FakeSlideshow("60", "MY-C0002")
        fp.ensure_slideshow(art, 60)
        self.assertEqual(art.sets, [])

    def test_zero_turns_it_off(self):
        art = FakeSlideshow("60", "MY-C0002")
        fp.ensure_slideshow(art, 0)
        self.assertEqual(art.status["value"], "off")

    def test_a_tv_that_refuses_does_not_fail_the_push(self):
        class Refuses(FakeSlideshow):
            def set_slideshow_status(self, **kw):
                raise RuntimeError("timed out")
        self.assertFalse(fp.ensure_slideshow(Refuses(), 60))

    def test_large_collections_are_deleted_in_batches(self):
        art = FakeArt([f"MY_F{n:04d}" for n in range(45)])
        fp.wipe_uploads(art, batch=20)
        self.assertEqual([len(b) for b in art.batches], [20, 20, 5])

    def test_an_empty_tv_deletes_nothing(self):
        art = FakeArt([])
        self.assertEqual(fp.wipe_uploads(art), ([], []))
        self.assertEqual(art.batches, [])


class TestDoWipe(TempState):
    """The --wipe entry point: what it prints, and what local state it may touch."""

    def setUp(self):
        super().setUp()
        self.art = FakeArt(["MY_F0001", "MY_F0002", "SAM-S0011"])
        real_locate, real_open = fp.locate_frame, fp.open_art
        fp.locate_frame = lambda a: "192.168.4.64"
        fp.open_art = lambda a, ip: self.art
        self.addCleanup(lambda: (setattr(fp, "locate_frame", real_locate),
                                 setattr(fp, "open_art", real_open)))
        self.args = argparse.Namespace(mac="a0:d0:5b:a0:e9:89", ip=None, token_file=None,
                                       timeout=30, no_wake=True, wake_wait=1, retries=1,
                                       dry_run=False, wipe=True)

    def _run(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fp.do_wipe(self.args)
        return buf.getvalue()

    def test_dry_run_counts_and_deletes_nothing(self):
        self.args.dry_run = True
        out = self._run()
        self.assertIn("uploads: 2", out)
        self.assertEqual(len(self.art.items), 3, "a dry run deleted something")

    def test_the_count_line_is_machine_readable_for_the_panel(self):
        """The panel parses this exact line to name the number before deleting."""
        self.args.dry_run = True
        self.assertRegex(self._run(), r"(?m)^uploads:\s*2$")

    def test_a_real_wipe_deletes_and_reports(self):
        out = self._run()
        self.assertIn("Wiped 2 image(s)", out)
        self.assertEqual([i["content_id"] for i in self.art.items], ["SAM-S0011"])

    def test_stale_local_state_is_cleared_but_history_and_favourites_survive(self):
        """History images and favourites are local files — browsing back re-uploads one, so
        they keep working after a wipe and must not be destroyed with it."""
        os.makedirs(fp.HIST_IMG_DIR, exist_ok=True)
        os.makedirs(fp.FAVS_DIR, exist_ok=True)
        with open(fp.STATE, "w") as f:
            f.write('["MY_F0001"]')
        with open(fp.CURRENT_IMG, "wb") as f:
            f.write(b"x")
        fp._save_list(fp.HISTORY, [{"id": "met:1", "title": "X", "file": "/tmp/x.jpg"}])
        fp._save_list(fp.FAVOURITES, [{"id": "met:1", "file": "/tmp/x.jpg"}])
        self._run()
        self.assertFalse(os.path.exists(fp.STATE), "the stale uploaded-id list was kept")
        self.assertFalse(os.path.exists(fp.CURRENT_IMG), "a thumbnail of nothing was kept")
        self.assertEqual(len(fp._load_list(fp.HISTORY)), 1, "history was destroyed")
        self.assertEqual(len(fp._load_list(fp.FAVOURITES)), 1, "favourites were destroyed")

    def test_the_outcome_is_recorded_for_the_panel(self):
        self._run()
        st = fp.read_status()
        self.assertTrue(st["ok"])
        self.assertIn("Wiped 2", st["message"])

    def test_an_unreachable_tv_fails_instead_of_handing_off_to_a_watcher(self):
        """A wipe is a deliberate, immediate action — it must never be queued for later."""
        fp.locate_frame = lambda a: None
        with self.assertRaises(RuntimeError) as e:
            self._run()
        self.assertIn("not found on the network", str(e.exception))

    def test_nothing_to_wipe_is_not_an_error(self):
        self.art = FakeArt(["SAM-S0011"])
        out = self._run()
        self.assertIn("uploads: 0", out)
        self.assertIn("Nothing to wipe", out)


# ------------------------------------------------------------------------- the panel
SERVICE_COMMANDS = ("crontab", "launchctl")


class NoServiceControl:
    """Intercept every subprocess call app.py makes, so a test can never reach the real
    `crontab` or `launchctl`. write_schedule() controls a live service, and HOME
    redirection alone does NOT protect it: launchctl acts on the job's LABEL, so a
    bootout/bootstrap fires against the real loaded job however the plist path is
    redirected. A test run on a real install used to swap the machine's own schedule.

    `handler(cmd)` returns a CompletedProcess or raises (e.g. FileNotFoundError).
    """

    class Done:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __init__(self, handler=None):
        self.handler, self.calls = handler, []

    def __enter__(self):
        self._real = app.subprocess.run

        def fake(cmd, *a, **k):
            self.calls.append(cmd)
            if self.handler:
                return self.handler(cmd)
            return self.Done()
        app.subprocess.run = fake
        return self

    def __exit__(self, *exc):
        app.subprocess.run = self._real
        return False

    def service_calls(self):
        return [c for c in self.calls
                if isinstance(c, (list, tuple)) and c and c[0] in SERVICE_COMMANDS]


class TestPanelRoutes(TempState):
    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def _forcing_platform(self, name):
        """Pin platform.system() so each write_schedule branch is testable on any host."""
        real = app.platform.system
        app.platform.system = lambda: name
        self.addCleanup(lambda: setattr(app.platform, "system", real))

    def test_save_reports_a_message_when_there_is_no_crontab(self):
        """Bug: the unguarded `crontab -` call made /save a 500 on any Linux host
        without cron — after the settings had already been written."""
        self._forcing_platform("Linux")

        def no_crontab(cmd, *a, **k):
            if cmd and cmd[0] == "crontab":
                raise FileNotFoundError("crontab")
            return NoServiceControl.Done()
        with NoServiceControl(no_crontab):
            r = self.client.post("/save", json={"content": "museum", "mat": "charcoal",
                                                "description": "off", "every": 2,
                                                "every_unit": "days", "time": "07:30"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("crontab", r.get_json()["message"])

    def test_save_on_linux_writes_the_interval_it_reports(self):
        self._forcing_platform("Linux")
        with NoServiceControl() as sp:
            r = self.client.post("/save", json={"content": "museum", "mat": "charcoal",
                                                "description": "off", "every": 2,
                                                "every_unit": "days", "time": "07:30"})
        self.assertEqual(r.status_code, 200)
        written = [c for c in sp.calls if c and c[0] == "crontab" and c[-1] == "-"]
        self.assertTrue(written, "no crontab was installed")
        self.assertIn("every 2 days", r.get_json()["message"])

    def test_save_on_macos_writes_a_plist_without_touching_a_real_service(self):
        """The Darwin branch shells out to launchctl. It must be reachable in tests
        without bootout/bootstrap ever hitting the machine's own job."""
        self._forcing_platform("Darwin")
        with NoServiceControl() as sp:
            r = self.client.post("/save", json={"content": "museum", "mat": "charcoal",
                                                "description": "off", "every": 30,
                                                "every_unit": "minutes", "time": "07:30"})
        self.assertEqual(r.status_code, 200)
        verbs = [c[1] for c in sp.calls if c and c[0] == "launchctl" and len(c) > 1]
        self.assertEqual(verbs, ["bootout", "bootstrap"])
        self.assertTrue(app.PLIST.startswith(_HOME),
                        f"the plist path escaped the test HOME: {app.PLIST}")
        with open(app.PLIST) as f:
            plist = f.read()
        self.assertIn("<key>StartInterval</key><integer>1800</integer>", plist)

    def test_no_service_control_command_escapes_to_the_real_subprocess(self):
        """The guard itself: /save must not reach a real crontab or launchctl. Getting
        this wrong once already swapped a live 30-minute schedule for a 2-day one."""
        for platform_name in ("Linux", "Darwin"):
            with self.subTest(platform=platform_name):
                self._forcing_platform(platform_name)
                with NoServiceControl() as sp:
                    self.client.post("/save", json={"content": "museum", "mat": "charcoal",
                                                    "description": "off", "every": 30,
                                                    "every_unit": "minutes", "time": "07:30"})
                self.assertTrue(sp.service_calls(),
                                "the branch under test never tried a service command")

    def test_ban_refuses_rather_than_replacing_without_banning(self):
        """Bug: with no recorded id it banned nothing but still changed the art."""
        fp.write_status(True, "Kept — art left unchanged", {})
        called = []
        real = app.run_push
        app.run_push = lambda *a, **k: called.append(a) or (None, "should not run")
        try:
            r = self.client.post("/ban")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()["ok"])
            self.assertEqual(called, [], "the art was replaced despite banning nothing")
            self.assertEqual(fp._load_list(fp.BLOCKLIST), [])
        finally:
            app.run_push = real

    def test_ban_records_the_piece_and_asks_for_a_replacement(self):
        fp.write_status(True, "Displayed X", {"id": "met:1", "title": "X"})
        real = app.run_push

        class Done:
            returncode, stdout, stderr = 0, "", ""
        app.run_push = lambda *a, **k: (Done(), None)
        try:
            r = self.client.post("/ban")
            self.assertTrue(r.get_json()["ok"])
            self.assertEqual(fp._load_list(fp.BLOCKLIST), ["met:1"])
        finally:
            app.run_push = real

    def test_a_timeout_becomes_a_message_not_an_exception(self):
        """Bug: TimeoutExpired escaped the route as a 500, and the page's fetch() only
        parses JSON — so a slow museum API surfaced as "Error: SyntaxError"."""
        real = app.subprocess.run
        app.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            app.subprocess.TimeoutExpired("cmd", 1))
        try:
            proc, err = app.run_push([], 1, "The preview")
            self.assertIsNone(proc)
            self.assertIn("took longer than 1s", err)
        finally:
            app.subprocess.run = real

    def test_run_push_returns_the_process_on_success(self):
        proc, err = app.run_push(["--help"], 60, "Help")
        self.assertIsNone(err)
        self.assertEqual(proc.returncode, 0)

    def test_preview_jpg_404s_when_nothing_has_been_rendered(self):
        real = app.PREVIEW_PATH
        app.PREVIEW_PATH = os.path.join(self.tmp, "nope.jpg")
        try:
            self.assertEqual(self.client.get("/preview.jpg").status_code, 404)
        finally:
            app.PREVIEW_PATH = real

    def test_navigation_refuses_before_there_is_any_history(self):
        r = self.client.post("/back")
        self.assertFalse(r.get_json()["ok"])

    def test_the_page_renders_and_never_leaks_the_password(self):
        app.save_config({"password": "hunter2"})
        try:
            authed = app.app.test_client()
            authed.post("/login", data={"password": "hunter2"})
            body = authed.get("/").get_data(as_text=True)
            self.assertEqual(authed.get("/").status_code, 200)
            self.assertNotIn("hunter2", body)
        finally:
            app.save_config({"password": ""})

    def test_an_unauthenticated_post_gets_json_not_an_empty_body(self):
        """Bug: the bare 401 body made the page report "Error: SyntaxError"."""
        app.save_config({"password": "hunter2"})
        try:
            r = app.app.test_client().post("/pin")
            self.assertEqual(r.status_code, 401)
            self.assertIsNotNone(r.get_json().get("message"))
        finally:
            app.save_config({"password": ""})

    def test_wipe_without_confirm_only_counts(self):
        """The first tap must never delete: it exists so the page can name the number."""
        app.save_config({"mac": "a0:d0:5b:a0:e9:89"})
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "uploads: 7\n", ""
        real = app.run_push
        app.run_push = lambda extra, t, w: (calls.append(extra), (Done(), None))[1]
        try:
            j = self.client.post("/wipe", json={}).get_json()
        finally:
            app.run_push = real
        self.assertTrue(j["ok"])
        self.assertEqual(j["count"], 7)
        self.assertTrue(j["armed"])
        self.assertIn("--dry-run", calls[0], "the counting call was not a dry run")

    def test_wipe_with_confirm_deletes(self):
        app.save_config({"mac": "a0:d0:5b:a0:e9:89"})
        calls = []

        class Done:
            returncode = 0
            stdout = "uploads: 7\ndeleted: 7 of 7\nWiped 7 image(s) from the TV.\n"
            stderr = ""
        real = app.run_push
        app.run_push = lambda extra, t, w: (calls.append(extra), (Done(), None))[1]
        try:
            j = self.client.post("/wipe", json={"confirm": True}).get_json()
        finally:
            app.run_push = real
        self.assertTrue(j["ok"])
        self.assertNotIn("--dry-run", calls[0])
        self.assertIn("Wiped 7", j["message"])

    def test_wipe_passes_only_the_mac_not_the_art_settings(self):
        """Sending flags_from() here would let an unrelated setting steer a destructive call."""
        app.save_config({"mac": "a0:d0:5b:a0:e9:89"})
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "uploads: 0\n", ""
        real = app.run_push
        app.run_push = lambda extra, t, w: (calls.append(extra), (Done(), None))[1]
        try:
            self.client.post("/wipe", json={"confirm": True})
        finally:
            app.run_push = real
        self.assertEqual(calls[0], ["--wipe", "--mac", "a0:d0:5b:a0:e9:89"])

    def test_wipe_refuses_without_a_mac(self):
        app.save_config({"mac": ""})
        ran = []
        real = app.run_push
        app.run_push = lambda *a, **k: ran.append(a) or (None, "should not run")
        try:
            j = self.client.post("/wipe", json={"confirm": True}).get_json()
        finally:
            app.run_push = real
        self.assertFalse(j["ok"])
        self.assertEqual(ran, [], "it tried to wipe with no TV configured")

    def test_wipe_reports_a_failed_run_rather_than_claiming_success(self):
        app.save_config({"mac": "a0:d0:5b:a0:e9:89"})

        class Failed:
            returncode, stdout, stderr = 1, "", "FAILED: Frame not found on the network."
        real = app.run_push
        app.run_push = lambda *a, **k: (Failed(), None)
        try:
            j = self.client.post("/wipe", json={"confirm": True}).get_json()
        finally:
            app.run_push = real
        self.assertFalse(j["ok"])
        self.assertIn("not found", j["message"])

    def test_wipe_surfaces_a_timeout_as_a_message(self):
        app.save_config({"mac": "a0:d0:5b:a0:e9:89"})
        real = app.run_push
        app.run_push = lambda *a, **k: (None, "Wiping the TV took longer than 300s and was stopped.")
        try:
            j = self.client.post("/wipe", json={"confirm": True}).get_json()
        finally:
            app.run_push = real
        self.assertFalse(j["ok"])
        self.assertIn("took longer", j["message"])

    def test_the_page_carries_a_two_step_wipe_button(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="wipe"', page)
        self.assertIn("Tap again to confirm", page)
        self.assertIn("body:JSON.stringify({confirm})", page)

    def test_history_entries_are_escaped_into_the_page(self):
        """Bug: museum-supplied title and url went into innerHTML raw."""
        page = self.client.get("/").get_data(as_text=True)
        block = page[page.index("el.historylist.innerHTML"):][:500]
        self.assertIn("esc(h.title", block)
        self.assertIn("esc(h.url)", block)
        self.assertIn("&quot;", page)         # esc() covers attribute quotes


class TestFlagsFrom(unittest.TestCase):
    def test_every_flag_the_panel_emits_is_one_the_pusher_accepts(self):
        """The easy mistake when adding a setting: a flag in flags_from() that argparse
        doesn't know, which breaks preview and change-now at runtime."""
        flags = app.flags_from(dict(fp.DEFAULTS, mac="a0:d0:5b:01:23:56"))
        with open(os.path.join(HERE, "frame_push.py")) as f:
            parser_text = f.read()
        for token in (f for f in flags if f.startswith("--")):
            base = token[5:] if token.startswith("--no-") else token[2:]
            with self.subTest(flag=token):
                self.assertTrue(f'"--{base}"' in parser_text or f"'--{base}'" in parser_text,
                                f"{token} is not declared in frame_push.py's argparse")

    def test_the_defaults_round_trip_through_the_pusher_unchanged(self):
        import subprocess, sys
        flags = app.flags_from(dict(fp.DEFAULTS, mac="a0:d0:5b:01:23:56"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "frame_push.py"), "--help"] + flags,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])


class TestShippedExamples(unittest.TestCase):
    """Bug: both shipped scheduling examples passed --describe with no value."""

    def _run(self, args):
        import subprocess, sys
        return subprocess.run([sys.executable, os.path.join(HERE, "frame_push.py"), "--help"] + args,
                              capture_output=True, text=True, timeout=60)

    def test_the_launchd_template_arguments_parse(self):
        import re
        with open(os.path.join(HERE, "com.example.frameart.plist")) as f:
            src = f.read()
        array = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", src, re.S).group(1)
        args = [a.replace("__FRAME_MAC__", "a0:d0:5b:01:23:56")
                for a in re.findall(r"<string>(.*?)</string>", array)][2:]
        r = self._run(args)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_the_readme_cron_example_arguments_parse(self):
        import re
        with open(os.path.join(HERE, "README.md")) as f:
            line = next(l for l in f if "frame_push.py --fetch" in l)
        args = line[line.index("--fetch"):].rstrip("`\n ").split()
        r = self._run(args + ["--mac", "a0:d0:5b:01:23:56"])
        self.assertEqual(r.returncode, 0, r.stderr[-400:])


class TestBanRemovesFromTv(TempState):
    """A ban used to only blocklist the id: the upload stayed among the newest pieces the
    TV's own slideshow cycles, so a banned church kept coming back (2026-11-09)."""

    def _history(self, *entries):
        os.makedirs(fp.HIST_IMG_DIR, exist_ok=True)
        hist = []
        for pid, cid in entries:
            f = os.path.join(fp.HIST_IMG_DIR, f"{cid}.jpg")
            open(f, "wb").close()
            hist.append({"id": pid, "content_id": cid, "title": pid, "file": f})
        fp._save_list(fp.HISTORY, hist)

    def test_ban_queues_the_upload_and_drops_history_and_favourite(self):
        self._history(("nasa:1", "MY_F0001"), ("wm:2", "MY_F0002"), ("nasa:3", "MY_F0003"))
        os.makedirs(fp.FAVS_DIR, exist_ok=True)
        fav = os.path.join(fp.FAVS_DIR, "wm_2.jpg")
        open(fav, "wb").close()
        fp._save_list(fp.FAVOURITES, [{"id": "wm:2", "file": fav}])
        queued = fp.ban_piece("wm:2", ["MY_F0002"])
        self.assertEqual(queued, ["MY_F0002"])
        self.assertEqual(fp._load_list(fp.BLOCKLIST), ["wm:2"])
        self.assertEqual(fp._load_list(fp.PENDING_DELETE), ["MY_F0002"])
        self.assertEqual([h["id"] for h in fp.history_navlist()], ["nasa:1", "nasa:3"])
        self.assertEqual(fp.nav_get(), 1)
        self.assertEqual(fp._load_list(fp.FAVOURITES), [])
        self.assertFalse(os.path.exists(fav))

    def test_purge_deletes_the_banned_upload_and_clears_the_queue(self):
        fp._save_list(fp.PENDING_DELETE, ["MY_F0002"])
        art = FakeArt(["MY_F0001", "MY_F0002", "MY_F0003", "SAM-S0011"])
        self.assertEqual(fp.purge_banned(art), ["MY_F0002"])
        self.assertEqual(sorted(i["content_id"] for i in art.items), ["MY_F0001", "MY_F0003", "SAM-S0011"])
        self.assertEqual(fp._load_list(fp.PENDING_DELETE), [])

    def test_a_ban_from_before_the_queue_is_found_through_history(self):
        """The church was banned by id only; its upload is still found and removed."""
        self._history(("wm:160021607", "MY_F2752"), ("nasa:PIA14873", "MY_F2753"))
        fp._save_list(fp.BLOCKLIST, ["wm:160021607"])
        art = FakeArt(["MY_F2751", "MY_F2752", "MY_F2753"])
        fp.purge_banned(art)
        self.assertEqual(sorted(i["content_id"] for i in art.items), ["MY_F2751", "MY_F2753"])

    def test_an_upload_that_wont_delete_stays_queued(self):
        fp._save_list(fp.PENDING_DELETE, ["MY_F0002"])
        art = FakeArt(["MY_F0002"], refuse=["MY_F0002"], batch_raises=True)
        self.assertEqual(fp.purge_banned(art), [])
        self.assertEqual(fp._load_list(fp.PENDING_DELETE), ["MY_F0002"])

    def test_an_unreachable_tv_keeps_the_queue_and_does_not_raise(self):
        class Asleep:
            def available(self, category=None):
                raise TimeoutError("[Errno 60] Operation timed out")
        fp._save_list(fp.PENDING_DELETE, ["MY_F0002"])
        self.assertEqual(fp.purge_banned(Asleep()), [])
        self.assertEqual(fp._load_list(fp.PENDING_DELETE), ["MY_F0002"])

    def test_prune_never_spares_a_banned_upload_for_the_slideshow(self):
        fp._save_list(fp.PENDING_DELETE, ["MY_F0004"])
        art = FakeArt(["MY_F0001", "MY_F0002", "MY_F0003", "MY_F0004", "MY_F0005"])
        fp.prune_uploads(art, keep=["MY_F0005"], recent=3)
        self.assertEqual(sorted(i["content_id"] for i in art.items), ["MY_F0002", "MY_F0003", "MY_F0005"])

    def test_panel_ban_queues_the_upload_on_the_tv(self):
        fp.write_status(True, "Displayed X", {"id": "wm:9", "title": "X", "content_id": "MY_F0009"})
        fp._save_list(fp.STATE, ["MY_F0009"])
        client = app.app.test_client()

        class Done:
            returncode, stdout, stderr = 0, "", ""
        with Patch(app, run_push=lambda *a, **k: (Done(), None)):
            r = client.post("/ban")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(fp._load_list(fp.BLOCKLIST), ["wm:9"])
        self.assertEqual(fp._load_list(fp.PENDING_DELETE), ["MY_F0009"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

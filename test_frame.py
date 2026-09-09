#!/usr/bin/env python3
"""Tests for the pure logic in frame_push.py and app.py — no network, no TV.

    python3 -m unittest -v test_frame          # or: python3 test_frame.py

Every case here corresponds to a bug that actually shipped, or to an invariant that
kept the fix honest. Stdlib unittest on purpose: no dev dependency to install, and
pytest runs this file unchanged if you prefer it.
"""

import os, tempfile, unittest, warnings

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
                        "CURRENT_IMG", "HIST_IMG_DIR", "FAVS_DIR", "CONFIG", "TMP")}
        fp.CFG = os.path.join(self.tmp, "cfg")
        fp.TMP = os.path.join(self.tmp, "art")
        for name, leaf in (("STATUS", "status.json"), ("HISTORY", "history.json"),
                           ("BLOCKLIST", "blocklist.json"), ("FAVOURITES", "favourites.json"),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

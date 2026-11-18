"""What the big-letter net sights is hidden whole: no pixel row of a sighting may stay outside every black box.

Run:  python tests/cover_test.py          (exit 0 = all green; no OCR engine is used, the worker is a stand-in)

A "sighting" is one box the unread-text net returns on one shrunken copy of the screen. Sightings of the same string
at different shrink factors differ by a few rows, and a sighting can overlap a hit that was found another way with a
different height. However they are merged, the union of what is finally blacked out must contain every sighting.
A merged box that grew past one text row is never handed to the second look (which judges one row at a time), and
neither is a single sighting whose ink clearly shows two lines (a partial guard: close lines can still read as one).
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from engine import ocr_redact as redact  # noqa: E402

W, H = 1600, 600
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def mask_of(boxes) -> np.ndarray:
    m = np.zeros((H, W), bool)
    for x0, y0, x1, y1 in boxes:
        m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    return m


def uncovered(sightings, hidden) -> int:
    """How many pixels of the sightings lie outside every hidden box."""
    return int((mask_of(sightings) & ~mask_of(hidden)).sum())


def full_size(u_box, f):
    x0, y0, x1, y1 = u_box
    return (max(0, x0 * f - f), max(0, y0 * f - f), min(W, x1 * f + f), min(H, y1 * f + f))


def scripted(per_factor):
    """A stand-in for the pixel net: per shrink factor, the boxes it 'sees' (in the shrunken copy's own pixels)."""
    def fake(small, words):
        f = round(W / small.shape[1])
        if small.shape[1] == W:      # the 1x pass of analyze: nothing
            return []
        return [redact.Hit("unreadable_long_string", b, b[3] - b[1], "unread") for b in per_factor.get(f, [])]
    return fake


def run_net(per_factor, known):
    """big_unread_hits with a scripted net. Returns (sightings at full size, every box that ends up hidden)."""
    real = redact.unread_hits
    redact.unread_hits = scripted(per_factor)
    try:
        got = redact.big_unread_hits(np.zeros((H, W), np.uint8), [], known)
    finally:
        redact.unread_hits = real
    hits, covers = got if isinstance(got, tuple) else (got, [])
    sightings = [full_size(b, f) for f, bs in per_factor.items() for b in bs]
    return sightings, [k.box for k in known] + [h.box for h in hits] + list(covers), hits


class Reader:
    """A stand-in OCR worker: reads nothing on the first passes."""
    def recognize(self, gray, timeout=4.0):
        return []

    def ensure(self):
        pass


def main() -> int:
    # 1. a hit found another way covers only PART of a big string, and sits at a different height
    known = [redact.Hit("some_pattern", (200, 210, 520, 236), 26, "token")]
    before = known[0].box
    sightings, hidden, _ = run_net({2: [(100, 100, 600, 122)]}, known)     # full size: x 198..1202, y 198..246
    check("a partly covering hit: every pixel of the sighting is hidden", uncovered(sightings, hidden) == 0,
          f"{uncovered(sightings, hidden)} px uncovered")
    check("a partly covering hit: the hit found another way is not reshaped", known[0].box == before and known[0].line_h == 26,
          f"{known[0].box} line_h {known[0].line_h}")

    # 2. one string seen at two shrink factors, with different rows
    sightings, hidden, hits = run_net({2: [(100, 100, 600, 120)], 3: [(66, 62, 402, 80)]}, [])
    check("two sightings of one string: every pixel of both is hidden", uncovered(sightings, hidden) == 0,
          f"{uncovered(sightings, hidden)} px uncovered")
    check("two sightings of one string: one counted hit", len(hits) == 1, f"{len(hits)} hits")

    # 3. a sighting that touches a known hit without sharing its row (below it, overlapping by a few rows)
    known = [redact.Hit("some_pattern", (200, 180, 900, 206), 26, "token")]
    sightings, hidden, hits = run_net({2: [(100, 100, 600, 122)]}, known)     # y 198..246: overlaps rows 198..206 only
    check("a sighting grazing a hit on the row above: hidden whole", uncovered(sightings, hidden) == 0,
          f"{uncovered(sightings, hidden)} px uncovered")

    # 4. three sightings, the third bridging two that did not touch
    sightings, hidden, hits = run_net({2: [(100, 100, 300, 120), (320, 100, 600, 120)], 4: [(50, 48, 300, 62)]}, [])
    check("a sighting bridging two others: every pixel hidden", uncovered(sightings, hidden) == 0,
          f"{uncovered(sightings, hidden)} px uncovered")

    # 5. through analyze, on a real drawn string: whatever the net sights is inside the final boxes
    im = Image.new("RGB", (W, H), (16, 24, 32))
    font = ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", "consola.ttf"), 34)
    ImageDraw.Draw(im).text((80, 200), "Zq7Lm2Pv8Rt4Ky6Nb1Hd3Fg5Js0AaBbCcDdEeFfGg9", fill=(255, 255, 255), font=font)
    rgb = np.asarray(im)
    gray = (rgb[..., 0].astype("uint16") * 77 + rgb[..., 1].astype("uint16") * 150
            + rgb[..., 2].astype("uint16") * 29 >> 8).astype("uint8")
    sightings = []
    for f in redact.BIG_FACTORS:
        h, w = H // f * f, W // f * f
        small = gray[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3)).astype(np.uint8)
        for u in redact.unread_hits(small, []):
            if (u.box[3] - u.box[1]) * f >= redact.BIG_MIN_H:
                sightings.append(full_size(u.box, f))
    check("a drawn big string: the net sights it", len(sightings) >= 1, f"{len(sightings)} sightings")
    r = redact.analyze(gray, Reader(), redact.load_patterns(), (1, 2), 4.0)
    check("a drawn big string, through analyze: every sighted pixel is hidden", uncovered(sightings, r.boxes) == 0,
          f"{uncovered(sightings, r.boxes)} px uncovered")
    check("a drawn big string, through analyze: counted once", r.count + r.random == 1, f"{r.count}+{r.random}")

    # 6. a merged box that grew past one text row is never shown again by the second look
    shown = {}
    real_net, real_look = redact.unread_hits, redact.second_look

    def lenient(gray_, worker, patterns, unread, timeout):   # a second look that would clear everything it is given
        shown["given"] = [u.box for u in unread]
        return [], []

    redact.unread_hits = scripted({2: [(100, 100, 600, 120)], 3: [(66, 73, 402, 89)]})   # one string, rows 198..242 and 216..270: the union is far taller than either
    redact.second_look = lenient
    try:
        r = redact.analyze(np.zeros((H, W), np.uint8), Reader(), redact.load_patterns(), (1, 2), 4.0)
    finally:
        redact.unread_hits, redact.second_look = real_net, real_look
    both = [full_size((100, 100, 600, 120), 2), full_size((66, 73, 402, 89), 3)]
    check("a box grown past one row: not handed to the second look", shown.get("given", []) == [], f"{shown.get('given')}")
    check("a box grown past one row: stays hidden whole", uncovered(both, r.boxes) == 0, f"{uncovered(both, r.boxes)} px uncovered")

    # 7. one plain sighting still gets its second look (ordinary big text is not blacked out for nothing)
    redact.unread_hits = scripted({2: [(100, 100, 600, 120)]})
    redact.second_look = lenient
    try:
        r = redact.analyze(np.zeros((H, W), np.uint8), Reader(), redact.load_patterns(), (1, 2), 4.0)
    finally:
        redact.unread_hits, redact.second_look = real_net, real_look
    check("one plain sighting: handed to the second look, and shown when it clears", len(shown.get("given", [])) == 1 and not r.boxes,
          f"given {shown.get('given')} boxes {r.boxes}")

    # 8. ONE sighting that swallowed two lines set close together: an ordinary line and a secret-looking line.
    #    A second look that can read the ordinary line must not get the chance to clear the pair.
    for fname, size, spacing in (("segoeui.ttf", 64, 0.9), ("consola.ttf", 48, 1.0), ("segoeui.ttf", 80, 1.1), ("consola.ttf", 40, 0.9)):
        font = ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", fname), size)
        im = Image.new("RGB", (W, H), (16, 24, 32))
        d = ImageDraw.Draw(im)
        top = 180
        d.text((60, top), "ordinary words stay here", fill=(255, 255, 255), font=font)
        d.text((60, top + int(size * spacing)), "Zq7Lm2Pv8Rt4Ky6Nb1Hd3F", fill=(255, 255, 255), font=font)
        a = d.textbbox((60, top), "ordinary words stay here", font=font)
        b = d.textbbox((60, top + int(size * spacing)), "Zq7Lm2Pv8Rt4Ky6Nb1Hd3F", font=font)
        rgb = np.asarray(im)
        g = (rgb[..., 0].astype("uint16") * 77 + rgb[..., 1].astype("uint16") * 150
             + rgb[..., 2].astype("uint16") * 29 >> 8).astype("uint8")
        pair = (min(a[0], b[0]) - 4, a[1] - 4, max(a[2], b[2]) + 4, b[3] + 4)       # both lines, as the net would join them
        shrunk = (pair[0] // 4 + 1, pair[1] // 4 + 1, pair[2] // 4 - 1, pair[3] // 4 - 1)
        redact.unread_hits = scripted({4: [shrunk]})
        redact.second_look = lenient
        shown.clear()
        try:
            r = redact.analyze(g, Reader(), redact.load_patterns(), (1, 2), 4.0)
        finally:
            redact.unread_hits, redact.second_look = real_net, real_look
        tag = f"two lines in one sighting ({fname[:-4]} {size}px, spacing {spacing})"
        check(tag + ": not handed to the second look", shown.get("given", []) == [], f"{shown.get('given')}")
        check(tag + ": both lines stay hidden", uncovered([full_size(shrunk, 4)], r.boxes) == 0,
              f"{uncovered([full_size(shrunk, 4)], r.boxes)} px uncovered")
        one = (a[0] - 4, a[1] - 4, a[2] + 4, a[3] + 4)
        check(tag + ": one line alone is still one line", not redact.spans_rows(g, one))

    # 9. the INK of the whole string, not the net's box: a cover and a keep hit hide all of it. The net sees the
    #    string on shrunken copies, so a sighting can stop short of the string's real ends (it can see only part).
    def drawn(text, size, fname, dark, spacing=1.6, lines=None):
        bg, fg = ((16, 24, 32), (255, 255, 255)) if dark else ((255, 255, 255), (36, 36, 36))
        im = Image.new("RGB", (1920, 720), bg)
        d = ImageDraw.Draw(im)
        f = ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", fname), size)
        for i, line in enumerate(lines or [text]):
            d.text((80, 150 + int(i * size * spacing)), line, fill=fg, font=f)
        px = np.asarray(im)
        ink = np.abs(px.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > 60
        g = (px[..., 0].astype("uint16") * 77 + px[..., 1].astype("uint16") * 150
             + px[..., 2].astype("uint16") * 29 >> 8).astype("uint8")
        return g, ink

    def visible(ink, boxes):
        m = np.zeros(ink.shape, bool)
        for x0, y0, x1, y1 in boxes:
            m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
        return int((ink & ~m).sum())

    KEY = "Zq7Lm2Pv8Rt4Ky6Nb1Hd3Fg5Js0AaBbCcDdEeFfGg9Hh"
    for size, fname, dark in ((64, "segoeui.ttf", True), (80, "segoeui.ttf", False), (80, "segoeuib.ttf", False),
                              (40, "consola.ttf", True), (52, "consola.ttf", False)):
        g, ink = drawn(KEY, size, fname, dark)
        ys, xs = np.nonzero(ink)
        string_box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        # a hit found another way covers the start of the key and only its middle rows; the net sees only the end
        mid = (string_box[1] + string_box[3]) // 2
        known = (string_box[0], mid - size // 6, string_box[0] + (string_box[2] - string_box[0]) // 3, mid + size // 6)
        tail = ((string_box[0] + string_box[2]) // 2 // 4, (string_box[1] + 2) // 4, (string_box[2] - 3) // 4, (string_box[3] - 2) // 4)

        class OneWord(Reader):
            def recognize(self, gray_, timeout=4.0):   # the first read finds the labelled start, as a pattern would
                return [[("sk-ant-api03-Zq7Lm2Pv8Rt4Ky6Nb1Hd3Fg5J", known[0], known[1], known[2] - known[0], known[3] - known[1])]]
        redact.unread_hits = lambda small, words: [] if small.shape[1] == 1920 else (
            [redact.Hit("unreadable_long_string", tail, tail[3] - tail[1], "unread")] if round(1920 / small.shape[1]) == 4 else [])
        try:
            r = redact.analyze(g, OneWord(), redact.load_patterns(), (1,), 6.0)
        finally:
            redact.unread_hits = real_net
        tag = f"ink beside a cover ({fname[:-4]} {size}px {'dark' if dark else 'light'})"
        check(tag + ": every ink pixel of the key is hidden", visible(ink, r.boxes) == 0, f"{visible(ink, r.boxes)} px visible")

        # the same string, sighted by the net only in part, at two factors whose rows differ: a keep hit
        part2 = ((string_box[0] + string_box[2]) // 2 // 2, (string_box[1] - 6) // 2, (string_box[2] - 4) // 2, (string_box[3] - 4) // 2)
        part4 = ((string_box[0] + string_box[2]) // 2 // 4, (string_box[1] + 6) // 4, (string_box[2] - 4) // 4, (string_box[3] + 8) // 4)
        wide = {2: [part2], 4: [part4]}   # these cards are 1920 px wide: the factor is read off that width
        redact.unread_hits = lambda small, words: [] if small.shape[1] == 1920 else [
            redact.Hit("unreadable_long_string", bx, bx[3] - bx[1], "unread") for bx in wide.get(round(1920 / small.shape[1]), [])]
        redact.second_look = lenient
        try:
            r = redact.analyze(g, Reader(), redact.load_patterns(), (1,), 6.0)
        finally:
            redact.unread_hits, redact.second_look = real_net, real_look
        tag = f"ink of a keep hit ({fname[:-4]} {size}px {'dark' if dark else 'light'})"
        check(tag + ": every ink pixel of the key is hidden", visible(ink, r.boxes) == 0, f"{visible(ink, r.boxes)} px visible")

    print("\nALL GREEN" if not failures else f"\n{len(failures)} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

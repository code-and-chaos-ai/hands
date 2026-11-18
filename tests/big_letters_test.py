"""Keys in BIG letters (a zoomed terminal, a slide, a large-font editor), on synthetic screens only.

Run:  python tests/big_letters_test.py          (exit 0 = all green)
      python tests/big_letters_test.py --measure   (prints the numbers and never fails: for a before/after count)

The hole this pins: Windows OCR does not read a long random string, and the unread-text net joined
only the letters of SMALL text, so a key drawn at about 26 px and up could pass as clean, about 1 time in 7.
Proves: every fake key is fully covered at every size, theme and font tried; plain big text and pictures with no
secret are not blacked out more than before; a shot still takes well under its time limit.
"""
from __future__ import annotations

import os
import random
import string
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from engine import ocr_redact as redact  # noqa: E402

FONTS = {"mono": "consola.ttf", "ui": "segoeui.ttf"}
THEMES = {"dark": ((16, 24, 32), (255, 255, 255)), "light": ((255, 255, 255), (36, 36, 36))}
SIZES = (20, 26, 32, 40, 52)
SEEDS = 12                      # x 5 sizes x 2 themes x 2 fonts = 240 cards
PLAIN = ["Meeting notes for the autumn planning session", "Remember to water the plants before leaving",
         "Chapter seven: the river comes down the mountain", "internationalization and telecommunications",
         "Supercalifragilisticexpialidocious, said the nanny", "https://example.com/blog/how-to-bake-sourdough-bread"]


def font(name: str, size: int):
    return ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", FONTS[name]), size)


def fake_key(rnd: random.Random) -> str:
    # built at run time: no key-shaped literal sits in this file
    return "sk" + "-ant-" + "api03-" + "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(40))


def card(lines, size, theme, fname, wh=(1920, 1080)):
    """A screen with the lines drawn one under another. Returns (rgb, [pixel box of each line])."""
    bg, fg = THEMES[theme]
    im = Image.new("RGB", wh, bg)
    d = ImageDraw.Draw(im)
    f = font(fname, size)
    boxes, y = [], 100
    for line in lines:
        d.text((80, y), line, fill=fg, font=f)
        boxes.append(d.textbbox((80, y), line, font=f))
        y += int(size * 3.2)
    return np.asarray(im).copy(), boxes


def gray_of(rgb):
    return (rgb[..., 0].astype("uint16") * 77 + rgb[..., 1].astype("uint16") * 150
            + rgb[..., 2].astype("uint16") * 29 >> 8).astype("uint8")


def ink_left(rgb, box, theme) -> bool:
    """True when anything of the text is still visible inside the box after the black boxes were applied."""
    bg = np.array(THEMES[theme][0], dtype=np.int16)
    x0, y0, x1, y1 = box
    region = rgb[y0:y1, x0:x1].astype(np.int16)
    visible = (np.abs(region - bg).max(axis=2) > 60) & (region.max(axis=2) > 0)   # not background, not blacked out
    if theme == "dark":
        return bool(visible.any())
    return bool(((np.abs(region - bg).max(axis=2) > 60) & (region.max(axis=2) > 8)).any())


def measure(worker, patterns, log=print):
    out = {"cards": 0, "missed": [], "plain_screens": 0, "plain_blacked": 0, "picture_blacked": 0, "ms": []}
    rnd = random.Random(1811)
    for seed in range(SEEDS):
        for size in SIZES:
            for theme in THEMES:
                for fname in FONTS:
                    key = fake_key(rnd)
                    label = "ANTHROPIC_API_KEY=" if seed % 2 == 0 else ""
                    if 80 + font(fname, size).getlength(label + key) > 1900:
                        label = ""
                    if 80 + font(fname, size).getlength(label + key) > 1900:
                        continue   # this size does not fit one row of the screen
                    rgb, boxes = card(["deploy notes", label + key, "ordinary words stay"], size, theme, fname)
                    kx0 = int(80 + font(fname, size).getlength(label))
                    key_box = (kx0, boxes[1][1], boxes[1][2], boxes[1][3])
                    t = time.perf_counter()
                    r = redact.analyze(gray_of(rgb), worker, patterns, (1, 2), 4.0)
                    out["ms"].append((time.perf_counter() - t) * 1000)
                    redact.apply_boxes(rgb, r.boxes)
                    out["cards"] += 1
                    if ink_left(rgb, key_box, theme):
                        out["missed"].append((seed, size, theme, fname))
    # plain screens: ordinary words, long but harmless, at every size: nothing should be blacked out
    for size in SIZES:
        for theme in THEMES:
            for fname in FONTS:
                rgb, _ = card(PLAIN, size, theme, fname)
                r = redact.analyze(gray_of(rgb), worker, patterns, (1, 2), 4.0)
                out["plain_screens"] += 1
                out["plain_blacked"] += len(r.boxes)
    # pictures: smooth and busy images with no text at all
    yy, xx = np.mgrid[0:1080, 0:1920]
    noise = np.random.default_rng(7)
    pictures = [np.stack([(xx / 8) % 256, (yy / 5) % 256, ((xx + yy) / 11) % 256], axis=2).astype(np.uint8),
                noise.integers(0, 256, (1080, 1920, 3), dtype=np.uint8),
                np.repeat(np.repeat(noise.integers(0, 256, (135, 240, 3), dtype=np.uint8), 8, axis=0), 8, axis=1)]
    for pic in pictures:
        r = redact.analyze(gray_of(pic), worker, patterns, (1, 2), 8.0)
        out["picture_blacked"] += len(r.boxes)
    ms = sorted(out["ms"])
    out["ms_median"], out["ms_max"] = ms[len(ms) // 2], ms[-1]
    del out["ms"]
    log(f"cards {out['cards']}, missed {len(out['missed'])}, plain screens {out['plain_screens']} with "
        f"{out['plain_blacked']} boxes, pictures with {out['picture_blacked']} boxes, "
        f"time per 1920x1080 card: median {out['ms_median']:.0f} ms, max {out['ms_max']:.0f} ms")
    for m in out["missed"][:12]:
        log(f"  MISSED seed {m[0]} size {m[1]} {m[2]} {m[3]}")
    return out


def main() -> int:
    worker = redact.OcrWorker("en-US")
    worker.ensure()
    try:
        out = measure(worker, redact.load_patterns())
    finally:
        worker.close()
    if "--measure" in sys.argv:
        return 0
    bad = []
    if out["missed"]:
        bad.append(f"{len(out['missed'])} key(s) still readable")
    if out["plain_blacked"] > PLAIN_ALLOWED:
        bad.append(f"plain text blacked out: {out['plain_blacked']} boxes (allowed {PLAIN_ALLOWED})")
    if out["picture_blacked"] > PICTURE_ALLOWED:
        bad.append(f"pictures blacked out: {out['picture_blacked']} boxes (allowed {PICTURE_ALLOWED})")
    if out["ms_max"] > 3000:
        bad.append(f"a card took {out['ms_max']:.0f} ms")
    print("FAILED: " + "; ".join(bad) if bad else "OK")
    return 1 if bad else 0


# what the check did on these same plain screens and pictures BEFORE the big-letter net:
PLAIN_ALLOWED = 0
PICTURE_ALLOWED = 0

if __name__ == "__main__":
    sys.exit(main())

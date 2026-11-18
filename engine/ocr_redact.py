"""Find secrets on a screenshot and black them out, in memory.

The pipeline: grayscale pixels → Windows' built-in OCR (a PowerShell 5.1 worker, see ocr_worker.ps1; nothing is
installed and nothing is written to disk) → the OCR words are matched against secret_patterns.json → each hit is
widened over its unbroken run of text pixels (OCR drops and garbles characters, the pixels don't lie) → black boxes.

OCR reduces leaks; it does not promise none. It can miss tiny or low-contrast text, text it can't read at all, and a
secret split across lines in a way none of the patterns see. Callers must fail closed when OCR fails (OcrError).
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERNS_FILE = os.path.join(HERE, "secret_patterns.json")
WORKER_FILE = os.path.join(HERE, "ocr_worker.ps1")

# Look-alikes OCR produces, mapped back one character for one character (so string offsets still line up).
_CONFUSABLES = str.maketrans({"Ø": "0", "ø": "0", "‘": "'", "’": "'", "“": '"', "”": '"', "—": "-", "–": "-",
                              "©": "@", "®": "@", "ß": "B"})


class OcrError(RuntimeError):
    pass


# ── patterns ─────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Pattern:
    id: str
    label: str
    rx: re.Pattern
    kind: str = "token"
    checks: dict = field(default_factory=dict)
    end_rx: re.Pattern | None = None
    compact: bool = True


def load_patterns(path: str = PATTERNS_FILE) -> list[Pattern]:
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    out = []
    for p in spec["patterns"]:
        flags = re.IGNORECASE if "i" in p.get("flags", "") else 0
        end = re.compile(p["end_regex"], flags) if p.get("end_regex") else None
        out.append(Pattern(p["id"], p.get("label", p["id"]), re.compile(p["regex"], flags), p.get("kind", "token"),
                           p.get("checks", {}), end, bool(p.get("compact", True))))
    return out


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def char_classes(s: str) -> int:
    return sum((any(c.islower() for c in s), any(c.isupper() for c in s), any(c.isdigit() for c in s),
                any(not c.isalnum() for c in s)))


def passes_checks(m: re.Match, checks: dict) -> bool:
    if not checks:
        return True
    v = m.group(checks.get("group", 0)) or ""
    if len(v) < checks.get("min_len", 0):
        return False
    if checks.get("reject") and re.search(checks["reject"], v):
        return False
    if checks.get("require_digit") and not any(c.isdigit() for c in v):
        return False
    if "max_hex_share" in checks:
        an = [c for c in v if c.isalnum()]
        if an and sum(c in "0123456789abcdefABCDEF" for c in an) / len(an) >= checks["max_hex_share"]:
            return False
    if (sum(c.isupper() for c in v) < checks.get("min_upper", 0)
            or sum(c.islower() for c in v) < checks.get("min_lower", 0)
            or sum(c.isdigit() for c in v) < checks.get("min_digits", 0)):
        return False
    if entropy(v) < checks.get("min_entropy", 0):
        return False
    need = checks.get("min_classes", 0)
    if need and char_classes(v) < need and len(v) < checks.get("or_min_len", 10 ** 9):
        return False
    return True


# ── OCR worker ───────────────────────────────────────────────────────────────────────────────────────────────────
class OcrWorker:
    """One long-lived Windows PowerShell 5.1 process running Windows.Media.Ocr; pixels go in over stdin."""

    def __init__(self, language: str = "en-US", start_timeout: float = 15.0):
        self.language = language
        self.start_timeout = start_timeout
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.lock = threading.Lock()

    def _start(self) -> None:
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe")
        self.proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", WORKER_FILE,
             "-Language", self.language],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        q: queue.Queue = queue.Queue()
        self.lines = q
        proc = self.proc

        def pump() -> None:
            for raw in proc.stdout:  # type: ignore[union-attr]
                q.put(raw)
            q.put(None)

        threading.Thread(target=pump, daemon=True).start()
        first = self._read(self.start_timeout)
        if not first.get("ready"):
            self.close()
            raise OcrError(f"OCR worker did not start: {first.get('error', first)}")

    def _read(self, timeout: float) -> dict:
        try:
            raw = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise OcrError(f"OCR timed out after {timeout:.1f} s")
        if raw is None:
            self.close()
            raise OcrError("OCR worker exited")
        return json.loads(raw.decode("utf-8"))

    def ensure(self) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()

    def recognize(self, gray: np.ndarray, timeout: float = 4.0) -> list[list[tuple[str, int, int, int, int]]]:
        """gray: uint8 HxW. Returns OCR lines, each a list of (text, x, y, w, h) words."""
        if gray.dtype != np.uint8 or gray.ndim != 2:
            raise ValueError("gray must be a 2-D uint8 array")
        h, w = gray.shape
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self.proc.stdin.write(f"{w} {h} {w * h}\n".encode("ascii"))  # type: ignore[union-attr]
                self.proc.stdin.write(np.ascontiguousarray(gray).data)  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
            except OSError as e:
                self.close()
                raise OcrError(f"OCR worker pipe broke: {e}")
            res = self._read(timeout)
        if not res.get("ok"):
            raise OcrError(f"OCR failed: {res.get('error')}")
        return [[(t, x, y, ww, hh) for t, x, y, ww, hh in line] for line in res["lines"]]

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass


# ── matching ─────────────────────────────────────────────────────────────────────────────────────────────────────
Box = tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)


@dataclass
class Hit:
    pattern: str
    box: Box
    line_h: int
    kind: str = "token"
    words: frozenset = frozenset()
    keep: bool = False              # stays hidden as it is: never widened, never handed to the second look


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _overlap(a: Box, b: Box, slack: int = 0) -> bool:
    return a[0] - slack < b[2] and b[0] - slack < a[2] and a[1] - slack < b[3] and b[1] - slack < a[3]


def _line_texts(words):
    """Two renderings of an OCR line: words joined by spaces, and with no spaces at all (OCR splits tokens).
    Each comes with a char→word index map."""
    out = []
    for sep in (" ", ""):
        text, owner = [], []
        for i, (t, *_rest) in enumerate(words):
            if i and sep:
                text.append(sep)
                owner.append(-1)
            text.append(t)
            owner.extend([i] * len(t))
        out.append(("".join(text).translate(_CONFUSABLES), owner))
    return out


def merge_rows(lines):
    """OCR cuts one visual row into several 'lines' (a label and its value, a token in two). Put back together the
    pieces that sit on the same row close to each other (columns of a layout stay apart)."""
    items = []
    for words in lines:
        if words:
            x0, y0, x1, y1 = _union([(w[1], w[2], w[1] + w[3], w[2] + w[4]) for w in words])
            items.append([x0, y0, x1, y1, list(words)])
    items.sort(key=lambda it: ((it[1] + it[3]) / 2, it[0]))
    rows: list[list] = []
    for it in items:
        h = it[3] - it[1]
        yc = (it[1] + it[3]) / 2
        for row in rows:
            rh = row[3] - row[1]
            gap = max(it[0], row[0]) - min(it[2], row[2])
            if abs(yc - (row[1] + row[3]) / 2) <= 0.4 * min(h, rh) and gap <= 3 * max(h, rh):
                row[:4] = [min(row[0], it[0]), min(row[1], it[1]), max(row[2], it[2]), max(row[3], it[3])]
                row[4].extend(it[4])
                break
        else:
            rows.append(it)
    return [sorted(row[4], key=lambda w: w[1]) for row in rows]


def find_hits(lines, patterns: list[Pattern], scale: float = 1.0) -> list[Hit]:
    """Match every pattern on every OCR row. Coordinates are divided by `scale` (OCR may run on an upscaled image)."""
    hits: list[Hit] = []
    for li, words in enumerate(merge_rows(lines)):
        if not words:
            continue
        for (text, owner), spaced in zip(_line_texts(words), (True, False)):
            for p in patterns:
                if not spaced and not p.compact:
                    continue
                for m in p.rx.finditer(text):
                    if not passes_checks(m, p.checks):
                        continue
                    idx = sorted({owner[k] for k in range(m.start(), m.end()) if owner[k] >= 0})
                    if not idx:
                        continue
                    if p.checks.get("words_need_digit") and any(
                            len(words[i][0]) > 2 and not any(c.isdigit() for c in words[i][0]) for i in idx):
                        continue
                    bxs = [(words[i][1], words[i][2], words[i][1] + words[i][3], words[i][2] + words[i][4])
                           for i in idx]
                    x0, y0, x1, y1 = _union(bxs)
                    box = (int(x0 / scale), int(y0 / scale), int(math.ceil(x1 / scale)), int(math.ceil(y1 / scale)))
                    lh = max(1, int(round(max(b[3] - b[1] for b in bxs) / scale)))
                    hits.append(Hit(p.id, box, lh, p.kind, frozenset((li, i) for i in idx)))
    return hits


def block_hits(lines, patterns: list[Pattern], scale: float, img_h: int) -> list[Hit]:
    """Blocks (private keys): from the BEGIN line down to its END line; without an END in sight, 40 lines down."""
    out = []
    flat = []
    for words in lines:
        if words:
            x0, y0, x1, y1 = _union([(w[1], w[2], w[1] + w[3], w[2] + w[4]) for w in words])
            flat.append(((x0 / scale, y0 / scale, x1 / scale, y1 / scale),
                         " ".join(w[0] for w in words).translate(_CONFUSABLES)))
    for p in patterns:
        if p.kind != "block":
            continue
        for (bx0, by0, bx1, by1), text in flat:
            if not p.rx.search(text) and not p.rx.search(text.replace(" ", "")):
                continue
            lh = max(1.0, by1 - by0)
            ends = [b for b, t in flat if b[1] > by1 and p.end_rx and
                    (p.end_rx.search(t) or p.end_rx.search(t.replace(" ", "")))]
            bottom = min(e[3] for e in ends) if ends else min(img_h, by1 + 40 * lh * 1.4)
            inside = [b for b, _t in flat if b[1] >= by0 and b[3] <= bottom + 1 and abs(b[0] - bx0) < 6 * lh]
            x0 = min([bx0] + [b[0] for b in inside])
            x1 = max([bx1] + [b[2] for b in inside] + [bx0 + 70 * lh * 0.6])
            out.append(Hit(p.id, (int(x0), int(by0), int(math.ceil(x1)), int(math.ceil(bottom))), int(lh), "block"))
    return out


def continuation_hits(lines, hits: list[Hit], scale: float) -> list[Hit]:
    """A token wrapped onto the next line. Hiding and counting are judged apart, and hiding always wins:
    - HIDDEN: any random-looking run (16+ chars, 2+ character kinds, entropy 3.2+) on the next line (within 1.2x
      the text height) that starts at or left of where the piece above started. Hex and all-lowercase keys count.
    - COUNTED AS THE SAME SECRET ("continuation") only when wrapped text's marks are all there: the piece above ends
      its line, the next piece sits right under it (0.8x), mixes upper, lower and digits and is not mostly hex.
      Otherwise it is hidden as a "continuation_loose", counted as a separate random string. A wrong count is a
      smaller harm than a visible secret."""
    out = []
    flat = []
    all_words = [(w[1] / scale, w[2] / scale, (w[1] + w[3]) / scale, (w[2] + w[4]) / scale)
                 for words in lines for w in words]

    def ends_its_line(box) -> bool:
        lh = max(1, box[3] - box[1])
        return not any(min(box[3], b[3]) - max(box[1], b[1]) >= 0.5 * lh and b[0] > box[2] + 2 * lh
                       for b in all_words)

    # candidates are runs of words, not whole lines: the wrapped piece may share its line with other words. A run
    # is consecutive words with almost no gap between them (OCR cuts one token into several words with ~0-2 px
    # between; a real space in a small UI font is only ~3 px, so the bar is set below it)
    for words in lines:
        run: list = []
        for wd in sorted(words, key=lambda q: q[1]) + [None]:
            if wd is not None and run:
                prev = run[-1]
                if wd[1] - (prev[1] + prev[3]) < 0.15 * max(prev[4], wd[4]):
                    run.append(wd)
                    continue
            if run:
                x0, y0, x1, y1 = _union([(q[1], q[2], q[1] + q[3], q[2] + q[4]) for q in run])
                flat.append(((x0 / scale, y0 / scale, x1 / scale, y1 / scale), "".join(q[0] for q in run)))
            run = [wd] if wd is not None else []
    for h in hits:
        if h.kind != "token":
            continue
        cur = h.box
        for _ in range(8):  # at most 8 wrapped lines
            nxt, strict = None, False
            lh = h.line_h
            for b, t in flat:
                if not (0 <= b[1] - cur[3] <= 1.2 * lh and b[0] <= cur[0] + 2 * lh and b[2] >= cur[0] - 2 * lh
                        and len(t) >= 16 and entropy(t) >= 3.2 and char_classes(t) >= 2):
                    continue
                an = [c for c in t if c.isalnum()]
                s = (ends_its_line(cur) and b[1] - cur[3] <= 0.8 * lh and any(c.isupper() for c in t)
                     and any(c.islower() for c in t) and any(c.isdigit() for c in t)
                     and sum(c in "0123456789abcdefABCDEF" for c in an) < 0.8 * len(an))
                if nxt is None or (s and not strict):
                    nxt, strict = b, s
            if nxt is None:
                break
            cur = (int(nxt[0]), int(nxt[1]), int(math.ceil(nxt[2])), int(math.ceil(nxt[3])))
            out.append(Hit(h.pattern, cur, h.line_h, "continuation" if strict else "continuation_loose"))
    return out


# ── widening over the pixels ─────────────────────────────────────────────────────────────────────────────────────
def widen_to_ink(gray: np.ndarray, box: Box, line_h: int) -> Box:
    """Extend a hit left and right along its row while the text keeps going: OCR may have dropped or misread part
    of the token, but the unbroken run of text pixels around it is the token. Stops at a gap wider than a space."""
    h, w = gray.shape
    x0, y0, x1, y1 = box
    y0c, y1c = max(0, y0), min(h, max(y0 + 1, y1))
    band = gray[y0c:y1c, :].astype(np.int16)
    if band.size == 0:
        return box
    bg = int(np.median(band))
    ink = (np.abs(band - bg) > 48).any(axis=0)
    gap_max = max(3, int(round(0.45 * line_h)))
    reach = 80 * max(line_h, 8)

    def walk(start: int, step: int, limit: int) -> int:
        edge, gap, x = start, 0, start
        while 0 <= x + step < w and abs(x + step - start) <= limit:
            x += step
            if ink[x]:
                edge, gap = x, 0
            else:
                gap += 1
                if gap > gap_max:
                    break
        return edge

    nx0 = walk(max(0, min(w - 1, x0)), -1, reach)
    nx1 = walk(max(0, min(w - 1, x1 - 1)), +1, reach) + 1
    return (min(x0, nx0), y0, max(x1, nx1), y1)


def pad(box: Box, line_h: int, w: int, h: int) -> Box:
    p = max(4, int(round(0.35 * line_h)))
    return (max(0, box[0] - p), max(0, box[1] - p), min(w, box[2] + p), min(h, box[3] + p))


# ── words OCR could not read ─────────────────────────────────────────────────────────────────────────────────────
# Windows OCR treats a long random string (exactly what a key looks like) as noise and drops it, often with its whole
# line. So the pixels are checked too: every long unbroken "word" on screen that OCR did not read is blacked out.
UNREAD_MIN_H, UNREAD_MAX_H = 7, 40      # glyph-row heights that are text on a normal screen
UNREAD_MIN_RATIO = 8.0                  # width / height: about 14+ characters with no space
UNREAD_MIN_W = 80
UNREAD_MIN_DENSITY = 0.12               # share of edge pixels: text is busy, frames and bars are not
UNREAD_MIN_STROKES = 0.2                # share of its columns holding an upright stroke: text yes, bars and rules no
UNREAD_MAX_COVERED = 0.6                # OCR read less than this share of its width → unread


def text_blobs(gray: np.ndarray) -> list[tuple[Box, float]]:
    """Unbroken runs of glyphs (words with no space inside), from the pixels alone. Returns (box, edge density);
    shapes that are not text (bars, rules, frames: long edges, hardly any upright strokes) are left out."""
    from scipy import ndimage as ndi
    g = gray.astype(np.int16)
    edges = np.zeros(g.shape, bool)
    upright = np.zeros(g.shape, bool)
    upright[:, 1:] = np.abs(g[:, 1:] - g[:, :-1]) > 40
    edges |= upright
    edges[1:, :] |= np.abs(g[1:, :] - g[:-1, :]) > 40
    m = ndi.binary_dilation(edges, structure=np.ones((3, 1), bool))      # join a glyph's strokes and dots
    m = ndi.binary_closing(m, structure=np.ones((1, 3), bool))            # join letters, not words
    lab, _n = ndi.label(m)
    out = []
    for i, sl in enumerate(ndi.find_objects(lab)):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        h, w = y1 - y0, x1 - x0
        if not (UNREAD_MIN_H <= h <= UNREAD_MAX_H) or w < max(UNREAD_MIN_W, UNREAD_MIN_RATIO * h):
            continue
        if upright[y0:y1, x0:x1].any(axis=0).mean() < UNREAD_MIN_STROKES:
            continue
        dens = float(edges[y0:y1, x0:x1][lab[y0:y1, x0:x1] == i + 1].mean())
        out.append(((x0, y0, x1, y1), dens))
    return out


def unread_hits(gray: np.ndarray, word_boxes: list[Box]) -> list[Hit]:
    hits = []
    for (x0, y0, x1, y1), dens in text_blobs(gray):
        if dens < UNREAD_MIN_DENSITY:
            continue
        cov = np.zeros(x1 - x0, bool)
        mid = (y0 + y1) / 2
        for wx0, wy0, wx1, wy1 in word_boxes:
            if wy0 - 2 <= mid <= wy1 + 2 and wx0 < x1 and wx1 > x0:
                cov[max(0, wx0 - x0):max(0, min(x1, wx1) - x0)] = True
        if cov.mean() < UNREAD_MAX_COVERED:
            hits.append(Hit("unreadable_long_string", (x0, y0, x1, y1), y1 - y0, "unread"))
    return hits


BIG_FACTORS = (2, 3, 4)        # the net is run again on copies shrunk this much: big letters become small ones
BIG_MIN_H = 18                 # full-size height from which a run counts as big letters; smaller text is the 1x net's
# One line seen at two shrink factors comes back at nearly the same rows: measured on drawn text (20 to 120 px, three
# fonts, dark and light), the union of one line's sightings was at most 1.4% (1 px) taller than the tallest of them.
# A union taller than the tallest sighting by this share or more holds rows of another line: it is kept from the
# second look. At the boundary it is kept (fails closed).
BIG_ONE_LINE_SLACK = 0.05


def spans_rows(gray: np.ndarray, box: Box) -> bool:
    """True when the box holds more than one line of text. Big lines set close together are joined by the net into
    ONE sighting, and the second look, which reads a crop as one row, could clear it on the strength of the line it
    can read. Read off the ink itself: per pixel row, the share of columns that hold ink. One line of text is one
    hump (its letters' middle zone, thinning towards ascenders and descenders). Two humps with a clear dip between
    them are two lines."""
    x0, y0, x1, y1 = box
    band = gray[max(0, y0):y1, max(0, x0):x1].astype(np.int16)
    if band.shape[0] < 2 * UNREAD_MIN_H or band.shape[1] < 2:
        return False
    profile = (np.abs(band - int(np.median(band))) > 48).mean(axis=1)
    k = max(1, band.shape[0] // 24)
    profile = np.convolve(profile, np.ones(k) / k, mode="same")
    peak = float(profile.max())
    if peak <= 0:
        return False
    humps, start = [], None
    for i, v in enumerate(list(profile >= 0.5 * peak) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 0.12 * band.shape[0]:
                humps.append((start, i))
            start = None
    return any(float(profile[a[1]:b[0]].min()) < 0.35 * peak for a, b in zip(humps, humps[1:]) if b[0] > a[1])


def big_unread_hits(gray: np.ndarray, word_boxes: list[Box], known: list[Hit]) -> tuple[list[Hit], list[Box]]:
    """The unread-text net joins only the letters of SMALL text (its closing is 3 px wide), so a long string in big
    letters is no 'unbroken run' to it and goes unseen: a zoomed terminal, a slide, a large-font editor. Shrunk, big
    letters are small letters. So the same net runs on shrunken copies (area mean) with the OCR word boxes shrunk
    alongside, and each thing it sees there (a sighting) comes back at full size.

    Returns (hits, covers). Whatever happens to a sighting, every pixel of it ends up inside a hidden box:
    - it shares its row with a hit found another way (`known`: a pattern, or the 1x net): that hit is left exactly as
      it is, and the sighting itself becomes a cover: a box that is blacked out and nothing else. It is not counted
      and not grouped (the secret is already counted once) and it is never looked at again;
    - it shares its row with an earlier sighting: the two are one hit over their full union, in both directions. If
      that union is taller than the tallest sighting by `BIG_ONE_LINE_SLACK` or more (it took in rows of a
      neighbouring line), the hit is marked `keep`:
      the second look judges one row at a time and must not clear it;
    - otherwise it is a hit of its own, like any string the 1x net could not read.
    A hit whose ink clearly shows two lines (`spans_rows`) is marked `keep` as well, merged or not. That check is
    partial: when the second line does not dip clearly below the first, two lines can still read as one."""
    H, W = gray.shape
    out: list[Hit] = []
    covers: list[Box] = []

    def same_row(a: Box, b: Box) -> bool:
        return _overlap(a, b) and min(a[3], b[3]) - max(a[1], b[1]) >= 0.5 * min(a[3] - a[1], b[3] - b[1])

    for f in BIG_FACTORS:
        h, w = H // f * f, W // f * f
        if h // f < 2 * UNREAD_MIN_H or w // f < UNREAD_MIN_W:
            continue
        small = gray[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3)).astype(np.uint8)
        words = [(x0 // f, y0 // f, -(-x1 // f), -(-y1 // f)) for x0, y0, x1, y1 in word_boxes]
        for u in unread_hits(small, words):
            x0, y0, x1, y1 = u.box
            if (y1 - y0) * f < BIG_MIN_H:   # small text shrunk: its words run together and whole lines would hit
                continue
            box = (max(0, x0 * f - f), max(0, y0 * f - f), min(W, x1 * f + f), min(H, y1 * f + f))
            if any(same_row(o.box, box) for o in known):
                covers.append(box)
                continue
            twins = [o for o in out if same_row(o.box, box)]
            if not twins:
                out.append(Hit("unreadable_long_string", box, box[3] - box[1], "unread", keep=spans_rows(gray, box)))
                continue
            tallest = max([box[3] - box[1]] + [t.box[3] - t.box[1] for t in twins])
            first = twins[0]
            first.box = _union([box] + [t.box for t in twins])
            first.line_h = min(first.box[3] - first.box[1], tallest)
            first.keep = (first.keep or any(t.keep for t in twins)
                          or first.box[3] - first.box[1] >= (1 + BIG_ONE_LINE_SLACK) * tallest
                          or spans_rows(gray, first.box))
            for t in twins[1:]:   # this sighting bridged hits that had not touched: they are one now
                out.remove(t)
    return out, covers


REREAD_MAX = 40                # past this many, the rest stay hidden without a second look
REREAD_MIN_CHARS = 0.75        # share of the characters its width should hold that must be read (char ~ 0.5 x height)
_RANDOM_RUN = re.compile("[A-Za-z0-9]{8,}")


def _looks_random(text: str) -> bool:
    return any(any(c.isdigit() for c in m.group()) and any(c.isalpha() for c in m.group())
               for m in _RANDOM_RUN.finditer(text))


def second_look(gray: np.ndarray, worker: OcrWorker, patterns: list[Pattern], unread: list[Hit],
                timeout: float) -> tuple[list[Hit], list[Hit]]:
    """Every string the first read skipped gets a second, closer read. Each is cut out on its OWN pixel rows only
    (a crop that takes in neighbouring rows lets OCR read those instead and fool the check), set on plain background
    with a margin one text-height deep, and all of them are stacked in ONE image read once, at 1x (in testing, zoomed
    crops sometimes read only the harmless start of a key and let it pass; 1x never did). It is shown after all only
    if it now reads as ordinary text: enough characters read for its width, no pattern fires, no run of 8+ mixed
    letters and digits. Returns (still hidden, now secrets)."""
    if not unread:
        return [], []
    side = 20
    todo = [u for u in unread[:REREAD_MAX] if u.box[2] - u.box[0] + 2 * side <= TILE_MAX]
    hidden = [u for u in unread if not any(u is t for t in todo)]  # too many or too wide: stays hidden, unread
    secrets: list[Hit] = []
    deadline = time.perf_counter() + timeout
    # stack the crops in batches that each fit the OCR engine
    batches, cur, height = [], [], 0
    for u in todo:
        step = 3 * max(4, u.box[3] - u.box[1])
        if cur and height + step > TILE_MAX:
            batches.append(cur)
            cur, height = [], 0
        cur.append(u)
        height += step
    if cur:
        batches.append(cur)
    for batch in batches:
        left = deadline - time.perf_counter()
        if left <= 0:
            raise OcrError("OCR timed out on the second look")
        h_, s_ = _second_look_batch(gray, worker, patterns, batch, side, left)
        hidden += h_
        secrets += s_
    return hidden, secrets


def _second_look_batch(gray, worker, patterns, todo, side, timeout):
    crops, y = [], 0
    for u in todo:
        x0, y0, x1, y1 = u.box
        m = max(4, y1 - y0)
        crops.append((u, gray[y0:y1, x0:x1], y + m))
        y += (y1 - y0) + 2 * m
    canvas = np.empty((y, max(c.shape[1] for _u, c, _t in crops) + 2 * side), np.uint8)
    for u, c, top in crops:
        x0, y0, x1, y1 = u.box
        m = max(4, y1 - y0)
        canvas[top - m:top + c.shape[0] + m, :] = int(np.median(gray[y0:y1, max(0, x0 - 3):x1 + 3]))
        canvas[top:top + c.shape[0], side:side + c.shape[1]] = c
    lines = worker.recognize(canvas, timeout=timeout)
    hidden, secrets = [], []
    for u, c, top in crops:
        h = c.shape[0]
        mine = [ln for ln in lines if top - h / 2 <= ln[0][2] + ln[0][4] / 2 < top + 1.5 * h]
        text = " ".join(t for line in mine for t, *_r in line).translate(_CONFUSABLES)
        found = find_hits(mine, patterns)
        enough = len(text.replace(" ", "")) >= REREAD_MIN_CHARS * c.shape[1] / (0.5 * max(1, h))
        if any(f.kind in ("token", "block") for f in found):
            secrets.append(Hit(found[0].pattern, u.box, u.line_h, "token"))
        elif found or not enough or _looks_random(text):
            hidden.append(u)
    return hidden, secrets


# ── the whole job ────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Redaction:
    count: int                       # distinct secrets found by the patterns
    random: int = 0                  # long random-looking strings, or ones OCR could not read: hidden to be safe
    boxes: list[Box] = field(default_factory=list)   # what was blacked out
    patterns: list[str] = field(default_factory=list)  # which pattern ids fired (for the log; never the text)
    ocr_ms: float = 0.0
    groups: list = field(default_factory=list)  # (box, kinds) per counted item: for tests and diagnostics


def upscale(gray: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return gray
    from PIL import Image
    h, w = gray.shape
    return np.asarray(Image.fromarray(gray).resize((w * factor, h * factor), Image.BICUBIC))


OCR_MAX_DIM = 10000            # Windows.Media.Ocr refuses larger images
TILE_MAX = 9000                # tile size after zoom, kept under the engine's limit
TILE_OVERLAP = 1200            # px at 1x: wider than any key on screen, so a key cut by one tile is whole in the next


def tiles(gray: np.ndarray, zoom: int):
    """Pieces that cover the whole image, each small enough for the OCR engine at this zoom, overlapping by at
    least TILE_OVERLAP. Yields (x, y, tile, core, inner_edges). The cores split the image at the middle of each
    overlap, so every pixel belongs to exactly one core; a word is taken only from the tile whose core holds its
    centre, which is the tile that holds the whole word (any word up to TILE_OVERLAP wide). inner_edges are this
    tile's edges that are not the image's edge: a reading touching one was cut off and is never trusted."""
    H, W = gray.shape
    size = TILE_MAX // zoom
    if size <= TILE_OVERLAP:
        raise OcrError(f"zoom {zoom} leaves no room for tiles")

    def spans(n):
        if n <= size:
            return [(0, n, 0, n)]
        starts, pos = [], 0
        while pos + size < n:
            starts.append(pos)
            pos += size - TILE_OVERLAP
        starts.append(n - size)
        out = []
        for i, s in enumerate(starts):
            lo = 0 if i == 0 else (s + starts[i - 1] + size) // 2
            hi = n if i == len(starts) - 1 else (starts[i + 1] + s + size) // 2
            out.append((s, s + size, lo, hi))
        return out

    for y0, y1, cy0, cy1 in spans(H):
        for x0, x1, cx0, cx1 in spans(W):
            inner = (x0 if x0 > 0 else None, y0 if y0 > 0 else None,
                     x1 if x1 < W else None, y1 if y1 < H else None)
            yield x0, y0, np.ascontiguousarray(gray[y0:y1, x0:x1]), (cx0, cy0, cx1, cy1), inner


def analyze(gray: np.ndarray, worker: OcrWorker, patterns: list[Pattern], scales=(1, 2),
            timeout: float = 4.0) -> Redaction:
    """OCR at each scale, match, merge. Raises OcrError if any pass fails (callers fail closed)."""
    H, W = gray.shape
    t0 = time.perf_counter()
    hits: list[Hit] = []
    words: list[Box] = []
    deadline = time.perf_counter() + timeout
    for s in scales:
        # every pass covers the whole screen: a screen too big for the OCR engine at this zoom is read in
        # overlapping tiles (never skipped: a skipped pass would be a silent hole in the check)
        lines, dropped = [], []
        for tx, ty, tile, (cx0, cy0, cx1, cy1), inner in tiles(gray, s):
            left = deadline - time.perf_counter()
            if left <= 0:
                raise OcrError(f"OCR timed out after {timeout:.1f} s")
            for line in worker.recognize(upscale(tile, s), timeout=left):
                # each word is taken from exactly one tile: the one whose core holds its centre (and so holds the
                # whole word); a reading that touches an inner tile edge was cut off. Nothing is dropped without a
                # replacement: see `dropped` below.
                kept = []
                for t, x, y, w, h in line:
                    bx0, by0 = x / s + tx, y / s + ty
                    bx1, by1 = (x + w) / s + tx, (y + h) / s + ty
                    xc, yc = (bx0 + bx1) / 2, (by0 + by1) / 2
                    ix0, iy0, ix1, iy1 = inner
                    cut = (ix0 is not None and bx0 <= ix0 + 2) or (iy0 is not None and by0 <= iy0 + 2) or \
                        (ix1 is not None and bx1 >= ix1 - 2) or (iy1 is not None and by1 >= iy1 - 2)
                    if cut or not (cx0 <= xc < cx1 and cy0 <= yc < cy1):
                        dropped.append((int(bx0), int(by0), int(math.ceil(bx1)), int(math.ceil(by1))))
                        continue
                    kept.append((t, x + tx * s, y + ty * s, w, h))
                if kept:
                    lines.append(kept)
        kept_boxes = [(int(x / s), int(y / s), int(math.ceil((x + w) / s)), int(math.ceil((y + h) / s)))
                      for line in lines for _t, x, y, w, h in line]
        words.extend(kept_boxes)
        # a dropped reading (cut at a seam, or a duplicate) must have a whole reading from another tile covering
        # it; if none does (a word wider than the tile overlap), it is blacked out unread
        for d in dropped:
            span = np.zeros(d[2] - d[0], bool)
            for k in kept_boxes:
                if min(d[3], k[3]) - max(d[1], k[1]) >= 0.5 * (d[3] - d[1]) and k[0] < d[2] and k[2] > d[0]:
                    span[max(0, k[0] - d[0]):max(0, min(d[2], k[2]) - d[0])] = True
            if span.size and span.mean() < 0.9:
                hits.append(Hit("cut_at_tile_seam", d, max(1, d[3] - d[1]), "unread"))
        # rows put back together across tiles before ANY matching (a key header cut by a seam is still one row)
        rows = merge_rows(lines)
        found = find_hits(lines, patterns, s) + block_hits(rows, patterns, s, H)
        # continuations from the raw lines AND the merged rows: merging can glue a wrapped piece to an unrelated
        # neighbour (too many words for the rule), and splitting can cut it at a seam; either one catching it is enough
        found += continuation_hits(lines, found, s) + continuation_hits(rows, found, s)
        hits.extend(found)
    # strings OCR skipped: those touching a found secret are hidden with it; the rest get a second, closer look
    unread = unread_hits(gray, words)
    big, covers = big_unread_hits(gray, words, unread + hits)
    unread += big
    # hidden without a second look: what touches a found secret, and a box that already holds more than one row
    beside = [u for u in unread if u.keep or any(_overlap(u.box, h.box) for h in hits)]
    alone = [u for u in unread if not (u.keep or any(_overlap(u.box, h.box) for h in hits))]
    left = deadline - time.perf_counter()
    if alone and left <= 0:
        raise OcrError(f"OCR timed out after {timeout:.1f} s")
    alone, late_secrets = second_look(gray, worker, patterns, alone, left)
    hits.extend(late_secrets)
    unread = beside + alone
    ocr_ms = (time.perf_counter() - t0) * 1000

    # widen token hits over their ink run, pad everything
    tight, boxes = [], []
    for hh in hits:
        b = widen_to_ink(gray, hh.box, hh.line_h) if hh.kind in ("token", "continuation", "continuation_loose",
                                                                  "random", "unread") \
            else hh.box
        tight.append(b)
        boxes.append(pad(b, hh.line_h, W, H))

    # every skipped string stays blacked out; one touching a secret is counted with it (see the grouping below)
    # every hidden box is widened along its row's ink and padded; a keep hit too (widening only grows a box sideways
    # over ink that continues the string, so nothing it held is ever shown)
    unread = [Hit(u.pattern, widen_to_ink(gray, u.box, u.line_h), u.line_h, u.kind, u.words, u.keep) for u in unread]
    boxes += [pad(u.box, u.line_h, W, H) for u in unread]
    # sightings of what was already found another way: blacked out whole, counted nowhere. The net sees the string
    # on shrunken copies, so its box can stop short of the string's real ends: each cover is widened along its row's
    # ink and padded like any other hidden box.
    for c in covers:
        ch = max(1, c[3] - c[1])
        boxes.append(pad(widen_to_ink(gray, c, ch), ch, W, H))

    # count: everything hidden on one text row with less than ~2 characters between is one item (a secret seen by
    # several patterns or passes, or a string the pixels split at ':' or '='); a wrapped continuation never counts
    items = [(b, hh.line_h, hh.kind) for b, hh in zip(tight, hits)] + [(u.box, u.line_h, "unread") for u in unread]
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i][0], items[j][0]
            lh = max(items[i][1], items[j][1])
            vert = min(a[3], b[3]) - max(a[1], b[1])
            gap = max(a[0], b[0]) - min(a[2], b[2])
            cont = "continuation" in (items[i][2], items[j][2])
            same_row = vert >= 0.5 * min(a[3] - a[1], b[3] - b[1]) and gap <= 1.0 * lh
            if same_row or (cont and _overlap(a, b, slack=lh)) or _overlap(a, b):
                parent[find(i)] = find(j)
    groups: dict[int, set] = {}
    where: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), set()).add(items[i][2])
        where.setdefault(find(i), []).append(items[i][0])
    count = sum(1 for kinds in groups.values() if kinds & {"token", "block"})
    loose_kinds = {"unread", "random", "continuation_loose"}
    n_random = sum(1 for kinds in groups.values() if not kinds & {"token", "block"} and kinds & loose_kinds)
    fired = sorted({h.pattern for h in hits} | ({"unreadable_long_string"} if unread else set()))
    return Redaction(count, n_random, boxes, fired, ocr_ms,
                     [(_union(where[k]), sorted(kinds)) for k, kinds in groups.items()])


def apply_boxes(pixels: np.ndarray, boxes: list[Box]) -> None:
    """Black out boxes in place. pixels: HxWxC (any channel order) or HxW."""
    for x0, y0, x1, y1 in boxes:
        pixels[y0:y1, x0:x1, ...] = 0

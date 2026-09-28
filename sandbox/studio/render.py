#!/usr/bin/env python3
"""render.py <capture-dir> <out.mp4> [--fps 30] [--bg mesh] [--character path.svg]
                                    [--frame-at SEC] [--no-vo] [--no-sfx] [--accent r,g,b]

Synthesizes a smooth 1080x1920 product demo from the keyframes and step log that capture.py
wrote, the voice clips and word timings that voice.py wrote, and an optional character that
narrates from the card's bottom-right corner. Nothing depends on machine speed: every frame is
a pure function of the timeline, so the output is constant-rate by construction.
"""

from __future__ import annotations

import colorsys
import json
import math
import os
import random
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from studio_contracts import (  # noqa: E402
    CHARACTER_MOUTH_IDS,
    character_variant_svg,
    validate_character_svg,
)

W, H = 1080, 1920
FPS = 30
CARD_W, CARD_H = 846, 1504  # device card on the canvas (9:16)
CARD_X, CARD_Y = (W - CARD_W) // 2, 340
RADIUS = 56
FONT_XB = os.path.join(HERE, "fonts", "Montserrat-ExtraBold.ttf")
YELLOW = (255, 210, 31)
MASCOT_PX = 620  # rendered width on the 1080 canvas; the narrator straddles the card corner
BACKDROPS = ("mesh", "brand", "aurora", "paper", "bold", "blur")
RESVG = os.environ.get("TIN_STUDIO_RESVG") or shutil.which("resvg") or os.path.join(HERE, "resvg")


def arg_val(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def ease_in_out_cubic(t):
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def ease_out_cubic(t):
    return 1 - pow(1 - t, 3)


def ease_in_out_quint(t):
    return 16 * t**5 if t < 0.5 else 1 - pow(-2 * t + 2, 5) / 2


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def lerp(a, b, t):
    return a + (b - a) * t


def extract_accent(images):
    """Most frequent saturated color across keyframes (quantized). Falls back to iMessage blue."""
    from collections import Counter

    c = Counter()
    for im in images:
        small = im.resize((90, 160), Image.BILINEAR)
        for r, g, b in small.getdata():
            h, sat, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if sat > 0.45 and 0.25 < v < 0.95:
                c[(round(h * 24) / 24, round(sat * 4) / 4, round(v * 4) / 4)] += 1
    if not c:
        return (10, 132, 255)
    h, sat, v = c.most_common(1)[0][0]
    r, g, b = colorsys.hsv_to_rgb(h, min(1, sat + 0.1), v)
    return (int(r * 255), int(g * 255), int(b * 255))


def shift(rgb, dh=0.0, ds=0.0, dv=0.0):
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
    r, g, b = colorsys.hsv_to_rgb((h + dh) % 1, clamp(s + ds), clamp(v + dv))
    return (int(r * 255), int(g * 255), int(b * 255))


def linear_gradient(c0, c1, angle=0.55):
    g = Image.new("RGB", (W // 8, H // 8))
    px = g.load()
    w, h = g.size
    for yy in range(h):
        for xx in range(w):
            t = clamp(xx / w * (1 - angle) + yy / h * angle)
            px[xx, yy] = tuple(int(lerp(a, b, t)) for a, b in zip(c0, c1, strict=True))
    return g.resize((W, H), Image.BICUBIC)


def mesh_gradient(base, colors, seed=7):
    """Soft overlapping color blobs, heavily blurred (Screen Studio / wallpaper look)."""
    rnd = random.Random(seed)  # noqa: S311 - deterministic art, not security
    g = Image.new("RGB", (W // 8, H // 8), base)
    d = ImageDraw.Draw(g)
    w, h = g.size
    for c in colors:
        for _ in range(2):
            cx, cy = rnd.uniform(-0.2, 1.2) * w, rnd.uniform(-0.2, 1.2) * h
            rx, ry = rnd.uniform(0.35, 0.7) * w, rnd.uniform(0.3, 0.6) * h
            d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=c)
    g = g.filter(ImageFilter.GaussianBlur(28))
    return g.resize((W, H), Image.BICUBIC)


def grain(im, amount=6):
    n = Image.effect_noise((W // 2, H // 2), amount).resize((W, H), Image.BILINEAR).convert("L")
    return Image.blend(im, Image.merge("RGB", (n, n, n)), 0.06)


def backdrop(preset, accent, keyframe_blur=None):
    if preset == "blur":
        return keyframe_blur
    if preset == "brand":
        return linear_gradient(shift(accent, dv=-0.05), shift(accent, dh=0.06, ds=-0.1, dv=-0.35))
    if preset == "mesh":
        return grain(
            mesh_gradient(
                shift(accent, dv=-0.15),
                [
                    shift(accent, dh=0.08, ds=-0.2, dv=0.05),
                    shift(accent, dh=-0.08, dv=0.1),
                    shift(accent, dh=0.5, ds=-0.35, dv=0.1),
                ],
            )
        )
    if preset == "aurora":
        return grain(
            mesh_gradient(
                (14, 14, 20),
                [
                    shift(accent, ds=0.1, dv=-0.35),
                    shift(accent, dh=0.45, ds=-0.2, dv=-0.45),
                    (24, 22, 34),
                ],
            ),
            8,
        )
    if preset == "paper":
        return grain(linear_gradient((242, 238, 228), (228, 224, 212)), 5)
    if preset == "bold":
        return linear_gradient(shift(accent, dv=0.02), shift(accent, dv=-0.12), 0.9)
    raise SystemExit(f"unknown --bg preset {preset}; use one of {', '.join(BACKDROPS)}")


class Assets:
    def __init__(self, d):
        self.d = d
        self.cache = {}
        self.blur = {}

    def key(self, f):
        if f not in self.cache:
            im = Image.open(os.path.join(self.d, f)).convert("RGB")
            if "full" in os.path.basename(f):
                if im.width != W:
                    im = im.resize((W, int(im.height * W / im.width)), Image.BICUBIC)
            elif (im.width, im.height) != (W, H):
                im = im.resize((W, H), Image.BICUBIC)
            self.cache[f] = im
        return self.cache[f]

    def bg(self, f):
        if f not in self.blur:
            im = (
                self.key(f)
                .resize((W // 8, H // 8), Image.BILINEAR)
                .filter(ImageFilter.GaussianBlur(6))
            )
            im = im.resize((W, H), Image.BILINEAR)
            if not hasattr(self, "grad"):
                self.grad = linear_gradient((30, 28, 44), (14, 16, 26))
            self.blur[f] = Image.blend(im, self.grad, 0.68)
        return self.blur[f]


def rounded_mask(w, h, r):
    m = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=r * 2, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def shadow_layer():
    pad = 120
    m = Image.new("L", (CARD_W + pad * 2, CARD_H + pad * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (pad, pad + 30, pad + CARD_W, pad + CARD_H + 30), radius=RADIUS, fill=150
    )
    m = m.filter(ImageFilter.GaussianBlur(40))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", m.size, (0, 0, 0, 255))
    sh.putalpha(m)
    layer.alpha_composite(sh, (CARD_X - pad, CARD_Y - pad))
    return layer


def sprite_circle(r, fill, outline=None, ow=0, ss=4):
    s = int((r + ow + 2) * 2 * ss)
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = s / 2
    d.ellipse(
        (c - r * ss, c - r * ss, c + r * ss, c + r * ss),
        fill=fill,
        outline=outline,
        width=int(ow * ss),
    )
    return im.resize((s // ss, s // ss), Image.LANCZOS)


def text_sprite(
    text, size, fill=(255, 255, 255), stroke=8, hl=YELLOW, maxw=W - 160, upper=False, line_gap=1.08
):
    """CapCut-style caption: bold white with a black stroke; *word* gets a yellow box."""
    font = ImageFont.truetype(FONT_XB, size)
    words = text.upper().split() if upper else text.split()
    lines, cur = [], []

    def width(ws):
        return ImageDraw.Draw(Image.new("L", (1, 1))).textlength(
            " ".join(w.strip("*") for w in ws), font=font
        )

    for w in words:
        if cur and width(cur + [w]) > maxw:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    lh = int(size * line_gap)
    pad = stroke * 2 + 24
    im = Image.new("RGBA", (W, lh * len(lines) + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    y = pad
    for ln in lines:
        plain = " ".join(w.strip("*") for w in ln)
        lw = d.textlength(plain, font=font)
        x = (W - lw) / 2
        cx = x
        for w in ln:
            ww = d.textlength(w.strip("*"), font=font)
            if w.startswith("*"):
                d.rounded_rectangle(
                    (cx - 10, y - 4, cx + ww + 10, y + size * 1.02 + 4), radius=12, fill=hl
                )
            cx += ww + d.textlength(" ", font=font)
        cx = x
        for w in ln:
            t = w.strip("*")
            ww = d.textlength(t, font=font)
            if w.startswith("*"):
                d.text((cx, y), t, font=font, fill=(20, 20, 20))
            else:
                d.text((cx, y), t, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
            cx += ww + d.textlength(" ", font=font)
        y += lh
    return im


CONTENT_KINDS = ("goto", "scroll", "click", "hold", "focus")


def build_timeline(log, vo=None):
    """Steps -> segments (start, dur, kind). With a voiceover each content step's hold stretches
    so its clip finishes before the next step."""
    segs = []
    t = 0.0
    steps = log["steps"]
    clips = {c["step_idx"]: c for c in (vo or {}).get("clips", [])}
    audio = []

    def hold_for(s, lead):
        base = s.get("hold", 1200) / 1000
        c = clips.get(s["idx"])
        if not c:
            return base
        return max(base, c["duration"] + 0.45 - lead)

    for i, s in enumerate(steps):
        kind = s["kind"]
        step_start = t
        if kind == "goto" and i == 0:
            segs.append(dict(kind="enter", start=t, dur=0.55, key=s))
            t += 0.55
            h = hold_for(s, 0.55)
            segs.append(dict(kind="hold", start=t, dur=h, key=s))
            t += h
        elif kind == "goto":
            segs.append(dict(kind="cut", start=t, dur=0.3, prev=steps[i - 1], key=s))
            t += 0.3
            h = hold_for(s, 0.3)
            segs.append(dict(kind="hold", start=t, dur=h, key=s))
            t += h
        elif kind == "scroll":
            prev = steps[i - 1]
            dur = s.get("duration", 900) / 1000
            segs.append(dict(kind="scroll", start=t, dur=dur, prev=prev, key=s))
            t += dur
            segs.append(dict(kind="settle", start=t, dur=0.18, prev=None, key=s))
            t += 0.18
            h = hold_for(s, dur + 0.18)
            segs.append(dict(kind="hold", start=t, dur=h, key=s))
            t += h
        elif kind == "pre-click":
            prev = steps[i - 1]
            if s.get("fullpage") and "scrollFrom" in s:
                dur = s.get("duration", 800) / 1000
                segs.append(dict(kind="scroll", start=t, dur=dur, prev=prev, key=s))
                t += dur
                segs.append(dict(kind="settle", start=t, dur=0.18, prev=None, key=s))
                t += 0.18
            elif (
                prev.get("url") == s.get("url")
                and abs(prev.get("scrollY", 0) - s.get("scrollY", 0)) > 4
            ):
                segs.append(dict(kind="cut", start=t, dur=0.35, prev=prev, key=s))
                t += 0.35
            elif prev.get("url") != s.get("url"):
                segs.append(dict(kind="cut", start=t, dur=0.3, prev=prev, key=s))
                t += 0.3
            segs.append(dict(kind="glide", start=t, dur=0.6, key=s, target=s["target"]))
            t += 0.6
        elif kind == "click":
            prev = steps[i - 1]
            zoom = s.get("zoom", 1.12)
            segs.append(
                dict(kind="tap", start=t, dur=0.42, prev=prev, key=s, pt=s["click"], zoom=zoom)
            )
            t += 0.42
            segs.append(
                dict(kind="reveal", start=t, dur=0.55, prev=prev, key=s, pt=s["click"], zoom=zoom)
            )
            t += 0.55
            step_start = t - 0.55
            h = hold_for(s, 0.55)
            segs.append(dict(kind="hold", start=t, dur=h, key=s))
            t += h
        elif kind in ("focus", "typing", "hold"):
            prev = steps[i - 1]
            segs.append(dict(kind="cut", start=t, dur=0.12, prev=prev, key=s))
            t += 0.12
            hold = hold_for(s, 0.12) if kind in CONTENT_KINDS else s.get("hold", 300) / 1000
            segs.append(dict(kind="hold", start=t, dur=hold, key=s))
            t += hold
        if s["idx"] in clips and kind in CONTENT_KINDS:
            audio.append((step_start + 0.12, clips[s["idx"]]))
    return segs, t, audio


def make_sfx(d):
    """Synthesize the two UI sounds with ffmpeg so the renderer needs no audio assets."""
    sfx = {"whoosh": os.path.join(d, "sfx_whoosh.wav"), "tap": os.path.join(d, "sfx_tap.wav")}
    if not os.path.exists(sfx["whoosh"]):
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:r=48000:d=0.55:seed=7",
                "-filter_complex",
                "[0:a]asplit=2[lo][hi];"
                "[lo]highpass=f=250,lowpass=f=1200,afade=t=in:d=0.12,afade=t=out:st=0.18:d=0.3,volume=1.0[l];"
                "[hi]highpass=f=900,lowpass=f=4200,afade=t=in:st=0.08:d=0.2,afade=t=out:st=0.3:d=0.25,volume=0.7[h];"
                "[l][h]amix=inputs=2:normalize=0,volume=0dB[out]",
                "-map",
                "[out]",
                sfx["whoosh"],
            ],
            check=True,
        )
    if not os.path.exists(sfx["tap"]):
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=0.9*sin(2*PI*1750*t)*exp(-t*70)+0.4*sin(2*PI*620*t)*exp(-t*40):s=48000:d=0.12",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=white:r=48000:d=0.02:seed=3",
                "-filter_complex",
                "[1:a]highpass=f=2500,afade=t=out:d=0.02,volume=0.35[n];[0:a][n]amix=inputs=2:normalize=0,volume=-8dB[out]",
                "-map",
                "[out]",
                sfx["tap"],
            ],
            check=True,
        )
    return sfx


def rasterize_character(svg_path, cache_dir):
    """Character states -> transparent PNG sprites through resvg (Pillow cannot render SVG)."""
    if not os.path.exists(RESVG):
        raise SystemExit("resvg is not available; set TIN_STUDIO_RESVG")
    content = open(svg_path, "rb").read()
    summary = validate_character_svg(content)
    os.makedirs(cache_dir, exist_ok=True)
    sprites = {}
    for level, mouth in enumerate(CHARACTER_MOUTH_IDS):
        variants = {
            f"talk_m{level}": dict(mouth=mouth, eyes="eyes-open"),
            f"blink_m{level}": dict(mouth=mouth, eyes="eyes-closed"),
        }
        if "expr-happy" in summary.optional_states:
            variants[f"happy_m{level}"] = dict(
                mouth=mouth, eyes="eyes-open", expression="expr-happy"
            )
        for name, kw in variants.items():
            png = os.path.join(cache_dir, f"{name}.png")
            if not os.path.exists(png):
                svg_variant = os.path.join(cache_dir, f"{name}.svg")
                open(svg_variant, "wb").write(character_variant_svg(content, **kw))
                subprocess.run(
                    [RESVG, "--width", "1024", svg_variant, png],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            sprites[name] = (
                Image.open(png).convert("RGBA").resize((MASCOT_PX, MASCOT_PX), Image.LANCZOS)
            )
    return sprites


def speech_envelope(d, audio, total, fps):
    """Per-frame speech energy 0..1 from the voice clips (RMS per 1/fps window)."""
    import numpy as np

    env = np.zeros(int(total * fps) + 2)
    for start, clip in audio:
        raw = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                os.path.join(d, clip["file"]),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-",
            ]
        )
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        win = 16000 // fps
        n = len(x) // win
        if n == 0:
            continue
        r = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
        if r.max() > 0:
            r = r / max(np.percentile(r, 95), 1e-6)
        f0 = int(start * fps)
        m = min(n, len(env) - f0)
        env[f0 : f0 + m] = np.maximum(env[f0 : f0 + m], r[:m])
    out = env.copy()
    for i in range(1, len(env)):
        out[i] = max(env[i], out[i - 1] * 0.8)
    return np.clip(out, 0, 1)


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    d = sys.argv[1]
    out = sys.argv[2]
    fps = int(arg_val("--fps", FPS))
    preset = arg_val("--bg", "mesh")
    frame_at = float(arg_val("--frame-at")) if "--frame-at" in sys.argv else None
    log = json.load(open(os.path.join(d, "log.json")))
    dpr = log["viewport"]["dpr"]
    assets = Assets(d)
    accent = (
        tuple(int(x) for x in arg_val("--accent").split(","))
        if "--accent" in sys.argv
        else extract_accent([assets.key(st["file"]) for st in log["steps"]])
    )
    print(f"backdrop preset={preset} accent={accent}", file=sys.stderr)
    character = arg_val("--character")
    sprites = (
        rasterize_character(character, os.path.join(d, "character_cache")) if character else {}
    )
    envelope = None
    static_bg = None if preset == "blur" else backdrop(preset, accent)
    real_bg = assets.bg
    assets.bg = (lambda f: static_bg) if static_bg is not None else real_bg
    vo_path = os.path.join(d, "vo.json")
    vo = json.load(open(vo_path)) if os.path.exists(vo_path) and "--no-vo" not in sys.argv else None
    segs, total, audio = build_timeline(log, vo)
    n = int(total * fps)
    if sprites and audio:
        envelope = speech_envelope(d, audio, total, fps)
    pages = []
    for start, clip in audio:
        ws = [dict(w, start=w["start"] + start, end=w["end"] + start) for w in clip["words"]]
        page = []
        for w in ws:
            if page and (
                len(page) >= 3
                or w["start"] - page[0]["start"] > 1.1
                or w["start"] - page[-1]["end"] > 0.5
            ):
                pages.append(page)
                page = []
            page.append(w)
        if page:
            pages.append(page)
    for i, pg in enumerate(pages):
        pg_end = pages[i + 1][0]["start"] if i + 1 < len(pages) else pg[-1]["end"] + 0.6
        pg_end = min(pg_end, pg[-1]["end"] + 0.6)
        pages[i] = {"start": pg[0]["start"], "end": pg_end, "words": pg}
    page_cache = {}

    def page_sprite(pg, active):
        k = (id(pg), active)
        if k not in page_cache:
            txt = " ".join(
                ("*" + w["w"] + "*") if j == active else w["w"] for j, w in enumerate(pg["words"])
            )
            page_cache[k] = text_sprite(txt, 80, upper=True, stroke=10, maxw=CARD_W + 40)
        return page_cache[k]

    sfx_events = (
        []
        if "--no-sfx" in sys.argv
        else (
            [("whoosh", sg["start"]) for sg in segs if sg["kind"] == "scroll"]
            + [("tap", sg["start"] + 0.06) for sg in segs if sg["kind"] == "tap"]
        )
    )
    sfx_files = make_sfx(d) if sfx_events else {}
    video_out = out if not (audio or sfx_events) else out + ".video.mp4"
    mask = rounded_mask(CARD_W, CARD_H, RADIUS)
    shadow = shadow_layer()
    border = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        (CARD_X - 2, CARD_Y - 2, CARD_X + CARD_W + 1, CARD_Y + CARD_H + 1),
        radius=RADIUS + 2,
        outline=(255, 255, 255, 70),
        width=3,
    )
    pointer = sprite_circle(24, (255, 255, 255, 215), (20, 20, 20, 200), 3)
    pointer_small = sprite_circle(17, (255, 255, 255, 240), (20, 20, 20, 220), 3)
    hook = (
        text_sprite(log["hook"], 92, upper=True, stroke=10, maxw=W - 120)
        if log.get("hook")
        else None
    )
    captions = {}

    def caption(txt):
        if txt not in captions:
            captions[txt] = text_sprite(txt, 58, upper=True, stroke=8, maxw=CARD_W - 60)
        return captions[txt]

    ff = (
        None
        if frame_at is not None
        else subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{W}x{H}",
                "-r",
                str(fps),
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                video_out,
            ],
            stdin=subprocess.PIPE,
        )
    )

    def screen_for(key, scroll_css=None):
        if scroll_css is not None and key.get("fullpage"):
            full = assets.key(key["fullpage"]["file"])
            y = int(round(scroll_css * dpr))
            y = max(0, min(full.height - H, y))
            return full.crop((0, y, W, y + H))
        return assets.key(key["file"])

    def compose(
        screen,
        bg,
        zoom=1.0,
        focal=None,
        cur=None,
        tap_r=0.0,
        cur_small=False,
        cap=None,
        cap_scale=1.0,
        hook_alpha=0.0,
        enter=1.0,
        cur_alpha=1.0,
    ):
        frame = bg.copy()
        if zoom > 1.001 and focal is not None:
            cw, ch = W / zoom, H / zoom
            fx, fy = focal
            x0 = clamp(fx - cw / 2, 0, W - cw)
            y0 = clamp(fy - ch / 2, 0, H - ch)
            src = screen.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch)))
        else:
            src = screen
        s = enter
        cw_, ch_ = int(CARD_W * s), int(CARD_H * s)
        card = src.resize((cw_, ch_), Image.BICUBIC)
        m = mask if s == 1.0 else mask.resize((cw_, ch_), Image.BILINEAR)
        cx, cy = CARD_X + (CARD_W - cw_) // 2, CARD_Y + (CARD_H - ch_) // 2
        if s == 1.0:
            frame.paste(shadow, (0, 0), shadow)
        frame.paste(card, (cx, cy), m)
        if s == 1.0:
            frame.paste(border, (0, 0), border)
        if cur is not None:
            px, py = cur
            if tap_r > 0:
                ring = sprite_circle(
                    int(tap_r), (255, 255, 255, 0), (255, 255, 255, int(200 * (1 - tap_r / 110))), 6
                )
                frame.paste(ring, (int(px - ring.width / 2), int(py - ring.height / 2)), ring)
            sp = pointer_small if cur_small else pointer
            if cur_alpha > 0.01:
                a = sp.split()[3].point(lambda v: int(v * cur_alpha)) if cur_alpha < 1 else sp
                frame.paste(sp, (int(px - sp.width / 2), int(py - sp.height / 2)), a)
        if cap is not None and cap_scale > 0 and not sprites:
            sp = (
                cap
                if cap_scale == 1.0
                else cap.resize(
                    (max(1, int(cap.width * cap_scale)), max(1, int(cap.height * cap_scale))),
                    Image.BILINEAR,
                )
            )
            frame.paste(sp, ((W - sp.width) // 2, 1400 - sp.height // 2), sp)
        if hook is not None and hook_alpha > 0:
            a = hook.split()[3].point(lambda v: int(v * hook_alpha))
            frame.paste(hook, ((W - hook.width) // 2, 185 - hook.height // 2), a)
        return frame

    def to_canvas(pt_css):
        return (CARD_X + pt_css["x"] * dpr * CARD_W / W, CARD_Y + pt_css["y"] * dpr * CARD_H / H)

    cur_pos = None
    mstate = {"expr": None, "since": 0.0, "lvl": 0, "mouth_since": -99}
    hook_end = 2.6
    last_cap_text = None
    cap_start = 0.0
    last_step_idx = log["steps"][-1]["idx"]
    frames_iter = [int(frame_at * fps)] if frame_at is not None else range(n)
    for fi in frames_iter:
        t = fi / fps
        seg = next((s for s in segs if s["start"] <= t < s["start"] + s["dur"]), segs[-1])
        lt = (t - seg["start"]) / max(seg["dur"], 1e-6)
        key = seg["key"]
        kind = seg["kind"]
        cap = None
        cap_scale = 1.0
        if pages:
            pg = next((p for p in pages if p["start"] <= t < p["end"]), None)
            if pg is not None:
                active = max(
                    [j for j, w in enumerate(pg["words"]) if w["start"] <= t + 0.03], default=0
                )
                cap = page_sprite(pg, active)
                if pg["start"] != last_cap_text:
                    last_cap_text = pg["start"]
                    cap_start = t
                cap_scale = lerp(0.86, 1.0, ease_out_cubic(clamp((t - cap_start) / 0.12)))
        else:
            cap_text = key.get("caption")
            if cap_text != last_cap_text:
                last_cap_text = cap_text
                cap_start = t
            cap_scale = (
                1.0
                if cap_text is None
                else lerp(0.82, 1.0, ease_out_cubic(clamp((t - cap_start) / 0.16)))
            )
            cap = caption(cap_text) if cap_text else None
        hook_alpha = 1.0 if t < hook_end - 0.35 else clamp((hook_end - t) / 0.35)
        if t < 0.35:
            hook_alpha = min(hook_alpha, t / 0.35)
        bg = assets.bg(key["file"])
        if kind == "enter":
            e = ease_out_cubic(lt)
            frame = compose(
                screen_for(key), bg, enter=lerp(0.94, 1.0, e), hook_alpha=hook_alpha * e
            )
            frame = Image.blend(bg, frame, e)
        elif kind == "hold":
            idle_a = clamp(1 - (t - seg["start"] - 0.35) / 0.4)
            frame = compose(
                screen_for(key),
                bg,
                cur=cur_pos,
                cur_alpha=idle_a,
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
            if idle_a <= 0:
                cur_pos = None
        elif kind == "cut":
            e = ease_in_out_cubic(lt)
            screen = Image.blend(screen_for(seg["prev"]), screen_for(key), e)
            bgm = Image.blend(assets.bg(seg["prev"]["file"]), bg, e)
            frame = compose(
                screen, bgm, cur=cur_pos, cap=cap, cap_scale=cap_scale, hook_alpha=hook_alpha
            )
        elif kind == "scroll":
            e = ease_in_out_quint(lt)
            y = lerp(key["scrollFrom"], key["scrollY"], e)
            cur_pos = None
            frame = compose(
                screen_for(key, scroll_css=y),
                bg,
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
        elif kind == "settle":
            e = ease_out_cubic(lt)
            screen = Image.blend(screen_for(key, scroll_css=key["scrollY"]), screen_for(key), e)
            frame = compose(screen, bg, cap=cap, cap_scale=cap_scale, hook_alpha=hook_alpha)
        elif kind == "glide":
            tgt = to_canvas(seg["target"])
            if cur_pos is None:
                cur_pos = (tgt[0] + 160, tgt[1] + 260)
            if lt == 0 or "glide_from" not in seg:
                seg["glide_from"] = cur_pos
            e = ease_in_out_cubic(lt)
            f = seg["glide_from"]
            arc = -70 * math.sin(lt * math.pi)
            pos = (lerp(f[0], tgt[0], e), lerp(f[1], tgt[1], e) + arc)
            frame = compose(
                screen_for(key),
                bg,
                cur=pos,
                cur_alpha=clamp(lt / 0.25),
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
            if lt >= 0.98:
                cur_pos = tgt
        elif kind == "tap":
            pt = to_canvas(seg["pt"])
            cur_pos = pt
            z = lerp(1.0, seg["zoom"], ease_out_cubic(clamp(lt / 0.7)))
            tap_r = 110 * clamp(lt / 0.9) if lt < 0.9 else 0
            focal = (seg["pt"]["x"] * dpr, seg["pt"]["y"] * dpr)
            frame = compose(
                screen_for(seg["prev"]),
                assets.bg(seg["prev"]["file"]),
                zoom=z,
                focal=focal,
                cur=pt,
                tap_r=tap_r,
                cur_small=lt < 0.35,
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
        elif kind == "reveal":
            pt = to_canvas(seg["pt"])
            e = ease_in_out_cubic(lt)
            z = lerp(seg["zoom"], 1.0, e)
            screen = Image.blend(screen_for(seg["prev"]), screen_for(key), clamp(lt / 0.45))
            bgm = Image.blend(assets.bg(seg["prev"]["file"]), bg, e)
            focal = (seg["pt"]["x"] * dpr, seg["pt"]["y"] * dpr)
            cur_pos = (pt[0] + 40 * e, pt[1] + 90 * e)
            frame = compose(
                screen,
                bgm,
                zoom=z,
                focal=focal,
                cur=cur_pos,
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
        else:
            frame = compose(
                screen_for(key),
                bg,
                cur=cur_pos,
                cap=cap,
                cap_scale=cap_scale,
                hook_alpha=hook_alpha,
            )
        if sprites:
            happy = key["idx"] == last_step_idx and kind == "hold" and "happy_m0" in sprites
            e = float(envelope[fi]) if envelope is not None and fi < len(envelope) else 0.0
            # Three mouth levels from speech energy with hysteresis; the level moves one step per
            # ~65 ms so every open and close passes through the middle shape.
            prev_lvl = mstate["lvl"]
            held = fi - mstate["mouth_since"]
            if prev_lvl == 0:
                target = 2 if e > 0.62 else (1 if e > 0.30 else 0)
            elif prev_lvl == 1:
                target = 2 if e > 0.58 else (0 if e < 0.16 else 1)
            else:
                target = 0 if e < 0.16 else (1 if e < 0.42 else 2)
            lvl = prev_lvl
            if target != prev_lvl and held >= max(2, round(fps / 15)):
                lvl = prev_lvl + (1 if target > prev_lvl else -1)
                mstate["lvl"] = lvl
                mstate["mouth_since"] = fi
            blink = (t % 3.2) < 0.1 and lvl == 0
            expr = ("happy_" if happy else ("blink_" if blink else "talk_")) + f"m{lvl}"
            state_key = "happy" if happy else "talk"
            if state_key != mstate["expr"]:
                mstate["expr"] = state_key
                mstate["since"] = t
            pop = lerp(0.85, 1.0, ease_out_cubic(clamp((t - mstate["since"]) / 0.2)))
            enter = ease_out_cubic(clamp((t - 0.3) / 0.55))
            bob = 4 * math.sin(t * 1.5) + 3 * e
            squash = 1.0 + 0.025 * e
            if kind == "tap":
                squash += 0.04 * math.sin(clamp(lt / 0.5) * math.pi)
            if happy:
                squash += 0.03 * math.sin(clamp((t - mstate["since"]) / 0.35) * math.pi)
            sp = sprites[expr]
            w = int(MASCOT_PX * pop * squash)
            h = int(MASCOT_PX * pop * (2 - squash))
            spr = sp.resize((max(1, w), max(1, h)), Image.BILINEAR)
            x = int(CARD_X + CARD_W - 0.70 * w)
            y = int(lerp(H + 40, CARD_Y + CARD_H - 0.80 * h - bob, enter))
            frame.paste(spr, (x, y), spr)
            if cap is not None and cap_scale > 0:
                sp2 = (
                    cap
                    if cap_scale == 1.0
                    else cap.resize(
                        (max(1, int(cap.width * cap_scale)), max(1, int(cap.height * cap_scale))),
                        Image.BILINEAR,
                    )
                )
                frame.paste(sp2, ((W - sp2.width) // 2, 1400 - sp2.height // 2), sp2)
        if ff is None:
            frame.save(out)
            print(f"wrote {out} (frame at {t:.2f}s of {total:.1f}s)")
            return
        ff.stdin.write(frame.tobytes())
        if fi % 120 == 0:
            print(f"frame {fi}/{n} t={t:.2f}s seg={kind}", file=sys.stderr)
    ff.stdin.close()
    ff.wait()
    if ff.returncode:
        raise SystemExit("ffmpeg video encode failed")
    if audio or sfx_events:
        inputs = ["-i", video_out]
        filters = []
        labels = []
        tracks = [(start, os.path.join(d, clip["file"])) for start, clip in audio] + [
            (t0, sfx_files[k]) for k, t0 in sfx_events
        ]
        for j, (start, f) in enumerate(tracks):
            inputs += ["-i", f]
            filters.append(
                f"[{j + 1}:a]aresample=48000,adelay={int(start * 1000)}|{int(start * 1000)},apad[a{j}]"
            )
            labels.append(f"[a{j}]")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(tracks)}:normalize=0,atrim=0:{total:.3f},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                "0:v",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                "-shortest",
                out,
            ],
            check=True,
        )
        os.remove(video_out)
    else:
        # The contract requires a voice track: pad silence so a voiceless preview still validates.
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                video_out,
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-shortest",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                out,
            ],
            check=True,
        )
        os.remove(video_out)
    print(
        f"wrote {out}: {n} frames, {total:.1f}s at {fps} fps, {len(audio)} voice clips, {len(sfx_events)} sfx"
    )


if __name__ == "__main__":
    main()

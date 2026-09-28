#!/usr/bin/env python3
"""capture.py <script.json> <outdir>          keyframes + step log for render.py
capture.py --inspect <url> <outdir>            page outline, selectors, colors, one screenshot

Drives one Camoufox (fingerprint-hardened Firefox) through Playwright at a phone viewport with a
2.77 device scale factor so every screenshot is a native 1080x1920 PNG. It executes the scripted
steps and writes keyframes plus a step log; there is no real-time video capture, the renderer
synthesizes all motion from the log afterwards, which is what keeps the result smooth on a
loaded machine.
"""

from __future__ import annotations

import json
import os
import sys
import time

from camoufox.sync_api import Camoufox
from PIL import Image

VW, VH = 390, 693  # css px; at dpr 2.77 this is 1080x1920, a true 9:16 frame
DPR = 2.77
SETTLE_MS = 700
MAX_FULL_SCREENS = 8
STYLE = "html{scroll-behavior:auto !important} *{caret-color:transparent !important}"
INSPECT_JS = r"""
(() => {
  const sel = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const href = el.getAttribute && el.getAttribute('href');
    const scope = el.closest('header, nav, footer, main, section');
    const prefix = scope ? (scope.tagName.toLowerCase() + (scope.id ? `#${CSS.escape(scope.id)}` : '')) + ' ' : '';
    if (href) return `${prefix}${el.tagName.toLowerCase()}[href='${href.replace(/'/g, "\\'")}']`;
    const text = (el.innerText || '').trim().slice(0, 40);
    return `${prefix}${el.tagName.toLowerCase()}` + (text ? ` /* ${text} */` : '');
  };
  const headings = [...document.querySelectorAll('h1, h2, h3')].slice(0, 60).map((h) => ({
    tag: h.tagName.toLowerCase(), text: (h.innerText || '').trim().slice(0, 120),
    scrollY: Math.round(h.getBoundingClientRect().top + window.scrollY),
  }));
  const actions = [...document.querySelectorAll('a, button')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 24 && r.height > 16 && (el.innerText || '').trim();
  }).slice(0, 80).map((el) => ({
    text: (el.innerText || '').trim().slice(0, 60), selector: sel(el),
    href: el.getAttribute('href') || null,
    scrollY: Math.round(el.getBoundingClientRect().top + window.scrollY),
    inHeader: Boolean(el.closest('header, nav')),
  }));
  const colors = {};
  for (const el of [...document.querySelectorAll('button, a, [class*=btn], [class*=cta]')].slice(0, 200)) {
    const s = getComputedStyle(el);
    for (const c of [s.backgroundColor, s.color]) {
      if (c && !/rgba?\(0, 0, 0|rgba?\(255, 255, 255|transparent|rgba\(.*, 0\)/.test(c)) colors[c] = (colors[c] || 0) + 1;
    }
  }
  const body = getComputedStyle(document.body);
  return {
    title: document.title, url: location.href,
    docHeight: document.documentElement.scrollHeight, viewport: {w: innerWidth, h: innerHeight},
    metaDescription: document.querySelector('meta[name=description]')?.content || '',
    headings, actions,
    frequentColors: Object.entries(colors).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([c, n]) => ({color: c, uses: n})),
    bodyBackground: body.backgroundColor, bodyFont: body.fontFamily.split(',')[0],
    text: (document.body.innerText || '').replace(/\n{3,}/g, '\n\n').slice(0, 6000),
  };
})()
"""


def png_size(path):
    with Image.open(path) as im:
        return im.size


class Session:
    def __init__(self, script):
        self.script = script
        self.vw = script.get("viewport", {}).get("width", VW)
        self.vh = script.get("viewport", {}).get("height", VH)
        self.dpr = script.get("viewport", {}).get("dpr", DPR)
        self.settle_ms = script.get("settleMs", SETTLE_MS)
        # The sandbox carries Codex's proxy variables for OpenAI egress; the product pages are
        # fetched directly, so the browser must not inherit them (Firefox honours them and
        # fails with NS_ERROR_PROXY_CONNECTION_REFUSED).
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(name, None)
        options = {
            "headless": "virtual",
            "os": "macos",
            "humanize": False,
            "firefox_user_prefs": {"network.proxy.type": 0},
        }
        proxy = os.environ.get("TIN_BROWSER_PROXY", "")
        if proxy:
            options["proxy"] = {"server": proxy}
            options["firefox_user_prefs"] = {"network.proxy.type": 1}
        self.manager = Camoufox(**options)
        self.browser = self.manager.__enter__()
        self.context = self.browser.new_context(
            viewport={"width": self.vw, "height": self.vh}, device_scale_factor=self.dpr
        )
        self.context.add_init_script(
            "(() => { const s = document.createElement('style'); s.textContent = "
            + json.dumps(STYLE)
            + "; document.addEventListener('DOMContentLoaded', () => document.head.appendChild(s)); })();"
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(60_000)

    def close(self):
        self.manager.__exit__(None, None, None)

    def evaluate(self, expression):
        return self.page.evaluate(expression)

    def frames(self):
        """Wait for two animation frames, bounded: after a full-page screenshot Firefox may not
        paint again until something else happens, and an unbounded rAF wait never resolves."""
        try:
            self.page.evaluate(
                "new Promise(r => { requestAnimationFrame(() => requestAnimationFrame(r));"
                " setTimeout(r, 400); })"
            )
        except Exception:  # noqa: BLE001 - a navigation mid-wait is fine
            pass

    def settle(self, ms=None):
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001 - a busy page still settles on the timer
            pass
        self.frames()
        time.sleep((self.settle_ms if ms is None else ms) / 1000)
        self.frames()

    def goto(self, url, settle_ms=None):
        self.page.goto(url, wait_until="load", timeout=60_000)
        self.settle(settle_ms if settle_ms is not None else 1200)

    def state(self):
        return self.evaluate(
            "({scrollY: window.scrollY, url: location.href, docH: document.documentElement.scrollHeight, title: document.title})"
        )

    def scroll_to(self, y):
        self.evaluate(f"window.scrollTo(0, {float(y)})")

    def locate(self, selector):
        rect = self.evaluate(
            "(sel => { const el = document.querySelector(sel); if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'nearest'});"
            " const b = el.getBoundingClientRect();"
            " return {x: b.left + b.width / 2, y: b.top + b.height / 2, w: b.width, h: b.height, text: (el.innerText || '').slice(0, 60)}; })"
            f"({json.dumps(selector)})"
        )
        if rect is None:
            raise SystemExit(f"selector not found: {selector}")
        return rect

    def click_at(self, x, y):
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        time.sleep(0.06)
        self.page.mouse.up()


def inspect(url, outdir):
    os.makedirs(outdir, exist_ok=True)
    session = Session({})
    try:
        session.goto(url)
        info = session.evaluate(INSPECT_JS)
        shot = os.path.join(outdir, "inspect.png")
        session.page.screenshot(path=shot)
        full = os.path.join(outdir, "inspect-full.png")
        session.page.screenshot(path=full, full_page=True)
        with Image.open(full) as im:
            if im.height > 1920 * MAX_FULL_SCREENS:
                im.crop((0, 0, im.width, 1920 * MAX_FULL_SCREENS)).save(full)
        info["screenshot"] = shot
        info["fullPageScreenshot"] = full
        info["cssViewport"] = {"width": session.vw, "height": session.vh, "dpr": session.dpr}
        print(json.dumps(info, indent=1))
    finally:
        session.close()


def capture(script_path, outdir):
    script = json.load(open(script_path))
    os.makedirs(os.path.join(outdir, "frames"), exist_ok=True)
    session = Session(script)
    log = {
        "viewport": {"width": session.vw, "height": session.vh, "dpr": session.dpr},
        "hook": script.get("hook"),
        "steps": [],
    }
    # "step" is the script step being captured; voice.py pairs each vo line with its keyframes.
    counter = {"k": 0, "step": 0}
    cursor = {"x": session.vw * 0.55, "y": session.vh * 0.6}

    def snapshot(**extra):
        idx = counter["k"]
        counter["k"] += 1
        file = f"frames/k{idx:03d}.png"
        session.page.screenshot(path=os.path.join(outdir, file))
        entry = {
            "idx": idx,
            "script_idx": counter["step"],
            "file": file,
            **session.state(),
            "cursor": dict(cursor),
            **extra,
        }
        log["steps"].append(entry)
        print(
            f"k{idx} {entry.get('kind')} {entry.get('label', '')} scrollY={entry['scrollY']}",
            flush=True,
        )
        return entry

    def full_page():
        file = f"frames/full{counter['k']:03d}.png"
        path = os.path.join(outdir, file)
        session.page.screenshot(path=path, full_page=True)
        session.frames()
        with Image.open(path) as im:
            limit = int(session.vh * session.dpr * MAX_FULL_SCREENS)
            if im.height > limit:
                im.crop((0, 0, im.width, limit)).save(path)
            height_css = min(im.height, limit) / session.dpr
        return {"file": file, "heightCss": height_css}

    try:
        for n, step in enumerate(script["steps"]):
            counter["step"] = n
            if step.get("goto"):
                session.goto(step["goto"], step.get("settleMs"))
                if step.get("scrollTo") is not None:
                    session.scroll_to(step["scrollTo"])
                    session.settle(300)
                snapshot(
                    kind="goto",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    hold=step.get("hold", 1200),
                )
            elif step.get("click"):
                before = session.evaluate("window.scrollY")
                full = None
                if isinstance(step["click"], str):
                    full = full_page()
                    pt = session.locate(step["click"])
                    session.settle(200)
                else:
                    pt = step["click"]
                after = session.evaluate("window.scrollY")
                extra = {}
                if full and abs(after - before) > 4:
                    extra = {
                        "scrollFrom": before,
                        "fullpage": full,
                        "duration": step.get("scrollDuration", 800),
                    }
                snapshot(
                    kind="pre-click",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    target={"x": pt["x"], "y": pt["y"]},
                    hold=step.get("holdBefore", 300),
                    **extra,
                )
                session.click_at(pt["x"], pt["y"])
                cursor.update(x=pt["x"], y=pt["y"])
                session.settle(step.get("settleMs"))
                if step.get("scrollTo") is not None:
                    session.scroll_to(step["scrollTo"])
                    session.settle(300)
                snapshot(
                    kind="click",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    click={"x": pt["x"], "y": pt["y"]},
                    hold=step.get("hold", 1400),
                    zoom=step.get("zoom", 1.12),
                )
            elif step.get("scroll") is not None:
                before = session.evaluate("window.scrollY")
                full = full_page()
                target = step["scroll"]
                if isinstance(target, str):
                    session.locate(target)
                    target = session.evaluate("window.scrollY")
                elif isinstance(target, dict):
                    target = target.get("to", before + target.get("by", 0))
                session.scroll_to(target)
                session.settle(step.get("settleMs", 500))
                snapshot(
                    kind="scroll",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    scrollFrom=before,
                    fullpage=full,
                    hold=step.get("hold", 1200),
                    duration=step.get("duration", 900),
                )
            elif step.get("type"):
                pt = session.locate(step["type"]["selector"])
                session.settle(150)
                session.click_at(pt["x"], pt["y"])
                cursor.update(x=pt["x"], y=pt["y"])
                snapshot(
                    kind="focus",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    click={"x": pt["x"], "y": pt["y"]},
                    hold=300,
                )
                text = step["type"]["text"]
                every = step["type"].get("every", 3)
                for i, ch in enumerate(text, start=1):
                    session.page.keyboard.insert_text(ch)
                    if i % every == 0 or i == len(text):
                        session.settle(60)
                        snapshot(
                            kind="typing",
                            label=step.get("label"),
                            caption=step.get("caption"),
                            hold=70,
                            cursorHidden=True,
                        )
            elif step.get("hold"):
                snapshot(
                    kind="hold",
                    label=step.get("label"),
                    caption=step.get("caption"),
                    hold=step["hold"],
                )
            else:
                raise SystemExit(f"unknown step: {json.dumps(step)[:120]}")
        json.dump(log, open(os.path.join(outdir, "log.json"), "w"), indent=2)
        json.dump(script, open(os.path.join(outdir, "script.json"), "w"), indent=2)
        print(f"wrote {counter['k']} keyframes to {outdir}")
    finally:
        session.close()


def main():
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "--inspect":
        inspect(args[1], args[2])
    elif len(args) == 2:
        capture(args[0], args[1])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()

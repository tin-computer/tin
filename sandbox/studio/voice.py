#!/usr/bin/env python3
"""voice.py <capture-dir> [--voice Kore] [--style TEXT] [--language "English (US)"] [--stt-language en]

Reads the `vo` line of every step in <capture-dir>/script.json, asks Tin's run-bound studio
voice endpoint for one clip per line (the switchboard owns the provider credential and returns
the audio plus the words it heard with timestamps), aligns those words back onto the script
text, and writes <capture-dir>/vo.json for render.py. Clips whose text did not change since
the last run are kept, so editing one line regenerates only that line.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# fal's Gemini TTS content checker refuses ordinary product lines when the style asks for a
# "short-form voiceover" or "creator talking to camera"; this plainer style passes them.
DEFAULT_STYLE = "Warm, upbeat, and quick. Friendly product narration."
# The one keyframe per script step that carries its voice line; a type step speaks over its focus.
CONTENT_KINDS = ("goto", "scroll", "click", "hold", "focus")


class VoiceRefused(RuntimeError):
    """The switchboard or its provider refused one line; other lines still proceed."""


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def endpoint() -> tuple[str, str]:
    url = os.environ.get("TIN_RUN_TOOLS_URL", "")
    grant = os.environ.get("TIN_RUN_TOOLS_GRANT", "")
    if not url or not grant:
        raise SystemExit(
            "this run has no studio voice grant (TIN_RUN_TOOLS_URL/TIN_RUN_TOOLS_GRANT)"
        )
    base = url[: -len("/mcp")] if url.endswith("/mcp") else url.rstrip("/")
    return f"{base}/studio/voice", grant


def request_voice(text, voice, style, language, stt_language, request_id):
    url, grant = endpoint()
    body = json.dumps(
        {
            "request_id": request_id,
            "text": text,
            "voice": voice,
            "style": style,
            "language_code": language,
            "transcription_language": stt_language,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {grant}"},
    )
    return post_with_backoff(req)


# The switchboard restarts for about a minute on every deploy (HTTP 502/503/504, refused or
# reset connections). Keep asking with doubling waits for up to ~2 minutes before giving up.
RETRY_STATUSES = (429, 502, 503, 504)
RETRY_WINDOW_SECONDS = 120
RETRY_MAX_DELAY_SECONDS = 30


def backoff_delays(window=RETRY_WINDOW_SECONDS, cap=RETRY_MAX_DELAY_SECONDS):
    """Sleep lengths 1, 2, 4, ... capped, whose sum stays inside the window."""
    delays, total, delay = [], 0, 1
    while total + delay <= window:
        delays.append(delay)
        total += delay
        delay = min(delay * 2, cap)
    return delays


def post_with_backoff(req, *, opener=None, sleep=time.sleep, delays=None):
    opener = opener or urllib.request.urlopen
    delays = backoff_delays() if delays is None else list(delays)
    attempt = 0
    while True:
        try:
            with opener(req, timeout=300) as response:  # noqa: S310 - Tin's own https endpoint
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read()[:400].decode("utf-8", "replace")
            if error.code in RETRY_STATUSES and attempt < len(delays):
                sleep(delays[attempt])
                attempt += 1
                continue
            raise VoiceRefused(f"studio voice request failed ({error.code}): {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < len(delays):
                sleep(delays[attempt])
                attempt += 1
                continue
            raise


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def script_words(text):
    return [w for w in re.sub(r"\[[a-z ]+\]", "", text).split() if norm(w)]


def align(script_text, heard, duration):
    """Map the script's words (what goes on screen) onto heard timings; unmatched script words
    get interpolated times inside the gap their neighbours leave."""
    sw = script_words(script_text)
    a = [norm(w) for w in sw]
    b = [norm(h["word"]) for h in heard]
    times = [None] * len(sw)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                times[i1 + k] = (heard[j1 + k]["start"], heard[j1 + k]["end"])
    known = [i for i, t in enumerate(times) if t]
    if not known:
        step = duration / max(1, len(sw))
        return [{"w": w, "start": i * step, "end": (i + 1) * step} for i, w in enumerate(sw)]
    out = []
    for i, w in enumerate(sw):
        if times[i]:
            out.append({"w": w, "start": times[i][0], "end": times[i][1]})
            continue
        prev = max([k for k in known if k < i], default=None)
        nxt = min([k for k in known if k > i], default=None)
        t0 = times[prev][1] if prev is not None else 0.0
        t1 = times[nxt][0] if nxt is not None else duration
        run_start = (prev + 1) if prev is not None else 0
        run_end = nxt if nxt is not None else len(sw)
        n = run_end - run_start
        pos = i - run_start
        out.append(
            {"w": w, "start": t0 + (t1 - t0) * pos / n, "end": t0 + (t1 - t0) * (pos + 1) / n}
        )
    return out


def probe_duration(path):
    return float(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
        )
        .decode()
        .strip()
    )


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    d = sys.argv[1]
    voice = arg("--voice", "Kore")
    style = arg("--style", DEFAULT_STYLE)
    language = arg("--language", "English (US)")
    stt_language = arg("--stt-language", "en")
    script = json.load(open(os.path.join(d, "script.json")))
    log = json.load(open(os.path.join(d, "log.json")))
    os.makedirs(os.path.join(d, "vo"), exist_ok=True)
    previous = {}
    vo_path = os.path.join(d, "vo.json")
    if os.path.exists(vo_path):
        for clip in json.load(open(vo_path)).get("clips", []):
            previous[clip["step_idx"]] = clip
    content = {s.get("script_idx"): s for s in log["steps"] if s["kind"] in CONTENT_KINDS}
    out = {"voice": voice, "style": style, "language": language, "clips": []}
    failed = []
    for i, sstep in enumerate(script["steps"]):
        line = (sstep.get("vo") or "").strip()
        if not line:
            continue
        kstep = content.get(i)
        if kstep is None:
            raise SystemExit(f"script step {i} has no keyframe in log.json; run capture again")
        f = os.path.join(d, "vo", f"vo{i:02d}.mp3")
        kept = previous.get(kstep["idx"])
        # Stable across CLI retries/restarts, including an ambiguous HTTP response.
        # The server also binds this identifier to the run and request payload.
        request_id = hashlib.sha256(
            json.dumps(
                [kstep["idx"], line, voice, style, language, stt_language],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if kept and kept.get("request_id") == request_id and os.path.exists(f):
            out["clips"].append(kept)
            print(f"step {kstep['idx']} {kstep.get('label')}: kept ({kept['duration']:.2f}s)")
            continue
        t = time.time()
        try:
            result = request_voice(line, voice, style, language, stt_language, request_id)
        except VoiceRefused as refused:
            # Keep every clip made so far; a rerun regenerates only the refused line.
            failed.append((kstep["idx"], line, str(refused)))
            print(f"step {kstep['idx']} {kstep.get('label')}: REFUSED {refused}")
            json.dump(out, open(vo_path, "w"), indent=1)
            continue
        open(f, "wb").write(base64.b64decode(result["audio_base64"]))
        dur = probe_duration(f)
        heard = result.get("words", [])
        words = align(line, heard, dur)
        out["clips"].append(
            {
                "step_idx": kstep["idx"],
                "file": f"vo/vo{i:02d}.mp3",
                "duration": dur,
                "text": line,
                "voice": voice,
                "request_id": request_id,
                "words": words,
                "heard": heard,
            }
        )
        print(
            f"step {kstep['idx']} {kstep.get('label')}: {dur:.2f}s, {len(words)} words, {len(heard)} heard, {time.time() - t:.1f}s"
        )
        json.dump(out, open(vo_path, "w"), indent=1)
    json.dump(out, open(vo_path, "w"), indent=1)
    total = sum(c["duration"] for c in out["clips"])
    print(f"wrote {vo_path} ({len(out['clips'])} clips, {total:.1f}s of speech)")
    if failed:
        for idx, line, reason in failed:
            print(f"REFUSED step {idx}: {line!r}: {reason}")
        raise SystemExit(
            f"{len(failed)} line(s) were refused; rewrite them in script.json and run voice again "
            "(accepted lines are kept)"
        )


if __name__ == "__main__":
    main()

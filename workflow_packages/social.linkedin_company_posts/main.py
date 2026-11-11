"""Write a batch of posts for a company's LinkedIn page, each with a brief for its card.

One model call writes the batch from the project's brand guide, audience, writing guide, notes
and site wording. Code checks every post (length, paragraphs, links, numbers, banned marks and
stock phrases, repeats of earlier batches) and one more call fixes the posts that fail. The
batch is saved as a LinkedIn draft in project Files. Every post waits for its card: the
project's coding agent makes the cards with Tin's card kit, and a member picks which post goes
out. Nothing is posted here.

Once a model call is paid for, nothing fails the run: a problem the fix call leaves becomes a
note beside the post.
"""

import json
import re
from datetime import datetime
from pathlib import Path

OUTPUT_PATH = "social/linkedin/drafts/{date}-{slug}.json"
OUTPUT_BYTES = 256_000
ROOT = Path(__file__).parent
POSTS = 6
STYLE_PATH = ".agents/skills/writing-style/SKILL.md"
FORMATS_PATH = "social/linkedin/company/FORMATS.md"
# Business files, read when they exist. A request that runs long drops the last ones first.
CONTEXT_FILES = (
    ("brand/BRAND.md", 12_000),
    ("BRAND.md", 12_000),
    ("strategy/ICP.md", 10_000),
    ("context/ICP.md", 10_000),
    ("reports/GROWTH_ONBOARDING_PLAN.md", 8_000),
    ("reports/RELEASE_ANNOUNCE.md", 6_000),
    ("social/linkedin/RESULTS.md", 4_000),
)
CONTEXT_SECTIONS = (("### Feature map", 8_000),)
# The site's wording, then notes, then the product's feature films, each of which describes one
# shipped feature.
NOTE_GLOBS = ("context/site/*.md", "context/notes/*.md", "context/*.md", "films/*.md")
NOTE_FILES = 14
NOTE_CHARS = 8_000
STYLE_CHARS = 8_000
FORMATS_CHARS = 6_000
EARLIER_GLOB = "social/linkedin/drafts/*.json"
EARLIER_POSTS = 36
# Must match the route's max_input_bytes in workflow.json.
INPUT_BYTES = 200_000
SOURCE_EXCERPT_CHARS = 6_000
MIN_WORDS, MAX_WORDS = 40, 220
CARD_WORDS = 9
TEXT_LIMIT = 3000
REPEAT_SHARE = 0.5
CARD_KINDS = ("type", "screen", "terminal", "artwork")
TEXT_FIELDS = (
    "format",
    "audience",
    "text",
    "card_line",
    "card_kind",
    "card_brief",
    "alt_text",
    "first_comment",
)
CALL_ERRORS = {
    "model_output_invalid": "its answer did not have the expected shape",
    "model_call_limit": "the workflow reached its model-call limit",
    "model_result_unconfirmed": "a model call has no confirmed result, so Tin did not buy it again",
}

URL = re.compile(r"https?://[^\s<>()\"'`]+")
HASHTAG = re.compile(r"(?<![\w&])#[A-Za-z]\w*")
MENTION = re.compile(r"(?<![\w.])@[A-Za-z]\w*")
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]")
NUMBER = re.compile(r"\d[\d,.]*\d|\d")
PRICE = re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars|euros)\b", re.I)
CONTRAST = re.compile(
    r"\b(?:it'?s|that'?s|this is|is) not\b[^.!?\n]{0,60}[,;:]\s*(?:it'?s|but|just)\b"
    r"|\bisn'?t\b[^.!?\n]{0,60}[,;]\s*it'?s\b"
    r"|\bnot (?:just|only|merely)\b"
    r"|\w,\s+not\s+\w",
    re.I,
)
CREDENTIAL = re.compile(
    r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _rule(name):
    return (ROOT / "rules" / name).read_text(encoding="utf-8")


def _tells():
    lines = _rule("tells.txt").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def _safe_path(path, label):
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 512
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(character in path for character in "*?[]")
    ):
        raise ValueError(f"{label} must be a relative path to a file in project Files")
    return path


def _optional(files, path):
    try:
        value = files.read_text(path)
    except FileNotFoundError:
        return ""
    return value if isinstance(value, str) else ""


def _section(files, heading):
    try:
        value = files.read_section(heading)
    except (FileNotFoundError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def _cut(text, limit):
    """Text within limit characters, ended at a paragraph or line break when one is near."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    for mark in ("\n\n", "\n"):
        at = head.rfind(mark)
        if at > limit * 0.6:
            return head[:at].rstrip(), True
    return head.rstrip(), True


def _text_input(inputs, name, limit, label):
    value = inputs.get(name) or ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} must be at most {limit:,} characters")
    return value.strip()


def _sources(files, style_path, formats_path):
    """The project's files this batch rests on, most useful first, each with a short ID."""
    items, seen = [], set()

    def add(path, text, limit, label=None):
        text, cut = _cut(text, limit)
        if not text or CREDENTIAL.search(text):
            return
        seen.add(path)
        items.append({"id": f"s{len(items) + 1}", "path": label or path, "text": text, "cut": cut})

    for path, limit in CONTEXT_FILES:
        if path == "BRAND.md" and "brand/BRAND.md" in seen:
            continue
        if path == "context/ICP.md" and "strategy/ICP.md" in seen:
            continue
        add(path, _optional(files, path), limit)
    for heading, limit in CONTEXT_SECTIONS:
        add("wiki/INDEX.md", _section(files, heading), limit, f"wiki/INDEX.md {heading[4:]}")
    notes = []
    for pattern in NOTE_GLOBS:
        for path in files.glob(pattern):
            if path not in seen and path not in notes:
                notes.append(path)
    # The site's own wording first, then notes newest first (dated notes are named by date),
    # then films.
    site = sorted(path for path in notes if path.startswith("context/site/"))
    films = sorted(path for path in notes if path.startswith("films/"))
    dated = sorted((path for path in notes if path not in site + films), reverse=True)
    for path in (site + dated + films)[:NOTE_FILES]:
        add(path, _optional(files, path), NOTE_CHARS)
    style, _ = _cut(_optional(files, style_path), STYLE_CHARS)
    formats, _ = _cut(_optional(files, formats_path), FORMATS_CHARS)
    return items, style, formats


def _earlier_posts(files):
    """Text of posts in earlier LinkedIn drafts, so the batch doesn't repeat them."""
    posts = []
    for path in sorted(files.glob(EARLIER_GLOB), reverse=True):
        try:
            draft = json.loads(_optional(files, path) or "{}")
        except ValueError:
            continue
        for post in draft.get("posts", []) if isinstance(draft, dict) else []:
            text = post.get("text") if isinstance(post, dict) else None
            if isinstance(text, str) and text.strip():
                posts.append({"path": path, "text": text[:1_500]})
            if len(posts) >= EARLIER_POSTS:
                return posts
    return posts


def _request_bytes(instructions, data, schema):
    rough = json.dumps(
        {"system": instructions, "user": json.dumps(data, allow_nan=False), "schema": schema},
        ensure_ascii=False,
    )
    return len(rough.encode("utf-8")) + 4_096


# --- schemas -------------------------------------------------------------------------------


def _string(limit, minimum=0):
    value = {"type": "string", "maxLength": limit}
    if minimum:
        value["minLength"] = minimum
    return value


def _object(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


POST_SCHEMA = _object(
    {
        "format": _string(80, 1),
        "audience": _string(160),
        "text": _string(3_000, 1),
        "card_line": _string(120, 1),
        "card_kind": {"type": "string", "enum": list(CARD_KINDS)},
        "card_brief": _string(600, 1),
        "alt_text": _string(600, 1),
        "first_comment": _string(300),
        "support": _array(_object({"source": _string(10, 1), "excerpt": _string(600, 1)}), 4),
    }
)
WRITE_SCHEMA = _object(
    {
        "formats": _array(_object({"name": _string(80, 1), "what": _string(300, 1)}), 8),
        "posts": _array(POST_SCHEMA, POSTS),
        "gaps": _array(_object({"angle": _string(300, 1), "reason": _string(600, 1)}), 6),
    }
)
FIX_SCHEMA = _object({"posts": _array(_object({"id": _string(4, 1), "post": POST_SCHEMA}), POSTS)})

WRITE_INSTRUCTIONS = """{guide}

## This batch

Write {count} posts for this company's page from the sources. Each source has an ID; in each
post's `support`, name the sources it rests on with a short exact excerpt from each. List the
formats you used in `formats`, one line each on what the format is. When the sources can't
support {count} different posts, write fewer and say in `gaps` what is missing.

`earlier_posts` went out or were drafted before: don't repeat their ideas or sentences.
`direction`, when given, is what the founder wants this batch to cover; treat it as decided.
`formats`, when given, is the project's own formats file: use its formats.
`writing_guide`, when given, is how the company writes."""

FIX_INSTRUCTIONS = """{guide}

## Fix these posts

Each post below failed checks that are listed under `problems`. Rewrite each one so that every
problem is gone and everything else stays as it was: same format, same idea, same card unless a
problem names the card. Use only facts in the sources. Return every post listed, by its ID."""


# --- checks --------------------------------------------------------------------------------


def _words(text):
    return re.findall(r"[\w'’-]+", text)


def _shingles(text, size=5):
    words = [word.lower() for word in _words(text)]
    return {" ".join(words[i : i + size]) for i in range(max(0, len(words) - size + 1))}


def _numbers(text):
    return {match.group(0).rstrip(".,").replace(",", "") for match in NUMBER.finditer(text)}


def _utf16(text):
    return len(text.encode("utf-16-le")) // 2


def _findings(post, known_numbers, tells, earlier, allowed_urls):
    """What is wrong with one post, as sentences the fix call and the founder can act on."""
    found = []
    text, card = post["text"], post["card_line"]
    words = len(_words(text))
    if words < MIN_WORDS or words > MAX_WORDS:
        found.append(f"The post has {words} words; keep it between 60 and 170.")
    if _utf16(text) > TEXT_LIMIT:
        found.append("The post is over LinkedIn's 3,000 characters.")
    if words > 60 and "\n\n" not in text.strip():
        found.append("The post is one block; break it into short paragraphs with blank lines.")
    if URL.search(text) or "www." in text.lower():
        found.append("The post has a link; move it to the first comment.")
    for label, value in (("post", text), ("card line", card)):
        if HASHTAG.search(value):
            found.append(f"The {label} has a hashtag.")
        if MENTION.search(value):
            found.append(f"The {label} has an @mention.")
        if EMOJI.search(value):
            found.append(f"The {label} has an emoji.")
        if "—" in value or "–" in value:
            found.append(f"The {label} has an em or en dash.")
        if PRICE.search(value):
            found.append(f"The {label} names a price.")
        if CONTRAST.search(value):
            found.append(f"The {label} uses a 'not X, Y' contrast; say what the thing is.")
        lowered = value.lower()
        for tell in tells:
            if re.search(rf"(?<![\w]){re.escape(tell)}(?![\w])", lowered):
                found.append(f'The {label} uses the stock phrase "{tell}".')
        unknown = sorted(_numbers(value) - known_numbers)
        if unknown:
            found.append(f"The {label} has numbers no source gives: {', '.join(unknown[:5])}.")
    if len(_words(card)) > CARD_WORDS or "\n" in card:
        found.append(f"The card line is over {CARD_WORDS} words; make it one short line.")
    for url in URL.findall(post["first_comment"]):
        if url.rstrip(".,;:!?") not in allowed_urls:
            found.append(f"The first comment links to {url}, which no source gives.")
    mine = _shingles(text)
    for other in earlier:
        theirs = _shingles(other["text"])
        if mine and len(mine & theirs) / len(mine) >= REPEAT_SHARE:
            found.append(f"The post repeats an earlier post in {other['path']}.")
            break
    return found


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [" ".join(_words(part)).lower() for part in parts if len(_words(part)) >= 6]


def _shared(posts):
    """For each post, a sentence it shares with another post in the batch, with that post's ID."""
    found = [None] * len(posts)
    seen = {}
    for index, post in enumerate(posts):
        for sentence in _sentences(post["text"]):
            if sentence in seen and seen[sentence] != index:
                found[index] = (seen[sentence], sentence)
                break
            seen.setdefault(sentence, index)
    return found


def _clean(post):
    return {
        key: (value.strip() if isinstance(value, str) else value) for key, value in post.items()
    }


# --- the run -------------------------------------------------------------------------------


class Run:
    def __init__(self, ctx):
        self.ctx, self.notes, self.calls, self.blocked = ctx, [], 0, False

    async def call(self, step, instructions, data, schema):
        """One model step on the write route. A failure is recorded and returned as None."""
        if self.blocked:
            return None
        try:
            response = await self.ctx.models.generate(
                route="write",
                step=step,
                instructions=instructions,
                data=data,
                output_schema=schema,
            )
        except ValueError as error:
            text = str(error)
            reason = next((v for k, v in CALL_ERRORS.items() if k in text), "the call failed")
            self.notes.append(f"The {step} step returned nothing usable: {reason}.")
            if "limit" in text or "unconfirmed" in text:
                self.blocked = True
            return None
        self.calls += 1
        parsed = response.get("parsed") if isinstance(response, dict) else None
        return parsed if isinstance(parsed, dict) else None


def _valid_post(post):
    return (
        isinstance(post, dict)
        and all(isinstance(post.get(key), str) for key in TEXT_FIELDS)
        and post.get("text", "").strip()
        and post.get("card_line", "").strip()
        and post.get("card_kind") in CARD_KINDS
        and isinstance(post.get("support"), list)
    )


def _support(post, sources):
    by_id = {source["id"]: source for source in sources}
    items, unverified = [], 0
    for item in post.get("support", [])[:4]:
        source = by_id.get(item.get("source")) if isinstance(item, dict) else None
        excerpt = (item.get("excerpt") or "").strip() if isinstance(item, dict) else ""
        if source is None or not excerpt:
            unverified += 1
            continue
        flat = " ".join(source["text"].split()).lower()
        if " ".join(excerpt.split()).lower() not in flat:
            unverified += 1
        items.append({"source_path": source["path"], "excerpt": excerpt[:4_000]})
    return items, unverified


def _notes(post, problems, unverified):
    lines = [
        f"Format: {post['format']}",
        *([f"Audience: {post['audience']}"] if post.get("audience") else []),
        f"Card: {post['card_line']}",
        f"Card kind: {post['card_kind']}",
        f"Card brief: {post['card_brief']}",
        f"Alt text: {post['alt_text']}",
        f"First comment: {post['first_comment'] or 'none'}",
    ]
    if unverified:
        lines.append(f"Check: {unverified} supporting excerpt(s) aren't word for word in a source.")
    lines.extend(f"Check: {problem}" for problem in problems)
    return "\n".join(lines)[:4_000]


def _summary(posts, formats, gaps, state, sources, path):
    lines = [
        f"{len(posts)} posts for the company page are in {path}. Each one waits for its card: "
        "your coding agent makes the cards with Tin's card kit, then you pick which post goes out.",
        "",
    ]
    if formats:
        lines.append("Formats in this batch:")
        lines.extend(f"- {item['name']}: {item['what']}" for item in formats)
        lines.append("")
    flagged = [post["id"] for post in posts if "Check:" in post["editor_notes"]]
    if flagged:
        lines.append(f"Read the checks on {', '.join(flagged)} before picking them.")
    for gap in gaps:
        lines.append(f"Missing: {gap['angle']}. {gap['reason']}")
    lines.append("Read: " + ", ".join(source["path"] for source in sources) + ".")
    lines.extend(state.notes)
    return "\n".join(lines).strip()[:4_000]


async def run(ctx, inputs):
    files = ctx.files
    style_path = STYLE_PATH
    if inputs.get("style_path"):
        style_path = _safe_path(inputs["style_path"], "Writing guide")
    formats_path = (
        _safe_path(inputs["formats_path"], "Formats file")
        if inputs.get("formats_path")
        else FORMATS_PATH
    )
    direction = _text_input(inputs, "direction", 2_000, "Direction")
    sources, style, formats = _sources(files, style_path, formats_path)
    if not sources:
        raise ValueError(
            "Add a brand guide (brand/BRAND.md) or notes under context/ so Tin knows what the "
            "company does"
        )
    earlier = _earlier_posts(files)
    guide = _rule("guide.md")
    tells = _tells()
    state = Run(ctx)

    def make(kept):
        return {
            "count": POSTS,
            "direction": direction,
            "formats": formats,
            "writing_guide": style,
            "sources": [{"id": s["id"], "path": s["path"], "text": s["text"]} for s in kept],
            "earlier_posts": [post["text"] for post in earlier],
        }

    instructions = WRITE_INSTRUCTIONS.format(guide=guide, count=POSTS)
    kept = list(sources)
    data = make(kept)
    while _request_bytes(instructions, data, WRITE_SCHEMA) > INPUT_BYTES and len(kept) > 1:
        dropped = kept.pop()
        state.notes.append(f"{dropped['path']} was left out to fit one request.")
        data = make(kept)
    for source in kept:
        if source["cut"]:
            state.notes.append(f"{source['path']} was cut to fit.")

    written = await state.call("write", instructions, data, WRITE_SCHEMA)
    raw = [_clean(post) for post in (written or {}).get("posts", []) if _valid_post(post)]
    if written and len(raw) < len((written or {}).get("posts", [])):
        state.notes.append("Posts that came back incomplete were left out.")
    formats_used = [
        item
        for item in (written or {}).get("formats", [])
        if isinstance(item, dict) and item.get("name") and item.get("what")
    ]
    gaps = [
        {"angle": item["angle"].strip(), "reason": item["reason"].strip()}
        for item in (written or {}).get("gaps", [])
        if isinstance(item, dict) and item.get("angle") and item.get("reason")
    ]

    known = set()
    for source in kept:
        known |= _numbers(source["text"])
    known |= _numbers(direction)
    allowed = {url.rstrip(".,;:!?") for s in kept for url in URL.findall(s["text"])}
    problems = [_findings(post, known, tells, earlier, allowed) for post in raw]
    for index, shared in enumerate(_shared(raw)):
        if shared:
            other, sentence = shared
            problems[index].append(
                f'The post repeats a sentence from p{other + 1} ("{sentence[:80]}"); say it '
                "differently or leave it out."
            )

    failing = [i for i, found in enumerate(problems) if found]
    if failing:
        fix_data = {
            "sources": data["sources"],
            "posts": [
                {"id": f"p{i + 1}", "post": raw[i], "problems": problems[i]} for i in failing
            ],
        }
        fixed = await state.call("fix", FIX_INSTRUCTIONS.format(guide=guide), fix_data, FIX_SCHEMA)
        for item in (fixed or {}).get("posts", []):
            if not isinstance(item, dict) or not _valid_post(item.get("post")):
                continue
            match = re.fullmatch(r"p(\d)", str(item.get("id", "")))
            index = int(match.group(1)) - 1 if match else -1
            if index in failing:
                candidate = _clean(item["post"])
                again = _findings(candidate, known, tells, earlier, allowed)
                if len(again) <= len(problems[index]):
                    raw[index], problems[index] = candidate, again

    posts = []
    for index, post in enumerate(raw[:POSTS]):
        support, unverified = _support(post, kept)
        posts.append(
            {
                "id": f"p{index + 1}",
                "text": post["text"][:TEXT_LIMIT],
                "target": "",
                # Every post goes out with an image or a video; it waits until its card is in.
                "readiness": "needs_asset",
                "support": support,
                "editor_notes": _notes(post, problems[index], unverified),
                "attachments": [],
                "missing_assets": [f"Card: {post['card_line']}"[:200]],
            }
        )
    if not posts and not gaps:
        gaps.append(
            {
                "angle": "This batch",
                "reason": "Tin could not write posts from these sources. "
                + " ".join(state.notes[-2:]),
            }
        )
    used = {item["source_path"] for post in posts for item in post["support"]}
    draft = {
        "schema_version": "tin.social.linkedin_draft.v1",
        "posts": posts,
        "gaps": gaps[:12],
        "sources": [
            {
                "id": source["id"],
                "label": source["path"][:300],
                "author": "",
                "use": "publishable" if source["path"] in used else "background",
                "excerpt": _cut(source["text"], SOURCE_EXCERPT_CHARS)[0],
            }
            for source in kept[:24]
        ],
    }
    content = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
    while len(content.encode("utf-8")) > OUTPUT_BYTES and draft["sources"]:
        draft["sources"].pop()
        content = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
    date = datetime.fromisoformat(str(ctx["created_at"]).replace("Z", "+00:00")).date().isoformat()
    # One file per run: a second batch on the same day keeps the first.
    slug = "company-posts-" + str(ctx.get("run_id", ""))[:8].lower()
    path = OUTPUT_PATH.replace("{date}", date).replace("{slug}", slug.strip("-"))
    return {
        "path": path,
        "content": content,
        "summary": _summary(posts, formats_used, gaps, state, kept, path),
    }

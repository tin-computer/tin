#!/usr/bin/env python3
"""intel: one day's pulled items and the stories written from them, built for an agent's shell.

Tin's daily intelligence judges run this inside their sandbox (/usr/local/bin/intel). The
day's items live in a read-only SQLite file with full-text search (INTEL_DB), loaded by Tin's
root helper from the run's snapshot. Stories and the brief are written to Tin through this
run's grant (INTEL_URL, INTEL_GRANT), which checks every write and answers in sentences the
agent can act on. Every command prints a header line, short tab-separated rows and a footer
that says what was left out and how to get it. Output is capped at 9 KB per call: Antigravity
truncates tool output past about 10 KB. Python standard library only.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

VERSION = "2"
SNAPSHOT_FORMAT = "intel-snapshot/1"
CAP = 9000  # bytes per call: Antigravity cuts tool output past ~10 KB (middle elided)
MAX_INPUT_BYTES = 64 * 1024 * 1024
HELP = """intel: one day's news items, searchable, plus the stories you write from them.

Workflow
  1. intel status                      what is loaded, what you've covered, failed sources
  2. intel fronts                      each source's top items, in the source's own order
  3. intel search "words"              find coverage; intel count "words" sizes it by source
  4. intel similar <id>                other items about the same thing as <id>
  5. intel read <id>                   full text of one item (read only what you need)
  6. intel story put --key <slug> ...  record a story; re-running with the same key updates it
  7. intel brief put brief.json        the Slack brief (intel brief schema shows its shape)

Commands
  status [--all]                       --all also lists every source that came back empty
  sources                              source keys and item counts
  fronts [--source S] [--n 5] [--offset N]
  search "q" [--source S] [--since 12h] [--sort match|rank|new] [--limit 20] [--offset N]
  count "q" [--by source|hour]
  similar <id> [--limit 15]
  browse <source> [--from N] [--limit 25]
  read <id> [<id> ...] [--max-words 600] [--from-word N]   several ids share one call's budget:
                                       read a long or primary source on its own
  stories [--since 7d] [--q "words"] [--json]
  story show <sid>
  story put --json story.json          one story, or a list of up to 6, as JSON (keeps $ amounts)
  story put --key <slug> --title '...' --summary '...' --items id,id [--relevance high|medium|low]
            [--tags a,b] [--sensitive | --not-sensitive] [--continues <sid>]
  story attach <sid> id,id
  tags                                 the tags a story may carry
  brief schema                         the brief's JSON shape, limits and an example
  brief put <file.json>                send the brief; Tin checks it and says what to fix

Your shell cuts any single call's output past ~10 KB. Each intel command stays under that, so
run one or two per call; to read several short items, pass them to one intel read, not a loop.
Rows: id  source  rank  age  title (domain)  [+N dup: same story from N more outlets]
Rank is the source's own: #3 = third on its front page, 812pts 340c = HN points and comments,
1.2M views = Wikipedia, 120likes = X.
Search: plain words are ANDed; also "exact phrase", OR, -exclude, prefix*. Hyphens and dots
are handled for you (GPT-5, U.S.). --source takes a key or part of one (gn matches every
Google News source). Ids are 5 characters and stay the same for the same URL across days.
Stories and the brief go to Tin, not to a file: never edit the database.
Write story text in a JSON file (story put --json): inside double quotes the shell erases
$ amounts ("$850M" becomes "50M").
Story JSON: {"key": "nvidia-ceo-reinstated", "title": "...", "summary": "...",
  "items": ["k3f9a", "p0qe2"], "relevance": "high", "tags": ["ai", "markets"],
  "sensitive": false, "continues": "s-nvidia-ceo-fired"}   (title and summary on a new story)
Examples
  intel search "Zscaler CrowdStrike" --limit 5
  intel count "GTA VI" --by source
  intel story put --json /home/tin-work/scratch/stories-1.json
"""

STOP = set(
    """a an and are as at be by for from has have how in into is it its of on or that the this
to was were what when where which who why will with after over new says say said amid could would
than then they their them you your our out off up down more most first last next""".split()
)


# ---------------------------------------------------------------- errors and output
class Usage(Exception):
    code = 2


class NotFound(Exception):
    code = 3


def fail(exc):
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(getattr(exc, "code", 1))


class Out:
    """Collects lines up to the cap; says what was cut."""

    def __init__(self, header):
        self.lines = [header]
        self.size = len(header.encode()) + 1
        self.cut = False

    def add(self, line):
        if self.cut:
            return False
        n = len(line.encode()) + 1
        if self.size + n > getattr(self, "cap", CAP):
            self.cut = True
            return False
        self.lines.append(line)
        self.size += n
        return True

    def done(self, footer=None):
        if footer:
            self.lines.append(footer)
        print("\n".join(self.lines))


# ---------------------------------------------------------------- helpers
def canon(url):
    u = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u.lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if not re.match(r"^(utm_|ref$|ref_src|cmpid|smid|guccounter|fbclid|gclid|mc_)", k, re.I)
    ]
    return f"{host}{p.path.rstrip('/')}" + (f"?{urllib.parse.urlencode(q)}" if q else "")


def short_id(key, n=5):
    return base64.b32encode(hashlib.sha1(key.encode()).digest()).decode().lower()[:n]


def slug(name):
    s = name.lower()
    s = s.replace("google news", "gn").replace(" via gn", " gn").replace("hacker news", "hn")
    s = s.replace("wikipedia most viewed", "wiki").replace("google trends daily", "trends")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def domain(url):
    try:
        h = urllib.parse.urlsplit(url or "").netloc.lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def norm_title(t):
    t = re.sub(r"\s+[-|]\s+[^-|]{2,40}$", "", t or "")  # drop " - Outlet" tails
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age(published, now):
    t = parse_ts(published)
    if not t:
        return "?"
    m = max(0, int((now - t).total_seconds() // 60))
    return f"{m}m" if m < 60 else f"{m // 60}h" if m < 48 * 60 else f"{m // 1440}d"


def human(n):
    n = float(n)
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(int(n))


def metric(eng):
    """The source's own popularity number, if it has one."""
    for k in ("points", "score", "views", "likes"):
        v = eng.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def rank_label(eng, srank):
    if eng.get("points") is not None:
        return f"{human(eng['points'])}pts {human(eng.get('comments') or 0)}c"
    if eng.get("score") is not None:
        return f"{human(eng['score'])}pts {human(eng.get('comments') or 0)}c"
    if eng.get("views") is not None:
        return f"{human(eng['views'])} views"
    if eng.get("likes") is not None:
        return f"{human(eng['likes'])}likes"
    return f"#{srank}"


def since_to_iso(spec, now):
    m = re.fullmatch(r"(\d+)([mhd])", spec or "")
    if not m:
        raise Usage(f"--since takes a duration like 90m, 12h or 7d, not {spec!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    return (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def fts_query(q):
    """Turn what an agent types into a valid FTS5 query."""
    toks = re.findall(r'"[^"]*"|\S+', q or "")
    pos, neg = [], []
    for t in toks:
        if t in ("OR", "AND"):
            pos.append(t)
            continue
        if t.startswith('"'):
            inner = t.strip('"').replace('"', "")
            if inner:
                pos.append(f'"{inner}"')
            continue
        is_neg = t.startswith("-") and len(t) > 1
        if is_neg:
            t = t[1:]
        star = t.endswith("*")
        w = t.rstrip("*").replace('"', "")
        w = w.strip(",;:!?()[]{}")
        if not w:
            continue
        term = f'"{w}"' + ("*" if star else "")
        (neg if is_neg else pos).append(term)
    while pos and pos[0] in ("OR", "AND"):
        pos.pop(0)
    while pos and pos[-1] in ("OR", "AND"):
        pos.pop()
    if not pos:
        raise Usage('search needs at least one word to look for, e.g. intel search "Nvidia CEO"')
    expr = " ".join(pos)
    if neg:
        expr = f"({expr})" + "".join(f" NOT {n}" for n in neg)
    return expr


# ---------------------------------------------------------------- database
def day_db():
    """Today's items, read-only: the file is root's and immutable for the whole run."""
    path = os.environ.get("INTEL_DB", "")
    if not path or not os.path.exists(path):
        raise NotFound("today's data is not loaded (INTEL_DB). Tell Tin in your final message.")
    uri = "file:" + urllib.parse.quote(path) + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def meta(db):
    return {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM meta")}


def now_of(db):
    return parse_ts(meta(db).get("now")) or datetime.now(UTC)


# ---------------------------------------------------------------- Tin's intel service
class ServiceError(Exception):
    code = 2


def service(method, path, body=None):
    """One call to this run's intel routes. Tin's refusals come back as sentences."""
    base = os.environ.get("INTEL_URL", "").rstrip("/")
    grant = os.environ.get("INTEL_GRANT", "")
    if not base.startswith("https://") or not grant:
        raise ServiceError(
            "this run has no intel grant (INTEL_URL, INTEL_GRANT); tell Tin in your final message"
        )
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(  # noqa: S310 — https checked above
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + grant,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "tin-intel/" + VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read() or b"{}").get("error")
        except ValueError:
            said = None
        raise ServiceError(said or f"Tin refused the request (HTTP {exc.code})") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServiceError(
            f"Tin's intel service didn't answer ({type(exc).__name__}); try again in a minute"
        ) from None


def match_sources(db, spec, single=False):
    keys = [r["skey"] for r in db.execute("SELECT skey FROM sources ORDER BY skey")]
    if not spec:
        return keys
    s = slug(spec)
    hit = (
        [k for k in keys if k == s]
        or [k for k in keys if k.startswith(s)]
        or [k for k in keys if s in k]
    )
    if not hit:
        raise NotFound(f"no source matches {spec!r}. See the keys with: intel sources")
    if single and len(hit) > 1:
        raise Usage(
            f"{spec!r} matches {len(hit)} sources ({', '.join(hit[:8])}{'...' if len(hit) > 8 else ''}); "
            f"name one"
        )
    return hit


def row_line(r, now):
    dup = f"  [+{r['dups']} dup]" if r["dups"] else ""
    # A Google News link's domain says nothing; its outlet does.
    shown = r["source"] if r["domain"] == "news.google.com" and r["source"] else r["domain"]
    dom = f" ({shown})" if shown else ""
    title = (r["title"] or (r["text"] or "")[:120]).replace("\t", " ").replace("\n", " ")
    return f"{r['id']}\t{r['skey']}\t{r['rlabel']}\t{age(r['published'], now)}\t{title}{dom}{dup}"


def item(db, iid):
    r = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not r:
        raise NotFound(f'no item {iid} today. Find ids with: intel search "<words>"')
    return r


# ---------------------------------------------------------------- load
def read_snapshot(path):
    info = os.stat(path)
    if info.st_size > MAX_INPUT_BYTES:
        raise Usage("the snapshot is larger than 64 MB")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        first = handle.readline()
        head = json.loads(first) if first.strip() else {}
        if head.get("kind") != "meta" or head.get("format") != SNAPSHOT_FORMAT:
            raise Usage(f"not an {SNAPSHOT_FORMAT} snapshot")
        rows = [json.loads(line) for line in handle if line.strip()]
    return head, rows


def cmd_load(a):
    head, rows = read_snapshot(a.input)
    if os.path.exists(a.db):
        os.remove(a.db)
    db = sqlite3.connect(a.db)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE sources(skey TEXT PRIMARY KEY, name TEXT, stype TEXT, n INTEGER);
      CREATE TABLE source_status(id TEXT, name TEXT, status TEXT, items INTEGER, error TEXT);
      CREATE TABLE items(id TEXT PRIMARY KEY, curl TEXT UNIQUE, skey TEXT, source TEXT, stype TEXT,
        url TEXT, durl TEXT, title TEXT, author TEXT, published TEXT, text TEXT, eng TEXT,
        srank INTEGER, rlabel TEXT, domain TEXT, ntitle TEXT, dup_of TEXT, dups INTEGER DEFAULT 0,
        dup_sources TEXT DEFAULT '');
      CREATE VIRTUAL TABLE items_fts USING fts5(title, text, content='items', content_rowid='rowid',
        tokenize='porter unicode61');
      CREATE TABLE prior_stories(sid TEXT PRIMARY KEY, key TEXT, title TEXT, summary TEXT,
        relevance TEXT, tags TEXT, sensitive INTEGER, continues TEXT, first_day TEXT, last_day TEXT);
      CREATE TABLE prior_story_items(sid TEXT, day TEXT, item_id TEXT, url TEXT, title TEXT,
        source TEXT);
    """)
    items = [r for r in rows if r.get("kind") == "item"]
    # A source is the configured one (`source_id`), named as its config names it: its items'
    # ranks are its own order. An item's `source` stays the outlet (a Google News item's
    # publisher), which `intel read` and the row's label show.
    names = {str(s.get("id")): s.get("name") for s in head.get("sources") or [] if s.get("id")}
    by_src = {}
    for r in items:
        name = names.get(str(r.get("source_id"))) or r.get("source") or "unknown"
        by_src.setdefault(name, []).append(r)
    # Each source's own order: its popularity number if it has one, else its feed position.
    ranked = []
    for name, group in by_src.items():
        if any(metric(i.get("engagement") or {}) is not None for i in group):
            group.sort(key=lambda i: -(metric(i.get("engagement") or {}) or 0))
        else:
            group.sort(
                key=lambda i: (
                    i.get("rank") or 10**6,
                    -(
                        parse_ts(i.get("published_at")) or datetime.min.replace(tzinfo=UTC)
                    ).timestamp(),
                )
            )
        for n, i in enumerate(group, 1):
            ranked.append((n, name, i))
    ranked.sort(key=lambda t: t[0])  # top-ranked items claim their URL and title first
    ids, by_ntitle, src_count = {}, {}, {}
    for n, name, i in ranked:
        cu = canon(i.get("url"))
        if not cu:
            continue
        sk = slug(name)
        src_count[sk] = src_count.get(sk, 0) + 1
        db.execute(
            "INSERT OR IGNORE INTO sources VALUES(?,?,?,0)", (sk, name, i.get("source_type"))
        )
        existing = db.execute("SELECT id FROM items WHERE curl=?", (cu,)).fetchone()
        if existing:
            db.execute(
                "UPDATE items SET dups=dups+1, dup_sources=dup_sources||? WHERE id=?",
                (name + "; ", existing["id"]),
            )
            continue
        iid = i.get("item_key") or short_id(cu)
        k = len(iid)
        while iid in ids and ids[iid] != cu:
            k += 1
            iid = short_id(cu, k)
        ids[iid] = cu
        eng = i.get("engagement") or {}
        nt = norm_title(i.get("title"))
        dup_of = by_ntitle.get(nt) if len(nt) >= 25 else None
        if dup_of:
            db.execute(
                "UPDATE items SET dups=dups+1, dup_sources=dup_sources||? WHERE id=?",
                (name + "; ", dup_of),
            )
        elif len(nt) >= 25:
            by_ntitle[nt] = iid
        db.execute(
            "INSERT INTO items(id,curl,skey,source,stype,url,durl,title,author,published,text,eng,srank,"
            "rlabel,domain,ntitle,dup_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                iid,
                cu,
                sk,
                i.get("source") or name,
                i.get("source_type"),
                i.get("url"),
                i.get("discussion_url"),
                i.get("title"),
                i.get("author"),
                i.get("published_at"),
                i.get("text") or "",
                json.dumps(eng),
                n,
                rank_label(eng, n),
                domain(i.get("url")),
                nt,
                dup_of,
            ),
        )
    for sk, c in src_count.items():
        db.execute("UPDATE sources SET n=? WHERE skey=?", (c, sk))
    db.execute("INSERT INTO items_fts(rowid,title,text) SELECT rowid,title,text FROM items")
    for s in head.get("sources") or []:
        db.execute(
            "INSERT INTO source_status VALUES(?,?,?,?,?)",
            (s.get("id"), s.get("name"), s.get("status"), s.get("items"), s.get("error")),
        )
    for r in rows:
        if r.get("kind") == "story":
            db.execute(
                "INSERT OR REPLACE INTO prior_stories VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    r["sid"],
                    r["key"],
                    r["title"],
                    r["summary"],
                    r.get("relevance"),
                    ",".join(r.get("tags") or []),
                    1 if r.get("sensitive") else 0,
                    r.get("continues"),
                    r.get("first_day"),
                    r.get("last_day"),
                ),
            )
        elif r.get("kind") == "story_item":
            db.execute(
                "INSERT INTO prior_story_items VALUES(?,?,?,?,?,?)",
                (
                    r["sid"],
                    r.get("day"),
                    r.get("item_key"),
                    r.get("url"),
                    r.get("title"),
                    r.get("source"),
                ),
            )
    for k in ("scope", "day", "now", "client_name", "world_day", "world_status"):
        db.execute("INSERT INTO meta VALUES(?,?)", (k, head.get(k)))
    db.commit()
    n_items = db.execute("SELECT count(*) FROM items").fetchone()[0]
    n_dup = db.execute("SELECT count(*) FROM items WHERE dup_of IS NOT NULL").fetchone()[0]
    db.close()
    print(
        f"loaded {len(items)} rows into {n_items} items ({n_dup} title duplicates) from "
        f"{len(src_count)} sources -> {a.db}"
    )


# ---------------------------------------------------------------- status
def today_stories():
    return service("GET", "/stories")


def cmd_status(a):
    db = day_db()
    m, now = meta(db), now_of(db)
    n = db.execute("SELECT count(*) FROM items WHERE dup_of IS NULL").fetchone()[0]
    d = db.execute("SELECT count(*) FROM items WHERE dup_of IS NOT NULL").fetchone()[0]
    ns = db.execute("SELECT count(*) FROM sources").fetchone()[0]
    today = m.get("day")
    state = today_stories()
    touched = state.get("stories") or []
    prior = db.execute("SELECT count(*) FROM prior_stories").fetchone()[0]
    covered = {
        x["item_key"] for s in touched for x in s.get("items") or [] if x.get("day") == today
    }
    tops = [r["id"] for r in db.execute("SELECT id FROM items WHERE dup_of IS NULL AND srank<=3")]
    open_tops = [t for t in tops if t not in covered]
    brief = state.get("brief") or {}
    lines = [
        f"# status {today} (as of {now:%Y-%m-%d %H:%MZ})",
        f"items\t{n} (+{d} duplicate copies folded in)",
        f"sources\t{ns}",
        f"stories\t{len(touched)} touched today, {prior} from earlier days",
        f"items in today's stories\t{len(covered)}",
        f"top-3 items of every source not yet in a story\t{len(open_tops)} of {len(tops)}",
        "brief\t" + ("set" if brief.get("state") == "set" else "missing (fallback will be used)"),
    ]
    if m.get("scope") == "client":
        lines.append(
            f"world stories\t{m.get('world_status') or 'missing'}"
            + (f" ({m.get('world_day')})" if m.get("world_day") else "")
        )
    out = Out("\n".join(lines))
    # Failed and skipped sources are gaps worth naming; an empty one usually had a quiet day,
    # and a client can have hundreds, so they are counted unless --all asks for them.
    rows = db.execute(
        "SELECT id, name, status, error FROM source_status WHERE status <> 'ok' ORDER BY status, id"
    ).fetchall()
    gaps = [r for r in rows if r["status"] != "empty"]
    empty = [r for r in rows if r["status"] == "empty"]
    out.add(f"sources failed or skipped\t{len(gaps)}")
    for r in gaps:
        out.add(f"  {r['id']}\t{r['status']}\t{(r['error'] or '')[:160]}")
    if empty:
        listed = getattr(a, "all", False)
        out.add(
            f"sources with nothing in the window\t{len(empty)}"
            + ("" if listed else " (intel status --all lists them)")
        )
        for r in empty if listed else ():
            out.add(f"  {r['id']}\tempty")
    out.done("-- next: intel fronts, or intel stories --since 7d to see what is already covered")


# ---------------------------------------------------------------- read commands
def cmd_sources(a):
    db = day_db()
    rows = db.execute("SELECT skey, name, n FROM sources ORDER BY n DESC").fetchall()
    out = Out(f"# sources ({len(rows)})")
    for r in rows:
        out.add(f"{r['skey']}\t{r['n']}\t{r['name']}")
    out.done("-- use a key, or part of one, with --source; browse one with intel browse <key>")


def cmd_fronts(a):
    db, now = day_db(), None
    now = now_of(db)
    keys = match_sources(db, a.source)
    lines = []
    for k in keys:
        rows = db.execute(
            "SELECT * FROM items WHERE skey=? AND dup_of IS NULL ORDER BY srank LIMIT ?", (k, a.n)
        ).fetchall()
        total = db.execute(
            "SELECT count(*) FROM items WHERE skey=? AND dup_of IS NULL", (k,)
        ).fetchone()[0]
        if rows:
            lines.append(f"## {k} ({len(rows)} of {total})")
            lines += [row_line(r, now) for r in rows]
    shown = lines[a.offset :]
    out = Out(
        f"# fronts: top {a.n} of {len(keys)} sources (lines {a.offset + 1}-, of {len(lines)})"
    )
    used = 0
    for ln in shown:
        if not out.add(ln):
            break
        used += 1
    if a.offset + used < len(lines):
        out.done(
            f"-- cut at the size cap: {len(lines) - a.offset - used} more lines. "
            f"next: intel fronts{' --source ' + a.source if a.source else ''} --n {a.n} --offset {a.offset + used}"
        )
    else:
        out.done("-- end. Go deeper on one source with intel browse <key> --from " + str(a.n + 1))


def run_search(db, q, a, limit, offset):
    where, args = ["items_fts MATCH ?", "i.dup_of IS NULL"], [fts_query(q)]
    if getattr(a, "source", None):
        keys = match_sources(db, a.source)
        where.append(f"i.skey IN ({','.join('?' * len(keys))})")
        args += keys
    if getattr(a, "since", None):
        where.append("i.published >= ?")
        args.append(since_to_iso(a.since, now_of(db)))
    base = f"FROM items_fts JOIN items i ON i.rowid=items_fts.rowid WHERE {' AND '.join(where)}"
    total = db.execute(f"SELECT count(*) {base}", args).fetchone()[0]
    order = {
        "match": "bm25(items_fts, 5.0, 1.0)",
        "rank": "i.srank, bm25(items_fts, 5.0, 1.0)",
        "new": "i.published DESC",
    }[getattr(a, "sort", None) or "match"]
    rows = db.execute(
        f"SELECT i.*, snippet(items_fts, 1, '[', ']', '...', 14) AS snip {base} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return total, rows


def cmd_search(a):
    db = day_db()
    now = now_of(db)
    try:
        total, rows = run_search(db, a.query, a, a.limit, a.offset)
    except sqlite3.OperationalError as e:
        raise Usage(
            f'search could not parse {a.query!r} ({e}). Use plain words, "a phrase", OR, -word'
        ) from None
    how = {
        "match": "best match first",
        "rank": "highest on its source first",
        "new": "newest first",
    }[a.sort]
    out = Out(f'# search "{a.query}" ({len(rows)} of {total}, {how})')
    shown = 0
    for r in rows:
        snip = (r["snip"] or "").replace("\n", " ").strip()
        if not out.add(
            row_line(r, now) + (f"\n\t{snip}" if snip and snip != (r["title"] or "") else "")
        ):
            break
        shown += 1
    nxt = a.offset + shown
    if total == 0:
        out.done("-- no matches. Try fewer or broader words, a prefix* or OR")
    elif nxt < total:
        srt = f" --sort {a.sort}" if a.sort != "match" else ""
        out.done(
            f'-- {total - nxt} more: intel search "{a.query}"{srt} --offset {nxt}'
            f' | narrow: --source <key>, --since 12h, or more words | sizes: intel count "{a.query}" --by source'
        )
    else:
        out.done("-- end of matches")


def cmd_count(a):
    db = day_db()
    try:
        q = fts_query(a.query)
    except Usage:
        raise
    base = "FROM items_fts JOIN items i ON i.rowid=items_fts.rowid WHERE items_fts MATCH ?"
    total = db.execute(f"SELECT count(*), coalesce(sum(i.dups),0) {base}", (q,)).fetchone()
    if a.by == "hour":
        rows = db.execute(
            f"SELECT substr(i.published,1,13) AS k, count(*) AS n {base} GROUP BY k ORDER BY k",
            (q,),
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT i.skey AS k, count(*) AS n {base} GROUP BY k ORDER BY n DESC", (q,)
        ).fetchall()
    out = Out(
        f'# count "{a.query}" by {a.by}: {total[0]} items (+{total[1]} duplicate copies), '
        f"{len(rows)} {'sources' if a.by == 'source' else 'hours'}"
    )
    for r in rows:
        out.add(f"{r['k']}\t{r['n']}")
    out.done('-- see them: intel search "' + a.query + '"')


def cmd_similar(a):
    db = day_db()
    now = now_of(db)
    r = item(db, a.id)
    words = [
        w
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-']+", r["title"] or "")
        if w.lower() not in STOP and len(w) > 2
    ][:10]
    if not words:
        raise Usage(f"{a.id} has no title words to compare; read it with intel read {a.id}")
    q = " OR ".join(f'"{w}"' for w in words)
    total = db.execute(
        "SELECT count(*) FROM items_fts JOIN items i ON i.rowid=items_fts.rowid "
        "WHERE items_fts MATCH ? AND i.id<>?",
        (q, a.id),
    ).fetchone()[0]
    rows = db.execute(
        "SELECT i.* FROM items_fts JOIN items i ON i.rowid=items_fts.rowid WHERE items_fts MATCH ? "
        "AND i.id<>? ORDER BY bm25(items_fts, 5.0, 1.0) LIMIT ?",
        (q, a.id, a.limit),
    ).fetchall()
    out = Out(
        f"# similar to {a.id}: {r['title']}  (best {len(rows)} of {total} sharing words; "
        f"{r['dups']} exact copies already folded in)"
    )
    if r["dups"]:
        out.add(f"copies\t{r['dup_sources'].strip('; ')}")
    for x in rows:
        out.add(row_line(x, now) + ("  (copy of " + x["dup_of"] + ")" if x["dup_of"] else ""))
    out.done(
        '-- best matches first; the tail drifts to other stories. Narrow with intel search "<key words>"'
    )


def cmd_browse(a):
    db = day_db()
    now = now_of(db)
    (k,) = match_sources(db, a.source, single=True)
    total = db.execute(
        "SELECT count(*) FROM items WHERE skey=? AND dup_of IS NULL", (k,)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT * FROM items WHERE skey=? AND dup_of IS NULL ORDER BY srank LIMIT ? OFFSET ?",
        (k, a.limit, a.from_ - 1),
    ).fetchall()
    out = Out(f"# browse {k} (ranks {a.from_}-{a.from_ + len(rows) - 1} of {total})")
    shown = 0
    for r in rows:
        if not out.add(row_line(r, now)):
            break
        shown += 1
    nxt = a.from_ + shown
    out.done(
        f"-- {total - nxt + 1} more: intel browse {k} --from {nxt}"
        if nxt <= total
        else "-- end of source"
    )


def cmd_read(a):
    if len(a.ids) > 1:
        # Several ids share one call's budget, so a batch never overruns the harness's output cut.
        db = day_db()
        for iid in a.ids:
            item(db, iid)
        per = max(60, min(a.max_words, (CAP // len(a.ids) - 600) // 7))
        if per < a.max_words:
            # Say so up front: an agent that asked for more otherwise reads each item again.
            print(
                f"# {len(a.ids)} items share this call's ~9 KB: at most {per} words each "
                f"(you asked for {a.max_words}). Read a long or primary source on its own."
            )
            print()
        for n, iid in enumerate(a.ids):
            if n:
                print()
            one = argparse.Namespace(
                ids=[iid], max_words=per, from_word=a.from_word, budget=CAP // len(a.ids)
            )
            cmd_read(one)
        return
    db = day_db()
    r = item(db, a.ids[0])
    eng = json.loads(r["eng"] or "{}")
    words = (r["text"] or "").split()
    start = max(0, a.from_word)
    chunk = words[start : start + a.max_words]
    head = [
        f"# read {r['id']}: {r['title'] or ''}",
        f"source\t{r['source']} ({r['skey']}), rank {r['rlabel']}",
        f"published\t{r['published']}",
        f"url\t{r['url']}",
    ]
    if r["durl"]:
        head.append(f"discussion\t{r['durl']}")
    if r["author"]:
        head.append(f"author\t{r['author']}")
    if eng:
        head.append("engagement\t" + ", ".join(f"{k} {v}" for k, v in eng.items() if v is not None))
    if r["dups"]:
        head.append(f"also carried by\t{r['dup_sources'].strip('; ')}")
    if r["dup_of"]:
        head.append(f"copy of\t{r['dup_of']}")
    out = Out("\n".join(head))
    if getattr(a, "budget", None):
        out.cap = a.budget
    out.add("")
    out.add(" ".join(chunk) if chunk else "(no text beyond the title)")
    end = start + len(chunk)
    out.done(
        f"-- words {start + 1}-{end} of {len(words)}; more: intel read {r['id']} --from-word {end}"
        if end < len(words)
        else "-- end of text"
    )


# ---------------------------------------------------------------- stories
def merged_stories(db, since):
    """Earlier days' stories from the snapshot and today's from Tin; today's win."""
    out = {}
    for r in db.execute(
        "SELECT s.*, (SELECT count(*) FROM prior_story_items x WHERE x.sid=s.sid) AS n "
        "FROM prior_stories s WHERE last_day >= ?",
        (since,),
    ):
        out[r["sid"]] = {
            **dict(r),
            "tags": [t for t in (r["tags"] or "").split(",") if t],
            "sensitive": bool(r["sensitive"]),
            "items": None,
        }
    for s in today_stories().get("stories") or []:
        out[s["sid"]] = {**s, "n": len(s.get("items") or [])}
    return list(out.values())


def cmd_stories(a):
    db = day_db()
    now = now_of(db)
    since = since_to_iso(a.since, now)[:10] if a.since else "0000-00-00"
    rows = merged_stories(db, since)
    for w in (a.q or "").split():
        w = w.lower()
        rows = [
            r
            for r in rows
            if w in " ".join(str(r.get(k) or "") for k in ("title", "summary", "key")).lower()
            or w in ",".join(r.get("tags") or [])
        ]
    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(
        key=lambda r: (
            -int((r["last_day"] or "0").replace("-", "") or 0),
            order.get(r.get("relevance"), 3),
            r["sid"],
        )
    )
    if a.json:
        out = Out(f"# stories as JSON ({len(rows)})")
        for r in rows:
            r = {k: v for k, v in r.items() if k != "n"}
            if not out.add(json.dumps(r, ensure_ascii=False)):
                break
        out.done("-- cut at the size cap; narrow with --since or --q" if out.cut else None)
        return
    out = Out(
        f"# stories ({len(rows)}{', since ' + a.since if a.since else ''}{', matching ' + a.q if a.q else ''})"
    )
    for r in rows:
        flag = " SENSITIVE" if r.get("sensitive") else ""
        cont = f" (continues {r['continues']})" if r.get("continues") else ""
        out.add(
            f"{r['sid']}\t{r['last_day']}\t{r.get('relevance') or '-'}{flag}\t{r['n']} items\t{r['title']}{cont}"
        )
    out.done("-- details: intel story show <sid>")


def print_write(result, verb):
    msg = f"{verb} {result['sid']}\t+{result.get('added', 0)} items, {result.get('total', 0)} in total"
    if result.get("missing"):
        msg += (
            f"\tnot found: {','.join(result['missing'])} (ids come from intel search/fronts; "
            "an earlier story's items keep their ids)"
        )
    print(msg)
    for w in result.get("warnings") or []:
        print(f"warning: {w}")


def item_ids(text):
    return [x.strip() for x in (text or "").split(",") if x.strip()]


MAX_STORIES_PER_FILE = 6
STORY_FIELDS = ("key", "title", "summary", "items", "relevance", "tags", "sensitive", "continues")


def story_body(value, where):
    """One story from a JSON file as the request Tin takes. The shell never sees its text."""
    if not isinstance(value, dict):
        raise Usage(f"{where} must be a JSON object with key, title, summary and items")
    extra = sorted(set(value) - set(STORY_FIELDS))
    if extra:
        raise Usage(f"{where} has fields Tin doesn't use: {', '.join(extra[:5])}")
    key = value.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", key):
        raise Usage(f"{where}: key is a lowercase slug like nvidia-ceo-reinstated")
    items = value.get("items", [])
    if isinstance(items, str):
        items = item_ids(items)
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise Usage(f'{where}: items is a list of ids such as ["k3f9a", "p0qe2"]')
    body = {"action": "put", "key": key, "items": [i.strip() for i in items if i.strip()]}
    for name in ("title", "summary", "relevance", "continues"):
        if value.get(name) is not None:
            body[name] = value[name]
    if value.get("tags") is not None:
        tags = value["tags"]
        body["tags"] = item_ids(tags) if isinstance(tags, str) else tags
    if value.get("sensitive") is not None:
        body["sensitive"] = value["sensitive"]
    return body


def put_stories_file(path):
    """`story put --json <file>`: one story or a short list, sent one at a time, so a refused
    story doesn't stop the others. Exit 2 when any was refused."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        raise NotFound(f"no file {path}") from None
    except ValueError as exc:
        raise Usage(f"{path} is not valid JSON ({exc})") from None
    stories = value if isinstance(value, list) else [value]
    if not stories:
        raise Usage(f"{path} holds no story")
    if len(stories) > MAX_STORIES_PER_FILE:
        raise Usage(
            f"{path} holds {len(stories)} stories; send at most {MAX_STORIES_PER_FILE} per call "
            "so each turn stays short"
        )
    bodies = [
        story_body(story, f"story {n}" if len(stories) > 1 else path)
        for n, story in enumerate(stories, 1)
    ]
    refused = 0
    for body in bodies:
        try:
            result = service("POST", "/story", body)
        except ServiceError as exc:
            refused += 1
            print(f"error: {body['key']}: {exc}")
            continue
        print_write(result, "created" if result.get("created") else "updated")
    if refused:
        print(
            f"error: {refused} of {len(bodies)} stories were refused; fix them and send again",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_story(a):
    if a.action == "show":
        r = service("GET", "/story/" + urllib.parse.quote(a.sid, safe=""))
        out = Out(f"# story {r['sid']}: {r['title']}")
        for k in ("relevance", "continues", "first_day", "last_day"):
            if r.get(k):
                out.add(f"{k}\t{r[k]}")
        if r.get("tags"):
            out.add("tags\t" + ",".join(r["tags"]))
        if r.get("sensitive"):
            out.add("sensitive\tyes")
        out.add(f"summary\t{r['summary']}")
        for x in r.get("items") or []:
            out.add(
                f"{x['item_key']}\t{x['day']}\t{x['source']}\t{x.get('title') or ''}\t{x['url']}"
            )
        out.done()
        return
    if a.action == "attach":
        result = service(
            "POST", "/story", {"action": "attach", "sid": a.sid, "items": item_ids(a.items)}
        )
        print_write(result, "attached to")
        return
    if a.json_file:
        flags = [
            n
            for n in ("key", "title", "summary", "items", "relevance", "tags", "continues")
            if getattr(a, n)
        ]
        if flags or a.sensitive or a.not_sensitive:
            raise Usage("story put --json takes the story from the file; leave out the other flags")
        put_stories_file(a.json_file)
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", a.key or ""):
        raise Usage(
            "--key is a lowercase slug like nvidia-ceo-reinstated (letters, digits, hyphens)"
        )
    if a.relevance and a.relevance not in ("high", "medium", "low"):
        raise Usage("--relevance is high, medium or low")
    body = {"action": "put", "key": a.key, "items": item_ids(a.items)}
    for name in ("title", "summary", "relevance", "continues"):
        if getattr(a, name):
            body[name] = getattr(a, name)
    if a.tags is not None:
        body["tags"] = item_ids(a.tags)
    if a.sensitive or a.not_sensitive:
        body["sensitive"] = bool(a.sensitive)
    result = service("POST", "/story", body)
    print_write(result, "created" if result.get("created") else "updated")


def cmd_tags(a):
    r = service("GET", "/help")
    print("# tags a story may carry (others are dropped)\n" + ", ".join(r.get("tags") or []))


def cmd_brief(a):
    if a.action == "schema":
        print(service("GET", "/help").get("brief_schema", ""))
        return
    if not a.file:
        raise Usage("intel brief put <file.json>")
    try:
        with open(a.file, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        raise NotFound(f"no file {a.file}") from None
    except ValueError as exc:
        raise Usage(f"{a.file} is not valid JSON ({exc})") from None
    result = service("PUT", "/brief", value)
    print(
        "brief saved"
        + (
            f" with {len(result.get('warnings') or [])} warning(s)"
            if result.get("warnings")
            else ""
        )
    )
    for w in result.get("warnings") or []:
        print(f"warning: {w}")


# ---------------------------------------------------------------- main
class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(f"{message}. Run intel --help for the commands and examples")


def main(argv=None):
    import signal

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    p = Parser(prog="intel", add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd", parser_class=Parser)
    s = sub.add_parser("load")
    s.add_argument("--input", required=True)
    s.add_argument("--db", required=True)
    s = sub.add_parser("status")
    s.add_argument("--all", action="store_true")
    sub.add_parser("sources")
    sub.add_parser("tags")
    s = sub.add_parser("fronts")
    s.add_argument("--source")
    s.add_argument("--n", type=int, default=5)
    s.add_argument("--offset", type=int, default=0)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--source")
    s.add_argument("--since")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--sort", choices=["match", "rank", "new"], default="match")
    s = sub.add_parser("count")
    s.add_argument("query")
    s.add_argument("--by", choices=["source", "hour"], default="source")
    s = sub.add_parser("similar")
    s.add_argument("id")
    s.add_argument("--limit", type=int, default=15)
    s = sub.add_parser("browse")
    s.add_argument("source")
    s.add_argument("--from", dest="from_", type=int, default=1)
    s.add_argument("--limit", type=int, default=25)
    s = sub.add_parser("read")
    s.add_argument("ids", nargs="+")
    s.add_argument("--max-words", type=int, default=600)
    s.add_argument("--from-word", dest="from_word", type=int, default=0)
    s = sub.add_parser("stories")
    s.add_argument("--since")
    s.add_argument("--q")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("story")
    s.add_argument("action", choices=["show", "put", "attach"])
    s.add_argument("sid", nargs="?")
    s.add_argument("items_pos", nargs="?")
    s.add_argument("--key")
    s.add_argument("--title")
    s.add_argument("--summary")
    s.add_argument("--items")
    s.add_argument("--relevance")
    s.add_argument("--tags")
    s.add_argument("--sensitive", action="store_true")
    s.add_argument("--not-sensitive", dest="not_sensitive", action="store_true")
    s.add_argument("--continues")
    s.add_argument("--json", dest="json_file")
    s = sub.add_parser("brief")
    s.add_argument("action", choices=["schema", "put"])
    s.add_argument("file", nargs="?")
    try:
        a = p.parse_args(argv)
        if a.version:
            print(VERSION)
            return
        if a.help or not a.cmd:
            print(HELP)
            return
        if a.cmd == "story":
            if a.action in ("show", "attach") and not a.sid:
                raise Usage(f"intel story {a.action} needs a story id like s-nvidia-ceo-reinstated")
            if a.action == "attach":
                a.items = a.items or a.items_pos
                if not a.items:
                    raise Usage("intel story attach <sid> id,id")
            if a.action == "put" and not a.key and not a.json_file:
                raise Usage("intel story put needs --json <file> (or --key <slug> and flags)")
            if a.sensitive and a.not_sensitive:
                raise Usage("use --sensitive or --not-sensitive, not both")
        for lim in ("limit", "n", "max_words"):
            if hasattr(a, lim) and getattr(a, lim) is not None and getattr(a, lim) < 1:
                raise Usage(f"--{lim.replace('_', '-')} must be at least 1")
        {
            "load": cmd_load,
            "status": cmd_status,
            "sources": cmd_sources,
            "fronts": cmd_fronts,
            "search": cmd_search,
            "count": cmd_count,
            "similar": cmd_similar,
            "browse": cmd_browse,
            "read": cmd_read,
            "stories": cmd_stories,
            "story": cmd_story,
            "tags": cmd_tags,
            "brief": cmd_brief,
        }[a.cmd](a)
    except (Usage, NotFound, ServiceError) as e:
        fail(e)


if __name__ == "__main__":
    main()

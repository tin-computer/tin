"""The judges' intel CLI: loading a snapshot, the 9 KB cap, search, ids, reads, the read-only
day file, and story and brief calls to a stand-in for Tin's intel routes."""

from __future__ import annotations

import datetime as dt
import gzip
import http.server
import ipaddress
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from intel_cli_loader import PATH, load

from tin_lite.intel_fetch.items import assign_keys, canon

CLI = load()
GRANT = "grant-for-tests"


def items(count=400):
    rows = []
    for n in range(count):
        source = ["BBC News", "Reuters via Google News", "Hacker News"][n % 3]
        engagement = {"points": 1000 - n, "comments": n} if source == "Hacker News" else {}
        rows.append(
            {
                "source_type": "world",
                "source": source,
                "source_id": source.lower().replace(" ", "-"),
                "url": f"https://www.example{n % 7}.com/story/{n}?utm_source=x",
                "discussion_url": None,
                "title": f"Story number {n} about {'Nvidia chips' if n % 5 == 0 else 'weather'}",
                "author": None,
                "published_at": "2026-11-24T05:00:00Z",
                "text": ("word " * 900) if n == 3 else f"Body of story {n}. GPT-5 and U.S. news.",
                "engagement": engagement,
                "rank": n // 3 + 1,
                "fetched_at": "2026-11-24T06:00:00Z",
            }
        )
    return rows


def snapshot(path: Path, rows: list[dict], *, prior=True) -> Path:
    keys = assign_keys([canon(r["url"]) for r in rows])
    lines = [
        {
            "kind": "meta",
            "format": "intel-snapshot/1",
            "scope": "world",
            "day": "2026-11-24",
            "now": "2026-11-24T06:00:00Z",
            "sources": [
                {"id": "ft", "name": "FT", "status": "failed", "items": 0, "error": "HTTP 403"},
                {"id": "quiet-one", "name": "Q1", "status": "empty", "items": 0, "error": None},
                {"id": "quiet-two", "name": "Q2", "status": "empty", "items": 0, "error": None},
            ],
        }
    ]
    lines += [{"kind": "item", "item_key": keys[canon(r["url"])], **r} for r in rows]
    if prior:
        lines += [
            {
                "kind": "story",
                "sid": "s-chip-curbs",
                "key": "chip-curbs",
                "title": "US widens chip export curbs",
                "summary": "Yesterday's story.",
                "relevance": "high",
                "tags": ["ai", "policy"],
                "sensitive": False,
                "continues": None,
                "first_day": "2026-11-23",
                "last_day": "2026-11-23",
            },
            {
                "kind": "story_item",
                "sid": "s-chip-curbs",
                "day": "2026-11-23",
                "item_key": "abcde",
                "url": "https://old.example/a",
                "title": "Old",
                "source": "BBC News",
            },
        ]
    target = path / "snap.jsonl.gz"
    target.write_bytes(gzip.compress("".join(json.dumps(x) + "\n" for x in lines).encode()))
    return target


@pytest.fixture
def day(tmp_path):
    db = tmp_path / "day.db"
    run(["load", "--input", str(snapshot(tmp_path, items())), "--db", str(db)], check=True)
    os.chmod(db, 0o444)
    return db


def run(args, *, env=None, check=False):
    result = subprocess.run(  # noqa: S603 — the CLI under test
        [sys.executable, "-I", str(PATH), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def test_load_builds_items_sources_and_prior_stories(day):
    db = sqlite3.connect(day)
    assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 400
    assert db.execute("SELECT count(*) FROM prior_stories").fetchone()[0] == 1
    meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
    assert meta["scope"] == "world" and meta["day"] == "2026-11-24"
    # Ids come from the snapshot, so they match Tin's item_key.
    first = items()[0]
    key = assign_keys([canon(first["url"])])[canon(first["url"])]
    assert (
        db.execute("SELECT id FROM items WHERE curl = ?", (canon(first["url"]),)).fetchone()[0]
        == key
    )


def test_load_refuses_anything_but_a_snapshot(tmp_path):
    bad = tmp_path / "x.gz"
    bad.write_bytes(gzip.compress(b'{"kind": "item"}\n'))
    result = run(["load", "--input", str(bad), "--db", str(tmp_path / "d.db")])
    assert result.returncode == 2 and "not an intel-snapshot/1 snapshot" in result.stderr


def test_every_listing_stays_under_the_cap_and_says_what_was_cut(day):
    env = {"INTEL_DB": str(day)}
    for args in (
        ["fronts", "--n", "100"],
        ["search", "story", "--limit", "400"],
        ["browse", "bbc"],
    ):
        result = run(args, env=env, check=True)
        body = result.stdout.rsplit("\n-- ", 1)[0]
        assert len(body.encode()) <= CLI.CAP, args
        assert "\n-- " in result.stdout
    cut = run(["fronts", "--n", "100"], env=env, check=True).stdout
    assert "cut at the size cap" in cut and "--offset" in cut


def test_search_rewrites_what_an_agent_types(day):
    env = {"INTEL_DB": str(day)}
    found = run(["search", "Nvidia chips", "--limit", "3"], env=env, check=True).stdout
    assert found.startswith('# search "Nvidia chips" (3 of ')
    assert run(["search", "GPT-5 U.S.", "--limit", "1"], env=env, check=True).returncode == 0
    assert run(["search", '"Nvidia chips" -weather', "--limit", "1"], env=env, check=True)
    assert CLI.fts_query('GPT-5 "exact phrase" -noise pre*') == (
        '("GPT-5" "exact phrase" "pre"*) NOT "noise"'
    )
    empty = run(["search", ", ;"], env=env)
    assert empty.returncode == 2 and "at least one word" in empty.stderr


def test_read_shares_one_budget_across_ids(day):
    env = {"INTEL_DB": str(day)}
    db = sqlite3.connect(day)
    long_id = db.execute("SELECT id FROM items WHERE length(text) > 3000").fetchone()[0]
    others = [r[0] for r in db.execute("SELECT id FROM items WHERE id <> ? LIMIT 5", (long_id,))]
    one = run(["read", long_id], env=env, check=True).stdout
    assert "--from-word 600" in one
    many = run(["read", long_id, *others], env=env, check=True).stdout
    assert len(many.encode()) <= CLI.CAP + 600
    # The agent is told the batch cut each item, so it doesn't read them all again.
    assert many.startswith("# 6 items share this call's ~9 KB: at most 128 words each")
    assert "(you asked for 600). Read a long or primary source on its own." in many
    short = run(["read", *others[:2], "--max-words", "50"], env=env, check=True).stdout
    assert "share this call" not in short
    missing = run(["read", "zzzzz"], env=env)
    assert missing.returncode == 3 and "no item zzzzz" in missing.stderr


def test_the_day_file_is_opened_read_only(day, monkeypatch):
    monkeypatch.setenv("INTEL_DB", str(day))
    db = CLI.day_db()
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM items")


# Tin's intel routes, stood in by a local HTTPS server.


def certificate(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def service(tmp_path, day):
    seen: list[tuple[str, str, object]] = []
    today = {
        "sid": "s-nvidia-ceo",
        "key": "nvidia-ceo",
        "title": "Nvidia reinstates its CEO",
        "summary": "Today.",
        "relevance": "high",
        "tags": ["ai"],
        "sensitive": False,
        "continues": None,
        "first_day": "2026-11-24",
        "last_day": "2026-11-24",
        "items": [
            {
                "day": "2026-11-24",
                "item_key": "xxxxx",
                "url": "https://a/b",
                "title": "t",
                "source": "BBC News",
            }
        ],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append((self.command, self.path, body))
            if self.headers.get("Authorization") != f"Bearer {GRANT}":
                return self.reply(401, {"error": "active run grant required"})
            if self.path == "/internal/run-tools/intel/stories":
                return self.reply(200, {"stories": [today], "brief": {"state": "missing"}})
            if self.path == "/internal/run-tools/intel/help":
                return self.reply(200, {"brief_schema": "SCHEMA TEXT", "tags": ["ai", "tech"]})
            if self.path.startswith("/internal/run-tools/intel/story/"):
                return self.reply(200, today)
            if self.path == "/internal/run-tools/intel/story":
                if body.get("key") == "too-many":
                    return self.reply(429, {"error": "this run has written 400 stories"})
                return self.reply(
                    200,
                    {
                        "sid": "s-" + (body.get("key") or body.get("sid", "").removeprefix("s-")),
                        "created": True,
                        "added": 1,
                        "total": 1,
                        "missing": ["zzzzz"],
                        "warnings": ["dropped tags outside the taxonomy: made-up"],
                    },
                )
            if self.path == "/internal/run-tools/intel/brief":
                if not body.get("headline"):
                    return self.reply(400, {"error": "`headline` must be text and can't be empty."})
                return self.reply(200, {"ok": True, "warnings": ["item 1: link removed"]})
            return self.reply(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = handle_any

    cert, key = certificate(tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        "INTEL_DB": str(day),
        "INTEL_URL": f"https://127.0.0.1:{server.server_address[1]}/internal/run-tools/intel",
        "INTEL_GRANT": GRANT,
        "SSL_CERT_FILE": str(cert),
    }
    yield env, seen
    server.shutdown()


def test_story_put_sends_fields_and_prints_tins_answer(service):
    env, seen = service
    result = run(
        [
            "story",
            "put",
            "--key",
            "nvidia-ceo",
            "--title",
            "Nvidia reinstates its CEO",
            "--summary",
            "Today.",
            "--items",
            "abcde,zzzzz",
            "--relevance",
            "high",
            "--tags",
            "ai,made-up",
            "--sensitive",
        ],
        env=env,
        check=True,
    )
    method, path, body = seen[-1]
    assert (method, path) == ("POST", "/internal/run-tools/intel/story")
    assert body == {
        "action": "put",
        "key": "nvidia-ceo",
        "items": ["abcde", "zzzzz"],
        "title": "Nvidia reinstates its CEO",
        "summary": "Today.",
        "relevance": "high",
        "tags": ["ai", "made-up"],
        "sensitive": True,
    }
    assert result.stdout.startswith("created s-nvidia-ceo\t+1 items, 1 in total\tnot found: zzzzz")
    assert "warning: dropped tags" in result.stdout


def test_attach_and_a_refusal_prints_the_sentence_with_exit_2(service):
    env, seen = service
    run(["story", "attach", "s-nvidia-ceo", "abcde"], env=env, check=True)
    assert seen[-1][2] == {"action": "attach", "sid": "s-nvidia-ceo", "items": ["abcde"]}
    refused = run(["story", "put", "--key", "too-many", "--title", "t", "--summary", "s"], env=env)
    assert refused.returncode == 2
    assert refused.stderr.strip() == "error: this run has written 400 stories"


def test_status_and_stories_merge_prior_and_today(service):
    env, _seen = service
    status = run(["status"], env=env, check=True).stdout
    assert "1 touched today, 1 from earlier days" in status
    assert "brief\tmissing (fallback will be used)" in status
    assert "ft\tfailed\tHTTP 403" in status
    # Empty sources are counted, not listed: Databasin had 225 of them, 3 to 5 KB a call.
    assert "sources failed or skipped\t1" in status
    assert "sources with nothing in the window\t2 (intel status --all lists them)" in status
    assert "quiet-one" not in status
    every = run(["status", "--all"], env=env, check=True).stdout
    assert "  quiet-one\tempty" in every and "  quiet-two\tempty" in every
    stories = run(["stories", "--since", "7d"], env=env, check=True).stdout
    assert "s-nvidia-ceo\t2026-11-24\thigh" in stories and "s-chip-curbs\t2026-11-23" in stories
    assert "SCHEMA TEXT" in run(["brief", "schema"], env=env, check=True).stdout
    assert "ai, tech" in run(["tags"], env=env, check=True).stdout


def test_brief_put_reports_warnings_and_refusals(service, tmp_path):
    env, seen = service
    good = tmp_path / "brief.json"
    good.write_text(json.dumps({"schema": "intel-world-brief/1", "headline": "H", "groups": []}))
    result = run(["brief", "put", str(good)], env=env, check=True)
    assert seen[-1][0] == "PUT" and "warning: item 1: link removed" in result.stdout
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "intel-world-brief/1"}))
    refused = run(["brief", "put", str(bad)], env=env)
    assert refused.returncode == 2 and "`headline` must be text" in refused.stderr
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert run(["brief", "put", str(broken)], env=env).returncode == 2


def test_without_a_grant_the_cli_says_so(day):
    result = run(["status"], env={"INTEL_DB": str(day)})
    assert result.returncode == 2 and "no intel grant" in result.stderr


def test_version():
    assert run(["--version"], check=True).stdout.strip() == CLI.VERSION == "2"


def test_story_put_json_keeps_dollar_amounts_and_sends_a_short_list(service, tmp_path):
    """Prod 2026-11-26: `--summary "...$850M..."` in double quotes reached Tin as "50M". A
    story file never passes through the shell."""
    env, seen = service
    one = tmp_path / "story.json"
    one.write_text(
        json.dumps(
            {
                "key": "workday-c1ai",
                "title": "Workday closes its $850M C1.ai deal",
                "summary": "Workday paid about $850M; Morgan Stanley cut its target to $175.",
                "items": ["abcde"],
                "relevance": "high",
                "tags": ["competitor", "m-and-a"],
                "continues": "s-workday-q3",
            }
        )
    )
    result = run(["story", "put", "--json", str(one)], env=env, check=True)
    assert result.stdout.startswith("created s-workday-c1ai")
    assert seen[-1][2] == {
        "action": "put",
        "key": "workday-c1ai",
        "items": ["abcde"],
        "title": "Workday closes its $850M C1.ai deal",
        "summary": "Workday paid about $850M; Morgan Stanley cut its target to $175.",
        "relevance": "high",
        "tags": ["competitor", "m-and-a"],
        "continues": "s-workday-q3",
    }
    batch = tmp_path / "stories.json"
    batch.write_text(
        json.dumps(
            [
                {"key": "first-story", "title": "T", "summary": "S", "items": "abcde,fghij"},
                {"key": "too-many", "title": "T", "summary": "S"},
                {"key": "third-story", "title": "T", "summary": "S", "sensitive": True},
            ]
        )
    )
    before = len(seen)
    refused = run(["story", "put", "--json", str(batch)], env=env)
    # One refused story doesn't stop the others; the call exits 2 and names it.
    assert refused.returncode == 2
    assert [body["key"] for _m, _p, body in seen[before:]] == [
        "first-story",
        "too-many",
        "third-story",
    ]
    assert seen[before][2]["items"] == ["abcde", "fghij"]
    assert "error: too-many: this run has written 400 stories" in refused.stdout
    assert "1 of 3 stories were refused" in refused.stderr


def test_story_put_json_refuses_a_bad_file_before_calling_tin(service, tmp_path):
    """Plausible but unusable files: too many stories, an unknown field, flags mixed in."""
    env, seen = service
    before = len(seen)
    many = tmp_path / "many.json"
    many.write_text(
        json.dumps([{"key": f"story-{n}", "title": "T", "summary": "S"} for n in range(7)])
    )
    assert "at most 6 per call" in run(["story", "put", "--json", str(many)], env=env).stderr
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"key": "a-story", "headline": "H"}))
    assert (
        "fields Tin doesn't use: headline"
        in run(["story", "put", "--json", str(odd)], env=env).stderr
    )
    mixed = run(["story", "put", "--json", str(odd), "--title", "T"], env=env)
    assert mixed.returncode == 2 and "leave out the other flags" in mixed.stderr
    assert len(seen) == before


def test_a_feed_is_one_source_and_its_items_keep_their_outlets(tmp_path):
    """Ranks are a configured source's own order, so fronts group by `source_id` and take its
    name from the status lines; a Google News item still shows its outlet."""

    def gn(n, outlet):
        return {
            "source_type": "world",
            "source": outlet,
            "source_id": "gn-business",
            "url": f"https://news.google.com/rss/articles/CBMiExampleArticle{n:04d}",
            "discussion_url": None,
            "publisher_site": None,
            "title": f"Business story {n}",
            "author": None,
            "published_at": "2026-11-24T05:00:00Z",
            "text": "",
            "engagement": {},
            "rank": n,
            "fetched_at": "2026-11-24T06:00:00Z",
        }

    path = snapshot(tmp_path, [gn(1, "Reuters"), gn(2, "The New York Times"), gn(3, "Reuters")])
    raw = gzip.decompress(path.read_bytes()).decode().splitlines()
    meta = json.loads(raw[0])
    meta["sources"].append(
        {"id": "gn-business", "name": "Google News business", "status": "ok", "items": 3}
    )
    path.write_bytes(gzip.compress(("\n".join([json.dumps(meta), *raw[1:]]) + "\n").encode()))
    db = tmp_path / "gn.db"
    run(["load", "--input", str(path), "--db", str(db)], check=True)
    sources = run(["sources"], env={"INTEL_DB": str(db)}, check=True).stdout
    assert "gn-business\t3\tGoogle News business" in sources
    fronts = run(["fronts", "--n", "3"], env={"INTEL_DB": str(db)}, check=True).stdout
    lines = [line for line in fronts.splitlines() if "\tgn-business\t" in line]
    assert [line.split("\t")[2] for line in lines] == ["#1", "#2", "#3"]
    assert lines[1].endswith("(The New York Times)")

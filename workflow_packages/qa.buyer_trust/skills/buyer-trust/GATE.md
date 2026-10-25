# Trust gate

Run this block exactly as written. After the fetches in SKILL.md step 3, save the observed
evidence as one JSON object in a scratch file, then call `gate(evidence, as_of)` with today's
UTC date and use its result for the verdict, the evidence table, the ranked fixes and the
hand-off. The rules live here so that two runs on the same evidence give the same verdict and
the same fixes. Do not re-rank or re-verdict by hand; if you disagree with a result, say why
under Interpretation next to that fix.

## Evidence object

Every key is required, except a missing `refund_checked` in older evidence is treated as
`false`. `null` means the check was not made or the sandbox refused it; it is never the
same as a failed check.

```json
{
  "origin": "https://example.com",
  "status": {
    "home": 200,
    "http_redirects_to_https": true,
    "privacy": 200,
    "security_txt": 404,
    "robots": 200,
    "git_head": 404,
    "env": 404
  },
  "headers": {
    "hsts": "max-age=31536000; includeSubDomains",
    "csp": "",
    "x_frame_options": "DENY",
    "referrer_policy": "",
    "x_content_type_options": "nosniff"
  },
  "security_txt": {"contact": null, "expires": null},
  "mixed_content": [],
  "public_contact": "https://example.com/contact",
  "checkout": {
    "guest_option": null,
    "total_before_pay": true,
    "contact_url": "https://example.com/contact",
    "refund_url": null,
    "refund_checked": false
  }
}
```

- `status.*`: the HTTP status code of the first response to one GET, or `null`. `home` is
  the origin root after following redirects. `privacy` is the privacy page linked from the
  footer (or `/privacy`). `http_redirects_to_https` is `true` or `false` from the plain-`http`
  probe, `null` when the sandbox refused it.
- `headers.*`: the exact response header value from the home GET, `""` when the header was
  absent. When `status.home` is `null` every header is ignored and reported `unknown`.
- `security_txt`: the `Contact:` and `Expires:` values, `null` when absent or not fetched.
- `mixed_content`: the `http://` script, style, image or form-action URLs that fetched
  `https` pages load (at most 10), `[]` when there are none, `null` when not checked.
- `public_contact`: a monitored contact address or page the site itself publishes (an
  `https://` URL or `mailto:` address), `null` when none was found. It is what a
  `security.txt` may name; never guess an address.
- `checkout`: `null` when no pricing or checkout page was reached. Otherwise
  `guest_option` and `total_before_pay` are `true`, `false` or `null` (not visible without
  signing in), and `contact_url` / `refund_url` are the `https://` pages linked within one
  click of pricing or checkout, or `null`. A refund or cancellation section in Terms counts;
  its link need not be labelled "Refunds". Set `refund_checked` to `true` only after reading
  the relevant linked policy pages and checking for refund or cancellation terms. Keep it
  `false` or `null` when the pages were unread, unreachable or outside this run's depth.
  A link alone cannot verify its content. With `refund_checked: true`, `refund_url: null`
  means the completed check found no accessible linked terms. This checks presence and
  access, never legal adequacy. Use the latest signup walkthrough's `## Billing` and
  `## Steps` for what lies behind signup; never sign in to find out.

## Ranks and owners

- `fix_now`: a careful buyer stops here. `fix_week`: a buyer notices. `roadmap`: worth doing.
- The verdict is `FAIL` when any `fix_now` item exists, `UNVERIFIED` when none exists but a
  gating signal (home headers, `/.git/HEAD`, `/.env`, privacy) is `unknown`, else `PASS`.
- `owner: site.health_improve` means one bounded repository change that the built-in
  workflow may make: it never changes product claims, pricing, legal text, analytics,
  dependencies, CI or deployment configuration. Each such fix carries the exact
  `site.health_improve` inputs; its context says to return no change when the fix only
  lives in host or deployment configuration, in which case the founder does it.
- `owner: founder` means a decision, policy text, host setting or secret rotation that no
  Tin workflow may make.

```python
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

HSTS_MIN_AGE = 31536000
MAX_OBSERVED = 80
MAX_CONTEXT = 2000
MAX_MIXED = 10
STATUS_KEYS = (
    "home",
    "privacy",
    "security_txt",
    "robots",
    "git_head",
    "env",
)
HEADER_KEYS = ("hsts", "csp", "x_frame_options", "referrer_policy", "x_content_type_options")
CHECKOUT_KEYS = ("guest_option", "total_before_pay", "contact_url", "refund_url", "refund_checked")
TOP_KEYS = (
    "origin",
    "status",
    "headers",
    "security_txt",
    "mixed_content",
    "public_contact",
    "checkout",
)
RANKS = ("fix_now", "fix_week", "roadmap")
HEADER_VALUES = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
HEADER_LABELS = {
    "x_frame_options": "X-Frame-Options",
    "x_content_type_options": "X-Content-Type-Options",
    "referrer_policy": "Referrer-Policy",
}


def _keys(value, field, keys):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ValueError(f"{field} has unsupported keys: {unknown}")
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(f"{field} is missing keys: {missing}")
    return value


def _origin(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("origin must be an https origin")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
        raise ValueError("origin must be an https origin without a path")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("origin must be an https origin without a query or credentials")
    return f"https://{parts.netloc.lower()}"


def _status(value, field):
    if value is None:
        return None
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError(f"{field} must be an HTTP status code or null")
    return value


def _mixed(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_MIXED:
        raise ValueError(f"mixed_content must be a list of at most {MAX_MIXED} URLs or null")
    for url in value:
        if not isinstance(url, str) or not url.startswith("http://") or len(url) > 500:
            raise ValueError("mixed_content entries must be the http:// URLs observed")
    return sorted(set(value))


def _flag(value, field):
    if value is not None and type(value) is not bool:
        raise ValueError(f"{field} must be true, false or null")
    return value


def _text(value, field, *, limit=2000):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field} must be a string of at most {limit} characters or null")
    return value.strip()


def _link(value, field, *, mailto=False):
    value = _text(value, field, limit=500)
    if not value:
        return None
    if value.startswith("https://") or (mailto and value.startswith("mailto:")):
        return value
    raise ValueError(f"{field} must be an https:// URL" + (" or mailto: address" if mailto else ""))


def check(evidence):
    """Validate the observed evidence; raise ValueError on anything unusable."""
    evidence = _keys(evidence, "evidence", TOP_KEYS)
    status = _keys(evidence["status"], "status", STATUS_KEYS + ("http_redirects_to_https",))
    headers = _keys(evidence["headers"], "headers", HEADER_KEYS)
    security_txt = _keys(evidence["security_txt"], "security_txt", ("contact", "expires"))
    clean = {
        "origin": _origin(evidence["origin"]),
        "status": {key: _status(status[key], f"status.{key}") for key in STATUS_KEYS},
        "headers": {key: _text(headers[key], f"headers.{key}") for key in HEADER_KEYS},
        "security_txt": {
            "contact": _text(security_txt["contact"], "security_txt.contact", limit=500),
            "expires": _text(security_txt["expires"], "security_txt.expires", limit=64),
        },
        "mixed_content": _mixed(evidence["mixed_content"]),
        "public_contact": _link(evidence["public_contact"], "public_contact", mailto=True),
        "checkout": None,
    }
    clean["status"]["http_redirects_to_https"] = _flag(
        status["http_redirects_to_https"], "status.http_redirects_to_https"
    )
    if clean["status"]["home"] is not None:
        for key in HEADER_KEYS:
            if clean["headers"][key] is None:
                raise ValueError(f"headers.{key} must be the observed value or empty")
    if evidence["checkout"] is not None:
        checkout = evidence["checkout"]
        if isinstance(checkout, dict):
            checkout = {"refund_checked": False, **checkout}
        checkout = _keys(checkout, "checkout", CHECKOUT_KEYS)
        clean["checkout"] = {
            "guest_option": _flag(checkout["guest_option"], "checkout.guest_option"),
            "total_before_pay": _flag(checkout["total_before_pay"], "checkout.total_before_pay"),
            "contact_url": _link(checkout["contact_url"], "checkout.contact_url"),
            "refund_url": _link(checkout["refund_url"], "checkout.refund_url"),
            "refund_checked": _flag(checkout["refund_checked"], "checkout.refund_checked"),
        }
    return clean


def _hsts_age(value):
    for part in (value or "").lower().split(";"):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return int(part.split("=", 1)[1].strip('"'))
            except ValueError:
                return None
    return None


def _expiry(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as error:
        raise ValueError("security_txt.expires must be ISO-8601") from error


def _handoff(origin, focus, issue, change):
    context = (
        f"Buyer-trust audit (reports/BUYER_TRUST.md): {issue} Make only this change: {change} "
        "If it can only be made in host or deployment configuration, return no change and "
        "name where it lives so the founder can make it."
    )
    if len(context) > MAX_CONTEXT:
        raise ValueError("hand-off context exceeds site.health_improve's 2000 characters")
    return {"site_url": f"{origin}/", "focus": focus, "change_budget": 1, "context": context}


def gate(evidence, as_of):
    """Deterministic PASS/FAIL/UNVERIFIED verdict, evidence rows, ranked fixes and hand-off."""
    today = date.fromisoformat(as_of)
    ev = check(evidence)
    origin, status, headers = ev["origin"], ev["status"], ev["headers"]
    fetched = status["home"] is not None
    rows, fixes, gating_unknown = [], [], []

    def row(signal, observed, result):
        shown = "not checked" if observed is None else str(observed) or "(absent)"
        rows.append({"signal": signal, "observed": shown[:MAX_OBSERVED], "result": result})

    def fix(fix_id, rank, text, owner, handoff=None):
        item = {"id": fix_id, "rank": rank, "fix": text, "owner": owner}
        if handoff is not None:
            item["site_health_improve"] = handoff
        fixes.append(item)

    # Transport: HSTS and the plain-http redirect.
    if not fetched:
        row("Strict-Transport-Security", None, "unknown")
        gating_unknown.append("home headers")
    else:
        age = _hsts_age(headers["hsts"])
        ok = age is not None and age >= HSTS_MIN_AGE
        row("Strict-Transport-Security", headers["hsts"], "pass" if ok else "fix")
        if not ok:
            fix(
                "hsts",
                "fix_now",
                "Send `Strict-Transport-Security: max-age=31536000; includeSubDomains`.",
                "site.health_improve",
                _handoff(
                    origin,
                    "reliability",
                    f"{origin}/ returns Strict-Transport-Security "
                    f"'{headers['hsts'][:MAX_OBSERVED] or 'absent'}'.",
                    "send `Strict-Transport-Security: max-age=31536000; includeSubDomains` "
                    "on every response from the application's own header configuration.",
                ),
            )
    redirect = status["http_redirects_to_https"]
    row(
        "http:// redirects to https://",
        redirect,
        "unknown" if redirect is None else ("pass" if redirect else "fix"),
    )
    if redirect is False:
        fix(
            "https_redirect",
            "fix_now",
            "Redirect every plain-http request to https in the host settings.",
            "founder",
        )

    # Exposed repository or environment paths.
    for key, label in (("git_head", "/.git/HEAD"), ("env", "/.env")):
        code = status[key]
        if code is None:
            gating_unknown.append(label)
        row(
            f"{label} status",
            code,
            "unknown" if code is None else ("pass" if code == 404 else "fix"),
        )
    exposed = [
        label
        for key, label in (("git_head", "/.git/HEAD"), ("env", "/.env"))
        if status[key] is not None and status[key] != 404
    ]
    if exposed:
        fix(
            "exposed_paths",
            "fix_now",
            f"{' and '.join(exposed)} must return 404. If either returned 200, block it at the "
            "host and rotate any secret it held today, out of band.",
            "founder",
        )

    # Privacy page.
    privacy = status["privacy"]
    if privacy is None:
        gating_unknown.append("privacy")
    row(
        "Privacy page status",
        privacy,
        "unknown" if privacy is None else ("pass" if privacy == 200 else "fix"),
    )
    if privacy is not None and privacy != 200:
        fix(
            "privacy",
            "fix_now",
            "Publish a privacy policy and link Privacy plus Terms in the footer of every page, "
            "including checkout.",
            "founder",
        )

    # Mixed content.
    mixed = ev["mixed_content"]
    row(
        "No mixed content",
        None if mixed is None else (", ".join(mixed) or "none"),
        "unknown" if mixed is None else ("fix" if mixed else "pass"),
    )
    if mixed:
        fix(
            "mixed_content",
            "fix_now",
            "Load every script, style, image and form action over https.",
            "site.health_improve",
            _handoff(
                origin,
                "reliability",
                f"https pages on {origin} load {', '.join(mixed)[:600]}.",
                "change those http:// resource URLs in the repository to the https:// URL "
                "that serves the same file.",
            ),
        )

    # Other security headers.
    if fetched:
        csp = headers["csp"]
        csp_ok = "frame-ancestors" in csp.lower()
        row("Content-Security-Policy", csp, "pass" if csp_ok else "fix")
        missing = []
        for key, label in HEADER_LABELS.items():
            present = bool(headers[key])
            row(label, headers[key], "pass" if present else "fix")
            if not present:
                missing.append(label)
        if not csp_ok:
            missing.insert(0, "Content-Security-Policy")
        if missing:
            lines = "; ".join(f"`{label}: {HEADER_VALUES[label]}`" for label in missing)
            fix(
                "security_headers",
                "fix_week",
                f"Send {' and '.join(missing)} on every response (starters in playbook.md).",
                "site.health_improve",
                _handoff(
                    origin,
                    "reliability",
                    f"{origin}/ does not send {' and '.join(missing)}.",
                    f"send {lines} on every response. If a Content-Security-Policy already "
                    "exists, keep its sources and only add `frame-ancestors 'none'`.",
                ),
            )
    else:
        for label in ("Content-Security-Policy", *HEADER_LABELS.values()):
            row(label, None, "unknown")

    # security.txt.
    sec = status["security_txt"]
    contact = ev["security_txt"]["contact"]
    expires = ev["security_txt"]["expires"]
    sec_ok = sec == 200 and bool(contact)
    row(
        "security.txt Contact",
        None if sec is None else (contact or f"status {sec}"),
        "unknown" if sec is None else ("pass" if sec_ok else "fix"),
    )
    expiry_ok = False
    if expires:
        expiry_ok = _expiry(expires) > today
    if sec is not None:
        row("security.txt Expires", expires or "", "pass" if expiry_ok else "fix")
    if sec is not None and (not sec_ok or not expiry_ok):
        public = ev["public_contact"]
        text = "Serve `/.well-known/security.txt` with `Contact:` and a future `Expires:`."
        if public:
            fix(
                "security_txt",
                "fix_week",
                text,
                "site.health_improve",
                _handoff(
                    origin,
                    "reliability",
                    f"{origin}/.well-known/security.txt returns {sec}; missing: "
                    + " and ".join(
                        part
                        for part, ok in (
                            ("a Contact line", sec_ok),
                            ("a future Expires", expiry_ok),
                        )
                        if not ok
                    )
                    + ".",
                    f"serve /.well-known/security.txt as text/plain with `Contact: {public}` "
                    f"and `Expires: {(today + timedelta(days=365)).isoformat()}T00:00:00Z`.",
                ),
            )
        else:
            fix(
                "security_txt",
                "fix_week",
                text + " Choose a monitored security contact first.",
                "founder",
            )

    # robots.txt.
    robots = status["robots"]
    row(
        "/robots.txt status",
        robots,
        "unknown" if robots is None else ("pass" if robots < 500 else "fix"),
    )
    if robots is not None and robots >= 500:
        fix(
            "robots",
            "fix_week",
            "Serve `/robots.txt` without a server error.",
            "site.health_improve",
            _handoff(
                origin,
                "technical_seo",
                f"{origin}/robots.txt returns {robots}.",
                "serve a static robots.txt that allows the public pages already linked from home.",
            ),
        )

    # Checkout trust surface.
    checkout = ev["checkout"]
    if checkout is None:
        row("Checkout", None, "unknown")
    else:
        total = checkout["total_before_pay"]
        guest = checkout["guest_option"]
        row(
            "Total shown before pay",
            total,
            "unknown" if total is None else ("pass" if total else "fix"),
        )
        row(
            "Guest or no-account option",
            guest,
            "unknown" if guest is None else ("pass" if guest else "fix"),
        )
        row(
            "Contact within one click",
            checkout["contact_url"] or "",
            "pass" if checkout["contact_url"] else "fix",
        )
        row(
            "Refund terms within one click",
            (checkout["refund_url"] or "") if checkout["refund_checked"] else None,
            "unknown"
            if not checkout["refund_checked"]
            else ("pass" if checkout["refund_url"] else "fix"),
        )
        if total is False:
            fix(
                "total_before_pay",
                "fix_now",
                "Show the line-item total before the buyer pays.",
                "founder",
            )
        if not checkout["contact_url"]:
            fix(
                "contact",
                "fix_week",
                "Link a real contact page or monitored inbox, with a response expectation, "
                "within one click of pricing or checkout.",
                "founder",
            )
        if checkout["refund_checked"] and not checkout["refund_url"]:
            fix(
                "refund",
                "fix_week",
                "Link existing refund or cancellation terms within one click of checkout; "
                "publish terms only if none exist.",
                "founder",
            )
        if guest is False:
            fix(
                "guest_option",
                "roadmap",
                "Offer a guest or no-account path alongside account creation.",
                "founder",
            )

    fixes.sort(key=lambda item: RANKS.index(item["rank"]))
    if any(item["rank"] == "fix_now" for item in fixes):
        verdict = "FAIL"
    elif gating_unknown:
        verdict = "UNVERIFIED"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "unknown": gating_unknown,
        "rows": rows,
        "fixes": fixes,
        "evidence": ev,
        "as_of": today.isoformat(),
    }


def evidence_block(result):
    """The fenced JSON the next run reads back from reports/BUYER_TRUST.md."""
    payload = {
        "as_of": result["as_of"],
        "verdict": result["verdict"],
        "evidence": result["evidence"],
        "fixes": [
            {"id": item["id"], "rank": item["rank"], "owner": item["owner"]}
            for item in result["fixes"]
        ],
    }
    return "```tin-buyer-trust\n" + json.dumps(payload, indent=1, sort_keys=True) + "\n```"
```

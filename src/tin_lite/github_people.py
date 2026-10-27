"""GitHub reads for `managed.github`: the public GitHub account behind a person's email.

Built for looking up who a new signup is. One `people.lookup` call takes an email and,
optionally, the name the person gave, and answers with the public profiles that tie to them:

- `exact_email`: GitHub links a commit to an account only when that email is verified on it,
  so a commit search by author email names the account with no namesake risk;
- `email_commits_in_own_repos`: the owner of the repositories those commits went to, when its
  profile carries the name the commits were signed with (a second account of the same person);
- `handle_and_name`: when no commit ties, a search for the name the person gave, keeping an
  account whose login is the email's local part (`magictps972` is `magic-tps`) and whose
  profile name contains every word of that name.

A profile is projected to work facts: name, company, site, bio and the LinkedIn profile the
person linked themselves. Never their location, followers' names, email or personal social
accounts. GitHub's search allows 30 reads a minute with Tin's token (10 without one), so a
rate-limited lookup comes back as such and the caller carries on without it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tin_lite.connection_records import ServiceArgumentError, text

PROVIDER = "managed.github"
CAPABILITY = "people.read"
ORIGIN = "https://api.github.com"
# Under the gateway's 60-second per-call limit, for up to seven reads.
SECONDS = 8
CALL_SECONDS = 50
MAX_BYTES = 2_000_000
# Accounts kept from one commit search, most commits first.
MAX_ACCOUNTS = 2
EMAIL = re.compile(r"[^@\s\"'<>()]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}\Z")
# Links people put on their profile that are a phone number or a private messenger, not work.
PRIVATE_HOSTS = frozenset(
    {"wa.me", "api.whatsapp.com", "t.me", "telegram.me", "m.me", "signal.me", "discord.gg"}
)

Read = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Operation:
    arguments: frozenset[str]


OPERATIONS = {"people.lookup": Operation(frozenset({"email", "name"}))}


class Unanswered(Exception):
    """A read GitHub did not answer usably; `reason` is Tin's own word for it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def request_for(operation: str, args: dict) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise ServiceArgumentError("unknown operation")
    email = args.get("email")
    if not isinstance(email, str) or not EMAIL.fullmatch(email.strip()):
        raise ServiceArgumentError("email must be one email address")
    name = args.get("name")
    if name is not None and (
        not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or not name.isprintable()
    ):
        raise ServiceArgumentError("name must be 1-120 printable characters")
    return {"email": email.strip().casefold(), "name": name.strip() if name else None}


def words(value: Any) -> list[str]:
    """Letters of a name without accents or case: "Tomás Pastor" -> ["tomas", "pastor"]."""
    if not isinstance(value, str):
        return []
    plain = unicodedata.normalize("NFKD", value)
    plain = "".join(c for c in plain if not unicodedata.combining(c)).casefold()
    return re.findall(r"[a-z0-9]+", plain)


def same_person(given: str | None, profile: Any) -> bool:
    """Every word of the given name, at least two of them, is in the profile's name."""
    given_words = words(given)
    return len(given_words) >= 2 and set(given_words) <= set(words(profile))


def handles(email: str) -> list[str]:
    """Usernames the email's local part suggests: as written, then without trailing digits."""
    local = email.split("@", 1)[0].split("+", 1)[0]
    joined = re.sub(r"[^a-z0-9]", "", local.casefold())
    found = []
    for value in (joined, joined.rstrip("0123456789")):
        if len(value) >= 4 and value not in found:
            found.append(value)
    return found


def _flat(login: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", login.casefold()) if isinstance(login, str) else ""


def _site(value: Any) -> str | None:
    value = text(value, 300)
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").removeprefix("www.")
    if parts.scheme not in {"http", "https"} or "." not in host or host in PRIVATE_HOSTS:
        return None
    return value


def profile_record(user: dict, social: Any) -> dict[str, Any]:
    links = social if isinstance(social, list) else []
    linkedin = next(
        (
            _site(link.get("url"))
            for link in links
            if isinstance(link, dict) and link.get("provider") == "linkedin"
        ),
        None,
    )
    sites = [
        site
        for link in links
        if isinstance(link, dict) and link.get("provider") == "generic"
        for site in [_site(link.get("url"))]
        if site
    ]
    return {
        "login": text(user.get("login"), 39),
        "url": text(user.get("html_url"), 200),
        "name": text(user.get("name"), 120),
        "company": text(user.get("company"), 120),
        "site": _site(user.get("blog")),
        "bio": text(user.get("bio"), 300),
        "linkedin_url": linkedin,
        "links": sites[:3],
        "public_repos": user.get("public_repos") if type(user.get("public_repos")) is int else None,
    }


async def lookup(request: dict, read: Read) -> dict[str, Any]:
    """The accounts that tie to this email, each with how it ties. `read(path, params)` returns
    GitHub's parsed JSON, None for a 404, or raises Unanswered."""
    email, name = request["email"], request["name"]
    commits = await read(
        "/search/commits", {"q": f"author-email:{email}", "per_page": 30, "sort": "author-date"}
    )
    items = commits.get("items") if isinstance(commits, dict) else None
    linked: Counter[str] = Counter()
    owners: Counter[str] = Counter()
    names: Counter[str] = Counter()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        login = (item.get("author") or {}).get("login")
        if isinstance(login, str):
            linked[login] += 1
        owner = ((item.get("repository") or {}).get("owner") or {}).get("login")
        if isinstance(owner, str):
            owners[owner] += 1
        author = text(((item.get("commit") or {}).get("author") or {}).get("name"), 120)
        if author:
            names[author] += 1
    signed = names.most_common(1)[0][0] if names else None
    # (login, tie, commits): GitHub's own link first, then the owner of the repositories the
    # commits went to when that profile carries the name the commits were signed with.
    candidates = [(login, "exact_email", count) for login, count in linked.most_common()]
    candidates += [
        (login, "email_commits_in_own_repos", count)
        for login, count in owners.most_common(MAX_ACCOUNTS + 1)
        if login not in linked
    ]
    if not candidates and name:
        wanted = handles(email)
        found = await read("/search/users", {"q": f'"{name}" in:name type:user', "per_page": 30})
        users = found.get("items") if isinstance(found, dict) else None
        candidates = [
            (user["login"], "handle_and_name", None)
            for user in (users if isinstance(users, list) else [])
            if isinstance(user, dict) and _flat(user.get("login")) in wanted
        ]
    accounts = []
    for login, tie, count in candidates:
        if len(accounts) == MAX_ACCOUNTS:
            break
        user = await read(f"/users/{login}", {})
        if not isinstance(user, dict):
            continue
        if tie == "handle_and_name" and not same_person(name, user.get("name")):
            continue
        if tie == "email_commits_in_own_repos" and not same_person(signed, user.get("name")):
            continue
        social = await read(f"/users/{login}/social_accounts", {})
        accounts.append({**profile_record(user, social), "tie": tie, "commits": count})
    if not accounts:
        return {"email": email, "status": "not_found"}
    return {
        "email": email,
        "status": "found",
        "accounts": accounts,
        "commit_names": [author for author, _ in names.most_common(3)],
    }

"""Where a proposed website page appears, and whether it is live yet.

Tin never presents a guess as the final address. A delivery PR's recorded public URL, an
existing page that an update targets, or a folder route Tin confirmed on the live site is
where the page will be published; anything else is a proposed URL. After a merge or a direct
commit, a bounded public fetch must find the page, with its title, before Tin calls it live.

Reads for cards come from one Postgres projection per run. Provider reads (GitHub state and
public fetches) happen only in an explicit, rate-limited check.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from tin_lite import content_draft
from tin_lite.content_delivery import (
    ANSWER_PAGE_WORKFLOW_ID,
    DRAFT_WORKFLOW_ID,
    PUBLIC_ARTICLE_WORKFLOW_ID,
    slug_for,
)
from tin_lite.organic_audit import public_site

PAGE_WORKFLOW_IDS = frozenset(
    {DRAFT_WORKFLOW_ID, PUBLIC_ARTICLE_WORKFLOW_ID, ANSWER_PAGE_WORKFLOW_ID}
)
OPERATION = "page_url_projection_v1"
CHECK_EVERY = timedelta(minutes=10)
# A deploy normally lands within minutes; after this, a missing page is reported plainly.
DEPLOY_WINDOW = timedelta(minutes=30)
# Tin stops checking a merged page it has not found after this long.
CHECK_FOR = timedelta(days=30)
FOLDER_CHECK_EVERY = timedelta(hours=12)
CHECK_SECONDS = 20
# Runs whose inputs name the project's site, newest first, when a draft has no plan host.
SITE_INPUTS = (
    ("organic.traffic_system", "site_url"),
    ("organic.audit", "site_url"),
    ("organic.keyword_plan", "site_url"),
    ("visibility.audit", "target"),
)
PR_URL = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+$")
ROUTE_LINE = re.compile(
    r"(?im)^[\s>*_-]*public\s+(?:url|route)[*_]*\s*:[\s*_]*[`<]*(\S+?)[`>*_.]*\s*$"
)
ROUTE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,299}")


def key(run_id):
    return f"page-url:{UUID(str(run_id))}"


def site_host(value):
    """The public DNS host of a site input (origin, URL or bare domain), else None."""
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        return None
    value = value.strip()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        return public_site(f"https://{parsed.hostname}")[1]
    except ValueError:
        return None


def same_site(a, b):
    return bool(a and b) and a.removeprefix("www.") == b.removeprefix("www.")


def page_url(value, host=None):
    """A clean HTTPS page URL on the site (with or without www), else None."""
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 33 for c in value):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        if parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    own = site_host(value)
    path = parsed.path or "/"
    if (
        own is None
        or (host and not same_site(own, host))
        or not ROUTE_PATH.fullmatch(path)
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        return None
    return f"https://{own}{path}"


def route_url(route, host):
    """A route from a delivery ("/blog/x" or a full URL on the site) as a page URL."""
    if not isinstance(route, str) or not host:
        return None
    if route.startswith("/") and not route.startswith("//"):
        return page_url(f"https://{host}{route}", host)
    return page_url(route, host)


def public_route(body):
    """The public address an adaptation PR names on its own "Public URL:" line, if any."""
    match = ROUTE_LINE.search(body or "")
    if not match:
        return None
    value = match.group(1)
    if value.startswith("/") and not value.startswith("//") and ROUTE_PATH.fullmatch(value):
        return value
    return page_url(value)


def safe_pull_request(value):
    if not isinstance(value, dict) or not PR_URL.fullmatch(str(value.get("url") or "")):
        return None
    number = value.get("number")
    return {"url": value["url"], "number": number if type(number) is int else None}


def base_for(run, *, selection=None, site=None, title=None):
    """The page's address before delivery: the plan's destination, else a proposed slug."""
    title = title or run.artifact_title
    if run.workflow_id == DRAFT_WORKFLOW_ID:
        if not selection:
            return None
        item = selection.get("item") or {}
        host = site_host(selection.get("host"))
        title = title or item.get("title")
        destination = page_url(item.get("destination"), host)
        if destination and host:
            return {
                "url": destination,
                "host": host,
                "title": title,
                "source": "plan_destination",
                "final": item.get("action") == "update_page",
            }
    else:
        host = site_host(site)
    if not host or not title:
        return None
    return {
        "url": f"https://{host}/{slug_for(title, 'page')}",
        "host": host,
        "title": title,
        "source": "title_slug",
        "final": False,
    }


def _stem(path):
    return re.sub(r"\.mdx?$", "", str(path).rsplit("/", 1)[-1])


def _folder(path):
    return str(path).rsplit("/", 1)[0] if "/" in str(path) else ""


def _minutes_since(value, now):
    try:
        return (now - datetime.fromisoformat(value)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def present(record, delivery=None, *, now=None):
    """The card and API view: where the page is, what state it is in, in plain words."""
    record = record or {}
    base = record.get("base")
    if not base:
        return None
    now = now or datetime.now(UTC)
    check = record.get("check") or {}
    folder = record.get("folder") or {}
    approval = record.get("approval")
    delivery = delivery or None
    url, source, final = base["url"], base["source"], bool(base["final"])
    repository = (delivery or approval or {}).get("repository")
    path = (delivery or {}).get("path") or (approval or {}).get("path")
    file_path = path if isinstance(path, str) and re.search(r"\.mdx?$", path) else None
    routed = route_url((delivery or {}).get("public_route"), base["host"])
    if routed:
        url, source, final = routed, "delivery_route", True
    elif file_path and folder.get("folder") == _folder(file_path) and folder.get("pattern"):
        confirmed = route_url(folder["pattern"].replace("{slug}", _stem(file_path)), base["host"])
        if confirmed:
            url, source, final = confirmed, "folder_route", True
    pull_request = safe_pull_request((delivery or {}).get("pull_request"))
    commit = (delivery or {}).get("commit") if isinstance(delivery, dict) else None
    commit = commit if isinstance(commit, dict) and commit.get("commit") else None
    published = bool(commit) or bool(check.get("merged"))
    live = bool(check.get("live")) and check.get("url") == url
    site = base["host"]
    branch = (commit or {}).get("branch") or "its default branch"
    view = {
        "url": url,
        "source": source,
        "final": final,
        "pull_request": pull_request,
        "repository": repository,
        "file_path": file_path,
        "checked_at": check.get("checked_at"),
        "published_outside_tin": delivery is None and approval is None,
    }
    if live:
        return {
            **view,
            "state": "live",
            "label": "Live at",
            "note": "Tin found the page on your site.",
            "checkable": False,
        }
    no_route = (
        file_path
        and source not in {"delivery_route", "folder_route"}
        and folder.get("folder") == _folder(file_path)
        and folder.get("checked_at")
        and not folder.get("pattern")
    )
    if published:
        since = _minutes_since(check.get("merged_at"), now)
        waited = since is not None and since > DEPLOY_WINDOW.total_seconds() / 60
        where = f"Committed to {repository} {branch}" if commit else f"Merged into {repository}"
        if waited and check.get("checked_at"):
            note = f"{where}; not a page on {site} yet."
            if file_path:
                note += f" The file is {file_path}."
            if no_route:
                note += (
                    f" Tin found no page on your site that shows files from {_folder(file_path)}/."
                )
        else:
            note = f"{where}. Waiting for your site to deploy it."
        return {
            **view,
            "state": "merged",
            "label": "Will be published at" if final else "Proposed URL",
            "note": note,
            "checkable": since is None or since < CHECK_FOR.total_seconds() / 60,
        }
    state, label = ("planned", "Will be published at") if final else ("proposed", "Proposed URL")
    status = (delivery or {}).get("status")
    if delivery is None and approval is None:
        note = "Publishing happens outside Tin."
    elif status == "failed":
        note = "Delivery needs attention. Nothing is on your site yet."
    elif pull_request:
        number = f" #{pull_request['number']}" if pull_request["number"] else ""
        note = (
            f"Pull request{number} is open. The page appears after it merges and your site deploys."
        )
    elif (delivery or {}).get("system_run_id") or (delivery or {}).get("path") == (
        "Repository-adapted article"
    ):
        note = "After you approve, Tin adapts the article to your site and opens a pull request."
    elif file_path and no_route:
        note = (
            f"Tin found no page on your site that shows files from {_folder(file_path)}/. "
            f"Approving commits {file_path} to {repository} as a Markdown file only."
        )
    elif file_path and source == "folder_route":
        note = (
            f"Approving commits {file_path} to {repository}; your site shows that folder's files."
        )
    elif file_path:
        note = (
            f"Approving commits {file_path} to {repository}. Tin hasn't confirmed that your site "
            f"shows files from {_folder(file_path) or 'the repository root'}/ as pages."
        )
    else:
        note = "Tin opens a pull request after you approve."
    return {
        **view,
        "state": state,
        "label": label,
        "note": note,
        # An open PR is checked for its merge; a file path for the route that shows its folder.
        "checkable": bool(pull_request)
        or bool(file_path and repository and not folder.get("checked_at")),
    }


def page_found(title, final_url, url, status, content_type, text):
    """Live means a 200 page at the same address that carries the article's title."""
    if status != 200 or "html" not in (content_type or "text/html"):
        return False
    if urlsplit(final_url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
        return False
    words = " ".join(re.sub(r"[`*_#]", "", title or "").casefold().split())
    page = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).casefold().split())
    return bool(words) and words in page


async def fetch_page(url, *, client=None, resolver=None):
    """One vetted GET: public addresses only, same-site redirects, bounded body."""
    from tin_lite.growth_plan_site import REQUEST_SECONDS, _fetch

    own = client is None
    client = client or httpx.AsyncClient(
        trust_env=False,
        timeout=REQUEST_SECONDS,
        follow_redirects=False,
        headers={"Accept": "text/html,*/*;q=0.8"},
    )
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    try:
        return await _fetch(client, url, resolver)
    finally:
        if own:
            await client.aclose()


class PageUrls:
    def __init__(self, *, database, storage=None, integrations=None, fetch=None):
        self.db, self.storage, self.integrations = database, storage, integrations
        self.fetch = fetch or fetch_page

    async def _load(self, run_ids):
        rows = await self.db.pool.fetch(
            "SELECT execution_key, result FROM effect_receipts "
            "WHERE execution_key = ANY($1::text[]) AND operation = $2",
            [key(run_id) for run_id in run_ids],
            OPERATION,
        )
        return {
            row["execution_key"]: (
                json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
            )
            or {}
            for row in rows
        }

    async def _save(self, run_id, record):
        await self.db.pool.execute(
            "INSERT INTO effect_receipts (execution_key, operation, status, result) "
            "VALUES ($1, $2, 'completed', $3::jsonb) ON CONFLICT (execution_key) DO UPDATE "
            "SET result = EXCLUDED.result, updated_at = now() "
            "WHERE effect_receipts.operation = EXCLUDED.operation",
            key(run_id),
            OPERATION,
            json.dumps(record),
        )

    async def _selection(self, run_id):
        receipt = await self.db.get_effect(content_draft.selection_key(run_id))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def _site(self, project_id):
        rows = await self.db.pool.fetch(
            "SELECT executor, input FROM workflow_runs WHERE project_id = $1 "
            "AND status = 'succeeded' AND executor = ANY($2::text[]) "
            "ORDER BY created_at DESC LIMIT 10",
            project_id,
            [executor for executor, _ in SITE_INPUTS],
        )
        fields = dict(SITE_INPUTS)
        for row in rows:
            value = row["input"]
            value = json.loads(value) if isinstance(value, str) else value or {}
            host = site_host(value.get(fields.get(row["executor"], "site_url")))
            if host:
                return host
        return None

    async def _title(self, run):
        if run.artifact_title or not (self.storage and run.canonical_commit_sha):
            return run.artifact_title
        from tin_lite.content_delivery import ContentDelivery

        try:
            _raw, _article, title = await ContentDelivery(
                database=self.db, storage=self.storage
            ).document_source(run)
        except (ValueError, LookupError):
            return None
        return title

    async def _base(self, run):
        selection = None
        if run.workflow_id == DRAFT_WORKFLOW_ID:
            selection = await self._selection(run.id)
            return base_for(run, selection=selection)
        title = await self._title(run)
        if not title:
            return None
        return base_for(run, site=await self._site(run.project_id), title=title)

    async def _approval(self, run, delivery):
        """What Publish now or Open a pull request would write, before the reviewer chooses."""
        if (
            delivery is not None
            or run.review_decision == "approved"
            or run.status.value != "needs_input"
            or self.storage is None
        ):
            return None
        from tin_lite.content_delivery import (
            ContentDelivery,
            DeliverySettings,
            destination,
            document_destination,
        )

        service = ContentDelivery(
            database=self.db, storage=self.storage, integrations=self.integrations
        )
        try:
            program_id, selected = await service.program_for(run)
            configured = await service.settings(project_id=run.project_id, program_id=program_id)
            repository = (
                configured["settings"].get("repository") or configured["available_repository"]
            )
            if not repository:
                return None
            settings = DeliverySettings.model_validate(
                {**configured["settings"], "mode": "github_pr", "repository": repository}
            )
            if selected is not None:
                path, _ = destination(settings, selected)
            else:
                _raw, _article, title = await service.document_source(run)
                path, _ = document_destination(settings, title, run.id)
        except (ValueError, LookupError):
            return None
        return {"repository": repository, "path": path}

    async def view(self, run, delivery, *, check=False, now=None):
        """One run's page view; computes the projection if missing, then checks if asked."""
        if run.workflow_id not in PAGE_WORKFLOW_IDS:
            return None
        now = now or datetime.now(UTC)
        record = (await self._load([run.id])).get(key(run.id)) or {}
        changed = False
        if not record.get("base"):
            base = await self._base(run)
            if base:
                record["base"], changed = base, True
        fresh = _minutes_since(record.get("approval_at"), now)
        if delivery is not None or fresh is None or fresh >= CHECK_EVERY.total_seconds() / 60:
            # Delivery settings and the draft's title are read at most every ten minutes.
            approval = await self._approval(run, delivery)
            if approval != record.get("approval") or (approval and fresh is None):
                record["approval"], changed = approval, True
            if approval:
                record["approval_at"], changed = now.isoformat(), True
        if not record.get("base"):
            return None
        if check and self._due(record, delivery, now):
            record = await self._check(run, record, delivery, now)
            changed = True
        if changed:
            await self._save(run.id, record)
        return present(record, delivery, now=now)

    async def views(self, runs, deliveries, *, now=None):
        """Card polling: saved projections only, plus plan drafts computed from Postgres."""
        candidates = [run for run in runs if run.workflow_id in PAGE_WORKFLOW_IDS]
        if not candidates:
            return {}
        saved = await self._load([run.id for run in candidates])
        output = {}
        for run in candidates:
            record = saved.get(key(run.id)) or {}
            if not record.get("base") and run.workflow_id == DRAFT_WORKFLOW_ID:
                base = base_for(run, selection=await self._selection(run.id))
                if base:
                    record = {**record, "base": base}
            view = present(record, deliveries.get(run.id), now=now)
            if view:
                output[run.id] = view
        return output

    def _due(self, record, delivery, now):
        view = present(record, delivery, now=now)
        if not view or not view["checkable"] or self.integrations is None:
            return False
        last = (record.get("check") or {}).get("checked_at")
        since = _minutes_since(last, now)
        return since is None or since >= CHECK_EVERY.total_seconds() / 60

    async def _check(self, run, record, delivery, now):
        """Bounded provider reads: whether the change merged, whether the page is live, and
        which route (if any) shows the target folder's files on the site."""
        check = dict(record.get("check") or {})
        folder = dict(record.get("folder") or {})
        base = record["base"]
        view = present(record, delivery, now=now)
        stamp = now.isoformat()
        try:
            async with asyncio.timeout(CHECK_SECONDS):
                file_path = view.get("file_path")
                repository = view.get("repository")
                folder_age = _minutes_since(folder.get("checked_at"), now)
                if (
                    file_path
                    and repository
                    and not base["final"]
                    and view["source"] != "delivery_route"
                    and (
                        folder.get("folder") != _folder(file_path)
                        or folder_age is None
                        or folder_age >= FOLDER_CHECK_EVERY.total_seconds() / 60
                    )
                ):
                    folder = await self._folder_route(run, repository, file_path, base, stamp)
                    record = {**record, "folder": folder}
                    view = present(record, delivery, now=now)
                pull_request = view.get("pull_request")
                if not check.get("merged") and pull_request and pull_request["number"]:
                    state = await self.integrations.github_pull_request_state(
                        project_id=run.project_id,
                        repository=view["repository"],
                        number=pull_request["number"],
                    )
                    if state["merged"]:
                        check.update(merged=True, merged_at=state["merged_at"] or stamp)
                if (delivery or {}).get("commit") and not check.get("merged_at"):
                    check["merged_at"] = stamp
                if check.get("merged") or (delivery or {}).get("commit"):
                    url = view["url"]
                    try:
                        final_url, status, kind, text = await self.fetch(url)
                        found = page_found(base.get("title"), final_url, url, status, kind, text)
                    except (httpx.HTTPError, OSError, TimeoutError, ValueError, LookupError):
                        found = False
                    check.update(live=found, url=url)
        except (TimeoutError, ValueError, LookupError, httpx.HTTPError, OSError):
            pass
        except Exception as exc:  # integration errors: keep the saved state, say nothing new
            from tin_lite.integrations import IntegrationError

            if not isinstance(exc, IntegrationError):
                raise
        check["checked_at"] = stamp
        return {**record, "check": check, "folder": folder}

    async def _folder_route(self, run, repository, file_path, base, stamp):
        """Does the site show other files from this folder as pages, and at which route?

        Up to two existing files from the folder are tried under three common routes. A route
        counts only when a made-up slug under it does not also answer 200.
        """
        folder = _folder(file_path)
        result = {"folder": folder, "pattern": None, "checked_at": stamp}
        if not folder:
            return result
        names = await self.integrations.github_markdown_names(
            project_id=run.project_id, repository=repository, folder=folder
        )
        siblings = [name for name in names if name != file_path.rsplit("/", 1)[-1]][:2]
        if not siblings:
            return {**result, "reason": "no_other_files"}
        last = folder.rsplit("/", 1)[-1]
        patterns = list(dict.fromkeys([f"/{last}/{{slug}}", "/{slug}", "/blog/{slug}"]))

        async def answers(url):
            try:
                final_url, status, _kind, _text = await self.fetch(url)
            except (httpx.HTTPError, OSError, TimeoutError, ValueError, LookupError):
                return False
            return status == 200 and (
                urlsplit(final_url).path.rstrip("/") == urlsplit(url).path.rstrip("/")
            )

        tries = [
            (pattern, route_url(pattern.replace("{slug}", _stem(name)), base["host"]))
            for pattern in patterns
            for name in siblings
        ]
        found = await asyncio.gather(*(answers(url) for _, url in tries if url))
        hits = list(dict.fromkeys(p for (p, url), ok in zip(tries, found, strict=False) if ok))
        for pattern in hits:
            control = route_url(
                pattern.replace("{slug}", f"tin-route-check-{run.id.hex[:8]}"), base["host"]
            )
            if control and not await answers(control):
                return {**result, "pattern": pattern}
        return {**result, "reason": "no_route"}

"""An offline Agent Traffic service that answers with the API contract's examples.

`FakeAgentTraffic` is an httpx handler: pass it to `httpx.MockTransport`. It keeps one site per
project reference the way the contract describes (PUT creates or updates it with a new read
token, DELETE removes it, a removed reference gets a new site on the next PUT) and answers the
status and report calls from tests/fixtures/agent_traffic. `state` sets what status says, and
`refuse` makes the next calls to a path answer an error status instead:

    service = FakeAgentTraffic()
    service.refuse["DELETE"] = [503, 429]   # the next two DELETEs fail, the third succeeds

`linked` links a reference to a site Agent Traffic runs itself, as tin.computer's project is
linked to `organic-tin`: PUT answers with that site and no `url` or dates, and DELETE unlinks it.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "agent_traffic"
SERVICE_CREDENTIAL = "agent-traffic-synthetic-service-credential"
SHARED = ("vercel.app", "netlify.app", "pages.dev", "github.io", "webflow.io", "framer.website")


def example(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeAgentTraffic:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.sites: dict[str, dict] = {}
        self.removed: dict[str, str] = {}
        # project reference -> (site ID, counted hosts) of a site Agent Traffic runs itself.
        self.linked: dict[str, tuple[str, list[str]]] = {}
        self.tokens: dict[str, str] = {}
        self.state = "waiting"
        self.status_extra: dict = {}
        # method or "status"/"report" -> error statuses to answer, in order.
        self.refuse: dict[str, list[int]] = {}
        self.issued = 0

    @staticmethod
    def reply(status: int, body=None, headers=None) -> httpx.Response:
        raw = json.dumps(body if body is not None else {}).encode()
        return httpx.Response(status, stream=httpx.ByteStream(raw), headers=headers)

    def paths(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.calls if method in (None, r.method)]

    def _refused(self, key: str) -> httpx.Response | None:
        queue = self.refuse.get(key) or []
        if not queue:
            return None
        status = queue.pop(0)
        codes = {401: "unauthorized", 403: "not_permitted", 404: "not_found", 429: "rate_limited"}
        if status == 409:
            return self.reply(409, {"error": "staging_limit"})
        if status == 400:
            return self.reply(400, {"error": "invalid_url"})
        headers = {"retry-after": "30"} if status == 429 else None
        return self.reply(status, {"error": codes.get(status, "unavailable")}, headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["user-agent"] == "Tin-Project-API/1"
        assert request.headers["accept-encoding"] == "identity"
        path, method = request.url.path, request.method
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        admin = re.fullmatch(r"/v1/admin/projects/([^/]+)/site", path)
        if admin:
            if bearer != SERVICE_CREDENTIAL:
                return self.reply(401, {"error": "unauthorized"})
            refused = self._refused(method)
            if refused is not None:
                return refused
            ref = unquote(admin.group(1))
            if method == "PUT":
                return self._put(ref, json.loads(request.content))
            if method == "DELETE":
                return self._delete(ref)
        read = re.fullmatch(r"/v1/sites/([^/]+)/(status|report)", path)
        if read and method == "GET":
            site_id, kind = read.groups()
            owner = self.tokens.get(bearer)
            if owner is None:
                return self.reply(401, {"error": "unauthorized"})
            if owner != site_id:
                return self.reply(404, {"error": "not_found"})
            refused = self._refused(kind)
            if refused is not None:
                return refused
            params = request.url.params
            return self._status(site_id) if kind == "status" else self._report(site_id, params)
        raise AssertionError(f"unexpected Agent Traffic route {method} {path}")

    def _put(self, ref: str, body: dict) -> httpx.Response:
        host = urlsplit(body["url"]).hostname
        domain = host.removeprefix("www.")
        counted = [host] if host.endswith(SHARED) else [domain, f"*.{domain}"]
        existing = self.sites.get(ref)
        if ref in self.linked:
            site_id, counted = self.linked[ref]
        else:
            site_id = (
                existing["site_id"]
                if existing
                else f"st-fake{len(self.removed) + len(self.sites) + 1:04d}"
            )
        self.issued += 1
        token = f"read-token-{site_id}-{self.issued:04d}-synthetic"
        site = copy.deepcopy(example("put_site.json"))
        site.update(
            project_ref=ref,
            site_id=site_id,
            url=body["url"],
            counted_hosts=counted,
            read_token=token,
            script=site["script"].replace("st-8k3m9q2x7v4c", site_id),
        )
        if ref in self.linked:
            site.update(linked=True, url=None, created_at=None, updated_at=None)
        self.sites[ref] = site
        self.tokens[token] = site_id
        self.removed.pop(ref, None)
        return self.reply(200 if existing else 201, site)

    def _delete(self, ref: str) -> httpx.Response:
        site = self.sites.pop(ref, None)
        if site is not None and self.linked.pop(ref, None):
            # Unlinking stops Tin's tokens; the site itself keeps recording.
            self.tokens = {k: v for k, v in self.tokens.items() if v != site["site_id"]}
            return self.reply(200, {"site_id": site["site_id"], "state": "unlinked"})
        if site is not None:
            self.removed[ref] = site["site_id"]
            # Every read token for the site stops working.
            self.tokens = {k: v for k, v in self.tokens.items() if v != site["site_id"]}
            return self.reply(202, {**example("delete_removing.json"), "site_id": site["site_id"]})
        if ref in self.removed:
            return self.reply(200, {**example("delete_removed.json"), "site_id": self.removed[ref]})
        return self.reply(404, {"error": "not_found"})

    def _status(self, site_id: str) -> httpx.Response:
        status = {**example("status_live.json"), "site_id": site_id, "state": self.state}
        if self.state == "waiting":
            status.update(first_page_at=None, last_page_at=None, pages_today=0, hosts_seen=[])
        if self.state == "quiet":
            status.update(pages_today=0, hosts_seen=[])
        status.update(self.status_extra)
        return self.reply(200, status)

    def _report(self, site_id: str, params) -> httpx.Response:
        period = params.get("period")
        if period not in {"day", "week"}:
            return self.reply(400, {"error": "invalid_period"})
        report = example("report_day.json" if period == "day" else "report_week.json")
        report.update(site_id=site_id, tz=params.get("tz"))
        if params.get("date") and period == "day":
            report.update(start=params["date"], end=params["date"])
        return self.reply(200, report)

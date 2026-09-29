"""Trusted preparation and delivery checks for the Codex technical repair policies."""

import hashlib
import json
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite import technical_fix as contract
from tin_lite import technical_site_rules as site_rules
from tin_lite.integrations import GitHubRepositoryBinding
from tin_lite.organic_audit import digest
from tin_lite.organic_audit_site import parse_robots, parse_sitemap, url_key
from tin_lite.run_reports import publish_run_report
from tin_lite.technical_build_profile import archive_files, match_profile
from tin_lite.technical_fix_sources import TechnicalFixSources

MAX_SITEMAP_READS = 6
SITEMAP_GUESSES = ("/sitemap.xml", "/sitemap_index.xml")
STATIC_SOURCE_EXTENSIONS = (".txt", ".xml", ".html", ".htm")


def binding_from(prepared):
    value = dict(prepared["repository_binding"])
    value["connection_id"] = UUID(str(value["connection_id"]))
    return GitHubRepositoryBinding(**value)


async def prepared_result(database, run_id):
    receipt = await database.get_effect(f"technical:{run_id}:prepare")
    if not receipt or receipt.status != "completed":
        raise ValueError("Technical repair preparation is unavailable.")
    return receipt.result


def body_of(observation):
    """The text of a fresh read: an HTML page or a site file."""
    return observation.get("html", observation.get("text", ""))


class TechnicalFixExecution:
    def __init__(
        self,
        *,
        database,
        storage,
        integrations,
        fetch=contract.fetch_page,
        fetch_file=contract.fetch_site_file,
    ):
        self.db, self.storage, self.integrations, self.fetch, self.fetch_file = (
            database,
            storage,
            integrations,
            fetch,
            fetch_file,
        )

    async def active(self, run_id):
        run = await self.db.get_run(run_id)
        if (
            run is None
            or run.executor != "codex.procedure"
            or run.status.value not in {"pending", "running"}
            or not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=run.started_by_clerk_user_id
            )
        ):
            raise ValueError("The technical repair is no longer active or accessible.")
        return run

    async def once(self, run_id, step, operation):
        key = f"technical:{run_id}:{step}"
        async with self.db.effect_lock(key, contract.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self.active(run_id)
            await self.db.start_effect(conn, execution_key=key, operation=contract.KEY)
            value = await operation()
            # UUID values belong to Postgres JSON, not Temporal payloads.
            value = json.loads(json.dumps(value, default=str))
            await self.db.complete_effect(conn, execution_key=key, result=value)
            return value

    async def prepare(self, run, *, policy=contract.LEGACY_POLICY):
        async def select():
            result = await TechnicalFixSources(
                database=self.db,
                storage=self.storage,
                integrations=self.integrations,
                supported_checks=contract.supported_checks(policy),
            ).preflight(
                project_id=run.project_id,
                audit_run_id=UUID(run.input["audit_run_id"]),
                **{
                    key: run.input[key]
                    for key in (
                        "audit_revision",
                        "finding_id",
                        "expected_repository",
                        "repository_serves_site",
                    )
                },
            )
            return {**result, "input_sha256": digest(run.input)}

        selection = await self.once(run.id, "binding", select)
        if selection["input_sha256"] != digest(run.input):
            raise ValueError("Technical repair inputs changed after preparation.")

        if policy == contract.SITE_POLICY:
            prepared = await self.once(
                run.id, "prepare", lambda: self._resolve_site(run, selection)
            )
            return await self._finish_preparation(run, prepared)

        async def resolve():
            pages = []
            for index, url in enumerate(selection["selection"]["affected_urls"]):

                async def observe(url=url):
                    return await self.fetch(
                        url, host=contract.verified_page_host(url, selection["target"])
                    )

                pages.append(await self.once(run.id, f"page:{index}", observe))
            check = selection["selection"]["finding"]["check_id"]
            reason, originals, profile, overlapping = None, {}, {}, []
            unsupported = []
            if all(contract.has_metadata(page["html"], check) for page in pages):
                reason = "already_resolved"
            else:
                binding = binding_from(selection)
                bundle = await self.integrations.github_repository_bundle(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_repository_workspace",
                    expected_binding=binding,
                )
                if policy == contract.LEGACY_POLICY:
                    originals = contract.matched_sources(bundle.archive, pages)
                else:
                    matched = (
                        match_profile(
                            bundle.archive, pages, check, allow_partial=policy == contract.POLICY
                        )
                        if getattr(bundle, "complete", True)
                        else None
                    )
                    originals = matched["originals"] if matched else None
                    profile = matched["verification_profile"] if matched else {}
                    unsupported = matched["unsupported_pages"] if matched else []
                if originals is None:
                    reason, originals = "unsupported_source", {}
                else:
                    evidence = await self.integrations.github_open_pull_requests(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=f"{run.id}:procedure_open_pull_requests",
                        base_branch=binding.default_branch,
                        expected_binding=binding,
                    )
                    if set(originals).intersection(evidence.changed_paths):
                        reason = "open_pr_overlap"
                        document = json.loads(getattr(evidence, "document", b"{}"))
                        overlapping = [
                            {"number": pull["number"], "url": pull["url"]}
                            for pull in document.get("pull_requests", [])
                            if any(
                                file["path"] in originals or file.get("previous_path") in originals
                                for file in pull.get("files", [])
                            )
                        ]
                    elif evidence.truncated:
                        reason = "incomplete_pr_evidence"
            prepared = {
                **selection,
                "policy": policy,
                "verification_profile": profile,
                "overlapping_pull_requests": overlapping,
                "unsupported_pages": unsupported,
                "pages": [
                    {key: value for key, value in page.items() if key != "html"} for page in pages
                ],
                "originals": originals,
                "reason": reason,
            }
            # Context travels through the existing bounded sandbox environment. Do
            # not discover an oversized escaped payload after allocating compute.
            if len(json.dumps(prepared, separators=(",", ":")).encode()) > 70_000:
                prepared.update(reason="unsupported_source", originals={})
            return prepared

        prepared = await self.once(run.id, "prepare", resolve)
        return await self._finish_preparation(run, prepared)

    async def _finish_preparation(self, run, prepared):
        if prepared["reason"]:
            await publish_run_report(
                database=self.db,
                storage=self.storage,
                run_id=run.id,
                workflow_key=contract.KEY,
                prefix="technical",
                path=f"reports/technical-fix/{run.id}/RESULT.md",
                content=contract.report(prepared, reason=prepared["reason"]),
                summary="Technical finding checked. No change proposed.",
            )
            return True
        return False

    # --- site-fix-v4 -------------------------------------------------------------------

    async def _read(self, run, key, url, target, kind):
        """One fresh read, receipted. An unreadable file or page is recorded, not raised."""

        async def read():
            host = contract.verified_page_host(url, target)
            try:
                if kind == "html":
                    return await self.fetch(url, host=host)
                return await self.fetch_file(url, host=host, kind=kind)
            except (ValueError, OSError, TimeoutError, UnicodeError) as exc:
                return {"url": url, "status_code": None, "unreadable": type(exc).__name__}

        return await self.once(run.id, key, read)

    async def _sitemap_files(self, run, target, robots):
        """The urlset files a sitemap fix may touch: robots.txt's sitemaps, else a guess."""
        host = target["host"]
        referenced = (
            parse_robots(body_of(robots))["sitemaps"] if robots.get("status_code") == 200 else []
        )
        queue = [
            url
            for url in dict.fromkeys(referenced)
            if urlsplit(url).scheme == "https" and urlsplit(url).hostname in target["site_hosts"]
        ] or [f"https://{host}{path}" for path in SITEMAP_GUESSES[:1]]
        files, seen = [], set()
        for index in range(MAX_SITEMAP_READS):
            if not queue:
                break
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            read = await self._read(run, f"site:sitemap:{index}", url, target, "sitemap")
            if read.get("status_code") != 200:
                continue
            parsed = parse_sitemap(body_of(read), max_urls=50_000)
            if parsed["kind"] == "index":
                queue += [
                    row["loc"]
                    for row in parsed["entries"]
                    if urlsplit(row["loc"]).scheme == "https"
                    and urlsplit(row["loc"]).hostname in target["site_hosts"]
                ]
            elif parsed["kind"] == "urlset":
                files.append(read)
        return files

    async def _sitemap_reference(self, run, target):
        """The sitemap robots.txt should name: the first guess the site actually serves."""
        for index, path in enumerate(SITEMAP_GUESSES):
            url = f"https://{target['host']}{path}"
            read = await self._read(run, f"site:sitemap-guess:{index}", url, target, "sitemap")
            if read.get("status_code") == 200 and parse_sitemap(body_of(read), max_urls=1)[
                "kind"
            ] in {"urlset", "index"}:
                return read["url"]
        return None

    async def _resolve_site(self, run, selection):
        chosen = selection["selection"]
        finding = chosen["finding"]
        kind = site_rules.SITE_FIXES[finding["check_id"]]
        kind_target = site_rules.target(kind)
        target = selection["target"]
        host = target["host"]
        listed = chosen["affected_urls"][: site_rules.page_limit(kind)]
        remaining = max(0, int(chosen.get("affected_count") or len(listed)) - len(listed))
        expected, reason = {}, None
        reads = []  # (read, needed, page_url or None)

        if kind_target == "robots":
            robots = await self._read(
                run, "site:robots", f"https://{host}/robots.txt", target, "robots"
            )
            if robots.get("status_code") not in {200, 404}:
                reason = "site_unreadable"
            else:
                state = "observed" if robots["status_code"] == 200 else "missing"
                need = site_rules.robots_needs(kind, {"status": state, "text": body_of(robots)})
                if kind == "robots_allow_ai_search":
                    expected = {"agents": need["agents"]}
                elif need["needed"]:
                    sitemap = await self._sitemap_reference(run, target)
                    if sitemap is None:
                        reason = "no_sitemap_to_reference"
                    expected = {"sitemaps": [sitemap] if sitemap else []}
                reads.append((robots, need["needed"], None))
        elif kind_target == "sitemap":
            robots = await self._read(
                run, "site:robots", f"https://{host}/robots.txt", target, "robots"
            )
            files = await self._sitemap_files(run, target, robots)
            wanted = {url_key(url): url for url in listed}
            if not files:
                reason = "site_unreadable"
            elif kind == "sitemap_remove_urls":
                present = sorted(
                    {
                        loc
                        for read in files
                        for loc in site_rules.sitemap_locs(body_of(read))
                        if url_key(loc) in wanted
                    }
                )
                expected = {"remove": present}
                for read in files:
                    needed = site_rules.still_needed(kind, body_of(read), read["url"], expected)
                    if needed:
                        reads.append((read, True, None))
                reads = reads[:3]
            else:
                keys = {
                    url_key(loc) for read in files for loc in site_rules.sitemap_locs(body_of(read))
                }
                expected = {"add": [url for key, url in wanted.items() if key not in keys]}
                reads.append((files[0], bool(expected["add"]), None))
        else:
            for index, url in enumerate(listed):
                read = await self._read(run, f"page:{index}", url, target, "html")
                if read.get("status_code") != 200:
                    remaining += 1
                    continue
                reads.append((read, site_rules.page_needs(kind, body_of(read), url), url))
            if not reads:
                reason = "site_unreadable"

        needed = [(read, page) for read, is_needed, page in reads if is_needed]
        if reason is None and not needed:
            reason = "already_resolved"
        mode, originals, pages, new_paths, overlapping = "static", {}, {}, [], []
        if reason is None:
            binding = binding_from(selection)
            bundle = await self.integrations.github_repository_bundle(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=f"{run.id}:procedure_repository_workspace",
                expected_binding=binding,
            )
            try:
                files = archive_files(bundle.archive) if getattr(bundle, "complete", True) else None
            except ValueError:
                files = None
            if files is None:
                reason = "unsupported_source"
            else:
                mode, originals, pages, new_paths, unmatched = self._match_sources(
                    kind, files, needed
                )
                remaining += unmatched
                if mode is None:
                    reason = "unsupported_source"
            if reason is None:
                evidence = await self.integrations.github_open_pull_requests(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_open_pull_requests",
                    base_branch=binding.default_branch,
                    expected_binding=binding,
                )
                touched = set(originals) | set(new_paths)
                if touched & set(evidence.changed_paths):
                    reason = "open_pr_overlap"
                    document = json.loads(getattr(evidence, "document", b"{}"))
                    overlapping = [
                        {"number": pull["number"], "url": pull["url"]}
                        for pull in document.get("pull_requests", [])
                        if any(
                            file["path"] in touched or file.get("previous_path") in touched
                            for file in pull.get("files", [])
                        )
                    ]
                elif evidence.truncated:
                    reason = "incomplete_pr_evidence"
        prepared = {
            **selection,
            "policy": contract.SITE_POLICY,
            "site_fix": {
                "kind": kind,
                "target": kind_target,
                "mode": mode if reason is None else None,
                "change": site_rules.CHANGES[kind],
                "expected": expected,
                "observations": [
                    {
                        "url": read["url"],
                        "status_code": read.get("status_code"),
                        "sha256": read.get("sha256"),
                        "observed_at": read.get("observed_at"),
                        "needed": is_needed,
                        "page_url": page,
                    }
                    for read, is_needed, page in reads
                ],
                "pages": pages,
                "new_paths": new_paths,
                "remaining": remaining,
            },
            "originals": originals if reason is None else {},
            "pages": [],
            "unsupported_pages": [],
            "verification_profile": {},
            "overlapping_pull_requests": overlapping,
            "reason": reason,
        }
        # Context travels through the existing bounded sandbox environment. Do not
        # discover an oversized escaped payload after allocating compute.
        if len(json.dumps(prepared, separators=(",", ":"), default=str).encode()) > 70_000:
            prepared.update(reason="unsupported_source", originals={})
            prepared["site_fix"]["mode"] = None
        return prepared

    def _match_sources(self, kind, files, needed):
        """Static files the site serves byte for byte, else named Next.js source files.

        Returns (mode, originals, pages, new_paths, unmatched_pages); mode None when neither.
        """
        by_digest = {}
        for path, raw in files.items():
            if path.endswith(STATIC_SOURCE_EXTENSIONS) and len(raw) <= site_rules.MAX_TEXT_BYTES:
                by_digest.setdefault(hashlib.sha256(raw).hexdigest(), []).append(path)
        originals, pages, unmatched = {}, {}, 0
        for read, page in needed:
            if read.get("status_code") == 404:
                continue  # robots.txt the site doesn't serve: created below, not matched.
            matches = by_digest.get(read.get("sha256"), [])
            if len(matches) == 1:
                try:
                    originals[matches[0]] = files[matches[0]].decode("utf-8")
                except UnicodeDecodeError:
                    unmatched += 1
                    continue
                if page:
                    pages[matches[0]] = page
            else:
                unmatched += 1
        kind_target = site_rules.target(kind)
        missing_robots = kind_target == "robots" and any(
            read.get("status_code") == 404 for read, _ in needed
        )
        if missing_robots and kind == "robots_sitemap_line":
            if any(path.startswith("public/") for path in files):
                return "static", {}, {}, ["public/robots.txt"], 0
            if "index.html" in files:
                return "static", {}, {}, ["robots.txt"], 0
        if originals and (kind_target == "html" or unmatched == 0):
            return "static", originals, pages, [], unmatched if kind_target == "html" else 0
        if not site_rules.is_nextjs(files.get("package.json", b"").decode("utf-8", "replace")):
            return None, {}, {}, [], 0
        urls = [page for _, page in needed if page]
        candidates = site_rules.framework_candidates(kind, sorted(files), urls)
        sources, total = {}, 0
        for path in candidates["existing"]:
            raw = files[path]
            if len(raw) > site_rules.MAX_FRAMEWORK_FILE_BYTES:
                continue
            if total + len(raw) > site_rules.MAX_FRAMEWORK_ORIGINAL_BYTES:
                break
            try:
                sources[path] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            total += len(raw)
        if not sources and not candidates["new"]:
            return None, {}, {}, [], 0
        return "framework", sources, {}, candidates["new"], 0

    async def _validate_site_delivery(self, prepared):
        """Recheck the live site before the PR goes out.

        A static patch needs the exact bytes it was made from; a framework patch needs the
        problem to still be there, since the live output isn't the file that changed.
        """
        fix, target = prepared["site_fix"], prepared["target"]
        for row in fix["observations"]:
            if not row["needed"]:
                continue
            host = contract.verified_page_host(row["url"], target)
            if fix["target"] == "html":
                current = await self.fetch(row["url"], host=host)
            else:
                current = await self.fetch_file(row["url"], host=host, kind=fix["target"])
            if fix["mode"] == "static":
                if current["sha256"] != row["sha256"]:
                    raise ValueError("The website changed after preparation. Start a new repair.")
            elif not site_rules.still_needed(
                fix["kind"],
                body_of(current),
                row["page_url"] or row["url"],
                fix["expected"],
                status_code=current["status_code"],
            ):
                raise ValueError("The live site no longer shows this problem. Start a new repair.")

    async def validate_delivery(self, run, manifest, prepared):
        await self.active(run.id)
        if digest(run.input) != prepared["input_sha256"]:
            raise ValueError("The technical repair inputs changed.")
        contract.validate_manifest(manifest, prepared)
        if manifest["outcome"] == "no_change":
            return
        if prepared.get("site_fix"):
            return await self._validate_site_delivery(prepared)
        # Recheck the website at delivery too. Changed or unavailable HTML never
        # becomes authorization to send a stale patch, nor a claim of resolution.
        for page in prepared["pages"]:
            if prepared.get("policy") == contract.POLICY and page["url"] not in {
                observation["url"]
                for observation in prepared["verification_profile"]["observations"]
            }:
                continue
            check = contract.selected_check(prepared)
            present = "has_title" if check == contract.TITLE_CHECK else "has_description"
            if page.get(present, False):
                continue
            current = await self.fetch(
                page["url"], host=contract.verified_page_host(page["url"], prepared["target"])
            )
            if current["sha256"] != page["sha256"] or contract.has_metadata(current["html"], check):
                raise ValueError("The website changed after preparation. Start a new repair.")

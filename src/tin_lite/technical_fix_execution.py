"""Trusted preparation and delivery checks for the Codex technical repair policies."""

import hashlib
import json
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite import technical_batch as batch_rules
from tin_lite import technical_fix as contract
from tin_lite import technical_repair_plan as repair_plan
from tin_lite import technical_site_rules as site_rules
from tin_lite.integrations import GitHubRepositoryBinding
from tin_lite.organic_audit import digest
from tin_lite.organic_audit_site import parse_robots, parse_sitemap, url_key
from tin_lite.repository_limits import describe_omissions
from tin_lite.run_reports import publish_run_report
from tin_lite.technical_build_profile import archive_files, match_profile
from tin_lite.technical_fix_sources import TechnicalFixSources

MAX_SITEMAP_READS = 6
SITEMAP_GUESSES = ("/sitemap.xml", "/sitemap_index.xml")


async def sitemap_files(target, robots, read):
    """The site's urlset files: robots.txt's Sitemap lines, else the first guess, following
    sitemap indexes on the audited hosts. `read(index, url)` returns one read; preparation
    receipts each one, the live check after merge doesn't."""
    hosts = target.get("site_hosts", [target["host"]])
    referenced = (
        parse_robots(body_of(robots))["sitemaps"]
        if (robots or {}).get("status_code") == 200
        else []
    )
    queue = [
        url
        for url in dict.fromkeys(referenced)
        if urlsplit(url).scheme == "https" and urlsplit(url).hostname in hosts
    ] or [f"https://{target['host']}{path}" for path in SITEMAP_GUESSES[:1]]
    files, seen = [], set()
    for index in range(MAX_SITEMAP_READS):
        if not queue:
            break
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        response = await read(index, url)
        if response.get("status_code") != 200:
            continue
        parsed = parse_sitemap(body_of(response), max_urls=50_000)
        if parsed["kind"] == "index":
            queue += [
                row["loc"]
                for row in parsed["entries"]
                if urlsplit(row["loc"]).scheme == "https" and urlsplit(row["loc"]).hostname in hosts
            ]
        elif parsed["kind"] == "urlset":
            files.append(response)
    return files


STATIC_SOURCE_EXTENSIONS = (".txt", ".xml", ".html", ".htm")


def strict_candidate(path, size):
    """A file _strict_files can match against a served copy: static text, within bounds."""
    return path.endswith(STATIC_SOURCE_EXTENSIONS) and size <= site_rules.MAX_TEXT_BYTES


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
        if contract.batches(policy):
            return await self._prepare_batch(run)

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
            unsupported, missing = [], {}
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
                # A build profile is proven over the whole repository, so an incomplete
                # snapshot stops the run and names what Tin couldn't read.
                incomplete = policy != contract.LEGACY_POLICY and not getattr(
                    bundle, "complete", True
                )
                if policy == contract.LEGACY_POLICY:
                    originals = contract.matched_sources(bundle.archive, pages)
                elif not incomplete:
                    matched = match_profile(
                        bundle.archive, pages, check, allow_partial=policy == contract.POLICY
                    )
                    originals = matched["originals"] if matched else None
                    profile = matched["verification_profile"] if matched else {}
                    unsupported = matched["unsupported_pages"] if matched else []
                if incomplete:
                    reason, missing = "repository_incomplete", contract.missing_record(bundle)
                elif originals is None:
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
            prepared.update(missing)
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
                summary=contract.preparation_summary(prepared),
                # A run that couldn't read the repository failed; it didn't find the site fine.
                failed=contract.preparation_failed(prepared),
            )
            return True
        return False

    # --- served-file reads ---------------------------------------------------------------

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

        def read(index, url):
            return self._read(run, f"site:sitemap:{index}", url, target, "sitemap")

        return await sitemap_files(target, robots, read)

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

    # --- site-fix-v5 -------------------------------------------------------------------

    async def _prepare_batch(self, run):
        async def select():
            result = await TechnicalFixSources(
                database=self.db,
                storage=self.storage,
                integrations=self.integrations,
                supported_checks=contract.supported_checks(contract.BATCH_POLICY),
                batch=True,
            ).batch(
                project_id=run.project_id,
                audit_run_id=UUID(run.input["audit_run_id"]),
                audit_revision=run.input["audit_revision"],
                expected_repository=run.input["expected_repository"],
                repository_serves_site=run.input["repository_serves_site"],
                finding_ids=run.input.get("finding_ids") or [],
                decisions=run.input.get("decisions") or [],
            )
            return {**result, "input_sha256": digest(run.input)}

        selection = await self.once(run.id, "binding", select)
        if selection["input_sha256"] != digest(run.input):
            raise ValueError("Technical repair inputs changed after preparation.")
        prepared = await self.once(run.id, "prepare", lambda: self._resolve_batch(run, selection))
        return await self._finish_preparation(run, prepared)

    async def _resolve_batch(self, run, selection):
        """Re-read the live site, drop what's already fixed, and name the files the diff
        can prove. Everything else in the plan goes to Codex as it is."""
        planned = selection["plan"]
        target = selection["target"]
        host = target["host"]
        repairs = [dict(entry) for entry in planned["repairs"]]
        left_out = {key: list(rows) for key, rows in planned["left_out"].items()}
        left_out.setdefault("already_resolved", [])
        kinds = {entry["kind"] for entry in repairs}
        robots = sitemaps = None
        if kinds & {k for k in kinds if k.startswith(("robots_", "sitemap_"))}:
            robots = await self._read(
                run, "site:robots", f"https://{host}/robots.txt", target, "robots"
            )
        if any(kind.startswith("sitemap_") for kind in kinds):
            sitemaps = await self._sitemap_files(run, target, robots or {})
        page_reads: dict[str, dict] = {}

        def resolved(entry, reason="The live site no longer shows this problem."):
            left_out["already_resolved"].append(
                {
                    "id": entry["finding_id"],
                    "check_id": entry["check_id"],
                    "issue": entry["issue"],
                    "reason": reason,
                }
            )

        still = []
        for entry in repairs:
            predicate = entry.get("live")
            if predicate in batch_rules.PAGE_PREDICATES and predicate != "reachable":
                checked, fixed = 0, True
                for url in entry["urls"][:5]:
                    if url not in page_reads:
                        if len(page_reads) >= repair_plan.MAX_LIVE_READS:
                            fixed = False
                            break
                        page_reads[url] = await self._read(
                            run, f"page:{len(page_reads)}", url, target, "html"
                        )
                    read = page_reads[url]
                    if read.get("status_code") != 200:
                        fixed = False
                        continue
                    checked += 1
                    if not batch_rules.page_fixed(
                        predicate, body_of(read), url, entry, hosts=target.get("site_hosts")
                    ):
                        fixed = False
                if checked and fixed:
                    resolved(entry)
                    continue
            elif predicate in batch_rules.ROBOTS_PREDICATES and robots is not None:
                if robots.get("status_code") in {200, 404} and batch_rules.robots_fixed(
                    predicate, body_of(robots), robots["status_code"], entry
                ):
                    resolved(entry)
                    continue
            elif predicate in batch_rules.SITEMAP_PREDICATES and sitemaps:
                if batch_rules.sitemap_fixed(predicate, [body_of(f) for f in sitemaps], entry):
                    resolved(entry)
                    continue
            still.append(entry)

        # What each strict change must do, from the fresh reads.
        expected_robots: dict = {}
        for entry in still:
            if entry["kind"] == "robots_allow_ai_search" and robots:
                state = "observed" if robots.get("status_code") == 200 else "missing"
                need = site_rules.robots_needs(
                    entry["kind"], {"status": state, "text": body_of(robots)}
                )
                entry["expected"] = {"agents": need["agents"]}
                expected_robots["agents"] = need["agents"]
            elif entry["kind"] == "robots_sitemap_line":
                sitemap = await self._sitemap_reference(run, target)
                entry["expected"] = {"sitemaps": [sitemap] if sitemap else []}
                if sitemap:
                    expected_robots["sitemaps"] = [sitemap]
            elif entry["kind"] in {"sitemap_remove_urls", "sitemap_add_urls"} and sitemaps:
                present = {
                    url_key(loc) for f in sitemaps for loc in site_rules.sitemap_locs(body_of(f))
                }
                if entry["kind"] == "sitemap_remove_urls":
                    entry["expected"] = {
                        "remove": [u for u in entry["urls"] if url_key(u) in present]
                    }
                else:
                    entry["expected"] = {
                        "add": [u for u in entry["urls"] if url_key(u) not in present]
                    }

        reason = None
        if not repairs:
            reason = "nothing_to_fix"
        elif not still:
            reason = "already_resolved"
        strict, overlap, missing = {}, [], {}
        if reason is None:
            binding = binding_from(selection)
            bundle = await self.integrations.github_repository_bundle(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=f"{run.id}:procedure_repository_workspace",
                expected_binding=binding,
            )
            files = None
            if getattr(bundle, "complete", True):
                try:
                    # Only served text files can be proven from the diff; read just those.
                    files = archive_files(bundle.archive, select=strict_candidate)
                except ValueError:
                    pass
            if not getattr(bundle, "complete", True):
                reason, missing = "repository_incomplete", contract.missing_record(bundle)
            elif files is None:
                reason = "unsupported_source"
            else:
                strict = self._strict_files(files, still, robots, sitemaps or [], page_reads)
                evidence = await self.integrations.github_open_pull_requests(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_open_pull_requests",
                    base_branch=binding.default_branch,
                    expected_binding=binding,
                )
                if evidence.truncated:
                    reason = "incomplete_pr_evidence"
                overlap = sorted(set(evidence.changed_paths))[:500]
        observations = [
            {
                "url": read["url"],
                "status_code": read.get("status_code"),
                "observed_at": read.get("observed_at"),
            }
            for read in [
                *([robots] if robots else []),
                *(sitemaps or []),
                *page_reads.values(),
            ]
        ][:60]
        selection = {key: value for key, value in selection.items() if key not in {"plan", "ask"}}
        prepared = {
            **selection,
            "decisions_needed": [],
            "policy": contract.BATCH_POLICY,
            "batch": {
                "repairs": still,
                "left_out": left_out,
                "strict_files": strict,
                "overlap_paths": overlap,
                "observations": observations,
                "caps": {
                    "files": repair_plan.MAX_FILES,
                    "changed_lines": repair_plan.MAX_CHANGED_LINES,
                    "new_file_bytes": repair_plan.MAX_NEW_FILE_BYTES,
                },
                "forbidden": sorted(batch_rules.BLOCKED_NAMES)
                + [prefix + "*" for prefix in batch_rules.BLOCKED_PREFIXES]
                + [".env*"],
            },
            "originals": {},
            "site_fix": None,
            "pages": [],
            "unsupported_pages": [],
            "verification_profile": {},
            "overlapping_pull_requests": [],
            "reason": reason,
        }
        prepared.update(missing)
        # Context travels through the bounded sandbox environment; trim what the procedure
        # doesn't need before refusing an oversized plan.
        if len(json.dumps(prepared, separators=(",", ":"), default=str).encode()) > 70_000:
            prepared["batch"]["observations"] = []
            prepared["batch"]["forbidden"] = []
        if len(json.dumps(prepared, separators=(",", ":"), default=str).encode()) > 70_000:
            prepared.update(reason="plan_too_large")
        return prepared

    def _strict_files(self, files, repairs, robots, sitemaps, page_reads):
        """Served files the diff can prove: an exact repository copy whose findings are all
        ones technical_site_rules checks from the diff."""
        by_digest = {}
        for path, raw in files.items():
            if path.endswith(STATIC_SOURCE_EXTENSIONS) and len(raw) <= site_rules.MAX_TEXT_BYTES:
                by_digest.setdefault(hashlib.sha256(raw).hexdigest(), []).append(path)

        def match(read):
            paths = by_digest.get(read.get("sha256") or "", [])
            return paths[0] if len(paths) == 1 else None

        strict = {}
        robot_kinds = {e["kind"] for e in repairs if e["kind"].startswith("robots_")}
        if robots and robots.get("status_code") == 200 and robot_kinds:
            path = match(robots)
            if path and batch_rules.strict_kinds("robots", robot_kinds):
                expected = {}
                for entry in repairs:
                    expected.update(entry.get("expected") or {})
                strict[path] = {
                    "target": "robots",
                    "url": robots["url"],
                    "sha256": robots["sha256"],
                    "kinds": sorted(robot_kinds),
                    "expected": {k: v for k, v in expected.items() if k in {"sitemaps", "agents"}},
                }
        sitemap_kinds = {e["kind"] for e in repairs if e["kind"].startswith("sitemap_")}
        if sitemaps and sitemap_kinds and batch_rules.strict_kinds("sitemap", sitemap_kinds):
            remove = [u for e in repairs for u in (e.get("expected") or {}).get("remove", [])]
            add = [u for e in repairs for u in (e.get("expected") or {}).get("add", [])]
            for index, read in enumerate(sitemaps):
                path = match(read)
                if not path:
                    continue
                locs = {url_key(loc) for loc in site_rules.sitemap_locs(body_of(read))}
                strict[path] = {
                    "target": "sitemap",
                    "url": read["url"],
                    "sha256": read["sha256"],
                    "kinds": sorted(sitemap_kinds),
                    "expected": {
                        "remove": [u for u in remove if url_key(u) in locs],
                        "add": add if index == 0 else [],
                    },
                }
        for url, read in page_reads.items():
            if read.get("status_code") != 200:
                continue
            page_kinds = {e["kind"] for e in repairs if url in e["urls"]}
            path = match(read)
            if path and batch_rules.strict_kinds("html", page_kinds):
                strict[path] = {
                    "target": "html",
                    "url": url,
                    "page_url": url,
                    "sha256": read["sha256"],
                    "kinds": sorted(page_kinds),
                    "expected": {},
                }
        return strict

    async def _validate_batch_delivery(self, run, manifest, prepared):
        """Bounds, blocked paths, strict diffs and a stale-site check before the PR opens."""
        binding = binding_from(prepared)
        bundle = await self.integrations.github_repository_bundle(
            project_id=run.project_id,
            run_id=run.id,
            execution_key=f"{run.id}:procedure_repository_workspace",
            expected_binding=binding,
        )
        if not getattr(bundle, "complete", True):
            named = describe_omissions(getattr(bundle, "missing", ()), limit=3)
            raise ValueError(
                "Tin couldn't read every file in the repository"
                + (f" ({named})" if named else "")
                + ", so it can't check this patch."
            )
        changed = {item["path"] for item in manifest["files"]}
        skipped = sorted(changed & {item["path"] for item in getattr(bundle, "skipped", ())})
        if skipped:
            raise ValueError(
                f"{skipped[0]} is a large media or built file Tin didn't read, so a fix "
                "can't change it."
            )
        files = archive_files(bundle.archive, select=lambda path, _size: path in changed)
        originals = {}
        for item in manifest["files"]:
            raw = files.get(item["path"])
            if raw is None:
                originals[item["path"]] = None
                continue
            try:
                originals[item["path"]] = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{item['path']} is not a text file.") from exc
        strict = prepared["batch"].get("strict_files", {})
        target = prepared["target"]
        for path in {item["path"] for item in manifest["files"]} & set(strict):
            entry = strict[path]
            host = contract.verified_page_host(entry["url"], target)
            if entry["target"] == "html":
                current = await self.fetch(entry["url"], host=host)
            else:
                current = await self.fetch_file(entry["url"], host=host, kind=entry["target"])
            if current["sha256"] != entry["sha256"]:
                raise ValueError("The website changed after preparation. Start a new repair.")
        batch_rules.validate(manifest, prepared, originals)
        prepared["batch"]["unproven"] = any(
            item["path"] not in strict for item in manifest["files"]
        )

    async def validate_delivery(self, run, manifest, prepared):
        await self.active(run.id)
        if digest(run.input) != prepared["input_sha256"]:
            raise ValueError("The technical repair inputs changed.")
        contract.validate_manifest(manifest, prepared)
        if manifest["outcome"] == "no_change":
            return
        if prepared.get("batch"):
            return await self._validate_batch_delivery(run, manifest, prepared)
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

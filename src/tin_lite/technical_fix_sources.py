"""Read-only technical-fix preparation; never dispatches a run or authorizes a write.

Callers enforce project membership. Source selection is an exact run/revision/finding,
not a mutable report path or the latest successful audit. Repository bindings returned
here are previews: a future run must resolve and persist its own trusted binding.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from uuid import UUID

from tin_lite.integrations import IntegrationError
from tin_lite.organic_audit import (
    ARTIFACT_LIMITS,
    CHECKS,
    audit_paths,
    audit_policy,
    canonical_json,
    check_outcome,
    digest,
    in_scope_url,
    public_site,
    technical_findings,
)
from tin_lite.organic_audit_scope import audit_hosts
from tin_lite.technical_metadata_rules import SUPPORTED_CHECKS

MAX_AFFECTED_PAGES = 5
CONTENT_FINDING_MESSAGE = (
    "This audit finding recommends reviewing buyer-answer coverage. It does not establish "
    "a technical defect. Inspect existing content; consider content.plan with this audit "
    "and matching keyword research if content work is needed."
)


class TechnicalFixError(ValueError):
    """Only Tin-owned, safe messages cross the HTTP/MCP boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _invalid_source() -> TechnicalFixError:
    return TechnicalFixError("invalid_source", "The audit failed source verification.")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _validate_inventory(*, evidence, inventory, run, project_id):
    """Recompute the pinned technical inventory from the complete saved page list."""
    policy = audit_policy(evidence["policy"]["version"])
    schema = 2 if policy.get("check_applicability") else 1
    if (
        type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 1
        or type(inventory["schema_version"]) is not int
        or inventory["schema_version"] != schema
        or evidence["run_id"] != str(run.id)
        or inventory["run_id"] != str(run.id)
        or evidence["project_id"] != str(project_id)
        or evidence["definition_commit_sha"] != run.definition_commit_sha
        or inventory["evidence_sha256"] != digest(evidence)
        or inventory["downstream_authority"] != "recommendations_only"
        or digest(evidence["policy"]) != digest(audit_policy(evidence["policy"]["version"]))
    ):
        raise _invalid_source()
    scope = evidence["scope"]
    if scope.get("policy_version", evidence["policy"]["version"]) != evidence["policy"]["version"]:
        raise _invalid_source()
    url, host = public_site(scope["url"])
    if (
        host != scope["host"]
        or host != inventory["target_host"]
        or public_site(run.input["site_url"]) != (url, host)
    ):
        raise _invalid_source()
    crawl = evidence["crawl"]
    pages = crawl["pages"]
    if (
        crawl["status"] not in {"completed", "partial", "unavailable"}
        or not isinstance(pages, list)
        or len(pages) > evidence["policy"]["max_pages"]
        or len(canonical_json(pages)) > 240_000
    ):
        raise _invalid_source()
    seen = set()
    known_flags = {flag for flag, *_ in CHECKS}
    for page in pages:
        page_url, checks = page["url"], page["checks"]
        if (
            not in_scope_url(page_url, host, aliases=audit_hosts(scope))
            or page_url in seen
            or not isinstance(checks, dict)
            or not set(checks) <= known_flags
            or any(type(value) is not bool for value in checks.values())
            or not isinstance(page["title"], str)
            or len(page["title"]) > 200
            or (
                page["status_code"] is not None
                and (type(page["status_code"]) is not int or not 100 <= page["status_code"] <= 599)
            )
        ):
            raise _invalid_source()
        seen.add(page_url)
    if [page["url"] for page in pages] != sorted(seen):
        raise _invalid_source()
    expected, coverage = technical_findings(pages, host, policy_version=policy["version"])
    if schema == 2:
        for page in pages:
            context = page.get("provider_context", {})
            if (
                not isinstance(context, dict)
                or set(context) - {"canonical", "respect_sitemap"}
                or context.get("canonical") is not None
                and type(context["canonical"]) is not bool
                or "respect_sitemap" in context
                and type(context["respect_sitemap"]) is not bool
            ):
                raise _invalid_source()
        expected_status = (
            "partial"
            if not pages or any(row["outcomes"]["unknown"] for row in coverage)
            else "complete"
        )
        if inventory.get("evidence_status") != expected_status:
            raise _invalid_source()
    findings = inventory["findings"]
    if not isinstance(findings, list) or any(
        not isinstance(row, dict)
        or row.get("category") not in {"technical", "content"}
        or not isinstance(row.get("id"), str)
        or not re.fullmatch(r"oa_[0-9a-f]{20}", row["id"])
        or not isinstance(row.get("check_id"), str)
        or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", row["check_id"])
        for row in findings
    ):
        raise _invalid_source()
    if len({row["id"] for row in findings}) != len(findings):
        raise _invalid_source()
    technical = [row for row in findings if row["category"] == "technical"]
    # Digests, not Python equality: booleans must not impersonate integer counts/versions.
    if digest(technical) != digest(expected) or digest(inventory["check_coverage"]) != digest(
        coverage
    ):
        raise _invalid_source()
    # Content recommendations are recognized only to explain their exclusion. They
    # never gain the recomputed crawl evidence or eligibility of a technical finding.
    excluded = [
        {
            "finding": {key: row[key] for key in ("id", "check_id", "category")},
            "source_eligible": False,
            "ineligible_reason": "content_finding",
            "next_action": "content.plan",
            "message": CONTENT_FINDING_MESSAGE,
        }
        for row in findings
        if row["category"] == "content"
    ]
    return scope, crawl, expected, coverage, excluded


class TechnicalFixSources:
    def __init__(self, *, database, storage, integrations=None, supported_checks=SUPPORTED_CHECKS):
        self.supported_checks = supported_checks
        self.db, self.storage, self.integrations = database, storage, integrations

    async def list_sources(self, *, project_id: UUID, offset: int = 0):
        if type(offset) is not int or not 0 <= offset <= 10_000:
            raise TechnicalFixError("invalid_offset", "Invalid source offset.", status_code=422)
        rows = await self.db.pool.fetch(
            """SELECT id, created_at, canonical_commit_sha, input->>'site_url' AS site_url
            FROM workflow_runs WHERE project_id = $1 AND executor = 'organic.audit'
              AND status = 'succeeded' AND canonical_commit_sha IS NOT NULL
            ORDER BY created_at DESC, id DESC LIMIT 51 OFFSET $2""",
            project_id,
            offset,
        )
        return {
            "sources": [dict(row) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
            "source_verification": "inspect_an_exact_run",
        }

    async def inspect(self, *, project_id: UUID, audit_run_id: UUID):
        run = await self.db.get_run(audit_run_id)
        if run is None or run.project_id != project_id:
            raise TechnicalFixError("source_not_found", "Audit source not found.", status_code=404)
        if (
            run.executor != "organic.audit"
            or run.status.value != "succeeded"
            or not re.fullmatch(r"[0-9a-f]{40}", run.canonical_commit_sha or "")
            or not re.fullmatch(r"[0-9a-f]{40}", run.definition_commit_sha or "")
        ):
            raise TechnicalFixError(
                "source_not_ready", "Choose a successful, pinned organic audit."
            )
        project = await self.db.get_project(project_id)
        if project is None:
            raise TechnicalFixError("source_not_found", "Audit source not found.", status_code=404)
        receipt = await self.db.get_effect(f"organic:{run.id}:publish")
        publication = receipt.result if receipt and receipt.status == "completed" else None
        if (
            not isinstance(publication, dict)
            or publication.get("canonical_commit_sha") != run.canonical_commit_sha
        ):
            raise _invalid_source()
        paths = audit_paths(str(run.id))
        documents = {}
        for name, path in paths.items():
            try:
                content = await self.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=run.canonical_commit_sha, path=path
                )
            except Exception as exc:
                # Storage adapters may contain credential-bearing URLs in their exceptions.
                raise TechnicalFixError(
                    "source_unavailable",
                    "The pinned audit could not be read. Try again.",
                    status_code=503,
                ) from exc
            if not 0 < len(content) <= ARTIFACT_LIMITS[name]:
                raise _invalid_source()
            try:
                documents[path] = content.decode("utf-8")
            except UnicodeError as exc:
                raise _invalid_source() from exc
        if digest(documents) != publication.get("documents_sha256"):
            raise _invalid_source()
        try:
            evidence, inventory = (
                json.loads(
                    documents[paths[name]],
                    object_pairs_hook=_object,
                    parse_constant=_reject_constant,
                )
                for name in ("evidence.json", "findings.json")
            )
            scope, crawl, findings, coverage, excluded = _validate_inventory(
                evidence=evidence, inventory=inventory, run=run, project_id=project_id
            )
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            raise _invalid_source() from exc
        selections = []
        applicability = audit_policy(evidence["policy"]["version"]).get("check_applicability")
        for finding in findings:
            flag = finding["affected_url_evidence"]["flag"]
            # Select the same pages the pinned audit counted, not every raw provider flag.
            affected = [
                p["url"]
                for p in crawl["pages"]
                if (
                    check_outcome(p, flag) == "problem"
                    if applicability
                    else p["checks"].get(flag) is True
                )
            ]
            reason = None
            if finding["check_id"] not in self.supported_checks:
                reason = "check_not_supported"
            elif crawl["status"] != "completed":
                reason = "crawl_incomplete"
            elif len(affected) > MAX_AFFECTED_PAGES:
                reason = "affected_page_limit"
            selections.append(
                {
                    "finding": finding,
                    "affected_urls": affected,
                    "source_eligible": reason is None,
                    "ineligible_reason": reason,
                }
            )
        available = any(row["source_eligible"] for row in selections)
        return {
            "source": {
                "audit_run_id": str(run.id),
                "audit_revision": run.canonical_commit_sha,
                "definition_revision": run.definition_commit_sha,
                "documents_sha256": publication["documents_sha256"],
                "evidence_sha256": inventory["evidence_sha256"],
                "paths": paths,
            },
            "target": {
                "url": scope["url"],
                "host": scope["host"],
                "site_hosts": list(audit_hosts(scope)),
            },
            "crawl_status": crawl["status"],
            "check_coverage": coverage,
            "findings": selections,
            "excluded_findings": excluded,
            "repair_availability": {
                "available": available,
                "reason": None
                if available
                else "no_technical_findings"
                if not selections
                else "no_eligible_findings",
            },
            "execution_available": True,
            "limitations": [
                "Saved crawl observations, not a live verification of the website.",
                "Unobserved checks are unknown, not passes.",
                "Supported missing-metadata findings affecting at most five pages are eligible. "
                "Execution still requires a verified source/build profile and no overlapping PR.",
                "Execution requires fresh verification and an exact source match. "
                "This preview makes no changes.",
            ],
        }

    async def preflight(
        self,
        *,
        project_id: UUID,
        audit_run_id: UUID,
        audit_revision: str,
        finding_id: str,
        expected_repository: str,
        repository_serves_site: bool,
    ):
        if not re.fullmatch(r"[0-9a-f]{40}", audit_revision) or not re.fullmatch(
            r"oa_[0-9a-f]{20}", finding_id
        ):
            raise TechnicalFixError(
                "invalid_selection", "Choose an exact audit finding.", status_code=422
            )
        source = await self.inspect(project_id=project_id, audit_run_id=audit_run_id)
        if source["source"]["audit_revision"] != audit_revision:
            raise TechnicalFixError("source_changed", "The selected audit revision does not match.")
        selection = next(
            (row for row in source["findings"] if row["finding"]["id"] == finding_id), None
        )
        if selection is None:
            excluded = next(
                (row for row in source["excluded_findings"] if row["finding"]["id"] == finding_id),
                None,
            )
            if excluded is not None:
                raise TechnicalFixError(excluded["ineligible_reason"], excluded["message"])
            raise TechnicalFixError(
                "finding_not_found", "Finding not found in this audit.", status_code=404
            )
        if not selection["source_eligible"]:
            reason = selection["ineligible_reason"]
            message = {
                "check_not_supported": (
                    "The pinned repair policy does not support this technical finding; it "
                    f"is {selection['finding']['check_id']}."
                ),
                "crawl_incomplete": "A completed technical crawl is required before repair.",
                "affected_page_limit": (
                    f"This version supports findings affecting at most {MAX_AFFECTED_PAGES} pages."
                ),
            }[reason]
            raise TechnicalFixError(
                reason,
                message,
            )
        if repository_serves_site is not True:
            raise TechnicalFixError(
                "repository_confirmation_required",
                "Confirm that the selected repository serves the audited site.",
            )
        if self.integrations is None:
            raise TechnicalFixError(
                "github_unavailable", "GitHub preparation is unavailable.", status_code=503
            )
        try:
            binding = await self.integrations.github_repository_binding(
                project_id=project_id, expected_repository=expected_repository
            )
        except IntegrationError as exc:
            raise TechnicalFixError("github_binding_failed", str(exc)) from exc
        return {
            "source": source["source"],
            "target": source["target"],
            "selection": selection,
            "repository_binding": asdict(binding),
            "repository_mapping": "member_asserted_not_verified",
            "live_verification": "not_performed",
            "execution_available": True,
            "limitations": source["limitations"]
            + [
                "The repository binding is a preview, not an execution or write authorization.",
                "A run must pin its own binding and verify the affected routes before editing.",
            ],
        }

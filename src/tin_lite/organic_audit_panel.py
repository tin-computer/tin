"""V3: public research → grounded question drafting → frozen measurement panel.

Preparation may correct one known bad proposal. Measured answers are never retried
or selected for favorable outcomes. Every provider call uses the audit's receipts.
"""

import asyncio
import re
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite import prompt_panel
from tin_lite.organic_audit import audit_policy, canonical_json, digest, question_results
from tin_lite.organic_audit_ai import (
    AuditValidationError,
    BuyerPanel,
    PanelValidation,
    apply_review,
    panel_models,
    payload,
    validate_panel,
)
from tin_lite.organic_audit_report import query_rows
from tin_lite.organic_audit_scope import audit_hosts


def research_sources(research, host, *, aliases=()):
    """The model selects only sources we actually received, on the requested host."""
    result = []
    for url in sorted(set(research["sources"] + research["citations"])):
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname in (host, *aliases)
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and not any(ord(char) < 33 for char in url)
        ):
            result.append(url)
    if not result:
        raise AuditValidationError("research_sources_missing")
    return result


def grounded_panel(draft, research, host, *, aliases=(), model=BuyerPanel, max_jobs=3):
    sources = research_sources(research, host, aliases=aliases)
    try:
        proposal = model.model_validate_json(draft["text"])
    except ValueError:
        raise AuditValidationError("panel_questions_invalid") from None
    if proposal.host not in (host, *aliases) or proposal.site_type == "unsupported":
        raise AuditValidationError("panel_identity_invalid")
    if any(question.source_url not in sources for question in proposal.questions):
        raise AuditValidationError("panel_source_unobserved")
    for question in proposal.questions:
        text = question.question
        if len(re.findall(r"\b[\w'-]+\b", text)) > 22 or re.search(
            r"\b(?:pick exactly one|recommend exactly one|name only (?:one|your top choice))\b"
            r"|\[(?:draft|placeholder)",
            text,
            re.I,
        ):
            raise AuditValidationError("panel_questions_invalid")
    try:
        return validate_panel(
            {**draft, "sources": sources, "citations": []},
            host,
            aliases=aliases,
            model=model,
            max_jobs=max_jobs,
        )
    except ValueError:
        raise AuditValidationError("panel_questions_invalid") from None


async def interpret_questions(activities, run_id, questions, scope, suffix):
    """Each reader sees ONE question; another question must not supply missing context."""
    policy = audit_policy(scope["policy_version"])
    limit = asyncio.Semaphore(policy["question_interpretation_concurrency"])

    async def read(index, question):
        async with limit:
            return await activities._model(
                run_id,
                f"panel_interpretation{suffix}:{index}",
                payload(
                    stage="interpret",
                    data=question["question"],
                    market=scope["market"],
                    search=False,
                    policy_version=scope["policy_version"],
                ),
                search=False,
            )

    results = await asyncio.gather(*(read(index, q) for index, q in enumerate(questions)))
    for result in results:
        if result["status"] != "completed":
            return result
    return {
        "status": "completed",
        "value": {
            "text": canonical_json(
                [
                    {"number": index + 1, "interpretation": result["value"]["text"]}
                    for index, result in enumerate(results)
                ]
            ).decode()
        },
    }


def search_queries(saved: dict | None, hosts, limit: int) -> list[dict]:
    """The site's most-searched queries from this run's Search Console read, for the draft.

    Impressions are summed over the pages a query reached. A query that names the site
    (its host label, with or without spaces) is a search for the site, not a buyer job.
    """
    labels = {
        label
        for host in hosts
        for label in [re.sub(r"[^a-z0-9]", "", host.removeprefix("www.").split(".")[0].lower())]
        if len(label) >= 3
    }
    totals: dict[str, int] = {}
    for row in query_rows(saved):
        squashed = re.sub(r"[^a-z0-9]", "", row["query"].lower())
        if squashed and not any(label in squashed for label in labels):
            totals[row["query"]] = totals.get(row["query"], 0) + int(row["impressions"])
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"query": query[:120], "impressions": impressions} for query, impressions in ranked]


def limit_panel(panel: dict, *, max_jobs: int, repetitions: int, unsearched: bool = False) -> dict:
    """Keep the first proposed buyer jobs before any answer is measured.

    The selection depends only on the proposal's order, never on answers or scores. The
    question-set digest covers exactly the questions that will be asked. With `unsearched`,
    each question also gets one answer without web search, after all searched answers.
    """
    jobs = list(dict.fromkeys(question["job"] for question in panel["questions"]))[:max_jobs]
    value = {
        key: item for key, item in panel.items() if key not in {"sha256", "planned_observations"}
    }
    value["questions"] = [q for q in panel["questions"] if q["job"] in jobs]
    planned = len(value["questions"]) * (repetitions + (1 if unsearched else 0))
    return {
        **value,
        "sha256": digest(value),
        "planned_observations": planned,
        "repetitions": repetitions,
        **({"unsearched": True} if unsearched else {}),
    }


async def panel_questions(activities, run_id, scope):
    """The newest succeeded buyer prompt panel for this site, as audit questions.

    organic.prompt_panel publishes a panel only when every check passes, so a succeeded run is
    ready to ask; there is no review step. It is read at that run's own published revision, so
    a later panel does not change it. The choice is saved once, so a retry asks the same
    questions.
    """
    saved = await activities._result(run_id, "prompt_panel")
    if saved is not None:
        return saved.get("panel")
    run = await activities._active(run_id)
    reader = getattr(activities.db, "list_prerequisite_runs", None)
    policy = audit_policy(scope["policy_version"])
    chosen = None
    if reader is not None and not run.input.get("refresh_questions"):
        project = await activities.db.get_project(run.project_id)
        hosts = {prompt_panel.host_of(h) for h in audit_hosts(scope)}
        rows = await reader(
            project_id=run.project_id, workflow_keys=[prompt_panel.WORKFLOW_KEY], limit=10
        )
        for key, source in rows:
            if key != prompt_panel.WORKFLOW_KEY or not getattr(
                source, "canonical_commit_sha", None
            ):
                continue
            try:
                raw = await activities.storage.read_canonical_artifact(
                    repo_id=project.state_repo_id,
                    commit_sha=source.canonical_commit_sha,
                    path=source.artifact_path or prompt_panel.PANEL_PATH,
                )
            except (LookupError, ValueError):
                continue
            block = prompt_panel.parse(raw)
            if (
                block is None
                or block.get("status") != "ready"
                or prompt_panel.host_of(block.get("target")) not in hosts
            ):
                continue
            asked = prompt_panel.questions(block, policy["max_questions"], scope["url"])
            if not asked:
                continue
            value = {
                "site_type": "product",
                "host": scope["host"],
                **prompt_panel.identity(block, scope["host"]),
                "public_description": "The buyer prompt panel for this site.",
                "questions": asked,
                "origin": {
                    "workflow": prompt_panel.WORKFLOW_KEY,
                    "run_id": str(source.id),
                    "revision": source.canonical_commit_sha,
                    "panel_sha256": digest(block),
                },
            }
            aliases = audit_hosts(scope)[1:]
            if aliases:
                value["site_hosts"] = list(audit_hosts(scope))
            unsearched = bool(policy.get("unsearched_answers"))
            chosen = {
                **value,
                "sha256": digest(value),
                "planned_observations": len(asked) * (policy["repetitions"] + int(unsearched)),
                "repetitions": policy["repetitions"],
                **({"unsearched": True} if unsearched else {}),
            }
            break
    await activities._save(run_id, "prompt_panel", {"panel": chosen})
    return chosen


async def reuse_panel(activities, run_id, scope, *, prompts=None):
    """The newest published audit of this project, host and market with a v10 question set.

    Its panel is copied unchanged, with its per-question results as the comparison
    baseline. A run that asked for new questions, or has no earlier set, drafts its own.
    With a buyer prompt panel, only an earlier audit that asked exactly that panel is reused,
    so a new panel starts a new baseline.
    """
    run = await activities._active(run_id)
    policy = audit_policy(scope["policy_version"])
    reader = getattr(activities.db, "list_prerequisite_runs", None)
    if not policy.get("reuse_questions") or run.input.get("refresh_questions") or reader is None:
        return None
    # A retry after a crash keeps the source it already chose, even if a newer audit exists.
    chosen = await activities._result(run_id, "panel_baseline")
    if chosen:
        candidates = [chosen["source_run_id"]]
    else:
        candidates = [
            str(source.id)
            for _, source in await reader(
                project_id=run.project_id, workflow_keys=["organic.audit"], limit=20
            )
        ]
    for source_id in candidates:
        if source_id == run_id:
            continue
        source_scope = await activities._result(source_id, "scope") or {}
        panel = await activities._result(source_id, "panel") or {}
        if (
            panel.get("status") != "completed"
            or (prompts is not None and panel.get("sha256") != prompts["sha256"])
            or source_scope.get("host") != scope["host"]
            or source_scope.get("market") != scope["market"]
            or panel.get("repetitions") != policy["repetitions"]
            or bool(panel.get("unsearched")) != bool(policy.get("unsearched_answers"))
        ):
            continue
        observations = [
            await activities._result(source_id, f"observation:{index}") or {}
            for index in range(panel["planned_observations"])
        ]
        rows = question_results(panel, observations)
        await activities._save(
            run_id,
            "panel_baseline",
            {
                "source_run_id": source_id,
                "source_started_at": source_scope.get("started_at"),
                "panel_sha256": panel["sha256"],
                "repetitions": panel["repetitions"],
                "questions": rows,
                "planned": panel["planned_observations"],
            },
        )
        await activities._save(
            run_id,
            "panel_preparation",
            {
                "status": "completed",
                "reason": None,
                "method": "reused_frozen_panel",
                "source_run_id": source_id,
                "attempts": [],
                "research_sha256": None,
            },
        )
        return await activities._save(run_id, "panel", panel)
    return None


async def prepare_panel(activities, run_id):
    saved = await activities._result(run_id, "panel")
    if saved:
        return saved["planned_observations"] if saved.get("status") == "completed" else 0
    scope = await activities._result(run_id, "scope")
    policy_version = scope["policy_version"]
    policy = audit_policy(policy_version)
    prompts = (
        await panel_questions(activities, run_id, scope) if policy.get("prompt_panel") else None
    )
    reused = await reuse_panel(activities, run_id, scope, prompts=prompts)
    if reused:
        return reused["planned_observations"]
    if prompts:
        await activities._save(
            run_id,
            "panel_preparation",
            {
                "status": "completed",
                "reason": None,
                "method": "buyer_prompt_panel",
                "source_run_id": prompts["origin"]["run_id"],
                "attempts": [],
                "research_sha256": None,
            },
        )
        saved = await activities._save(run_id, "panel", {"status": "completed", **prompts})
        return saved["planned_observations"]
    aliases = audit_hosts(scope)[1:]
    draft_model, review_model, max_jobs = panel_models(policy_version)
    reason, research, research_stage = None, None, None
    attempts = []
    await activities.db.project_run_progress(
        run_id=UUID(run_id),
        mode="steps",
        step="public_research",
        current=0,
        total=2,
        summary="Reading public product evidence before drafting buyer questions.",
    )
    for index in range(policy["max_research_attempts"]):
        research_stage = "panel_research" if index == 0 else "panel_research_recovery"
        research = await activities._model(
            run_id,
            research_stage,
            payload(
                stage="research",
                data={
                    "website": scope["url"],
                    "focus_hint": scope.get("focus", ""),
                    "previous_failure": reason,
                },
                market=scope["market"],
                search=True,
                policy_version=policy_version,
            ),
            search=True,
        )
        reason = research.get("reason")
        if research["status"] == "completed":
            try:
                research_sources(research["value"], scope["host"], aliases=aliases)
            except (TypeError, ValueError) as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, AuditValidationError)
                    else "research_sources_missing"
                )
            else:
                reason = None
        attempts.append({"stage": research_stage, "reason": reason, "status": research["status"]})
        if reason is None:
            break
        # Unknown provider acceptance must never cause another paid dispatch.
        if research["status"] == "unknown" or reason not in {
            "search_not_called",
            "search_not_completed",
            "search_limit_exceeded",
            "research_sources_missing",
            "response_incomplete",
            "response_empty",
        }:
            break

    panel, feedback = None, None
    if reason is None:
        value = research["value"]
        evidence = {
            "website": scope["url"],
            "public_research": value["text"],
            "observed_source_urls": research_sources(value, scope["host"], aliases=aliases),
            **({"verified_site_hosts": list(audit_hosts(scope))} if aliases else {}),
        }
        if policy.get("search_console_questions"):
            searches = search_queries(
                await activities._result(run_id, "search_console_queries"),
                audit_hosts(scope),
                policy["search_console_questions"],
            )
            if searches:
                evidence["search_console_queries"] = searches
        await activities.db.project_run_progress(
            run_id=UUID(run_id),
            mode="steps",
            step="buyer_questions",
            current=1,
            total=2,
            summary="Drafting and checking neutral questions from the saved public evidence.",
        )
        for index in range(policy["max_panel_attempts"]):
            suffix = "" if index == 0 else "_recovery"
            draft = await activities._model(
                run_id,
                f"panel_draft{suffix}",
                payload(
                    stage="panel",
                    data={**evidence, "correction": feedback},
                    schema=draft_model,
                    market=scope["market"],
                    search=False,
                    policy_version=policy_version,
                ),
                search=False,
            )
            reason = draft.get("reason")
            judgment = None
            interpretation = None
            if draft["status"] == "completed":
                try:
                    candidate = grounded_panel(
                        draft["value"],
                        value,
                        scope["host"],
                        aliases=aliases,
                        model=draft_model,
                        max_jobs=max_jobs,
                    )
                    if policy.get("max_panel_jobs"):
                        candidate = limit_panel(
                            candidate,
                            max_jobs=policy["max_panel_jobs"],
                            repetitions=policy["repetitions"],
                            unsearched=bool(policy.get("unsearched_answers")),
                        )
                    review_data = {**evidence, "panel": candidate}
                    if policy.get("standalone_question_review"):
                        review_data = {
                            **evidence,
                            "identity": {
                                key: candidate[key] for key in ("host", "name", "aliases")
                            },
                            "standalone_questions": [
                                {"number": index + 1, "question": question["question"]}
                                for index, question in enumerate(candidate["questions"])
                            ],
                        }
                    if policy.get("blind_question_interpretation"):
                        interpretation = await interpret_questions(
                            activities, run_id, candidate["questions"], scope, suffix
                        )
                        if interpretation["status"] != "completed":
                            raise AuditValidationError(
                                interpretation.get("reason", "panel_review_rejected")
                            )
                        review_data["blind_interpretation"] = interpretation["value"]["text"]
                    # v11 reviews each question, so one ambiguous question no longer sinks
                    # the panel; v10 and older accept or reject the panel as a whole.
                    per_question = policy.get("min_panel_questions")
                    judgment = await activities._model(
                        run_id,
                        f"panel_validation{suffix}",
                        payload(
                            stage="validate",
                            data=review_data,
                            schema=review_model if per_question else PanelValidation,
                            market=scope["market"],
                            search=False,
                            policy_version=policy_version,
                        ),
                        search=False,
                    )
                    if judgment["status"] == "completed" and per_question:
                        panel, reason = apply_review(
                            candidate,
                            review_model.model_validate_json(judgment["value"]["text"]),
                            min_questions=per_question,
                        )
                    elif judgment["status"] == "completed":
                        checked = PanelValidation.model_validate_json(judgment["value"]["text"])
                        if checked.accepted:
                            panel, reason = candidate, None
                        else:
                            reason = "panel_review_rejected"
                    else:
                        reason = judgment.get("reason", "panel_review_rejected")
                except ValueError as exc:
                    reason = (
                        exc.reason
                        if isinstance(exc, AuditValidationError)
                        else "panel_questions_invalid"
                    )
            attempts.append(
                {
                    "stage": f"panel_draft{suffix}",
                    "reason": reason,
                    "status": "completed" if panel else "rejected",
                }
            )
            if (
                panel
                or draft["status"] != "completed"
                or (judgment and judgment["status"] != "completed")
                or (interpretation and interpretation["status"] != "completed")
            ):
                break
            feedback = {"reason": reason, "previous_proposal": draft["value"]["text"]}
            if judgment:
                feedback["review"] = judgment["value"]["text"]
            if interpretation:
                feedback["blind_interpretation"] = interpretation["value"]["text"]
            if len(canonical_json({**evidence, "correction": feedback})) > 60_000:
                break
    preparation = {
        "status": "completed" if panel else "unavailable",
        "reason": reason,
        "research_stage": research_stage,
        "attempts": attempts,
        "research_sha256": digest(research["value"])
        if research and research.get("value")
        else None,
        "method": "public_research_then_blind_question_review"
        if policy.get("blind_question_interpretation")
        else "public_research_then_grounded_draft",
    }
    await activities._save(run_id, "panel_preparation", preparation)
    result = await activities._save(
        run_id, "panel", {"status": "completed", **panel} if panel else preparation
    )
    return result["planned_observations"] if result["status"] == "completed" else 0

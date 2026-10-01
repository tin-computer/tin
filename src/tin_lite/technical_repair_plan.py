"""site-fix-v5: what one technical fix does with every finding an audit makes.

Each audit check falls into one of four places:

- **Fixed in the repository.** One run collects every such finding into one pull request,
  grouped by the kind of change (indexing, sitemap, robots.txt, redirects, page structure,
  accessibility, structured data, social previews, links).
- **A judgment call.** Some fixes depend on what the founder intends, such as which of two
  competing pages survives a merge. `preflight_technical_fix` lists these as
  `decisions_needed`; the coding agent answers from the codebase and what it knows about the
  product, asks the founder when unsure, and passes the answers as the run's `decisions`.
  A finding whose decision is unanswered stays out of the pull request and is listed.
- **Copy.** Titles, descriptions, H1 wording, opening answers, dates and authors belong to
  the content workflows; the technical fix never writes marketing copy.
- **Manual.** Some problems live outside the repository (a CDN's bot settings, analytics,
  performance work); the pull request and the report name the step and where it lives.

Pure data and functions, shared by the preview, preparation, delivery and the live check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from tin_lite.organic_audit_search import page_topic
from tin_lite.organic_audit_site import language_prefix, url_key

POLICY = "site-fix-v5"
DECISION_ITEM = re.compile(r"^(oa_[0-9a-f]{20})=(.{1,500})$", re.S)

# Bounds on one run's pull request. A plan that would exceed them is cut by priority, and
# the rest is listed for a later run.
MAX_FINDINGS = 30
MAX_FILES = 20
MAX_CHANGED_LINES = 800
MAX_FILE_BYTES = 200_000
MAX_NEW_FILE_BYTES = 20_000
MAX_URLS_PER_FINDING = 10
MAX_LIVE_READS = 40
MAX_DECISIONS = 30

# The order groups appear in the pull request and the report.
GROUPS = (
    ("indexing", "Indexing directives"),
    ("sitemap", "Sitemap"),
    ("robots", "robots.txt"),
    ("redirects", "Merges and redirects"),
    ("structure", "Page structure"),
    ("accessibility", "Accessibility"),
    ("schema", "Structured data"),
    ("social", "Social previews"),
    ("links", "Internal links"),
)
GROUP_TITLES = dict(GROUPS)


@dataclass(frozen=True)
class Decision:
    """A judgment call a fix depends on. `options` maps each value to what it means; `keep`
    is the value that leaves the site as it is (the finding is then left out)."""

    question: str
    options: tuple[tuple[str, str], ...]
    suggestion: str
    why: str
    keep: str


@dataclass(frozen=True)
class Repair:
    kind: str
    group: str
    change: str
    # The live check for one page or file after merge; None means the next audit confirms it.
    live: str | None = None
    decision: Decision | None = None
    # How the change is written: rules for the procedure, in plain words.
    how: str = ""
    extra: dict = field(default_factory=dict)


def _yes_no(question, yes, no, suggestion, why, *, yes_label=None, no_label=None):
    return Decision(
        question=question,
        options=((yes, yes_label or yes.replace("_", " ")), (no, no_label or no.replace("_", " "))),
        suggestion=suggestion,
        why=why,
        keep=no,
    )


REPAIRS: dict[str, Repair] = {
    # --- Indexing directives -----------------------------------------------------------
    "indexation.utility_pages_indexable": Repair(
        "html_noindex",
        "indexing",
        "marks sign-in and account pages noindex",
        live="noindex",
        how="Add robots noindex in the page's own metadata (a robots meta tag, or the "
        "framework's metadata API).",
    ),
    "indexation.ad_landing_pages_indexable": Repair(
        "html_noindex",
        "indexing",
        "marks ad landing pages noindex",
        live="noindex",
        how="Add robots noindex in the page's own metadata.",
    ),
    "indexation.noindex_with_search_traffic": Repair(
        "html_remove_noindex",
        "indexing",
        "lets search engines index pages that get search traffic",
        live="indexable",
        decision=_yes_no(
            "These pages are marked noindex but people find them in search. Should search "
            "engines index them?",
            "index",
            "keep_noindex",
            "keep_noindex",
            "A noindex set on the page itself is usually deliberate, for example on event or "
            "campaign pages. Suggest removing it only when the code shows it comes from a shared "
            "template the page was never meant to inherit.",
            yes_label="Remove noindex so they can be indexed",
            no_label="Keep them out of search",
        ),
        how="Remove the noindex directive from these pages only.",
    ),
    "indexation.canonical_elsewhere": Repair(
        "html_self_canonical",
        "indexing",
        "points each page's canonical at the page itself",
        live="self_canonical",
        decision=_yes_no(
            "These pages name another URL as their canonical. Is each one its own page, or a "
            "duplicate of the page it points to?",
            "own_page",
            "duplicate",
            "own_page",
            "They are indexable and distinct URLs; a canonical to another page hides them from "
            "search. Choose duplicate if they really repeat another page.",
            yes_label="Its own page: point the canonical at itself",
            no_label="A duplicate: keep the canonical as it is",
        ),
        how="Set the canonical to the page's own absolute URL.",
    ),
    "indexation.google_canonical_differs": Repair(
        "canonical_consistency",
        "indexing",
        "makes the canonical, internal links and sitemap agree on one URL",
        decision=_yes_no(
            "Google chose a different canonical URL than the page declares. Which URL should "
            "be the one people find?",
            "declared",
            "googles",
            "declared",
            "The page's own canonical is usually the address you link to and market.",
            yes_label="The URL the page declares",
            no_label="The URL Google chose (Tin makes no change)",
        ),
        how="Point internal links and the sitemap at the declared canonical URL, and keep the "
        "canonical tag as it is.",
    ),
    "indexation.multiple_canonicals": Repair(
        "html_one_canonical",
        "indexing",
        "keeps one canonical tag per page",
        live="one_canonical",
        how="Remove the extra canonical tags and keep one existing tag as it is.",
    ),
    "canonical.broken": Repair(
        "canonical_fix",
        "indexing",
        "points canonicals that lead to a missing page at the page itself",
        how="Point each broken canonical at the page's own absolute URL.",
    ),
    "canonical.redirect": Repair(
        "canonical_fix",
        "indexing",
        "points canonicals at the final URL instead of a redirect",
        how="Replace each canonical that redirects with the URL it redirects to.",
    ),
    "indexation.soft_404": Repair(
        "soft_404",
        "indexing",
        "returns a real 404 status for pages that don't exist",
        how="Make the not-found route answer with HTTP 404 (for example Next.js notFound()), "
        "without changing what it shows.",
    ),
    # --- Sitemap -----------------------------------------------------------------------
    "sitemap.non_indexable_urls": Repair(
        "sitemap_remove_urls",
        "sitemap",
        "removes pages that shouldn't be indexed from the sitemap",
        live="sitemap_remove",
    ),
    "sitemap.ad_landing_urls": Repair(
        "sitemap_remove_urls",
        "sitemap",
        "removes ad landing pages from the sitemap",
        live="sitemap_remove",
    ),
    "sitemap.missing_search_pages": Repair(
        "sitemap_add_urls",
        "sitemap",
        "adds pages that get search traffic to the sitemap",
        live="sitemap_add",
    ),
    "sitemap.missing": Repair(
        "sitemap_create",
        "sitemap",
        "adds a sitemap listing the site's indexable pages",
        live="sitemap_exists",
        how="Generate the sitemap the framework's way (for example app/sitemap.ts), or add a "
        "static sitemap.xml; list only indexable pages.",
    ),
    "sitemap.uniform_lastmod": Repair(
        "sitemap_lastmod",
        "sitemap",
        "stops stamping every sitemap entry with the same date",
        how="Use each page's real change date where the repository has one, else leave "
        "lastmod out; never use the build time for every entry.",
    ),
    "sitemap.unreadable_files": Repair(
        "sitemap_references",
        "sitemap",
        "fixes or removes references to sitemap files that can't be read",
    ),
    # --- robots.txt --------------------------------------------------------------------
    "robots.sitemap_reference_missing": Repair(
        "robots_sitemap_line", "robots", "adds a Sitemap line to robots.txt", live="robots"
    ),
    "robots.ai_search_crawlers_blocked": Repair(
        "robots_allow_ai_search",
        "robots",
        "lets AI search crawlers read the site",
        live="robots",
        decision=_yes_no(
            "robots.txt blocks AI search crawlers, the ones that fetch pages to cite them in "
            "answers. Should they be allowed?",
            "allow",
            "keep_blocked",
            "allow",
            "Blocking them keeps the site out of ChatGPT search and Perplexity answers; "
            "training crawlers are a separate choice and stay as they are.",
            yes_label="Allow AI search crawlers",
            no_label="Keep them blocked",
        ),
    ),
    "robots.blocks_important_pages": Repair(
        "robots_unblock",
        "robots",
        "stops robots.txt blocking pages that should be found",
        live="robots_paths",
        decision=_yes_no(
            "robots.txt blocks pages in the sitemap or pages that get search traffic. Should "
            "search engines be allowed to crawl them?",
            "allow",
            "keep_blocked",
            "allow",
            "A blocked page can't be crawled, so its listing goes stale or disappears.",
            yes_label="Allow crawling these pages",
            no_label="Keep them blocked",
        ),
        how="Remove or narrow only the rules that block these paths.",
    ),
    "robots.wildcard_blocks_other_crawlers": Repair(
        "robots_open_wildcard",
        "robots",
        "stops the general robots.txt group closing the site to every other crawler",
        live="robots_wildcard",
        decision=_yes_no(
            "The general group in robots.txt (User-agent: *) blocks the whole site for every "
            "crawler not named. Should the site be open to them?",
            "open",
            "keep_closed",
            "open",
            "Search engines and AI assistants you haven't named can't read any page. Keep it "
            "closed only for a staging or private site.",
            yes_label="Open the site to other crawlers",
            no_label="Keep it closed",
        ),
    ),
    "robots.named_group_rules": Repair(
        "robots_named_groups",
        "robots",
        "repeats the general disallow rules in the named groups",
        live="robots_named",
        decision=_yes_no(
            "A named group in robots.txt lets a crawler into paths the general group closes "
            "(named groups don't inherit the * rules). Should those paths stay closed for it too?",
            "repeat_rules",
            "keep",
            "repeat_rules",
            "This is usually an oversight: the named group was meant to add rules, not remove "
            "them.",
            yes_label="Close those paths for the named crawlers too",
            no_label="Keep the named groups as they are",
        ),
    ),
    # --- Merges and redirects ----------------------------------------------------------
    "search.cannibalization": Repair(
        "merge_redirect",
        "redirects",
        "merges competing pages: redirects the others to the page that stays, and updates "
        "internal links and the sitemap",
        live="redirects_to",
        how="Add permanent (301 or 308) redirects in the framework's or host's redirect "
        "config, point internal links at the surviving page, and remove the redirected URLs "
        "from the sitemap. Don't rewrite the surviving page's copy.",
    ),
    "http.redirect_chain": Repair(
        "redirect_chain",
        "redirects",
        "points redirects and links straight at the final URL",
        how="Make each redirect go directly to its final destination and update internal "
        "links that point at a redirecting URL.",
    ),
    "redirects.loop": Repair(
        "redirect_loop",
        "redirects",
        "breaks redirect loops",
        live="reachable",
        how="Remove or correct the rule that sends the URL back to itself.",
    ),
    "https.http_not_redirected": Repair(
        "https_redirect",
        "redirects",
        "redirects the plain-HTTP site to HTTPS",
        how="Add the redirect where the repository controls it (framework middleware or host "
        "config). If only the host's settings can do it, leave it for the manual list.",
    ),
    # --- Page structure ----------------------------------------------------------------
    "metadata.title_missing": Repair(
        "html_title",
        "structure",
        "gives each page a title",
        live="title",
        how="Derive the title from the page's existing H1 or main heading; don't write new copy.",
    ),
    "metadata.description_missing": Repair(
        "html_description",
        "structure",
        "gives each page a meta description",
        live="description",
        how="Use the page's existing opening sentence, trimmed to 155 characters; don't write "
        "new copy.",
    ),
    "onpage.h1_missing": Repair(
        "html_h1",
        "structure",
        "gives each page one H1",
        live="h1",
        how="Promote the page's existing main heading to an H1, or add one from the page's "
        "existing title text; don't write new copy.",
    ),
    "onpage.h1_multiple": Repair(
        "html_one_h1",
        "structure",
        "keeps one H1 per page",
        live="one_h1",
        how="Keep the first H1 and change the others to H2, without changing their text.",
    ),
    "onpage.lang_missing": Repair(
        "html_lang",
        "structure",
        "declares each page's language",
        live="lang",
        how="Set lang on <html>, in the root layout where the framework has one.",
    ),
    "onpage.lang_mismatch": Repair(
        "html_lang_match",
        "structure",
        "declares the language the page's address says",
        live="lang_match",
        decision=_yes_no(
            "Pages under a language folder (such as /nl/) declare another language. Are they "
            "in the folder's language?",
            "folder_language",
            "keep",
            "folder_language",
            "The folder is how people and search engines choose the page; lang should match it.",
            yes_label="Yes: declare the folder's language",
            no_label="No: leave them as they are",
        ),
    ),
    "onpage.hreflang_missing": Repair(
        "hreflang",
        "structure",
        "links each language version of a page to the others with hreflang",
        live="hreflang",
        how="Add hreflang alternates (and x-default) for each page's language versions, in the "
        "framework's metadata (for example alternates.languages).",
    ),
    "onpage.viewport_missing": Repair(
        "viewport",
        "structure",
        "adds the mobile viewport tag",
        live="viewport",
        how='Add <meta name="viewport" content="width=device-width, initial-scale=1"> once, '
        "in the shared layout.",
    ),
    # --- Accessibility -----------------------------------------------------------------
    "onpage.image_alt_missing": Repair(
        "image_alt",
        "accessibility",
        "describes each meaningful image in its alt text",
        live="image_alt",
        how='Describe what each image shows, from its context; use alt="" for decorative '
        "images. This is accessibility text, not marketing copy.",
    ),
    "onpage.accessible_name_missing": Repair(
        "accessible_name",
        "accessibility",
        "names icon-only links and buttons for screen readers",
        live="accessible_name",
        how="Add an aria-label that says what the link or button does.",
    ),
    "onpage.form_label_missing": Repair(
        "form_label",
        "accessibility",
        "labels form fields",
        live="form_label",
        how="Connect each field to a <label> or give it an aria-label, using the field's "
        "existing placeholder or purpose.",
    ),
    "lighthouse.failed_audits": Repair(
        "lighthouse_markup",
        "accessibility",
        "fixes the markup problems Lighthouse lists (contrast, names, tap targets, SEO tags)",
        how="Fix only markup and style items; list performance items as manual steps.",
    ),
    # --- Structured data ---------------------------------------------------------------
    "schema.invalid": Repair(
        "schema_fields",
        "schema",
        "makes structured data parse and carry its required fields",
        live="schema",
        how="Fix JSON that doesn't parse, and fill required fields only from facts already on "
        "the page; drop a type whose required facts the site doesn't state.",
    ),
    # --- Social previews ---------------------------------------------------------------
    "onpage.open_graph_missing": Repair(
        "open_graph",
        "social",
        "adds Open Graph tags that mirror each page's title and description",
        live="open_graph",
        how="Copy the page's existing title and description into og:title and og:description, "
        "and use an existing site image for og:image. Write no new text.",
    ),
    # --- Internal links ----------------------------------------------------------------
    "discovery.possible_orphan": Repair(
        "internal_links",
        "links",
        "links to pages nothing links to, from related pages",
        how="Add a link from one or two closely related pages, using the target page's "
        "existing title as the link text.",
    ),
    "links.broken": Repair(
        "broken_links",
        "links",
        "fixes internal links that lead to missing pages",
        how="Point each broken link at the live page it meant, or remove the link.",
    ),
    "http.client_error": Repair(
        "broken_links",
        "links",
        "stops linking to URLs that return an error",
        how="Point internal links and sitemap entries at a live page, or remove them.",
    ),
    "aeo.llms_txt_missing": Repair(
        "llms_txt",
        "links",
        "adds an llms.txt listing the site's main pages",
        decision=_yes_no(
            "The site has no llms.txt. No major AI assistant has confirmed it reads one. Add it "
            "anyway?",
            "add",
            "skip",
            "skip",
            "It costs little but nothing confirms it helps; add it only if you want to be early.",
            yes_label="Add an llms.txt with the main pages and their existing titles",
            no_label="Skip it",
        ),
        how="List the site's main pages with their existing titles and descriptions; write no "
        "new text.",
    ),
}

# Findings the content workflows handle, since fixing them means writing copy.
COPY: dict[str, str] = {
    "metadata.title_duplicate": "Distinct titles need new copy for each page.",
    "metadata.description_duplicate": "Distinct descriptions need new copy for each page.",
    "onpage.title_length": "A shorter or longer title is new copy.",
    "onpage.description_length": "A new description is copy.",
    "search.low_ctr": "Titles and descriptions that earn clicks are copy.",
    "search.near_page_one": "Moving up means a stronger page: copy and content.",
    "search.decay": "Pages losing clicks need refreshed content.",
    "search.brand_landing_page": "The brand result's title and page are copy decisions.",
    "aeo.answer_structure": "Answer-first openings and self-contained sections are copy.",
    "aeo.dates_missing": "Dates must be real publication dates, not invented ones.",
    "trust.author_missing": "Authors must be real people, not invented ones.",
    "trust.about_contact_missing": "An about or contact page is new content.",
    "ai.cited_instead": "Being cited instead of other sites takes content.",
    "content.buyer_answer_coverage": "Answering buyer questions takes new content.",
}

# Findings that can't be fixed in the repository, and where the step lives.
MANUAL: dict[str, str] = {
    "access.ai_crawlers_refused": (
        "Your CDN or host refuses AI crawlers. Allow them in its bot or firewall settings "
        "(for example Cloudflare's AI crawler setting); robots.txt can't override it."
    ),
    "access.readers_refused": (
        "Bot protection refuses crawlers. Loosen it for verified search and AI crawlers in "
        "the CDN or host settings."
    ),
    "speed.core_web_vitals": (
        "Speed work (images, scripts, server response) needs profiling, not a small change."
    ),
    "onpage.html_over_limit": "Very large HTML needs the page restructured, not a tag change.",
    "rendering.content_not_in_html": (
        "Content that appears only after JavaScript runs needs server rendering or "
        "prerendering, which is an architecture change."
    ),
    "measurement.analytics_inconsistent": (
        "Analytics setup is left to you; the technical fix never touches tracking."
    ),
    "http.server_error": "Server errors need the server's logs to find the cause.",
    "robots.unavailable": "robots.txt fails to load; check the host or server serving it.",
    "indexation.not_indexed_by_google": (
        "Once the other fixes deploy, ask Google to index these pages with Search Console's "
        "URL Inspection."
    ),
}

# Findings that ask for no change: the audit reports them for review.
NO_CHANGE: dict[str, str] = {
    "http.redirect": "A redirect is usually intentional; nothing to change.",
}


def classify(check_id: str) -> str:
    """fix, copy, manual, no_change or unknown."""
    if check_id in REPAIRS:
        return "fix"
    if check_id in COPY:
        return "copy"
    if check_id in MANUAL:
        return "manual"
    if check_id in NO_CHANGE:
        return "no_change"
    return "unknown"


# An audit finding's next_action, by where this plan puts it (organic-audit-v11 onward).
NEXT_ACTIONS = {
    "fix": "technical_fix",
    "copy": "content_plan",
    "manual": "manual",
    "no_change": "review",
    "unknown": "manual",
}


def next_action(check_id: str) -> str:
    return NEXT_ACTIONS[classify(check_id)]


def supported_checks() -> frozenset[str]:
    return frozenset(REPAIRS)


# --- Decisions ---------------------------------------------------------------------------


def _merge_decision(finding: dict) -> Decision:
    """Which page survives a merge: one URL pattern of the top competing pair, or one URL."""
    patterns = (finding.get("verification") or {}).get("url_patterns") or []
    if patterns:
        first, second = patterns[0]["templates"]
        options = (
            (first, f"Keep pages at {first}; redirect {second} to them"),
            (second, f"Keep pages at {second}; redirect {first} to them"),
        )
        what = f"pages at {first} and {second}"
    else:
        urls = finding.get("urls", [])[:2]
        options = tuple((url, f"Keep {url}; redirect the other page to it") for url in urls)
        what = " and ".join(urls) or "these pages"
    options += (("keep_both", "Keep both and give them different intents (Tin makes no change)"),)
    return Decision(
        question=f"{what} compete for the same searches. Which should stay when they merge?",
        options=options,
        suggestion=options[0][0],
        why="The first option got more impressions. Keep the one your navigation and "
        "marketing already use if that differs.",
        keep="keep_both",
    )


def decision_for(finding: dict) -> Decision | None:
    if finding.get("check_id") == "search.cannibalization":
        return _merge_decision(finding)
    repair = REPAIRS.get(finding.get("check_id"))
    return repair.decision if repair else None


def parse_decisions(items: list[str] | None) -> dict[str, str]:
    """`finding_id=choice` strings, as the run input carries them, to a mapping."""
    answers: dict[str, str] = {}
    for item in items or []:
        match = DECISION_ITEM.fullmatch(item.strip()) if isinstance(item, str) else None
        if not match:
            raise ValueError("Write each decision as finding_id=choice.")
        answers[match.group(1)] = match.group(2).strip()
    if len(answers) > MAX_DECISIONS:
        raise ValueError(f"A run takes at most {MAX_DECISIONS} decisions.")
    return answers


def decision_view(finding: dict, decision: Decision) -> dict:
    return {
        "id": finding["id"],
        "finding": {
            "check_id": finding["check_id"],
            "issue": finding.get("issue") or finding.get("title") or finding["check_id"],
            "urls": list(finding.get("urls") or [])[:5],
        },
        "question": decision.question,
        "options": [{"value": value, "label": label} for value, label in decision.options],
        "suggestion": decision.suggestion,
        "why": decision.why,
    }


ASK = {
    "how_to_answer": (
        "Answer each decision from the codebase and what you know about the product. Ask the "
        "founder only the ones you're unsure of, in one message, with Tin's suggestion first "
        "and why. Pass every answer in the run's decisions input as finding_id=choice."
    ),
    "unanswered": "A finding whose decision you leave out stays out of the pull request.",
}


# --- The plan ----------------------------------------------------------------------------

PRIORITY_ORDER = {"critical": 0, "high_impact": 1, "quick_win": 2, "long_term": 3}
IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}


def rank(finding: dict) -> tuple[int, int, str]:
    return (
        PRIORITY_ORDER.get(finding.get("priority") or "", len(PRIORITY_ORDER)),
        IMPACT_ORDER.get(finding.get("impact") or finding.get("severity") or "", len(IMPACT_ORDER)),
        finding.get("id", ""),
    )


def merge_expectations(finding: dict, survivor: str) -> list[dict]:
    """Which URLs should redirect where once the merge deploys: [{from, to}]."""
    urls = list(finding.get("urls") or [])
    if survivor.startswith(("http://", "https://")):
        return [{"from": url, "to": survivor} for url in urls if url_key(url) != url_key(survivor)]
    moves = []
    for url in urls:
        topic = page_topic(url)
        if not topic or topic[0] == survivor or language_prefix(url):
            continue
        parts = urlsplit(url)
        path = survivor.replace("{x}", topic[1])
        moves.append({"from": url, "to": f"{parts.scheme}://{parts.netloc}{path}"})
    return moves[:MAX_URLS_PER_FINDING]


def build_plan(selections: list[dict], answers: dict[str, str]) -> dict:
    """Sort a verified audit's findings into repairs and what stays out, with reasons.

    `selections` are the audit's findings with their affected URLs (TechnicalFixSources);
    each has `finding`, `affected_urls`, `affected_count` and `source_eligible`.
    """
    repairs, decisions_needed = [], []
    left_out: dict[str, list[dict]] = {
        "copy": [],
        "manual": [],
        "no_change": [],
        "decided_keep": [],
        "decision_unanswered": [],
        "ineligible": [],
        "over_cap": [],
    }

    def brief(row, reason, **extra):
        finding = row["finding"]
        return {
            "id": finding["id"],
            "check_id": finding["check_id"],
            "issue": finding.get("issue") or finding.get("title") or finding["check_id"],
            "reason": reason,
            **extra,
        }

    candidates = []
    for row in selections:
        finding = row["finding"]
        place = classify(finding["check_id"])
        if place == "copy":
            left_out["copy"].append(brief(row, COPY[finding["check_id"]]))
            continue
        if place == "manual":
            left_out["manual"].append(brief(row, MANUAL[finding["check_id"]]))
            continue
        if place == "no_change":
            left_out["no_change"].append(brief(row, NO_CHANGE[finding["check_id"]]))
            continue
        if place == "unknown":
            left_out["manual"].append(brief(row, "Tin doesn't repair this kind of finding yet."))
            continue
        if not row.get("source_eligible", True):
            left_out["ineligible"].append(
                brief(row, row.get("ineligible_reason") or "not eligible")
            )
            continue
        decision = decision_for(finding)
        choice = None
        if decision is not None:
            view = decision_view(finding, decision)
            answer = answers.get(finding["id"])
            if answer is None:
                decisions_needed.append(view)
                left_out["decision_unanswered"].append(
                    brief(row, "Waiting for a decision.", question=decision.question)
                )
                continue
            if answer not in {value for value, _ in decision.options}:
                raise ValueError(
                    f"Decision {finding['id']} must be one of: "
                    + ", ".join(value for value, _ in decision.options)
                )
            if answer == decision.keep:
                left_out["decided_keep"].append(brief(row, "You chose to keep it as it is."))
                continue
            choice = answer
        candidates.append((row, choice))

    candidates.sort(key=lambda item: rank(item[0]["finding"]))
    for row, choice in candidates:
        finding = row["finding"]
        repair = REPAIRS[finding["check_id"]]
        if len(repairs) >= MAX_FINDINGS:
            left_out["over_cap"].append(brief(row, "Left for a later run; this one was full."))
            continue
        entry = {
            "finding_id": finding["id"],
            "check_id": finding["check_id"],
            "kind": repair.kind,
            "group": repair.group,
            "change": repair.change,
            "how": repair.how,
            "live": repair.live,
            "issue": finding.get("issue") or finding.get("title") or finding["check_id"],
            "fix": finding.get("fix", "")[:400],
            "priority": finding.get("priority"),
            "urls": list(row.get("affected_urls") or finding.get("urls") or [])[
                :MAX_URLS_PER_FINDING
            ],
            "affected_count": row.get("affected_count"),
            **({"decision": choice} if choice else {}),
        }
        if repair.kind == "merge_redirect" and choice:
            entry["redirects"] = merge_expectations(finding, choice)
            entry["survivor"] = choice
        repairs.append(entry)
    return {"repairs": repairs, "decisions_needed": decisions_needed, "left_out": left_out}


def grouped(repairs: list[dict]) -> list[tuple[str, list[dict]]]:
    order = [name for name, _ in GROUPS]
    groups: dict[str, list[dict]] = {}
    for entry in repairs:
        groups.setdefault(entry["group"], []).append(entry)
    return [(name, groups[name]) for name in order if name in groups]

"""Guide the caller through sample selection before the ordinary hosted capture run."""

STYLE_PATH = ".agents/skills/writing-style/SKILL.md"

DISCOVERY_INSTRUCTION = (
    "Before preparing style.capture inputs, lead the source discovery conversation using "
    "the guide included in get_workflow's preparation, or call get_writing_style_guide. "
    "Use one simple invitation for representative writing, including their own blog posts "
    "as links or files, unless sources were already supplied. Other writing, Obsidian notes "
    "and recent Codex, Claude Code or other harness sessions are options, not a questionnaire. "
    "Ask only for missing context or scoped access; do not ask about every source family. "
    "Use the supplied sources when sufficient, "
    "curate representative passages and confirm the selection before sharing it with Tin. "
    "Do not substitute a few messages from the current conversation unless the user explicitly "
    "chooses that limited basis. An existing prepared packet may be reused when requested."
)


def style_capture_preparation(project_id, *, include_guide=False):
    """Client-side preparation, surfaced at discovery and incomplete starts; not a run."""
    return {
        "kind": "agent_conversation",
        "before": "prepare_new_samples",
        "next_tool": {
            "name": "get_writing_style_guide",
            "arguments": {"project_id": str(project_id)},
        },
        "instruction": DISCOVERY_INSTRUCTION,
        **({"guide": writing_style_guide()} if include_guide else {}),
    }


def writing_style_guide(*, author=None):
    result = {
        "schema": "writing-style-guide-v2",
        "path": STYLE_PATH,
        "execution": "Your agent selects samples; style.capture extracts the guide. "
        "This read starts no run.",
        "workflow_key": "style.capture",
        "caller_instruction": DISCOVERY_INSTRUCTION,
        "opening_question": (
            "Do you have a few pieces that sound like you—blog posts, drafts or notes you "
            "can share as links or files—or would you like help finding examples?"
        ),
        "source_discovery": {
            "owner": "The calling coding agent, using its own available tools and permissions.",
            "conversation_pacing": [
                "The source list is a menu, not a checklist of questions. Start with one "
                "short invitation, or skip it when the user already supplied sources.",
                "Reuse the conversation's intended writing context and preferences. Do not "
                "ask audience, language, format or history questions just to populate fields.",
                "If the supplied blog posts or other writing are sufficient, use them. "
                "Do not also request an Obsidian vault and agent histories. Offer help finding "
                "those sources when the user wants help or the available writing has a gap.",
                "Ask one focused follow-up only when missing access, authorship or context "
                "would materially affect the capture. Combine selection review and permission "
                "to share/run into one concise confirmation; honor approval already given.",
            ],
            "access_request": (
                "Ask permission for named source families, folders and a session range. "
                "For example: 'May I read the writing folder in that vault and your last ten "
                "sessions for this project to propose samples?' An explicit request to read "
                "those sources already grants that scope; do not ask again for each file. "
                "Permission to inspect is not permission to upload whole documents or logs."
            ),
            "sources": [
                {
                    "kind": "authored_writing",
                    "ask": "Ask whether the user has blog posts they wrote and still likes, "
                    "and invite links or files. Also offer articles, essays, newsletters, "
                    "drafts or substantial emails. Reuse the intended audience and language "
                    "from context; clarify only if it would materially change the selection.",
                    "discover": "Use supplied files/URLs or permitted folders. Prefer original "
                    "prose and edits over writing already generated for the user.",
                    "sample_kind": "authored",
                },
                {
                    "kind": "obsidian",
                    "ask": "Ask for a vault or writing subfolder, or permission to help locate "
                    "it. The user should not have to export notes that your permitted tools "
                    "can already read.",
                    "discover": "Inventory Markdown titles/paths in the approved scope, then "
                    "read likely essays, reflections and developed notes. Exclude .obsidian "
                    "configuration/plugins, attachments and templates. Do not follow links or "
                    "symlinks outside that scope. A vault location is not a request to upload it.",
                    "sample_kind": "note; use authored for an actual user-written essay",
                },
                {
                    "kind": "agent_sessions",
                    "ask": "Offer recent Codex, Claude Code or the user's other harness "
                    "sessions. Establish the harness, projects and time range; propose the "
                    "last ten relevant sessions as a starting search, not a mandatory limit.",
                    "discover": "Use the harness's available history tools, permitted local "
                    "session store or user-provided exports. Discover the actual configured "
                    "location/format from that installation or its current documentation. "
                    "Do not assume every harness exposes history or uses the same paths. "
                    "List recent session metadata first; inspect useful conversations within "
                    "the agreed range. Do not start/resume an agent session to extract writing.",
                    "extract": "Read original user passages that develop ideas or explain "
                    "trade-offs, plus explicit corrections of generated copy. Parse author "
                    "roles, deduplicate messages repeated in event and transcript records, "
                    "and exclude assistant/system/developer/tool output, hidden reasoning, "
                    "injected environment/AGENTS blocks and copied third-party instructions. "
                    "Do not count a compaction summary as original user writing. If a correction "
                    "needs an example, label the generated example as reference, not authored.",
                    "sample_kind": "conversation or correction",
                },
                {
                    "kind": "other_selected_sources",
                    "ask": "Offer project Files, personal documents, blog posts or writing "
                    "available through already-connected sources. References the user admires "
                    "are welcome, but distinguish them from the user's own writing.",
                    "discover": "Use only the user's selected documents and existing "
                    "permissions. Do not add an integration or search an entire mailbox. "
                    "Read project files at exact revisions and retain source labels locally.",
                    "sample_kind": "authored, note, correction or reference, as appropriate",
                },
            ],
            "search_budget": (
                "Start with a metadata inventory, then a bounded pass through relevant documents "
                "or sessions. The eight-sample packet limit is for curated excerpts, not the "
                "number of sources you may inspect. If the first pass is unrepresentative, "
                "explain what is missing and propose a focused wider search rather than "
                "declaring capture complete from convenient scraps."
            ),
            "unavailable_or_declined": (
                "State which requested source you could not inspect and why. Offer a selected "
                "export, a few documents, pasted passages or another permitted source. If the "
                "user declines histories, continue with their writing; histories are not a "
                "requirement. Do not silently use this conversation instead."
            ),
        },
        "selection": {
            "target": "Prefer 3–5 independent pieces with substantial passages, often "
            "1,500–4,000 words combined when available. These are editorial targets, not "
            "validation gates or a reason to collect irrelevant material.",
            "quality": [
                "Match the intended audience, language and writing format.",
                "Prefer developed original prose; use notes for reasoning and sessions for "
                "editorial preferences. Explain the different signals.",
                "Select enough surrounding paragraphs to preserve rhythm and development. "
                "Six one-line requests are not six independent writing samples.",
                "Ask whether material is authored, heavily co-written or aspirational when "
                "unclear; approved AI-generated prose is a reference, not proof of authorship.",
                "Separate explicit preferences from your own inferred patterns. Do not put "
                "your suggested style into the packet as if the user requested it.",
                "Exclude secrets, unrelated personal details and quoted material that does "
                "not contribute to style; disclose substantive omissions.",
            ],
            "preview": "Show a compact source list with readable title/date, kind, passage "
            "length and what each contributes. Show the chosen passages (or make them "
            "available to inspect), and note gaps or exclusions.",
            "confirm": "Confirm the selection and intended use before committing it to Tin. "
            "One approval can cover the selected excerpts, sharing with project members and "
            "running capture. Honor existing explicit approval of that exact selection; "
            "do not add per-file approvals or ask users to choose hashes/versions.",
            "limited_basis": "If only conversation, notes or preferences are available, "
            "explain the limitation and let the user choose whether to proceed or add writing. "
            "A provisional label is honest output metadata, not permission to skip discovery.",
        },
        "source_path_example": "style/sources/my-writing.md",
        # Deliberately invalid until filled: the example must not become invented preferences.
        "source_template": "# Style samples\n\n```json\n"
        '{"purpose": "", "preferences": "", "samples": []}\n```\n',
        "sample_fields": {
            "id": "s1 through s8, unique",
            "label": "A short readable name",
            "kind": "authored, note, conversation, correction, or reference",
            "origin": "User-selected source label; no absolute laptop paths",
            "text": "Selected original passage, not a summary of all the evidence",
        },
        "steps": [
            "Lead the source_discovery conversation first. Reuse context and scoped "
            "permissions the user already gave; ask only what is missing. A generic request "
            "to capture style is not a choice to use the current chat as the only evidence.",
            "Use list_project_files and read_project_file for approved project samples, recording "
            "their exact revisions. User-supplied local files may also be used; do not upload them "
            "unless asked. Identify their origin without exposing private local paths.",
            "Read any existing style guide before revising it. Preserve explicit user preferences. "
            "Distinguish stated preferences from patterns inferred from the samples and mark "
            "uncertain observations. Without samples, say preferences-only; do not fake capture.",
            "Follow the selection quality checks, preview and confirmation. Put only "
            "the approved passages and preferences in source_template. Save the packet through "
            "commit_project_changes, then get_workflow with workflow_key='style.capture' "
            "and start_workflow with source_path and optional direction. Use stable request IDs. "
            "The run proposes the editable guide and waits for the user's approval in Decisions "
            "or approve_workflow_run with get_workflow_review's review_token; the current guide "
            "stays in place until then. Return the real receipt, not a claimed success.",
            "Alternatively, if your agent has already extracted a complete guide and the user "
            "wants to save it without hosted extraction, save only that ordinary file through "
            "commit_project_changes using the current "
            "expected_revision and a stable request_id. On a stale revision, reread and reconcile "
            "rather than overwriting concurrent edits. Sample files stay unchanged.",
            "Show the user the proposed guide and distinguish provisional inference from "
            "confirmed preferences. Show the demonstration and ask what feels wrong or missing "
            "before claiming a validated personal voice. To correct the waiting proposal, call "
            "get_workflow_review, then revise_capture_proposal with the complete revised guide "
            "in the same sections; the user still approves or discards it in Decisions. After "
            "approval, amend the guide through Files or the same MCP file tools. No activation, "
            "new version picker or workflow creation is required.",
        ],
        "template": (
            "---\nname: writing-style\n"
            "description: Project writing voice and editorial preferences\n---\n"
            "# Writing style\n\n"
            "## Basis and confidence\n\n"
            "State whether this is sample-based, preferences-only, or provisional. "
            "List approved sample references and exact project revisions where available.\n\n"
            "## Explicit preferences\n\nRecord what the user actually asked for.\n\n"
            "## Voice and rhythm\n\nGive practical, evidence-backed rules.\n\n"
            "## Vocabulary and structure\n\n"
            "Describe preferred words, openings, headings and endings.\n\n"
            "## Do and avoid\n\nUse short original examples.\n\n"
            "## Boundaries\n\n"
            "Style is not evidence. Never invent facts, quotes, product capabilities "
            "or experience to imitate a voice. Workflow permissions and review rules still apply.\n"
        ),
        "consumers": ["New content.public_article definitions read this guide when present."],
        "limits": [
            "This is guidance for the caller, not a claim that style has already been captured.",
            "Existing saved workflow configurations keep their pinned definition.",
            "A plan-to-article workflow and GitHub article delivery are separate follow-up work.",
        ],
    }
    if author is not None:
        result["path"] = author["guide_path"]
        result["author"] = author
        result["capture_inputs"] = {"author_id": author["id"]}
        result["steps"].insert(
            0,
            "Use capture_inputs.author_id when starting style.capture. Read or save only "
            "this author's path, never the shared project guide. The selected author is "
            "an explicit binding, not a name match.",
        )
        result["consumers"] = [
            "This guide belongs to the selected author. "
            "Consumers must select that author explicitly."
        ]
    return result

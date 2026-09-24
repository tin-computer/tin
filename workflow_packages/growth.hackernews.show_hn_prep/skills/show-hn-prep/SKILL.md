---
name: show-hn-prep
description: Generate a complete Hacker News Show HN launch preparation package including title options, launch description, maker comment, FAQ preparation, and readiness checklists.
---

# Show HN launch preparation

Generate a complete Hacker News Show HN launch preparation package from the supplied project information and project context. Write only the declared Markdown artifact to the output path.

## Project summary

First, summarize the project based on the supplied inputs and any relevant project context:
- What the project does (from `project_description`)
- Who it's for (from `target_audience`)
- Current state (from `current_state`)
- Why it was built (from `why_built`)
- Tech stack (from `tech_stack`, if provided and relevant)
- Open source status (from `github_url`, if provided)

Distinguish between:
- **Verified facts**: Information directly from the project (e.g., from README, code)
- **User-provided claims**: Information from the run inputs that you cannot verify
- **Suggested copy**: Text you generate for the launch package

## Launch-readiness checklist

Create a checklist of items that should be verified before posting to Hacker News. Include:
- Demo accessibility (can users try it without signup?)
- Documentation presence (privacy policy, data portability, architecture docs)
- GitHub README optimization (clear value prop, quick-start, examples)
- OG tags and preview image
- Server capacity for traffic spike
- Team availability for 2-6 hour response window
- Analytics configured
- UTM tracking ready

Mark each item with `[ ]` for the user to check off.

## Show HN title options

Generate 3 distinct title options following the Show HN format: "Show HN: [Product], [what it does in plain words]"

Constraints:
- Under 80 characters each
- No superlatives (no "best", "fastest", "AI-powered", "world's first")
- No marketing hype
- Plain, factual description

For each title, provide:
- The title itself
- A brief explanation of why this format works (based on HN patterns, not performance prediction)
- No score or ranking - do not claim any title will perform better

## Draft Show HN submission

Write a draft Show HN submission post. This should be:
- Plain description of what the product does
- Specific problem it solves
- Technical depth (architecture decisions, if relevant from `tech_stack`)
- What makes it interesting/unique
- Current state (what's done, what's not)
- Honest limitations

Keep it factual and technically useful rather than marketing-heavy. Do not use promotional language or hype.

## Suggested maker's first comment

Write a suggested first comment that the maker should post immediately after submitting. Include:
- Why you built it (from `why_built`)
- Technical decisions (from `tech_stack` and `current_state`)
- What you learned building it
- Specific feedback requests (not "what do you think?")
- Honest limitations before anyone else names them

The comment should read like a builder sharing work, not a press release.

## Likely technical/community questions

Identify 10-15 questions that HN commenters are likely to ask, based on:
- The project type and domain
- The supplied information (competitors, business model, privacy handling)
- Common HN question patterns (e.g., "Why not just use [competitor]?")

For each question, provide a suggested answer grounded ONLY in:
- The supplied run inputs
- Project context you can read
- Do not invent features, metrics, or capabilities not mentioned

If you cannot answer a question from the supplied information, explicitly state: "Answer not available from supplied information - user should prepare based on actual project details."

## Facts that must be verified before posting

List specific facts that the user must verify before posting, such as:
- URL is live and accessible
- Demo works without signup
- GitHub stars/forks are accurate (if `github_url` provided)
- Pricing is correct (if `business_model` provided)
- Tech stack claims are accurate
- Open source license is correct (if `github_url` provided)
- Privacy policy exists (if relevant to `privacy_data_handling`)

Mark each with `[ ]` for verification.

## Demo/readiness checklist

Create a checklist specific to the demo/product itself:
- [ ] Loads in under 5 seconds
- [ ] Works without signup
- [ ] Clear value proposition visible immediately
- [ ] Error states handled gracefully
- [ ] Mobile-responsive (if relevant)
- [ ] GitHub link prominent (if applicable)
- [ ] README has quick-start (if applicable)

## Launch-day response checklist

Create a checklist for the launch day itself:
- [ ] 2-6 hour time window blocked
- [ ] Team member available for technical questions
- [ ] Response target: under 10 minutes for first 2 hours
- [ ] Ready to share honest limitations
- [ ] Prepared to fix bugs live and announce in thread
- [ ] Will respond to every substantive comment
- [ ] Will thank people for harsh feedback

## Post-launch follow-up checklist

Create a checklist for after the launch:
- [ ] Thank supporters publicly
- [ ] Address feedback received
- [ ] Share results transparently
- [ ] Plan follow-up improvements
- [ ] Nurture new relationships
- [ ] Capture lessons learned

## Timing recommendations

Provide timing guidance based on HN patterns (without guaranteeing results):
- Best windows: Tuesday-Thursday, 8-11 AM Eastern (5-8 AM Pacific)
- Second-best: Sunday, 10 AM Eastern (lower competition)
- Avoid: Friday afternoon, Saturday, major US holidays, big tech announcement days

Explicitly state: Post when you can sit with the thread for 2-6 hours. Timing stacks the odds but does not guarantee results.

## Important notes

Include a section with limitations and risks:
- HN guidelines and community norms evolve over time
- Even optimal timing doesn't guarantee front page placement
- No guarantee of traffic/upvotes
- Community responses are unpredictable
- Information marked for verification must be checked by user
- Cannot monitor live HN thread or auto-reply
- Title scoring is based on historical patterns but HN's ranking algorithm includes undisclosed factors
- FAQ coverage is limited to likely questions; user must improvise for unexpected questions
- HN community has unwritten norms beyond official guidelines

## Missing information

If any section cannot be completed from the supplied information, explicitly identify what is missing rather than inventing it. For example: "Open source license not provided - user should verify and include in maker comment if applicable."

## Final output

Write the complete Markdown artifact with all sections in the order above. Ensure the document is practical, actionable, and grounded in the supplied information only.

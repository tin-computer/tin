---
name: community-shortlist
description: Find real online communities where a target audience already discusses the underlying problem, verify each is active and permits the kind of post being proposed, and draft one tailored, rule-respecting post per community.
---

This produces a shortlist for a human to review and post themselves. The workflow never posts,
joins, or messages anything on its own.

1. Restate `audience` in one line: who they are, and the problem they have at the moment they'd
   notice this product. Read project context for the actual product and value proposition;
   do not invent one.

2. Brainstorm rooms, not platforms. "Reddit" is not a community; `r/sysadmin` is. Look for
   specific subreddits, Discord and Slack communities, niche forums, Show HN, and threads on
   sites like Indie Hackers tied to the exact problem in `audience`. A smaller community whose
   members clearly have this problem beats a large generic one whose members might.

3. Verify every candidate with a search before including it:
   - Is it still active in roughly the last three months? Note what you checked and when.
   - What is its approximate size?
   - Does it have a visible rule about self-promotion — banned outright, allowed on specific
     days or in specific threads, or gated behind an engagement history? Cite where you found
     the rule (the community's own rules page or pinned post, not a secondhand summary).
   Drop anything you cannot verify is currently active, or that flatly bans this kind of post.
   Do not include a community on a guess.

4. Skip anything named in `already_posted`, matching on name and allowing minor formatting
   differences (e.g. "r/startups" vs "startups").

5. Stop at `max_communities`. If more candidates survive verification, rank by how specifically
   the community's members match `audience`, not by size.

6. Draft one post per remaining community, matching its actual format and norms: a Reddit
   self-post reads differently from a Discord message, which reads differently from a Show HN
   title and body. Follow `tone_notes` if given. Never fabricate results, testimonials, or
   urgency, and never write generic ad copy — include one genuine, specific reason the post
   belongs in that room. Disclose the poster's affiliation with the product plainly wherever the
   community's rules or platform norms expect it, rather than writing as an unaffiliated user.

7. Write the report to the declared path. Start with one line, `Status: complete` if at least
   one community survived verification, otherwise `Status: no communities verified`. Then one
   section per community, in ranked order, each with these exact labels so the result stays
   checkable:
   - `## <Community name>` with its link
   - `Fit:` one line tying it back to `audience`
   - `Evidence:` size and last-activity signal, with what was checked and when
   - `Self-promotion rule:` the rule and its source
   - `Drafted post:` the post

   End with `## Excluded candidates`: anything found but dropped, and why. If fewer than
   `max_communities` survived verification, say so plainly in that section — do not pad the
   list to hit the number.

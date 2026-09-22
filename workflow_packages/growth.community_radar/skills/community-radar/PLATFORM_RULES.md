# Community Rules, Etiquette & Platform Guidelines

Every online platform has distinct cultural norms and automated spam detection. Violating these norms damages brand credibility and triggers domain-level shadowbanning.

---

## 1. Reddit Rules & Cultural Etiquette

### Baseline Guidance vs. Live Community Rules
The subreddit-specific notes in this document represent baseline guidance only. Subreddit rules, wikis, auto-moderator filters, and moderator policies change frequently.
**Mandatory Verification Rule**: When evaluating an actual Reddit opportunity, check the specific subreddit's sidebar, wiki, pinned announcement post, or visible community rules when accessible before finalizing an engagement recommendation. This static guidance document does NOT override current, live subreddit-specific rules. If a subreddit's live rules explicitly forbid vendor participation, external links, or tool mentions, respect the live rules and recommend "No Action".

### Global Rediquette & Anti-Spam
- **The 9:1 Rule**: Accounts should contribute at least 9 high-value community interactions (comments, troubleshooting help, non-promotional discussions) for every 1 mention of their own project.
- **Link Filters**: Many subreddits automatically remove comments containing raw URL shorteners, affiliate tags, or UTM tracking parameters. Use clean root links or documentation links only.
- **Direct Messages (DMs)**: Unsolicited DMs to users who posted in a subreddit are flagged as aggressive spam by Reddit's automated trust-and-safety heuristics. **Never send unsolicited DMs.**

### Subreddit-Specific Baseline Nuances
- **`r/devops`, `r/sysadmin`**: Highly technical, allergic to marketing buzzwords. If you recommend a tool, explain its architecture, daemon overhead, and failure modes. Never say "game-changer".
- **`r/selfhosted`**: Welcomes new tools only if they are open-source, have a Docker Compose file, and can run air-gapped without mandatory cloud telemetry.
- **`r/SaaS`, `r/startups`**: Strict rules against top-level self-promotional posts. Comments are acceptable if responding directly to an explicit recommendation request.
- **`r/webdev`, `r/reactjs`, `r/node`**: Code examples speak louder than marketing landing pages. Provide code snippets directly in the comment.

---

## 2. Hacker News (HN) Guidelines

Hacker News has the highest technical bar and lowest tolerance for marketing fluff on the internet.

### Core Principles
- **No Astroturfing**: Never create multiple accounts or coordinate upvotes. HN’s algorithmic spam filters detect voting rings and silently shadowban domains.
- **Mandatory Affiliation Disclosure**: If you mention your own project, company, or open-source tool, you must state your relationship clearly:
  * *"Disclosure: I'm one of the creators of [Product]..."*
  * *"I work on [Product], so take this with a grain of salt, but..."*
- **Acknowledge Competitors Honestly**: Good HN comments praise competing open-source or commercial alternatives when they are superior for specific use cases.
- **Depth Over Pitch**: The comment must provide substantive engineering or operational value. If a user asks about database sharding, provide a deep 3-paragraph explanation of sharding mechanics, and mention your database utility in a single closing sentence.

---

## 3. GitHub Discussions & Issues

GitHub is a collaborative engineering workspace, not a marketing forum.

### Issue vs. Discussion Etiquette
- **Public Issues**: Never hijack an open bug report in an open-source project to advertise a paid commercial product unless the maintainers explicitly discuss deprecation or external alternatives.
- **GitHub Discussions (Q&A / Ideas)**: If an author asks *"Is there an existing library or service for X?"*, proposing a tool is welcome, provided:
  1. You explain how it integrates with the project.
  2. You link to open-source repositories or public technical documentation rather than sales demo booking pages.
  3. You disclose affiliation.

---

## 4. When Is Direct Outreach (DM / Email) Legitimate?

### Direct Messaging (DM)
- **Allowed**: ONLY when the author explicitly invites it in the post body (e.g., *"DM me recommendations"*, *"If you build in this space, shoot me a message"*).
- **Prohibited**: Cold DMs sent to anyone who merely asked a question publicly.

### Business Email
- **Allowed (Exceptional B2B Case Only)**:
  1. The user posted an enterprise-critical blocker on behalf of an identifiable organization.
  2. The user has published their official corporate email address on their public GitHub profile or corporate contact card.
  3. The message is personalized, references the exact technical problem, and offers immediate engineering assistance without sales pressure.
- **Prohibited**: Scraping personal email addresses (Gmail, Hotmail, ProtonMail) from usernames or whois records to pitch forum posters.

# Intent & Demand Classification Taxonomy

Use this taxonomy to calibrate the buying intent of community discussions. Reject false positives (keyword mentions without problem-solution intent).

---

## Tier 1: Active Solution Search (Highest Intent)

The author has recognized a problem, exhausted existing tools, and is actively seeking a replacement, recommendation, or paid product.

### Trigger Phrasing & Signals
- *"Looking for an alternative to [Competitor]..."*
- *"What tool do you recommend for [Problem]?"*
- *"We are switching away from [Competitor] because of [Pricing/Complexity]. What are people using?"*
- *"Does any SaaS exist that connects [A] to [B] natively?"*
- Mentions explicit budget, timeline, or team scale: *"Willing to pay $100–$500/mo for something reliable."*
- Mentions trial evaluations: *"Currently evaluating X vs Y, but both feel clunky."*

### Engagement Recommendation
- **Action**: **Public Response** (Default) or **DM** (only if author explicitly requested private contact).
- **Goal**: Present the solution clearly, address the specific pain with the incumbent, and provide a direct path to test or learn more.

---

## Tier 2: Acute Workflow Blocker (High Technical Pain)

The author is not actively shopping for software, but is experiencing an acute, unblocked technical bottleneck in production or development.

### Trigger Phrasing & Signals
- *"How do you handle [Problem] in [Framework/Platform] without breaking [Constraint]?"*
- *"Been stuck on [Error/Bottleneck] for three days, documentation is useless."*
- *"Our build pipeline takes 45 minutes because of [Issue], is there a better pattern?"*
- *"Why is [Competitor] failing to ingest [Data format]?"*

### Engagement Recommendation
- **Action**: **Public Response** (Technical / Educational).
- **Goal**: Unblock the author with code snippets, architecture guidance, or diagnostic advice. Mention your tool only as a dedicated utility or open-source wrapper that completely automates that workaround.

---

## Tier 3: Exploratory / Architecture Evaluation (Medium Intent)

The author is designing a system, planning ahead, or comparing architectural paradigms for a future build.

### Trigger Phrasing & Signals
- *"Ask HN: How are modern teams structuring [Architecture] in 2026?"*
- *"Evaluating whether to build [Feature] in-house or buy an off-the-shelf solution."*
- *"Pros and cons of [Approach A] vs [Approach B] for scaling [System]?"*
- Broad survey questions asking for community experience.

### Engagement Recommendation
- **Action**: **Public Response** (High-Level Thought Leadership) or **No Action**.
- **Rule**: Never pitch a product directly in Tier 3 discussions unless you are responding to a specific comment asking about commercial solutions. Provide deep, objective trade-off analysis.

---

## Tier 4: Casual Chatter / News / Venting (Zero Intent — Discard)

Discussions that mention relevant keywords but exhibit zero commercial or solution-seeking intent.

### Examples of False Positives
- **News & Announcements**: *"Company X acquires Company Y"* or *"Version 4.0 released"*.
- **Memes & Humor**: Memes complaining about cloud bills, software engineers, or framework churn.
- **Cynical Venting**: Rants about a platform without asking for alternatives or assistance.
- **Academic / Theoretical**: Academic papers, student homework, philosophical debates (*"Is REST dead?"*).
- **Affiliate / Spam Threads**: Existing spam threads promoting sketchy tools.

### Engagement Recommendation
- **Action**: **No Action**. Discard immediately from actionable opportunities. Use only to inform general market sentiment if a widespread grievance is identified.

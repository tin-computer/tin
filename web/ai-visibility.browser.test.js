// Packaged dashboard with synthetic APIs. No live credentials or projects.
// AI Visibility: the tab shows once a week is stored; the week's table opens one engine's answers,
// folds the full answer to its first line and never turns an answer's text into markup.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp } from "./app-fixture.js";

const totals = (answers, recommended, mentioned, named, rivals) => ({
  answers, recommended, mentioned, questions: 2, questions_named: named, rivals,
  recommended_rate: recommended / answers, mentioned_rate: mentioned / answers,
});
const week = (start, web, memory) => ({week_start: start, web, memory});
const earlier = week("2026-10-26", totals(4, 0, 0, 0, []), totals(2, 0, 0, 0, []));
const latest = week("2026-11-02", totals(4, 1, 2, 1, [{name: "HubSpot", answers: 3}]), totals(2, 0, 0, 0, [{name: "Zapier", answers: 1}]));

const page = {
  week_start: "2026-11-02", collected_at: "2026-11-04T19:51:18+00:00", domain: "example.com", takes: 2,
  engines: [
    {key: "chatgpt_app", name: "ChatGPT", detail: "app", kind: "web", icon: "openai"},
    {key: "perplexity_sonar", name: "Perplexity", detail: "Sonar", kind: "web", icon: "perplexity"},
    {key: "sol_noweb", name: "GPT-6.1 Sol", detail: "OpenAI", kind: "memory", icon: "openai"},
  ],
  sections: [{key: "ICP2", title: "Technical founders running their own marketing"}, {key: "shared", title: "Both"}],
  groups: [
    {key: "C7", section: "ICP2", title: "Run marketing from my coding agent", note: ""},
    {key: "C13", section: "shared", title: "Branded comparison", note: "Names you in the question."},
  ],
  questions: [
    {id: "7d", group: "C7", text: "Which marketing tools can I add as an MCP server to Claude Code or Cursor?", in_score: true},
    {id: "7e", group: "C7", text: "marketing skills for claude code", in_score: true},
    {id: "13a", group: "C13", text: "Example Co vs Okara", in_score: false},
  ],
  cells: {
    "7d": {chatgpt_app: {outcome: "recommended", count: 1, of: 2}, perplexity_sonar: {outcome: "none", count: 2, of: 2}, sol_noweb: {outcome: "none", count: 2, of: 2}},
    "7e": {chatgpt_app: {outcome: "mentioned", count: 2, of: 2}, perplexity_sonar: {outcome: "none", count: 2, of: 2}, sol_noweb: {outcome: "none", count: 2, of: 2}},
  },
  rivals: {"7d": {web: ["HubSpot", "Ahrefs"], memory: ["Zapier"]}, "7e": {web: ["HubSpot"], memory: []}, "13a": {web: ["Okara"], memory: []}},
  score: {web: latest.web, memory: latest.memory},
  previous: {web: earlier.web, memory: earlier.memory},
  weeks: [earlier, latest],
};

const answer = (take, outcome, extra = {}) => ({
  question: "7d", engine: "chatgpt_app", take, outcome, quote: "", text: "", queries: [], cited: [], read: [], recommended_instead: [], ...extra,
});
const answers = {
  "7d|chatgpt_app": {
    question: page.questions[0], engine: page.engines[0],
    answers: [
      answer(1, "none", {text: "Plain answer.", recommended_instead: ["HubSpot"]}),
      answer(2, "recommended", {
        quote: "**Example Co** runs it from your coding agent.",
        text: "Here are the options people use today.\n\n## Picks\n- **Example Co** for agents\n- HubSpot <img src=x onerror=\"window.injected=1\">\n\n[Docs](https://example.com/docs) [bad](javascript:alert(1))",
        queries: ["marketing MCP servers Claude Code"],
        cited: [{url: "https://example.com/mcp", domain: "example.com", title: "Example"}, {url: "https://hubspot.com/", domain: "hubspot.com", title: "HubSpot"}],
        recommended_instead: ["HubSpot", "Ahrefs"],
      }),
    ],
  },
};

async function open(options, url, run) {
  const served = await serveApp({decisionCount: 0, ...options});
  const browser = await chromium.launch({headless: true});
  try {
    const {page: tab, context, errors} = await openApp(browser, served.base, {url});
    await run(tab);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    served.server.close();
  }
}

test("the tab stays hidden until the project has a stored week", {timeout: 120000}, async () => {
  await open({}, "/system", async tab => {
    await tab.locator(".system-view").waitFor();
    assert.equal(await tab.locator('.nav-item[data-view="ai-visibility"]').isHidden(), true);
  });
});

test("a week: score, trend, the table, one engine's answers and the folded full answer", {timeout: 120000}, async () => {
  await open({visibility: {page, answers}}, "/system", async tab => {
    const item = tab.locator('.nav-item[data-view="ai-visibility"]');
    await item.waitFor();
    await item.click();
    await tab.locator(".visibility-grid").first().waitFor();
    assert.equal(new URL(tab.url()).pathname, "/ai-visibility");
    assert.equal(await tab.locator(".visibility-week strong").innerText(), "Nov 2 – Nov 8");
    assert.equal(await tab.locator('.visibility-arrow[aria-label="Next week"]').isDisabled(), true);
    const tiles = await tab.locator(".visibility-tiles .analytics-tile strong").allInnerTexts();
    assert.deepEqual(tiles, ["25%", "50%", "1 of 2", "HubSpot"]);
    // Recommended leads; the answers without web search aren't shown.
    assert.equal(await tab.locator(".analytics-tile.is-hero > span").innerText(), "Recommended");
    assert.equal(await tab.getByRole("tab", {name: "What models already know"}).count(), 0);
    // The question that names the project sits apart, under its own heading.
    const apart = tab.locator(".visibility-apart");
    assert.equal(await apart.locator("h2").innerText(), "Not counted in the score");
    assert.deepEqual(await apart.locator(".visibility-question span").allInnerTexts(), ["Example Co vs Okara"]);
    assert.equal(await tab.locator(".visibility-table").first().getByText("Example Co vs Okara").count(), 0);
    assert.equal(await tab.locator(".visibility-trend").count(), 1);
    assert.deepEqual(await tab.locator(".visibility-table").first().locator(".visibility-engine strong").allInnerTexts(), ["ChatGPT", "Perplexity"]);
    assert.equal(await tab.locator('.visibility-table').first().locator('.visibility-engine img[src="/assets/ai-engines/openai.svg"]').count(), 1);
    assert.equal(await tab.locator('[data-visibility-cell="7d"][data-visibility-engine="chatgpt_app"] .visibility-frac').innerText(), "1/2");

    await tab.locator('[data-visibility-cell="7d"][data-visibility-engine="chatgpt_app"]').click();
    await tab.locator(".visibility-answer").waitFor();
    // It opens on the answer that went furthest, with the passage rendered, not its Markdown.
    assert.equal(await tab.locator(".visibility-take.is-active").innerText(), "Answer 2");
    assert.equal(await tab.locator(".visibility-quote strong").innerText(), "Example Co");
    assert.match(await tab.locator(".visibility-first").innerText(), /^Here are the options people use today\.…$/);
    assert.equal(await tab.locator(".visibility-facts li.is-yours a").getAttribute("href"), "https://example.com/mcp");

    await tab.getByRole("button", {name: "Read the full answer"}).click();
    const text = tab.locator(".visibility-text");
    await text.waitFor();
    assert.equal(await text.locator("h5").innerText(), "Picks");
    assert.equal(await text.locator("img").count(), 0);
    assert.match(await text.innerText(), /HubSpot <img src=x onerror/);
    assert.deepEqual(await text.locator("a").evaluateAll(links => links.map(link => link.getAttribute("href"))), ["https://example.com/docs"]);
    assert.equal(await tab.evaluate(() => window.injected), undefined);

    await tab.locator('[data-visibility-take="1"]').click();
    assert.equal(await tab.locator(".visibility-answer-head strong").innerText(), "Not mentioned");

  });
});

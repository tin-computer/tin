// The accelerator demo: a founder copies one line from tin.computer, adds Tin to Claude Code in
// their terminal, asks how Tin could help, and finds the marketing work running in the dashboard.
// A desk film (desk.js), about two minutes at full pace. Plan: with.md d7jvujea; Paper: Films ·
// Accelerator demo. Aupilot is a made-up company; its accounts and numbers are staged.
import {WINDOWS} from "../desk.js";
import {serveAupilot} from "../worlds/aupilot-film.js";

export const title = "Tin, from tin.computer to the dashboard";
export const now = "2026-11-02T22:40:00Z";
export const timezone = "Europe/Berlin";
const PROJECT = "99203a41-d1e2-5b3f-99dc-dd5e81103575";

export const desk = {
  project: PROJECT,
  siteUrl: "https://tin.computer/",
  start: "/system",
  ready: ".nav-item",
  serve: ({now}) => serveAupilot({now}),
};

// Where the hand rests between beats: an empty corner of the browser window, off every word.
const BLANK = {x: 1826, y: 826, width: 12, height: 12};
// Where the camera rests: the whole desk, and the terminal with a gutter around it.
const DESK_VIEW = {x: 0, y: 0, width: 1920, height: 940};
const t = WINDOWS.terminal;
const TERMINAL_VIEW = {x: t.left - 36, y: t.top - 36, width: t.width + 72, height: t.height + 72};
const COMMAND = "claude mcp add --transport http tin https://app.tin.computer/mcp";

// Claude Code's own lines, as version 2.1.314 prints them; the agent's tool calls are condensed
// from the session that created Aupilot over MCP on 2 November 2026.
const HEADER = [
  [[" ▐▛███▜▌   ", "claude"], ["Claude Code ", "bold"], ["v2.1.314", "dim"]],
  [["▝▜█████▛▘  ", "claude"], ["Opus 5.5", "soft"]],
  [["  ▘▘ ▝▝    ", "claude"], ["~/projects/aupilot", "soft"]],
  [],
];

// The terminal's lines once Claude Code has started: its header and the sign-in warning.
const OPENING = [...HEADER, [["⚠ 1 MCP server needs authentication · run /mcp", "warn"]], []];
const row = id => `.people-table tr[data-person="${id}"]`;

// A person's ID in People, by their name.
async function personId(f, name) {
  return f.app.evaluate(name => [...document.querySelectorAll(".people-table tr[data-person]")]
    .find(row => row.querySelector("td.is-name")?.textContent.trim().startsWith(name))?.getAttribute("data-person"), name);
}

// What the beats can't say: places on the desk, the terminal's long lines, and elements the film
// names on the page before it points at them.
export const values = {
  BLANK,
  DESK_VIEW,
  TERMINAL_VIEW,
  COMMAND,
  OPENING,
  // The page has an install block in its hero and more below: the film works in the hero's.
  tagHero: f => f.app.evaluate(() => document.querySelector(".oss-install")?.setAttribute("data-film-hero", "")),
  // The System calendar's cards, named by their titles.
  tagWeekRuns: f => f.app.evaluate(() => {
    for (const card of document.querySelectorAll("[data-week-run]")) card.setAttribute("data-film-title", card.textContent.trim().split("\n")[0].trim());
  }),
  // The first file the Files search lists, named for the cursor.
  searchResult: async f => {
    await f.ready("#files-search-results [data-project-file]");
    await f.app.evaluate(() => document.querySelector("#files-search-results [data-project-file]")?.setAttribute("data-film-result", ""));
    return "[data-film-result]";
  },
  danielName: async f => `${row(await personId(f, "Daniel Okafor"))} td.is-name`,
  pickDaniel: async f => `[data-people-pick="${await personId(f, "Daniel Okafor")}"]`,
  pickTariq: async f => `[data-people-pick="${await personId(f, "Tariq Aziz")}"]`,
  // The referrer row for ChatGPT, named for the scroll and its mark.
  tagAiRow: f => f.app.evaluate(() => [...document.querySelectorAll(".analytics-row")].find(row => /chatgpt\.com/.test(row.textContent))?.setAttribute("data-film-ai", "")),
};

const card = title => `[data-week-run][data-film-title="${title}"]`;

export const scenes = [
  {name: "site", beats: [
    {focus: "site"},
    {run: "tagHero"},
    {camera: "@DESK_VIEW", cut: true, nudge: 0, pad: 0},
    {begin: true},
    {say: "Tin is an open-source marketing system that works inside your coding agent.", brisk: true},
    {wait: [1.6, 1.0]},
    {click: '[data-film-hero] [role="tab"]:nth-of-type(2)'},
    {wait: [0.8, 0.4]},
    {say: "You add it to your agent with one command from tin.computer."},
    {click: "[data-film-hero] .oss-install__copy"},
    {mark: "copied"},
    {wait: [1.6, 0.9]},
  ]},
  {name: "terminal", beats: [
    {hide: true},
    {focus: "term"},
    {"term.prompt": true},
    {raise: "terminal"},
    {camera: "@TERMINAL_VIEW", nudge: 0, duration: 1.2},
    {say: "Paste the command in your terminal to add Tin to Claude Code.", brisk: true},
    {wait: [0.4, 0.2]},
    {"term.paste": "@COMMAND"},
    {wait: [0.6, 0.3]},
    {"term.enter": true},
    {"term.print": [
      [["Added HTTP MCP server tin with URL: https://app.tin.computer/mcp to local config", "soft"]],
      [["File modified: /Users/avery/.claude.json [project: /Users/avery/projects/aupilot]", "soft"]],
    ], over: 0.3},
    {"term.prompt": true},
    {wait: [0.5, 0.3]},
    {"term.type": "claude"},
    {"term.enter": true},
    {wait: [0.2, 0.1]},
    {"term.clear": true},
    {"term.print": "@OPENING"},
    {"term.box": 'Try "how could Tin help me grow aupilot.com?"'},
    {when: "full", beats: [
      {say: "The first time you use Tin, it asks you to sign in."},
      {wait: 0.6},
      {"term.type": "/mcp"},
      {"term.enter": true},
      {"term.print": [[["  ⎿  ", "dim"], ["Opening browser to sign in…", "soft"]]]},
      {wait: 0.9},
      {"term.print": [[["  ⎿  ", "dim"], ["Authentication successful. Connected to tin.", "ok"]], []]},
      {"term.box": true},
    ]},
    {"term.print": [[["  ⎿  ", "dim"], ["Authentication successful. Connected to tin.", "ok"]], []], when: "brisk"},
    {say: "Then ask your agent how Tin could help your product grow.", brisk: true},
    {wait: [0.5, 0.3]},
    {"term.type": "How could Tin help me grow aupilot.com?"},
    {wait: [0.3, 0.2]},
    {"term.enter": true},
    {say: "The agent reads the repository, creates a Tin project and starts the first workflows."},
    {"term.work": [14, 1.2], verb: "Reading"},
    {"term.print": [
      [["⏺ ", ""], ["Read 9 files", "bold"], [" (ctrl+o to expand)", "dim"]],
      [["⏺ ", "ok"], ["tin - create_project ", "bold"], ['(MCP)(name: "Aupilot")', "dim"]],
      [["  ⎿  ", "dim"], ["Created Aupilot", "soft"]],
    ], over: 0.9},
    {"term.work": [41, 1.2]},
    {"term.print": [
      [["⏺ ", "ok"], ["tin - start_workflow ", "bold"], ['(MCP)(workflow_id: "organic.traffic_system", site_url: "https://aupilot.com")', "dim"]],
      [["  ⎿  ", "dim"], ["Started: site audit, keyword research, a three-month content plan", "soft"]],
      [],
      [["⏺ ", ""], ["Tin is set up for Aupilot. It is auditing aupilot.com, researching keywords", ""]],
      [["  ", ""], ["and planning three months of articles. Drafts wait for you in Decisions:", ""]],
      [["  ", ""], [`https://app.tin.computer/system?project=${PROJECT.slice(0, 8)}…`, "link"]],
    ], over: 1.2},
    {say: "Aupilot is a made-up company, and its accounts and numbers are staged for this demo.", brisk: true},
    {mark: "answer"},
    {wait: [2.2, 1.6]},
    {click: ".link"},
    {wait: 0.2},
    {hide: true},
    {camera: "@DESK_VIEW", nudge: 0, duration: 1.1, pad: 0},
  ]},
  {name: "system", beats: [
    {show: ["app", "app.tin.computer/system"]},
    {focus: "app"},
    {raise: "browser"},
    {say: "Each piece of marketing work runs as a workflow, and My system shows all of them.", brisk: true},
    {run: "tagWeekRuns"},
    {wait: [1.4, 0.8]},
    {move: card("Run the organic traffic system")},
    {wait: [0.6, 0.3]},
    {say: "For Aupilot, Tin audited the site, researched keywords and planned three months of articles."},
    {wait: 1.6},
    {click: card("Audit organic visibility")},
    {address: "app.tin.computer/document/bf0e7c17"},
    {ready: ".markdown-viewer"},
    {say: "The audit lists what to fix on the site, starting with the changes that matter most.", brisk: true},
    {move: "@BLANK"},
    {mark: "audit"},
    {wait: [2.6, 1.6]},
  ]},
  {name: "integrations", beats: [
    {click: '[data-view="integrations"]'},
    {address: "app.tin.computer/integrations"},
    {ready: "[data-integration-filter]"},
    {say: "Tin connects to the tools you already use: GitHub, Search Console, LinkedIn and Tin Analytics.", brisk: true},
    {move: "@BLANK"},
    {mark: "integrations"},
    {wait: [2.4, 1.4]},
  ]},
  {name: "decisions", beats: [
    {click: '[data-view="decisions"]'},
    {address: "app.tin.computer/decisions"},
    {ready: "[data-decision-id]"},
    {say: "Anything Tin writes waits in Decisions until you review it.", brisk: true},
    {wait: [1.4, 0.8]},
    // What changed folds into the version row; at full pace the hand opens it under its line.
    {when: "full", beats: [
      {click: ".review-change-toggle", to: [0.95, 0.55]},
      {say: "When you ask for changes, Tin writes a new version and says what changed."},
      {wait: 2.4},
    ]},
    {say: "When you approve a page, Tin opens a pull request on your site's repository.", brisk: true},
    {move: '[data-apply-decision][data-delivery="github_pr"]', to: [0.5, 0.5]},
    {mark: "poster"},
    {wait: [0.6, 0.4]},
    {click: '[data-apply-decision][data-delivery="github_pr"]'},
    {ready: ".toast.is-visible"},
    {wait: [0.5, 0.3]},
    {mark: "approved"},
    {wait: [1.6, 1.0]},
    // Next in Decisions: the posts Tin drafted from the article.
    {say: "Tin also turns the article into posts for LinkedIn and X, for you to review.", brisk: true},
    {click: '[data-decision-id="bf30dd71-3e2b-4dca-9566-d79dd4846d78"]'},
    {wait: [0.6, 0.4]},
    {click: '[data-decision-read="bf30dd71-3e2b-4dca-9566-d79dd4846d78"]'},
    {address: "app.tin.computer/document/bf30dd71"},
    {ready: ".markdown-viewer"},
    {move: "@BLANK"},
    {mark: "posts"},
    {wait: [2.4, 1.4]},
  ]},
  {name: "files", beats: [
    {click: '[data-view="files"]'},
    {address: "app.tin.computer/files"},
    {wait: [0.6, 0.3]},
    {say: "Every report and draft is a plain file in the project, with its history."},
    {click: "#files-search", to: [0.3, 0.5]},
    {type: "ICP", interval: 0.09},
    {wait: [0.5, 0.3]},
    {click: "@searchResult", to: [0.2, 0.5]},
    {address: "app.tin.computer/file?path=strategy/ICP.md"},
    {ready: ".markdown-viewer"},
    {move: "@BLANK"},
    {mark: "icp"},
    {wait: [2.6, 1.0]},
  ]},
  {name: "people", beats: [
    {click: '[data-view="people"]'},
    {address: "app.tin.computer/people"},
    {ready: "[data-people-pick]"},
    {say: "People ranks everyone who engaged with your LinkedIn posts by how well they fit your ICP.", brisk: true},
    {wait: [1.6, 1.0]},
    {when: "full", beats: [
      {click: "@danielName"},
      {ready: ".person-panel"},
      {say: "Each score comes with its reason, taken from your ICP file and the person's profile."},
      {wait: 2.6},
      {click: ".person-panel [data-close-person]"},
      {wait: 0.6},
    ]},
    {say: "You choose who to meet, and Tin sends the requests from your LinkedIn one at a time.", brisk: true},
    {click: "@pickDaniel", to: [0.85, 0.85]},
    {click: "@pickTariq", to: [0.85, 0.85]},
    {wait: [0.4, 0.2]},
    {click: ".people-picked-send", to: [0.97, 0.3]},
    {ready: ".toast.is-visible"},
    {move: "@BLANK"},
    {wait: [0.4, 0.3]},
    {mark: "requests"},
    {wait: [2.2, 1.4]},
  ]},
  {name: "analytics", beats: [
    {click: '[data-view="analytics"]'},
    {address: "app.tin.computer/analytics"},
    {ready: ".analytics-view, [data-analytics-tab]"},
    {say: "Tin Analytics counts the visits to your site, including visits from AI assistants.", brisk: true},
    {move: "@BLANK"},
    {wait: [1.2, 0.8]},
    {say: "It counts automated browsers separately, so you can see how many people really came."},
    {scrollPage: ".analytics-chart", duration: 1.1, offset: 16},
    {wait: 1.6},
    {say: "Every week, Tin uses these numbers to plan the next week's work.", brisk: true},
    {run: "tagAiRow"},
    {scrollPage: "[data-film-ai]", duration: 1.1, offset: 260},
    {highlight: "[data-film-ai]", hold: 2},
    {mark: "analytics"},
    {wait: [2.4, 1.6]},
  ]},
  {name: "end", beats: [
    {hide: true},
    {camera: "@DESK_VIEW", nudge: 0, duration: 1.1, pad: 0},
    {endCard: true},
    {say: "Tin. Add it to your coding agent at tin.computer.", brisk: true},
    {mark: "end"},
    {wait: [1.0, 0.8]},
    {read: true},
    {say: null},
    {end: true},
  ]},
];

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

export const scenes = [
  {
    name: "site",
    async play(f) {
      f.focus("site");
      // The page has an install block in its hero and more below: the film works in the hero's.
      await f.app.evaluate(() => document.querySelector(".oss-install")?.setAttribute("data-film-hero", ""));
      await f.camera(DESK_VIEW, {cut: true, nudge: 0, pad: 0});
      await f.begin();
      await f.say("Tin is an open-source marketing system that works inside your coding agent.", {brisk: true});
      await f.wait(1.6, 1.0);
      await f.click('[data-film-hero] [role="tab"]:nth-of-type(2)');
      await f.wait(0.8, 0.4);
      await f.say("You add it to your agent with one command from tin.computer.");
      await f.click("[data-film-hero] .oss-install__copy");
      await f.mark("copied");
      await f.wait(1.6, 0.9);
    },
  },
  {
    name: "terminal",
    async play(f) {
      f.hide();
      f.focus("term");
      await f.term.prompt();
      await f.raise("terminal");
      await f.camera(TERMINAL_VIEW, {nudge: 0, duration: 1.2});
      await f.say("Paste the command in your terminal to add Tin to Claude Code.", {brisk: true});
      await f.wait(0.4, 0.2);
      await f.term.paste(COMMAND);
      await f.wait(0.6, 0.3);
      await f.term.enter();
      await f.term.print([
        [["Added HTTP MCP server tin with URL: https://app.tin.computer/mcp to local config", "soft"]],
        [["File modified: /Users/avery/.claude.json [project: /Users/avery/projects/aupilot]", "soft"]],
      ], {over: 0.3});
      await f.term.prompt();
      await f.wait(0.5, 0.3);
      await f.term.type("claude");
      await f.term.enter();
      await f.wait(0.2, 0.1);
      f.term.lines = [];
      await f.term.print([...HEADER, [["⚠ 1 MCP server needs authentication · run /mcp", "warn"]], []]);
      await f.term.box('Try "how could Tin help me grow aupilot.com?"');
      if (!f.brisk) {
        await f.say("The first time you use Tin, it asks you to sign in.");
        await f.wait(0.6);
        await f.term.type("/mcp");
        await f.term.enter();
        await f.term.print([[["  ⎿  ", "dim"], ["Opening browser to sign in…", "soft"]]]);
        await f.wait(0.9);
        await f.term.print([[["  ⎿  ", "dim"], ["Authentication successful. Connected to tin.", "ok"]], []]);
        await f.term.box();
      } else {
        await f.term.print([[["  ⎿  ", "dim"], ["Authentication successful. Connected to tin.", "ok"]], []]);
      }
      await f.say("Then ask your agent how Tin could help your product grow.", {brisk: true});
      await f.wait(0.5, 0.3);
      await f.term.type("How could Tin help me grow aupilot.com?");
      await f.wait(0.3, 0.2);
      await f.term.enter();
      await f.say("The agent reads the repository, creates a Tin project and starts the first workflows.");
      await f.term.work(14, 1.2, {verb: "Reading"});
      await f.term.print([
        [["⏺ ", ""], ["Read 9 files", "bold"], [" (ctrl+o to expand)", "dim"]],
        [["⏺ ", "ok"], ["tin - create_project ", "bold"], ['(MCP)(name: "Aupilot")', "dim"]],
        [["  ⎿  ", "dim"], ["Created Aupilot", "soft"]],
      ], {over: 0.9});
      await f.term.work(41, 1.2);
      await f.term.print([
        [["⏺ ", "ok"], ["tin - start_workflow ", "bold"], ['(MCP)(workflow_id: "organic.traffic_system", site_url: "https://aupilot.com")', "dim"]],
        [["  ⎿  ", "dim"], ["Started: site audit, keyword research, a three-month content plan", "soft"]],
        [],
        [["⏺ ", ""], ["Tin is set up for Aupilot. It is auditing aupilot.com, researching keywords", ""]],
        [["  ", ""], ["and planning three months of articles. Drafts wait for you in Decisions:", ""]],
        [["  ", ""], [`https://app.tin.computer/system?project=${PROJECT.slice(0, 8)}…`, "link"]],
      ], {over: 1.2});
      await f.say("Aupilot is a made-up company, and its accounts and numbers are staged for this demo.", {brisk: true});
      await f.mark("answer");
      await f.wait(2.2, 1.6);
      await f.click(".link");
      await f.wait(0.2);
      f.hide();
      await f.camera(DESK_VIEW, {nudge: 0, duration: 1.1, pad: 0});
    },
  },
  {
    name: "system",
    async play(f) {
      await f.show("app", "app.tin.computer/system");
      f.focus("app");
      await f.raise("browser");
      await f.say("Each piece of marketing work runs as a workflow, and My system shows all of them.", {brisk: true});
      await tagWeekRuns(f);
      await f.wait(1.4, 0.8);
      await f.move(weekRun("Run the organic traffic system"));
      await f.wait(0.6, 0.3);
      await f.say("For Aupilot, Tin audited the site, researched keywords and planned three months of articles.");
      await f.wait(1.6);
      await f.click(weekRun("Audit organic visibility"));
      await f.address("app.tin.computer/document/bf0e7c17");
      await f.ready(".markdown-viewer");
      await f.say("The audit lists what to fix on the site, starting with the changes that matter most.", {brisk: true});
      await f.move(BLANK);
      await f.mark("audit");
      await f.wait(2.6, 1.6);
    },
  },
  {
    name: "integrations",
    async play(f) {
      await f.click('[data-view="integrations"]');
      await f.address("app.tin.computer/integrations");
      await f.ready("[data-integration-filter]");
      await f.say("Tin connects to the tools you already use: GitHub, Search Console, LinkedIn and Tin Analytics.", {brisk: true});
      await f.move(BLANK);
      await f.mark("integrations");
      await f.wait(2.4, 1.4);
    },
  },
  {
    name: "decisions",
    async play(f) {
      await f.click('[data-view="decisions"]');
      await f.address("app.tin.computer/decisions");
      await f.ready("[data-decision-id]");
      await f.say("Anything Tin writes waits in Decisions until you review it.", {brisk: true});
      await f.wait(1.4, 0.8);
      await f.move(".review-change-summary", {to: [0.12, 0.5]});
      await f.say("When you ask for changes, Tin writes a new version and says what changed.");
      await f.wait(2.4);
      await f.say("When you approve a page, Tin opens a pull request on your site's repository.", {brisk: true});
      await f.move('[data-apply-decision][data-delivery="github_pr"]', {to: [0.5, 0.5]});
      await f.mark("poster");
      await f.wait(0.6, 0.4);
      await f.click('[data-apply-decision][data-delivery="github_pr"]');
      await f.ready(".toast.is-visible");
      await f.wait(0.5, 0.3);
      await f.mark("approved");
      await f.wait(1.6, 1.0);
      // Next in Decisions: the posts Tin drafted from the article.
      await f.say("Tin also turns the article into posts for LinkedIn and X, for you to review.", {brisk: true});
      await f.click('[data-decision-id="bf30dd71-3e2b-4dca-9566-d79dd4846d78"]');
      await f.wait(0.6, 0.4);
      await f.click('[data-decision-read="bf30dd71-3e2b-4dca-9566-d79dd4846d78"]');
      await f.address("app.tin.computer/document/bf30dd71");
      await f.ready(".markdown-viewer");
      await f.move(BLANK);
      await f.mark("posts");
      await f.wait(2.4, 1.4);
    },
  },
  {
    name: "files",
    async play(f) {
      await f.click('[data-view="files"]');
      await f.address("app.tin.computer/files");
      await f.wait(0.6, 0.3);
      await f.say("Every report and draft is a plain file in the project, with its history.");
      await f.click("#files-search", {to: [0.3, 0.5]});
      await f.type("ICP", {interval: 0.09});
      await f.wait(0.5, 0.3);
      await f.click(await searchResult(f), {to: [0.2, 0.5]});
      await f.address("app.tin.computer/file?path=strategy/ICP.md");
      await f.ready(".markdown-viewer");
      await f.move(BLANK);
      await f.mark("icp");
      await f.wait(2.6, 1.0);
    },
  },
  {
    name: "people",
    async play(f) {
      await f.click('[data-view="people"]');
      await f.address("app.tin.computer/people");
      await f.ready("[data-people-pick]");
      await f.say("People ranks everyone who engaged with your LinkedIn posts by how well they fit your ICP.", {brisk: true});
      await f.wait(1.6, 1.0);
      const daniel = await personId(f, "Daniel Okafor"), tariq = await personId(f, "Tariq Aziz");
      if (!f.brisk) {
        await f.click(`${row(daniel)} td.is-name`);
        await f.ready(".person-panel");
        await f.say("Each score comes with its reason, taken from your ICP file and the person's profile.");
        await f.wait(2.6);
        await f.click(".person-panel [data-close-person]");
        await f.wait(0.6);
      }
      await f.say("You choose who to meet, and Tin sends the requests from your LinkedIn one at a time.", {brisk: true});
      await f.click(`[data-people-pick="${daniel}"]`, {to: [0.85, 0.85]});
      await f.click(`[data-people-pick="${tariq}"]`, {to: [0.85, 0.85]});
      await f.wait(0.4, 0.2);
      await f.click(".people-picked-send", {to: [0.97, 0.3]});
      await f.ready(".toast.is-visible");
      await f.move(BLANK);
      await f.wait(0.4, 0.3);
      await f.mark("requests");
      await f.wait(2.2, 1.4);
    },
  },
  {
    name: "analytics",
    async play(f) {
      await f.click('[data-view="analytics"]');
      await f.address("app.tin.computer/analytics");
      await f.ready(".analytics-view, [data-analytics-tab]");
      await f.say("Tin Analytics counts the visits to your site, including visits from AI assistants.", {brisk: true});
      await f.move(BLANK);
      await f.wait(1.2, 0.8);
      await f.say("It counts automated browsers separately, so you can see how many people really came.");
      await f.scrollPage(".analytics-chart", {duration: 1.1, offset: 16});
      await f.wait(1.6);
      await f.say("Every week, Tin uses these numbers to plan the next week's work.", {brisk: true});
      await f.app.evaluate(() => [...document.querySelectorAll(".analytics-row")].find(row => /chatgpt\.com/.test(row.textContent))?.setAttribute("data-film-ai", ""));
      await f.scrollPage("[data-film-ai]", {duration: 1.1, offset: 260});
      await f.highlight("[data-film-ai]", {hold: 2});
      await f.mark("analytics");
      await f.wait(2.4, 1.6);
    },
  },
  {
    name: "end",
    async play(f) {
      f.hide();
      await f.camera(DESK_VIEW, {nudge: 0, duration: 1.1, pad: 0});
      await f.endCard();
      await f.say("Tin. Add it to your coding agent at tin.computer.", {brisk: true});
      await f.mark("end");
      await f.wait(1.0, 0.8);
      await f.read();
      await f.say(null);
      await f.end();
    },
  },
];

// The System calendar's cards, named by their titles.
async function tagWeekRuns(f) {
  await f.app.evaluate(() => {
    for (const card of document.querySelectorAll("[data-week-run]")) card.setAttribute("data-film-title", card.textContent.trim().split("\n")[0].trim());
  });
}

// A card on the System calendar, by its title.
function weekRun(title) {
  return `[data-week-run][data-film-title="${title}"]`;
}
const row = id => `.people-table tr[data-person="${id}"]`;

// A person's ID in People, by their name.
async function personId(f, name) {
  return f.app.evaluate(name => [...document.querySelectorAll(".people-table tr[data-person]")]
    .find(row => row.querySelector("td.is-name")?.textContent.trim().startsWith(name))?.getAttribute("data-person"), name);
}

// The first file the Files search lists, named for the cursor.
async function searchResult(f) {
  await f.ready("#files-search-results [data-project-file]");
  await f.app.evaluate(() => document.querySelector("#files-search-results [data-project-file]")?.setAttribute("data-film-result", ""));
  return "[data-film-result]";
}

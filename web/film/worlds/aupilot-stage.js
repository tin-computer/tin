// What the demo film stages on top of the recorded Aupilot world. The recording is the reviewers'
// project as it is; the film shows the whole loop a founder gets, so the integrations Tin works
// through show as connected: GitHub with the site's repository, Search Console for aupilot.com,
// LinkedIn for Avery (posting, reading and sending), and Tin Analytics as recorded. Aupilot and
// everyone in it are made up; the film's subtitle says the accounts and numbers are staged.
import {linkedin, sending} from "../world.js";

const at = (now, days, time = "09:00") => {
  const day = new Date(Date.parse(now) - days * 86400000);
  const [hours, minutes] = time.split(":").map(Number);
  day.setUTCHours(hours, minutes, 0, 0);
  return day.toISOString();
};

export function stagedIntegrations(recorded, now) {
  const connected = (key, configuration, label = null, days = 6) => {
    const item = recorded.find(entry => entry.key === key);
    return {...item, connection_id: `film-${key}`, project_id: item.project_id, status: "connected", external_account_label: label,
      configuration, connected_at: at(now, days), last_checked_at: at(now, 0, "08:40"), last_error_code: null};
  };
  const listed = recorded.map(item => {
    if (item.key === "infra.github") return connected("infra.github", {selected_repository: "aupilot/site", code_repositories: []}, "aupilot");
    if (item.key === "analytics.gsc") return connected("analytics.gsc", {selected_site_url: "sc-domain:aupilot.com"}, "aupilot.com");
    return item;
  });
  const posting = {
    key: "social.linkedin", name: "LinkedIn posting", badge: "in", description: "Publish approved posts to LinkedIn.",
    unlocks: ["Approved LinkedIn posts"], configured: true, connection_id: "film-social.linkedin", status: "connected",
    connected_at: at(now, 6), last_checked_at: at(now, 0, "08:40"),
    configuration: {accounts: [{id: "avery", name: "Avery Lane", kind: "profile", active: true, can_post: true}]},
  };
  const reading = linkedin("Avery Lane", {connected: at(now, 6), verified: at(now, 0, "08:40")});
  // LinkedIn's card stands where the first of its parts stands: after Search Console and GitHub.
  const index = listed.findIndex(item => item.key === "infra.github") + 1;
  listed.splice(index, 0, posting, reading);
  // The connected ones lead the list, so the window shows all four.
  return [...listed.filter(item => item.connection_id), ...listed.filter(item => !item.connection_id)];
}

export function stagedRequests(now) {
  return sending({week: {sent: 9, accepted: 4, queued: 0, pending: 5}, next: at(now, -1, "09:00"), timezone: "Europe/Berlin"});
}

// The System calendar's week, built from the recorded runs: the organic traffic system and the
// work it started on the evening of 2 November.
export function stagedWeek(start = "2026-11-02", timezone = "Europe/Berlin") {
  const run = (id, key, title, status, at, finished, extra = {}) => ({id, project_workflow_id: null, workflow_key: key, workflow_title: title, status,
    trigger_source: "mcp", at, finished_at: finished, title: null, summary: null, progress_current: null, progress_total: null, ...extra});
  // The weekly article, saved by the organic traffic system: Tuesdays at 10:00 UTC.
  const occurrences = [{project_workflow_id: "71125f90-2f88-4bf3-9256-479f013aeefc", at: "2026-11-03T10:00:00Z", state: "planned", name: "Weekly article", lands: "Decisions"}];
  return {start, timezone, occurrences, runs: [
    run("161181d1-2239-425b-9c6f-8d402c219090", "organic.traffic_system", "Run the organic traffic system", "running", "2026-11-02T21:25:50Z", null, {progress_current: 4, progress_total: 6}),
    run("bf0e7c17-0e03-43f2-843e-f4429a35fd90", "organic.audit", "Audit organic visibility", "succeeded", "2026-11-02T21:25:51Z", "2026-11-02T21:29:47Z",
      {summary: "Organic visibility audit is ready with partial evidence. Inspected 11 of 11 sitemap pages; AI visibility was not measured.", progress_current: 2, progress_total: 2}),
    run("bb310f7c-b4ed-4200-8d4e-b5faf6d765a2", "organic.keyword_plan", "Plan keyword opportunities", "succeeded", "2026-11-02T21:25:51Z", "2026-11-02T21:29:11Z",
      {summary: "Keyword plan is ready: 104 candidates, 4 reviewable groups. Coverage limits are in the report."}),
    run("0163bd17-fe74-414a-9ddd-245323983cb0", "content.plan", "Plan upcoming content", "succeeded", "2026-11-02T21:29:49Z", "2026-11-02T21:44:47Z",
      {project_workflow_id: "dd9242ff-78de-4164-a7cb-fc30ab0b4725", title: "Content plan for aupilot.com", summary: "Content roadmap approved. Nothing generated or published.", progress_current: 3, progress_total: 3}),
    run("2596919a-553a-4e0f-87eb-2c8c30e457aa", "content.generate", "Draft planned content", "needs_input", "2026-11-02T21:49:01Z", null,
      {title: "Collections that sound like you", progress_current: 3, progress_total: 3}),
  ]};
}

// The LinkedIn reader asks who may post: Avery's own profile, connected through LinkedIn posting.
export function stagedDrafts(recorded) {
  return {...recorded, connected: true, accounts: [{urn: "urn:li:person:avery-lane", kind: "person", name: "Avery Lane"}], unavailable: []};
}

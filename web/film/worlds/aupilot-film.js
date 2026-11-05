// The accelerator demo's world: the recorded Aupilot project (aupilot.json) with what the film
// stages on top (aupilot-stage.js), and the two things the founder does on screen. Approving the
// article opens a pull request; asking two people to connect queues them on Avery's LinkedIn.
// Aupilot and everyone in it are made up; the film's subtitle says its accounts are staged.
import {loadWorld, serveReplay} from "../replay.js";
import {stagedDrafts, stagedIntegrations, stagedRequests, stagedWeek} from "./aupilot-stage.js";

const slug = name => name.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z]+/g, "-").replace(/^-|-$/g, "");

// Avery is the project's one member, so whatever the recording calls a teammate's is Avery's.
const yours = value => (value === "a teammate" ? "you" : value);

// A person as the film shows them: known by a LinkedIn profile (the demo's people are known by a
// made-up address), and open to a request when Avery isn't connected to them yet.
function staged(person, queued) {
  const relationship = person.fields?.linkedin_relationship?.value || "none";
  const request = queued.has(person.id) ? "queued" : person.request || null;
  const out = {...person, email: "", profile_url: `https://www.linkedin.com/in/${slug(person.name)}`,
    requestable: relationship === "none" && !request, request};
  if (out.last_touch) out.last_touch = {...out.last_touch, who: yours(out.last_touch.who)};
  if (out.note) out.note = {...out.note, by: yours(out.note.by), who: yours(out.note.who)};
  if (Array.isArray(out.notes)) out.notes = out.notes.map(note => ({...note, by: yours(note.by), who: yours(note.who), mine: true}));
  if (Array.isArray(out.events)) out.events = out.events.map(event => ({...event, who: yours(event.who), yours: event.who === "a teammate" ? true : event.yours}));
  return out;
}

export async function serveAupilot({now}) {
  const world = await loadWorld("aupilot");
  const P = world.project;
  const at = now().toISOString();
  const recorded = key => world.responses[key]?.json;
  const integrations = stagedIntegrations(recorded(`/api/projects/${P}/integrations`), at);
  const requests = stagedRequests(at);
  const queued = new Set();
  const approved = new Set();
  const draftsKey = Object.keys(world.responses).find(key => key.includes("/linkedin/drafts"));
  const answer = (method, url, body) => {
    const route = url.pathname;
    if (method === "GET" && route === `/api/projects/${P}/integrations`) return {json: integrations};
    if (method === "GET" && route === `/api/projects/${P}/linkedin-requests`) return {json: requests};
    if (method === "GET" && route === `/api/projects/${P}/week`) return {json: stagedWeek()};
    if (method === "GET" && route.endsWith("/publish-preview")) return {json: {adapt: false}};
    if (method === "GET" && route === `/api/projects/${P}/linkedin/drafts`) return {json: stagedDrafts(recorded(draftsKey))};
    if (method === "GET" && route === `/api/projects/${P}/people`) {
      const list = recorded(route);
      return {json: {...list, people: list.people.map(person => staged(person, queued))}};
    }
    const person = route.match(/^\/api\/projects\/[^/]+\/people\/([^/]+)$/);
    if (method === "GET" && person) return {json: staged(recorded(route), queued)};
    if (method === "POST" && route === `/api/projects/${P}/linkedin-requests/send`) {
      for (const id of body.people) queued.add(id);
      requests.week.queued = (requests.week.queued || 0) + body.people.length;
      return {json: {status: "approved", batch_id: "batch-people", queued: body.people.length, repeated: false, first_send: requests.next_open, last_send: requests.next_open}};
    }
    // Approving a review: the decision leaves Decisions, and the run says its pull request is on
    // its way (GitHub delivery, as the staged repository allows).
    const apply = route.match(/^\/api\/decisions\/([^/]+)\/apply$/);
    if (method === "POST" && apply) {
      const decision = recorded(`/api/projects/${P}/decisions`).find(item => item.id === apply[1]);
      approved.add(apply[1]);
      const run = recorded(`/api/workflows/runs/${decision.run_id}`);
      return {json: {...run, status: "running", review_decision: "approved", allowed_actions: []}};
    }
    // The post batch's review: one version, nothing revised yet.
    if (method === "GET" && route === "/api/workflows/runs/bf30dd71-3e2b-4dca-9566-d79dd4846d78/review") {
      const plan = recorded("/api/workflows/runs/0163bd17-fe74-414a-9ddd-245323983cb0/review");
      const id = "bf30dd71-3e2b-4dca-9566-d79dd4846d78";
      return {json: {...plan, run_id: id, root_run_id: id, current_run_id: id, version: 1, status: "needs_input", versions: [], feedback: null, change_summary: null,
        artifact: {run_id: id, path: "social/posts/2026-11-03-bf30dd713e2b4dca9566d79dd4846d78.md", revision: "83f3907ae91d655231a27be9c397eec0823a6057", sha256: null, assessment: false},
        can_request_changes: false, review_url: `/document/${id}?project=${P}&return=decisions`}};
    }
    if (method === "GET" && route === `/api/projects/${P}/decisions`) {
      return {json: recorded(route).filter(item => !approved.has(item.id))};
    }
    return undefined;
  };
  return serveReplay({world, answer});
}

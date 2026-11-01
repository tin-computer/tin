// Film 1: People and connection requests. Everyone who engaged with a founder's LinkedIn posts,
// ranked by their ICP; ticking two people queues requests, which Tin sends one at a time from
// their LinkedIn; one accepts. The founder and everyone here are made up.
import {clock, linkedin, person, sending} from "../world.js";

export const title = "People and connection requests";
// A Wednesday morning in Berlin.
export const now = "2026-10-28T08:20:00Z";
export const timezone = "Europe/Berlin";

const ICP_AGENCIES = "ICP 1: agency founders who bill clients every month";
const ICP_FINANCE = "ICP 2: finance leads at growing studios";
const at = clock(now);

function daniel(accepted = false) {
  const events = [
    {kind: "reacted", at: at("1d", "18:12"), reaction: "INTEREST", post_url: "", yours: true, who: "you"},
    {kind: "viewed", at: at("2d", "08:40"), reaction: null, post_url: "", yours: true, who: "you"},
  ];
  if (accepted) events.unshift({kind: "accepted", at: at("0d", "14:52"), who: "you", yours: true, post_url: ""}, {kind: "invited", at: at("0d", "09:40"), who: "you", yours: true, post_url: ""});
  return person("p-daniel", "Daniel Okafor", "Founder, Fieldline Studio", {
    icp: ICP_AGENCIES, fit: 0.91, at: at("1d", "19:00"), relationship: accepted ? "connected" : "none", events,
    reason: "Runs a 14-person design studio and bills its clients every month.",
    touch: accepted ? {kind: "accepted", at: at("0d", "14:52"), who: "you"} : {kind: "reacted", at: at("1d", "18:12"), who: "you"},
    notes: [{id: "n-daniel", body: "Met at the studio founders' dinner in Berlin.", by: "a teammate", who: "Mira", mine: false, at: at("3d", "11:00"), edited: false}],
  });
}

function people() {
  const scored = at("1d", "19:00");
  const reacted = (ago, time, who = "you") => ({kind: "reacted", at: at(ago, time), who});
  const viewed = (ago, time) => ({kind: "viewed", at: at(ago, time), who: "you"});
  return [
    daniel(),
    person("p-grace", "Grace Liu", "Founder, Pinewood Collective", {icp: ICP_AGENCIES, fit: 0.88, at: scored, relationship: "connected", customer: true, reason: "Founder of a 20-person agency that invoices monthly retainers.", touch: reacted("2d", "12:30"),
      notes: [{id: "n-grace", body: "Customer since August.", by: "you", mine: true, at: at("20d", "10:00"), edited: false}]}),
    person("p-hannah", "Hannah Kim", "Co-founder, Brightcut Films", {icp: ICP_AGENCIES, fit: 0.86, at: scored, reason: "Co-founder of a video studio; posted about chasing late invoices.", touch: reacted("1d", "21:04")}),
    person("p-maya", "Maya Lindqvist", "Managing partner, Lindqvist & Co", {icp: ICP_AGENCIES, fit: 0.84, at: scored, relationship: "connected", reason: "Runs a branding agency with monthly client retainers.", touch: viewed("1d", "07:55")}),
    person("p-sofia", "Sofia Marchetti", "Head of Finance, Patchbay", {icp: ICP_FINANCE, fit: 0.79, at: scored, reason: "Owns billing at a 40-person creative agency.", touch: reacted("2d", "16:20", "Mira")}),
    person("p-tomas", "Tomás Rivera", "Founder, Rivera Digital", {icp: ICP_AGENCIES, fit: 0.74, at: scored, relationship: "pending", reason: "Runs a small web agency that bills by the month.", touch: {kind: "invited", at: at("4d", "10:15"), who: "you"}}),
    person("p-priya", "Priya Nair", "Operations lead, Kiln", {icp: ICP_FINANCE, fit: 0.71, at: scored, reason: "Runs operations and invoicing at a product studio.", touch: viewed("2d", "13:10")}),
    person("p-omar", "Omar Haddad", "CFO, Northlake Partners", {icp: ICP_FINANCE, fit: 0.69, at: scored, reason: "Finance lead at a consultancy of about 60 people.", touch: reacted("3d", "09:02", "Mira")}),
    person("p-elena", "Elena Petrova", "Studio manager, Atelier Nord", {icp: ICP_FINANCE, fit: 0.64, at: scored, reason: "Manages a design studio's clients and billing.", touch: reacted("3d", "17:45")}),
    person("p-lea", "Lea Fischer", "Freelance brand designer", {icp: ICP_AGENCIES, fit: 0.52, at: scored, reason: "Works alone and bills a handful of clients.", touch: reacted("4d", "11:30")}),
    person("p-jonas", "Jonas Weber", "Engineer, Quanta Labs", {icp: "none", fit: 0.12, at: scored, reason: "An engineer at a product company, not an agency.", touch: reacted("5d", "20:10")}),
    person("p-ben", "Ben Carter", "Building something new", {at: scored, reason: "Tin couldn't score this person this time; the next run tries again.", touch: viewed("5d", "08:20")}),
  ];
}

export function world() {
  // The tests' fixture adds a person's fields and pending invitation date this way too.
  const list = people();
  list.find(item => item.id === "p-tomas").invited_at = at("4d", "10:15");
  return {
    member: "Avery",
    start: "/people",
    ready: "[data-people-pick]",
    serve: {
      projectName: "Plainfield", engagers: true, people: list,
      integrations: [linkedin("Avery Lane", {connected: at("21d", "10:00"), verified: at("0d", "07:30")})],
      requests: sending({week: {sent: 9, accepted: 3, queued: 5, pending: 6}, next: at("0d", "09:40")}),
    },
  };
}

const rows = n => `.people-table tbody tr[data-person]:nth-child(-n+${n})`;
const row = id => `.people-table tr[data-person="${id}"]`;

export async function play(f) {
  // Close-ups stay in the dashboard's main column, clear of the sidebar.
  f.inside = "#main";
  // Everyone who engaged, ranked by ICP fit: the table, and in tall formats its compact layout.
  const grid = [".people-view h1", ".people-sections", ".people-table thead", rows(f.wide ? 7 : 9)];
  await f.camera(grid, {cut: true});
  f.caption("Everyone who engaged, ranked by your ICP.");
  await f.hold(2.4);
  await f.mark("grid");

  // The reason behind a score.
  f.caption("Every score comes with its reason.");
  await f.move(`${row("p-daniel")} td.is-fit`, {to: [0.6, 0.5]});
  await f.hold(0.3);
  await f.camera([`${row("p-daniel")} td.is-name`, `${row("p-daniel")} td.is-fit`, ".people-hover"], {pad: 40, text: true});
  await f.hold(1.8);
  await f.mark("reason");

  // Pick who to ask: tick two, then the bar's Send.
  f.caption("Pick who to ask.");
  await f.camera([`${rows(4)} td.is-pick`, `${rows(4)} td.is-name`, `${rows(4)} td.is-fit`], {wait: false, pad: 36});
  await f.click(`[data-people-pick="p-daniel"]`);
  await f.hold(0.3);
  await f.click(`[data-people-pick="p-hannah"]`);
  await f.hold(0.3);
  await f.camera([".people-picked", `${rows(4)} td.is-linkedin`], {pad: 32, text: true, wait: false});
  await f.move(".people-picked-send");
  await f.hold(1);
  await f.mark("poster");
  await f.click(".people-picked-send");
  await f.ready(".toast.is-visible");
  await f.camera([".people-table thead", `${rows(4)} td.is-name`, `${rows(4)} td.is-linkedin`, ".toast"], {pad: 28, text: true});
  await f.hold(1.6);
  await f.mark("queued");

  // Sent one at a time, from your LinkedIn.
  await f.cut(async () => {
    f.hide();
    await f.open("/integrations", {ready: ".linkedin-card"});
  });
  f.caption("Sent one at a time, from your LinkedIn.");
  await f.camera(".linkedin-card", {cut: true});
  await f.click('.linkedin-card [data-integration-expand="linkedin"]');
  await f.ready(".requests-sending");
  await f.camera(f.wide ? ".linkedin-card" : [".requests-sending", ".requests-sending + .linkedin-part"], {pad: 32, text: !f.wide});
  await f.hold(2.2);
  await f.mark("sending");

  // They accept.
  await f.cut(async () => {
    f.hide();
    const list = f.world.people;
    list[list.findIndex(item => item.id === "p-daniel")] = daniel(true);
    await f.open("/people", {ready: "[data-people-pick]"});
  });
  f.caption("They accept. You start the conversation.");
  await f.camera(grid, {cut: true});
  await f.click(`${row("p-daniel")} td.is-name`);
  await f.ready("#person-panel .person-section:last-of-type");
  await f.hold(0.3);
  await f.scroll("#person-panel .person-panel-body", "#person-panel .person-section:last-of-type", {offset: 80});
  // Wide, Daniel's row (now Connected) beside the panel; tall, the timeline close.
  const accepted = [`${row("p-daniel")} td.is-linkedin`, "#person-panel .person-section:last-of-type"];
  await f.camera(f.wide ? accepted : accepted.slice(1), {pad: 40, text: true, inside: null});
  await f.hold(2.4);
  await f.mark("accepted");
}

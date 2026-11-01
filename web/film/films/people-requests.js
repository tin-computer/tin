// Film 1: People and connection requests. Everyone who engaged with a founder's LinkedIn posts,
// ranked by their ICP. Tin has already asked the people who fit, one at a time from the founder's
// LinkedIn; the founder ticks two more just under the line; one of Tin's asks is accepted. The
// founder and everyone here are made up.
import {clock, linkedin, person, sending} from "../world.js";

export const title = "People and connection requests";
// A Wednesday morning in Berlin.
export const now = "2026-10-28T08:20:00Z";
export const timezone = "Europe/Berlin";
// The tall film's payoff closes in on the person panel alone, past twice the screen's scale.
export const deviceScale = {"4:5": 3};

const ICP_AGENCIES = "ICP 1: agency founders who bill clients every month";
const ICP_FINANCE = "ICP 2: finance leads at growing studios";
const at = clock(now);
const NEXT_SEND = at("0d", "09:40");

// Queued by the last read, as Tin does for everyone who fits while sending is on.
const asked = record => Object.assign(record, {requestable: false, request: "queued", queued: {queued_at: at("1d", "19:00"), sending: false, at: NEXT_SEND}});

function daniel(accepted = false) {
  const events = [
    {kind: "reacted", at: at("1d", "18:12"), reaction: "INTEREST", post_url: "", yours: true, who: "you"},
    {kind: "viewed", at: at("2d", "08:40"), reaction: null, post_url: "", yours: true, who: "you"},
  ];
  if (accepted) events.unshift({kind: "accepted", at: at("0d", "14:52"), who: "you", yours: true, post_url: ""}, {kind: "invited", at: NEXT_SEND, who: "you", yours: true, post_url: ""});
  const record = person("p-daniel", "Daniel Okafor", "Founder, Fieldline Studio", {
    icp: ICP_AGENCIES, fit: 0.91, at: at("1d", "19:00"), relationship: accepted ? "connected" : "none", events,
    reason: "Runs a 14-person design studio and bills its clients every month.",
    touch: accepted ? {kind: "accepted", at: at("0d", "14:52"), who: "you"} : {kind: "reacted", at: at("1d", "18:12"), who: "you"},
  });
  return accepted ? record : asked(record);
}

function people() {
  const scored = at("1d", "19:00");
  const reacted = (ago, time, who = "you") => ({kind: "reacted", at: at(ago, time), who});
  const viewed = (ago, time) => ({kind: "viewed", at: at(ago, time), who: "you"});
  return [
    daniel(),
    person("p-grace", "Grace Liu", "Founder, Pinewood Collective", {icp: ICP_AGENCIES, fit: 0.88, at: scored, relationship: "connected", customer: true, reason: "Founder of a 20-person agency that invoices monthly retainers.", touch: reacted("2d", "12:30"),
      notes: [{id: "n-grace", body: "Customer since August.", by: "you", mine: true, at: at("20d", "10:00"), edited: false}]}),
    asked(person("p-hannah", "Hannah Kim", "Co-founder, Brightcut Films", {icp: ICP_AGENCIES, fit: 0.86, at: scored, reason: "Co-founder of a video studio; posted about chasing late invoices.", touch: reacted("1d", "21:04")})),
    person("p-maya", "Maya Lindqvist", "Managing partner, Lindqvist & Co", {icp: ICP_AGENCIES, fit: 0.84, at: scored, relationship: "connected", reason: "Runs a branding agency with monthly client retainers.", touch: viewed("1d", "07:55")}),
    asked(person("p-sofia", "Sofia Marchetti", "Head of Finance, Patchbay", {icp: ICP_FINANCE, fit: 0.79, at: scored, reason: "Owns billing at a 40-person creative agency.", touch: reacted("2d", "16:20", "Mira")})),
    person("p-tomas", "Tomás Rivera", "Founder, Rivera Digital", {icp: ICP_AGENCIES, fit: 0.74, at: scored, relationship: "pending", reason: "Runs a small web agency that bills by the month.", touch: {kind: "invited", at: at("4d", "10:15"), who: "you"}}),
    person("p-chloe", "Chloé Martin", "Founder, Studio Lumen", {icp: ICP_AGENCIES, fit: 0.58, at: scored, reason: "Runs a six-person studio with a few monthly retainers.", touch: reacted("2d", "09:15")}),
    person("p-lea", "Lea Fischer", "Freelance brand designer", {icp: ICP_AGENCIES, fit: 0.52, at: scored, reason: "Works alone and bills a handful of clients.", touch: reacted("4d", "11:30"),
      notes: [{id: "n-lea", body: "Met at the studio founders' dinner in Berlin.", by: "a teammate", who: "Mira", mine: false, at: at("3d", "11:00"), edited: false}]}),
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
      requests: sending({week: {sent: 9, accepted: 3, queued: 3, pending: 6}, next: NEXT_SEND}),
    },
  };
}

const rows = n => `.people-table tbody tr[data-person]:nth-child(-n+${n})`;
const row = id => `.people-table tr[data-person="${id}"]`;

export async function play(f) {
  // Close-ups stay in the dashboard's main column, clear of the sidebar.
  f.inside = "#main";

  // Everyone who engaged, ranked by ICP fit: the table, and in tall formats its compact layout.
  const grid = [".people-view h1", ".people-sections", ".people-table thead", rows(f.wide ? 8 : 10)];
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

  // Tin has asked the people who fit: back to the whole grid, where they read "Queued". The
  // cursor rests on the empty end of the tabs row, so the reason card closes.
  f.caption("Tin asks the ones who fit.");
  await f.camera(grid, {wait: false});
  await f.move(".people-sections", {to: [0.9, 0.5]});
  await f.hold(2.2);
  await f.mark("asked");

  // Two more just under the line, by hand: the bar takes the header row's place.
  f.caption("Pick anyone else to ask.");
  const picks = [".people-table thead", `${rows(9)} td.is-pick`, `${rows(9)} td.is-name`, `${rows(9)} td.is-fit`, `${rows(9)} td.is-linkedin`];
  await f.camera(picks, {pad: 32, wait: false});
  await f.click('[data-people-pick="p-chloe"]');
  await f.hold(0.3);
  await f.click('[data-people-pick="p-lea"]');
  await f.hold(0.4);
  await f.move(".people-picked-send");
  await f.hold(0.9);
  await f.mark("poster");
  await f.click(".people-picked-send");
  await f.ready(".toast.is-visible");
  await f.camera([".people-table thead", `${rows(9)} td.is-name`, `${rows(9)} td.is-linkedin`, ".toast"], {pad: 28, text: true});
  await f.hold(1.8);
  await f.mark("queued");

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
  // Wide, Daniel's whole row (now Connected) beside the panel; tall, the panel's timeline alone.
  if (f.wide) await f.camera([row("p-daniel"), "#person-panel .person-section:last-of-type"], {pad: 28, inside: null});
  else await f.camera("#person-panel .person-section:last-of-type", {pad: 24, inside: null});
  await f.hold(2.4);
  await f.mark("accepted");
}

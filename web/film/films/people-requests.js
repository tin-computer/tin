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

const row = id => `.people-table tr[data-person="${id}"]`;

// Directed after Stageshow's motion rules: one framing for the whole story, a good still on its
// own; the cursor moves only with a cause, aims off the words it clicks and rests on empty surface
// between beats; at most one push-in, slow, where the payoff would be too small to read.
export async function play(f) {
  // The content column, as wide as the frame allows: in the tall format that is the compact
  // screen whole; wide, the main column from the top, clear of the sidebar.
  const main = await f.rect("#main");
  await f.camera({x: main.x, y: 0, width: main.width, height: 1}, {cut: true, pad: 0});
  f.caption("Everyone who engaged, ranked by your ICP.");
  await f.hold(2.2);
  await f.mark("grid");

  // The reason behind a score opens under the cursor.
  f.caption("Every score comes with its reason.");
  await f.move(`${row("p-daniel")} td.is-fit`, {to: [0.7, 0.6]});
  await f.hold(2.2);
  await f.mark("reason");

  // Tin has asked the people who fit: they read Queued. The cursor rests on the tabs row's empty
  // end, so the reason card closes.
  f.caption("Tin asks the ones who fit.");
  await f.move(".people-sections", {to: [0.88, 0.5]});
  await f.hold(2);
  await f.mark("asked");

  // Two more by hand, just under the line: the bar takes the header row's place; Send.
  f.caption("Pick anyone else to ask.");
  await f.click('[data-people-pick="p-chloe"]');
  await f.hold(0.35);
  await f.click('[data-people-pick="p-lea"]');
  await f.hold(0.5);
  await f.move(".people-picked-send", {to: [0.88, 0.62]});
  await f.hold(1);
  await f.mark("poster");
  await f.click(".people-picked-send", {to: [0.88, 0.62]});
  await f.ready(".toast.is-visible");
  await f.hold(1.6);
  await f.mark("queued");

  // They accept. Behind the cut time passes: the toast closes and Daniel has accepted.
  await f.cut(async () => {
    f.hide();
    await f.hold(3.6);
    const list = f.world.people;
    list[list.findIndex(item => item.id === "p-daniel")] = daniel(true);
    await f.open("/people", {ready: "[data-people-pick]"});
  }, {caption: "They accept. You start the conversation."});
  await f.click(`${row("p-daniel")} td.is-name`, {to: [0.86, 0.55]});
  await f.ready("#person-panel .person-section:last-of-type");
  await f.hold(0.6);
  // The film's one camera move, slow and with the cursor still. Wide, the panel's timeline sits
  // below the content column, so the camera pulls out to the whole app: the list with Daniel
  // Connected, and his panel. Tall, the panel is too small to read, so it closes in on it.
  if (f.wide) {
    await f.camera({x: 0, y: 0, width: f.screen.width, height: f.screen.height}, {pad: 0, inside: null, duration: 1.8, curve: "house"});
  } else {
    const panel = await f.rect("#person-panel");
    const timeline = await f.rect("#person-panel .person-section:last-of-type");
    await f.camera({x: panel.x, y: timeline.y - 24, width: panel.width, height: timeline.height + 48}, {pad: 0, inside: null, duration: 1.8, curve: "house"});
  }
  await f.hold(2.2);
  await f.mark("accepted");
}

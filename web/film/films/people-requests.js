// Film 1: People and connection requests. Everyone who engaged with a founder's LinkedIn posts,
// ranked by their ICP. Tin has already asked the people who fit, one at a time from the founder's
// LinkedIn; the founder ticks two more just under the line; one of Tin's asks is accepted. The
// founder and everyone here are made up.
import {clock, linkedin, person, sending} from "../world.js";

export const title = "People and connection requests";
// A Wednesday morning in Berlin.
export const now = "2026-10-28T08:20:00Z";
export const timezone = "Europe/Berlin";
// Tall: a 600-wide screen (the compact grid, text at about 23px in the frame), taller than the
// picture so the top menu stays out of frame. Wide: a screen exactly as tall as the main column's
// frame, so toasts at its foot are in the picture and the list ends whole.
export const screens = {"4:5": {width: 600, height: 760}, "16:9": {width: 1440, height: 572}};

const ICP_AGENCIES = "ICP 1: agency founders";
const ICP_FINANCE = "ICP 2: studio finance leads";
const at = clock(now);
const NEXT_SEND = at("0d", "09:40");
const scored = at("1d", "19:00");

// Queued by the last read, as Tin does for everyone who fits while sending is on.
const asked = record => Object.assign(record, {requestable: false, request: "queued", queued: {queued_at: scored, sending: false, at: NEXT_SEND}});

// The founder asks Chloé by hand; behind the cut she accepts.
function chloe(accepted = false) {
  const events = [{kind: "reacted", at: at("2d", "09:15"), reaction: "LIKE", post_url: "", yours: true, who: "you"}];
  if (accepted) events.unshift({kind: "accepted", at: at("0d", "14:10"), who: "you", yours: true, post_url: ""}, {kind: "invited", at: NEXT_SEND, who: "you", yours: true, post_url: ""});
  return person("p-chloe", "Chloé Martin", "Founder, Studio Lumen", {
    icp: ICP_AGENCIES, fit: 0.58, at: scored, relationship: accepted ? "connected" : "none", events,
    reason: "Runs a six-person studio with a few monthly retainers.",
    touch: accepted ? {kind: "accepted", at: at("0d", "14:10"), who: "you"} : {kind: "reacted", at: at("2d", "09:15"), who: "you"},
  });
}

function people() {
  const reacted = (ago, time, who = "you") => ({kind: "reacted", at: at(ago, time), who});
  const viewed = (ago, time) => ({kind: "viewed", at: at(ago, time), who: "you"});
  const note = (id, body, who = "Mira") => ({id, body, by: "a teammate", who, mine: false, at: at("3d", "11:00"), edited: false});
  return [
    asked(person("p-daniel", "Daniel Okafor", "Founder, Fieldline Studio", {icp: ICP_AGENCIES, fit: 0.91, at: scored, reason: "Runs a 14-person design studio and bills its clients every month.", touch: reacted("1d", "18:12")})),
    person("p-grace", "Grace Liu", "Founder, Pinewood Collective", {icp: ICP_AGENCIES, fit: 0.88, at: scored, relationship: "connected", customer: true, reason: "Founder of a 20-person agency that invoices monthly retainers.", touch: reacted("2d", "12:30"),
      notes: [{id: "n-grace", body: "Customer since August.", by: "you", mine: true, at: at("20d", "10:00"), edited: false}]}),
    asked(person("p-hannah", "Hannah Kim", "Co-founder, Brightcut Films", {icp: ICP_AGENCIES, fit: 0.86, at: scored, reason: "Co-founder of a video studio; posted about chasing late invoices.", touch: reacted("1d", "21:04"),
      notes: [note("n-hannah", "Asked about pricing under the post.")]})),
    person("p-maya", "Maya Lindqvist", "Managing partner, Lindqvist & Co", {icp: ICP_AGENCIES, fit: 0.84, at: scored, relationship: "connected", reason: "Runs a branding agency with monthly client retainers.", touch: viewed("1d", "07:55")}),
    asked(person("p-sofia", "Sofia Marchetti", "Head of Finance, Patchbay", {icp: ICP_FINANCE, fit: 0.79, at: scored, reason: "Owns billing at a 40-person creative agency.", touch: reacted("2d", "16:20", "Mira"),
      notes: [note("n-sofia", "Intro from Maya.")]})),
    person("p-tomas", "Tomás Rivera", "Founder, Rivera Digital", {icp: ICP_AGENCIES, fit: 0.74, at: scored, relationship: "pending", reason: "Runs a small web agency that bills by the month.", touch: {kind: "invited", at: at("4d", "10:15"), who: "you"}}),
    chloe(),
    person("p-lea", "Lea Fischer", "Freelance brand designer", {icp: ICP_AGENCIES, fit: 0.52, at: scored, reason: "Works alone and bills a handful of clients.", touch: reacted("4d", "11:30"),
      notes: [note("n-lea", "Met at the studio founders' dinner in Berlin.")]}),
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
const tab = key => `[data-people-segment="${key}"]`;

// Directed after Stageshow's motion rules (references/motion.md): one framing, a good still on
// its own, held the whole film; every beat is a real action with a cause; the cursor aims inside
// its target but off its words, and rests on empty surface near its next move; a cut lets time
// pass; the film ends on a fade.
export async function play(f) {
  // The content column, top to bottom: in the tall format below the top menu, wide clear of the
  // sidebar.
  const main = await f.rect("#main");
  const header = await f.rect(".people-view h1");
  await f.camera({x: main.x, y: header.y - 22, width: main.width, height: 1}, {cut: true, pad: 0, valign: "top"});
  f.caption("Everyone who engaged, ranked by your ICP.");
  await f.hold(2.2);
  await f.mark("grid");

  // The reason behind a score opens under the cursor, which sits above the card, off its words.
  f.caption("Every score comes with its reason.");
  await f.move(`${row("p-daniel")} td.is-fit`, {to: [0.2, 0.25]});
  await f.hold(2.2);
  await f.mark("reason");

  // Tin asks the ones who fit: their tab, and their LinkedIn column reads Queued.
  f.caption("Tin asks the ones who fit.");
  await f.click(tab("fit"), {to: [0.9, 0.8]});
  await f.hold(0.3);
  for (const id of ["p-daniel", "p-hannah", "p-sofia"]) await f.highlight(`${row(id)} td.is-linkedin`, {hold: 1});
  await f.hold(2.2);
  await f.mark("asked");

  // Anyone else, by hand: the people not connected yet; tick two; the bar takes the header row.
  f.caption("Pick anyone else to ask.");
  await f.click(tab("new"), {to: [0.92, 0.8]});
  await f.hold(0.4);
  await f.click('[data-people-pick="p-chloe"]', {to: [0.85, 0.85]});
  await f.hold(0.3);
  await f.click('[data-people-pick="p-lea"]', {to: [0.85, 0.85]});
  await f.hold(0.5);
  await f.move(".people-picked-send", {to: [0.9, 0.86]});
  await f.hold(0.9);
  await f.mark("poster");
  await f.click(".people-picked-send", {to: [0.9, 0.86]});
  await f.ready(".toast.is-visible");
  await f.hold(0.2);
  for (const id of ["p-chloe", "p-lea"]) await f.highlight(`${row(id)} td.is-linkedin`, {hold: 1});
  // The cursor clears to empty ground under the list, near the next scene's action.
  await f.move(".people-pager", {to: [0.3, 2.6]});
  await f.hold(1.2);
  await f.mark("queued");

  // They accept. Behind the cut time passes: the toast closes, Chloé accepts, and the founder
  // has her open (on a phone-width screen the panel fills it).
  await f.cut(async () => {
    await f.hold(3.6);
    const list = f.world.people;
    list[list.findIndex(item => item.id === "p-chloe")] = chloe(true);
    await f.open("/people", {ready: ".people-table tbody tr"});
    await f.page.click(tab("all"));
    await f.ready(row("p-chloe"));
    if (!f.wide) {
      await f.page.click(`${row("p-chloe")} td.is-name`);
      await f.ready("#person-panel .person-section:last-of-type");
      const panel = await f.rect("#person-panel");
      await f.camera({x: panel.x, y: 0, width: panel.width, height: 1}, {cut: true, pad: 0, valign: "top"});
    }
  }, {caption: "They accept. You start the conversation."});
  if (f.wide) {
    await f.highlight([`${row("p-chloe")} td.is-linkedin`, `${row("p-chloe")} td.is-touch`], {hold: 1.6});
    await f.move(`${row("p-chloe")} td.is-name`, {to: [0.92, 0.5]});
  } else {
    await f.highlight("#person-panel .person-section:last-of-type div.person-event:first-of-type", {hold: 1.6});
    await f.move("#person-panel .person-panel-linkedin", {to: [1.12, 0.55]});
  }
  await f.hold(2.2);
  await f.mark("accepted");
  await f.end();
}

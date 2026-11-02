// Film 1: People and connection requests. Everyone who engaged with a founder's LinkedIn posts,
// ranked by their ICP. Tin has already asked the people who fit, one at a time from the founder's
// LinkedIn; the founder ticks two more just under the line; one of Tin's asks is accepted. The
// founder and everyone here are made up.
import {clock, linkedin, person, sending} from "../world.js";

export const title = "People and connection requests";
// A Wednesday morning in Berlin.
export const now = "2026-10-28T08:20:00Z";
export const timezone = "Europe/Berlin";
// Each screen ends where the picture does, so a toast at its foot sits clear of the edge. Tall: a
// 600-wide screen (the compact grid, text at about 23px in the frame), its top menu above the
// picture. Wide: the main column's frame.
export const screens = {"4:5": {width: 600, height: 739}, "16:9": {width: 1440, height: 572}};

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
    related: accepted ? {by: "requests", at: at("0d", "14:10")} : null,
    reason: "Runs a six-person studio with a few monthly retainers.",
    touch: accepted ? {kind: "accepted", at: at("0d", "14:10"), who: "you"} : {kind: "reacted", at: at("2d", "09:15"), who: "you"},
  });
}

// The people who fit, before Tin's read has asked them.
const FITS = ["p-daniel", "p-hannah", "p-sofia"];

function people() {
  const reacted = (ago, time, who = "you") => ({kind: "reacted", at: at(ago, time), who});
  const viewed = (ago, time) => ({kind: "viewed", at: at(ago, time), who: "you"});
  const note = (id, body, who = "Mira") => ({id, body, by: "a teammate", who, mine: false, at: at("3d", "11:00"), edited: false});
  return [
    person("p-daniel", "Daniel Okafor", "Founder, Fieldline Studio", {icp: ICP_AGENCIES, fit: 0.91, at: scored, reason: "Runs a 14-person design studio and bills its clients every month.", touch: reacted("1d", "18:12")}),
    person("p-grace", "Grace Liu", "Founder, Pinewood Collective", {icp: ICP_AGENCIES, fit: 0.88, at: scored, relationship: "connected", customer: true, reason: "Founder of a 20-person agency that invoices monthly retainers.", touch: reacted("2d", "12:30"),
      notes: [{id: "n-grace", body: "Customer since August.", by: "you", mine: true, at: at("20d", "10:00"), edited: false}]}),
    person("p-hannah", "Hannah Kim", "Co-founder, Brightcut Films", {icp: ICP_AGENCIES, fit: 0.86, at: scored, reason: "Co-founder of a video studio; posted about chasing late invoices.", touch: reacted("1d", "21:04"),
      notes: [note("n-hannah", "Asked about pricing under the post.")]}),
    person("p-sofia", "Sofia Marchetti", "Head of Finance, Patchbay", {icp: ICP_FINANCE, fit: 0.79, at: scored, reason: "Owns billing at a 40-person creative agency.", touch: reacted("2d", "16:20", "Mira"),
      notes: [note("n-sofia", "Intro from Grace.")]}),
    chloe(),
    person("p-lea", "Lea Fischer", "Freelance brand designer", {icp: ICP_AGENCIES, fit: 0.52, at: scored, reason: "Works alone and bills a handful of clients.", touch: reacted("4d", "11:30"),
      notes: [note("n-lea", "Met at the studio founders' dinner in Berlin.")]}),
    person("p-nina", "Nina Weiss", "Studio manager, Kiln", {icp: ICP_FINANCE, fit: 0.47, at: scored, reason: "Runs operations at a product studio; billing sits with the founders.", touch: viewed("3d", "13:10")}),
    // Asked by hand last week, from under the line.
    person("p-tomas", "Tomás Rivera", "Founder, Rivera Digital", {icp: ICP_AGENCIES, fit: 0.44, at: scored, relationship: "pending", reason: "Runs a two-person web shop with a few clients.", touch: {kind: "invited", at: at("4d", "10:15"), who: "you"}}),
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
// A row of the person panel, edge to edge: its own top rule and the next row's, so a mark sits on
// the panel's lines instead of doubling them.
const panelRow = async (f, selector) => {
  const box = await f.rect(selector);
  return {x: box.x - 12, y: box.y, width: box.width + 24, height: box.height + 1};
};

// Directed after Stageshow's motion rules (references/motion.md) and three fresh-eye reviews: one
// framing per format, held the whole film; every beat a real change with a cause; the cursor moves
// only to act, aims off the words it clicks and stays where it pressed; marks on what changed; a
// cut changes only what time changed; the film fades in and out of the band's ground.
export async function play(f) {
  // The content column, top to bottom: in the tall format below the top menu, wide clear of the
  // sidebar. The list runs off the foot of the frame.
  const main = await f.rect("#main");
  const header = await f.rect(".people-view h1");
  await f.camera({x: main.x, y: header.y - 22, width: main.width, height: 1}, {cut: true, pad: 0, valign: "top"});
  f.caption("Everyone who engaged, ranked by your ICP.");
  await f.begin();
  await f.hold(2);
  await f.mark("grid");

  // The reason behind a score opens under the cursor, which sits above the card, off its words.
  f.caption("Every score comes with its reason.");
  await f.move(`${row("p-daniel")} td.is-fit`, {to: [0.2, 0.25]});
  await f.hold(2);
  await f.mark("reason");

  // Tin's read asks the ones who fit: their boxes go and their LinkedIn column reads Queued. The
  // cursor steps off the score onto the empty end of the title row first, so the card closes.
  f.caption("Tin asks the ones who fit.");
  const title = await f.rect(".people-view h1", true);
  await f.move({x: title.x + title.width + 48, y: title.y + title.height / 2, width: 1, height: 1});
  for (const id of FITS) asked(f.world.people.find(item => item.id === id));
  await f.open("/people", {ready: `${row("p-daniel")} td.is-linkedin`});
  for (const [index, id] of FITS.entries()) await f.highlight(`${row(id)} td.is-linkedin`, {hold: 1.2, delay: index * 0.07});
  await f.hold(2.2);
  await f.mark("asked");

  // Anyone else, by hand: two of the people not connected yet. The bar takes the header row.
  f.caption("Pick anyone else to ask.");
  await f.click('[data-people-pick="p-chloe"]', {to: [0.85, 0.85]});
  await f.hold(0.3);
  await f.click('[data-people-pick="p-lea"]', {to: [0.85, 0.85]});
  await f.hold(0.5);
  await f.move(".people-picked-send", {to: [0.94, 0.55]});
  await f.hold(0.9);
  await f.mark("poster");
  // What Send does, as its toast says it. The cursor stays where it pressed.
  await f.click(".people-picked-send", {to: [0.94, 0.55]});
  f.caption("Tin sends them one at a time, from your LinkedIn.");
  await f.ready(".toast.is-visible");
  await f.hold(0.2);
  for (const [index, id] of ["p-chloe", "p-lea"].entries()) await f.highlight(`${row(id)} td.is-linkedin`, {hold: 1.4, delay: index * 0.07});
  await f.hold(2.4);
  await f.mark("queued");

  // Open Chloé while her request waits, so the cut changes only what time changed. Tall, her
  // panel covers the screen and the frame takes its head.
  await f.click(`${row("p-chloe")} td.is-name`, {to: [0.97, 0.55]});
  await f.ready("#person-panel .person-section:last-of-type");
  if (!f.wide) await f.camera({x: 0, y: 0, width: f.screen.width, height: 1}, {cut: true, pad: 0, valign: "top"});
  await f.hold(1);

  // They accept: behind the cut time passes, the toast closes and Chloé's request is accepted.
  await f.cut(async () => {
    await f.hold(3.6);
    const list = f.world.people;
    list[list.findIndex(item => item.id === "p-chloe")] = chloe(true);
    // The panel reads her again, as a reload would.
    await f.page.keyboard.press("Escape");
    await f.open("/people", {ready: `${row("p-chloe")} td.is-name`});
    await f.page.click(`${row("p-chloe")} td.is-name`, {position: {x: 4, y: 4}});
    await f.ready("#person-panel .person-section:last-of-type");
  }, {caption: "They accept. You start the conversation."});
  await f.highlight(await panelRow(f, "#person-panel .person-section:first-of-type > div.person-field:nth-of-type(3)"), {hold: 3.6, pad: 0, over: true});
  const accepted = "#person-panel .person-section:last-of-type div.person-event:first-of-type";
  if (await f.shows(accepted)) await f.highlight(await panelRow(f, accepted), {hold: 3.5, delay: 0.1, pad: 0, over: true});
  if (f.wide) await f.highlight(`${row("p-chloe")} td.is-linkedin`, {hold: 3.5, delay: 0.1});
  await f.hold(1.2);
  // The next move is yours.
  await f.move("#person-panel .person-panel-linkedin", {to: [0.6, 0.9]});
  await f.hold(2.2);
  await f.mark("accepted");
  await f.end();
}

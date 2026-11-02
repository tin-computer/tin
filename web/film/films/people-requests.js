// Film 1: People and connection requests. Everyone who engaged with a founder's LinkedIn posts,
// ranked by their ICP. Tin has already asked the people who fit, one at a time from the founder's
// LinkedIn; the founder ticks two more just under the line; one of Tin's asks is accepted. The
// founder and everyone here are made up.
import {clock, linkedin, person, sending} from "../world.js";

export const title = "People and connection requests";
// A Wednesday morning in Berlin.
export const now = "2026-10-28T08:20:00Z";
export const timezone = "Europe/Berlin";
// After Stageshow's framing rules: hard edges, one gutter on every edge of the window that shows,
// and no word cut. Tall: a 600-wide screen (the compact grid, text at about 23px in the frame),
// sides to the picture's edges, its top menu above the frame. Wide: a window on the backdrop,
// framed from the main column out past its top-right corner, the same gutter on the top, the
// right and the foot. Both end just under the last row's words: the toast, 24px up, then covers
// whole rows and lets none half show beneath it.
export const screens = {"4:5": {width: 600, height: 703, margin: {bottom: 37}}, "16:9": {width: 1440, height: 486, margin: 56}};
// Pixels per CSS pixel: the wide film pushes in about 1.7x past a frame already 1.5x the screen.
export const deviceScale = {"4:5": 2, "16:9": 3};

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
const FITS = ["p-daniel", "p-hannah"];

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
const LINKEDIN_FIELD = "#person-panel .person-section:first-of-type > div.person-field:nth-of-type(3)";
const ACCEPTED = "#person-panel .person-section:last-of-type div.person-event:first-of-type";
// A row of the person panel, edge to edge: its own top rule and the next row's, so a mark sits on
// the panel's lines instead of doubling them.
const panelRow = async (f, selector) => {
  const box = await f.rect(selector);
  return {x: box.x - 12, y: box.y, width: box.width + 24, height: box.height + 1};
};

// Directed after Stageshow's motion rules (references/motion.md) and three fresh-eye reviews: one
// framing per format, held the whole film; every beat a real change with a cause; the cursor moves
// only to act, aims off the words it clicks and stays where it pressed; marks on what changed; a
// cut changes only what time changed; the film fades in from and out to its backdrop.
export async function play(f) {
  // The content column: tall, below the top menu, the list running off the foot of the frame;
  // wide, clear of the sidebar and out past the window's edge onto the backdrop.
  const main = await f.rect("#main");
  const header = await f.rect(".people-view h1");
  if (f.wide) await f.camera({x: main.x, y: 0, width: main.width + f.margin.right, height: 1}, {cut: true, pad: 0, valign: "top"});
  // Two px below the top menu, so its border can't show along the top edge.
  else await f.camera({x: main.x, y: header.y - 20, width: main.width, height: 1}, {cut: true, pad: 0, valign: "top"});
  f.caption("Everyone who engaged, ranked by your ICP.");
  await f.begin();
  await f.hold(2);
  await f.mark("grid");

  // The reason behind a score opens under the hand, which stops beside the number without
  // covering it or the status next to it: wide, past it over the empty notes; tall, before it.
  f.caption("Every score comes with its reason.");
  await f.move(`${row("p-daniel")} td.is-fit`, {to: f.wide ? [0.95, 0.12] : [0.25, 0.3]});
  await f.hold(2);
  await f.mark("reason");

  // Tin's read asks the ones who fit: their boxes go and their LinkedIn column reads Queued. The
  // cursor steps off the score first, so the card closes, onto the empty end of the tabs row: off
  // the list, so no row lights up while the eye goes to the change.
  f.caption("Tin asks the ones who fit.");
  const tabs = await f.rect(".people-sections", true);
  await f.move({x: tabs.x + tabs.width + 40, y: tabs.y + tabs.height / 2, width: 1, height: 1});
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
  await f.hold(0.3);
  // What Send does, as its toast will say, while the hand goes to it. It stays where it pressed.
  f.caption("Tin sends them one at a time, from your LinkedIn.");
  // Into the pill's round end, above the middle, so the hand covers none of its words.
  await f.move(".people-picked-send", {to: [0.97, 0.3]});
  await f.hold(0.3);
  await f.mark("poster");
  await f.click(".people-picked-send", {to: [0.97, 0.3]});
  await f.ready(".toast.is-visible");
  await f.hold(0.2);
  for (const [index, id] of ["p-chloe", "p-lea"].entries()) await f.highlight(`${row(id)} td.is-linkedin`, {hold: 1.4, delay: index * 0.07});
  await f.hold(2);
  await f.mark("queued");
  // The toast leaves on its own before her panel opens over it.
  await f.gone(".toast.is-visible");
  await f.hold(0.2);

  // Open Chloé while her request waits, so the cut changes only what time changed. Tall, her
  // panel covers the screen and the frame takes its head.
  await f.click(`${row("p-chloe")} td.is-name`, {to: [0.86, 0.55]});
  await f.ready("#person-panel .person-section:last-of-type");
  if (!f.wide) await f.camera({x: 0, y: 0, width: f.screen.width, height: 1}, {cut: true, pad: 0, valign: "top"});
  await f.hold(0.1);
  // The hand rests in the panel's head, past Open on LinkedIn: the head never moves, whatever
  // the cut changes below it.
  const link = await f.rect("#person-panel .person-panel-linkedin");
  await f.move({x: link.x + link.width + 36, y: link.y + link.height * 0.6, width: 1, height: 1});
  await f.hold(0.7);

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
    await f.hold(0.5);
  }, {caption: "They accept. You start the conversation."});
  await f.highlight(await panelRow(f, LINKEDIN_FIELD), {hold: f.wide ? 5 : 3.6, pad: 0, over: true});
  if (await f.shows(ACCEPTED)) await f.highlight(await panelRow(f, ACCEPTED), {hold: 3.5, delay: 0.1, pad: 0, over: true});
  await f.hold(0.8);
  // Wide, the film's one push-in, the hand still: onto her panel and out past the window's edge
  // onto the backdrop. The frame opens just before the search field, past the end of every ICP
  // and tab label, so it cuts no words, and starts below the window's rounded corner.
  // Past the edge it keeps the opening's gutter, as the picture sees it.
  if (f.wide) {
    const panel = await f.rect("#person-panel");
    const search = await f.rect(".people-view .search-field");
    const left = search.x - 8;
    const gutter = f.margin.right / f.frame.width;
    const width = (panel.x + panel.width - left) / (1 - gutter);
    await f.camera({x: left, y: panel.y + 12, width, height: 1}, {pad: 0, align: "left", valign: "top", floor: panel.y + 12});
  }
  await f.hold(0.5);
  // The next move is yours: a short hop onto the link's arrow, crossing none of its words.
  await f.move("#person-panel .person-panel-linkedin", {to: [0.95, 0.55]});
  await f.hold(2.2);
  await f.mark("accepted");
  await f.end();
}

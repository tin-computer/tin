// tin-film: the film kit as a command, for the feature-film workflow's agent in Tin's studio
// sandbox (and anyone making a film by hand). A film is a JSON file (film.json) that plays a stage
// (a page of photogenic parts beside it, stage/) or a live site; its recording (world/) and takes
// (takes/) sit beside it, never in the project.
//
//   tin-film directions --kind idea|product|both [--count 5] [--seed S]  ways to show it, dealt
//   tin-film sketch <dir> [--format 16:9]   one still of each sketch page in <dir>, side by side
//   tin-film new <dir> [--from people]  a staged film to start from: a worked example, to rebuild
//   tin-film inspect <url> [--dir DIR] [--format 16:9]  a page's look, outline and screenshots
//   tin-film record <film.json>      play it once, saving what it loads from outside
//   tin-film rehearse <film.json>    play it from the recording at low resolution, every frame checked
//   tin-film render <film.json>      the final take from the recording, at full resolution, checked
//   tin-film review <film.json>      a second look at the latest take's stills by a model that didn't make it
//   tin-film review --directions <dir>   that model chooses among the sketches in <dir>
//   tin-film deliver <film.json> <note.md>   the final take beside its note, as Tin keeps films
//   tin-film deliver --directions <dir> <note.md>   the sketches and the recommendation, to pick from
//   tin-film restore <note.md> <dir>   a delivered film's spec, stage and storyboard, to change
//
// Every take prints what went wrong, the checks' findings and the files to look at; a take with
// an error exits 1. See docs/feature-films.md.
import {spawnSync} from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {FORMATS, ffmpeg, filmFromSpec, shoot, takeName} from "./film.js";
import {inspect} from "./inspect.js";
import {deal, describe} from "./directions.js";

const TAKES = {
  record: {record: true, rehearse: true},
  rehearse: {record: false, rehearse: true},
  render: {record: false, rehearse: false},
};
// Tin keeps an MP4 of at most 16 MB beside a note (page_assets.KINDS).
const MAX_FILM_BYTES = 16_000_000;

const load = file => filmFromSpec(JSON.parse(fs.readFileSync(file, "utf8")), file);
const here = file => path.dirname(path.resolve(file));
const formatOf = (film, options) => options.format || film.format || "16:9";
const takeDir = (file, kind, film, format) => path.join(here(file), "takes", kind, film.name, takeName(format, film.theme));

async function take(kind, file, options) {
  const film = load(file);
  const format = formatOf(film, options);
  const {dir, report} = await shoot(film, {format, out: path.join(here(file), "takes", kind), check: true, gif: false, ...TAKES[kind]});
  const {errors, warnings, blocked} = report.issues;
  console.log(`${kind}: ${report.frames} frames, ${report.seconds.toFixed(1)} s → ${dir}`);
  for (const line of errors) console.log(`  error: ${line}`);
  for (const line of warnings) console.log(`  warning: ${line}`);
  if (blocked.length) console.log(`  blocked: ${blocked.join(", ")}`);
  for (const line of report.summary ?? []) console.log(`  check: ${line}`);
  if (report.recording) console.log(`  recording: ${report.recording.entries} answers, ${report.recording.added} new this take`);
  if (film.stage) for (const line of storyboardIssues(file)) console.log(`  storyboard: ${line}`);
  const sheet = ["sheet.jpg", "sheet.png"].map(name => path.join(dir, name)).find(file => fs.existsSync(file));
  if (sheet) console.log(`  look at: ${sheet} (each still is in ${path.join(dir, "marks")}/)`);
  if (errors.length) process.exitCode = 1;
}

const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

// The note Tin keeps: the film first, then its story, what it says when, and what the checks
// still found. Everything here comes from the spec and the take; nothing is written by hand.
export function note({film, report, folder, format}) {
  const name = path.basename(folder);
  const story = film.story || {};
  const lines = [`# ${film.title}`, "", "```tin-video", `src: ${name}/film.mp4`, `poster: ${name}/poster.png`, `captions: ${name}/captions.vtt`, `title: ${film.title}`, "```", ""];
  if (story.before) lines.push(`**Before:** ${story.before}`, "");
  if (story.after) lines.push(`**After:** ${story.after}`, "");
  lines.push("## What it says", "");
  for (const caption of report.captions) lines.push(`- ${clock(caption.start)} ${caption.text}`);
  if (!report.captions.length) lines.push("The film has no captions.");
  const findings = [...report.issues.errors, ...report.issues.warnings, ...(report.summary ?? [])];
  lines.push("", "## Checks", "");
  if (findings.length) {
    lines.push("Every frame was measured and checked. What the checks still found:", "");
    for (const line of findings) lines.push(`- ${line}`);
  } else {
    lines.push("Every frame was measured and checked, and nothing was found.");
  }
  for (const line of film.notes ?? []) lines.push("", line);
  const {width, height} = FORMATS[format];
  const how = film.site?.url ? `filmed from a recording of ${new URL(film.site.url).hostname}` : "staged in the product's look";
  lines.push("", "## The film", "",
    `${format}, ${width}×${height}, ${report.seconds.toFixed(1)} s, ${how}. Its spec is \`${name}/film.json\`${film.stage ? `, its stage \`${name}/stage.html\` and its storyboard \`${name}/storyboard.json\`` : ""}: change them and render again for a new cut.`, "");
  return lines.join("\n");
}

// A second look: the latest take's contact sheet and marked stills, the spec, its storyboard,
// the worked example's sheet and what the checks still report go to Tin's review route with the
// run's grant (film_review.py); the findings are printed and kept beside the take. A run has a
// few reviews; each costs a model call.
export function reviewRequest(file, dir) {
  const report = JSON.parse(fs.readFileSync(path.join(dir, "report.json"), "utf8"));
  // A rehearsal's stills are JPEGs, a final take's PNGs.
  const still = name => /\.(png|jpg)$/.test(name);
  const marks = fs.readdirSync(path.join(dir, "marks")).filter(still).sort().slice(0, 11);
  const sheet = fs.readdirSync(dir).find(name => name.startsWith("sheet.") && still(name));
  const images = [path.join(dir, sheet), ...marks.map(name => path.join(dir, "marks", name))]
    .map(file => ({media_type: file.endsWith(".jpg") ? "image/jpeg" : "image/png", data_base64: fs.readFileSync(file).toString("base64")}));
  const checks = [...report.issues.errors, ...report.issues.warnings, ...(report.summary ?? [])].join("\n");
  const spec = fs.readFileSync(file, "utf8");
  // The plan the stills draw, and the worked example's sheet the film started beside: the bar.
  const board = path.join(here(file), "storyboard.md");
  const storyboard = fs.existsSync(board) ? fs.readFileSync(board, "utf8") : "";
  const bar = path.join(here(file), "example.jpg");
  const example = fs.existsSync(bar) ? {media_type: "image/jpeg", data_base64: fs.readFileSync(bar).toString("base64")} : null;
  const request_id = crypto.createHash("sha256").update(spec).update(storyboard).update(checks).update(images.map(image => image.data_base64).join("")).update(example?.data_base64 ?? "").digest("hex").slice(0, 32);
  return {request_id, spec, storyboard, checks, images, example};
}

// Choosing a direction: the sketch sheet (tin-film sketch) and the maker's notes (the storyboard
// beside the sketches' folder: kind, research, directions) go to the same route; it answers with
// the number of the sketch that carries the claim, the runner-up, why, and what to keep.
export function directionsRequest(dir) {
  const root = path.resolve(dir);
  const sheet = path.join(root, "sketches.jpg");
  if (!fs.existsSync(sheet)) throw new Error(`No sketch sheet: run tin-film sketch ${dir} first.`);
  const sketches = fs.readdirSync(root, {withFileTypes: true}).filter(entry => entry.isDirectory() && fs.existsSync(path.join(root, entry.name, "index.html"))).length;
  const board = [path.join(root, "..", "storyboard.md"), path.join(root, "..", "film", "storyboard.md")].find(file => fs.existsSync(file));
  const notes = board ? fs.readFileSync(board, "utf8") : "";
  if (!notes.trim()) throw new Error("No notes to choose by: write the kind, the research and the directions in storyboard.md beside the sketches' folder.");
  const images = [{media_type: "image/jpeg", data_base64: fs.readFileSync(sheet).toString("base64")}];
  const request_id = crypto.createHash("sha256").update("directions").update(notes).update(images[0].data_base64).digest("hex").slice(0, 32);
  return {request_id, kind: "directions", sketches, spec: notes, checks: "", images};
}

async function post(body) {
  const url = process.env.TIN_RUN_TOOLS_URL || "";
  const grant = process.env.TIN_RUN_TOOLS_GRANT || "";
  if (!url || !grant) throw new Error("This run has no studio grant (TIN_RUN_TOOLS_URL and TIN_RUN_TOOLS_GRANT): a review runs inside Tin's studio sandbox.");
  const response = await fetch(`${url.replace(/\/mcp$/, "").replace(/\/$/, "")}/studio/review`, {
    method: "POST",
    headers: {"authorization": `Bearer ${grant}`, "content-type": "application/json"},
    body: JSON.stringify(body),
  });
  const answer = await response.json().catch(() => ({error: `the review route answered ${response.status}`}));
  if (!response.ok) throw new Error(`Review refused: ${answer.error || response.status}`);
  return answer;
}

async function reviewDirections(dir) {
  const answer = await post(directionsRequest(dir));
  fs.writeFileSync(path.join(path.resolve(dir), "review.json"), `${JSON.stringify(answer, null, 2)}\n`);
  console.log(`directions: build sketch ${answer.choice} (runner-up ${answer.runner_up})${answer.replayed ? " (already bought; replayed)" : ""}`);
  if (answer.summary) console.log(`  ${answer.summary}`);
  if (answer.keep) console.log(`  Keep: ${answer.keep}`);
}

async function review(file) {
  const film = load(file);
  const format = formatOf(film, {});
  // The newest take, whichever kind it was.
  const dir = ["render", "rehearse", "record"].map(kind => takeDir(file, kind, film, format)).filter(candidate => fs.existsSync(path.join(candidate, "report.json")))
    .sort((a, b) => fs.statSync(path.join(b, "report.json")).mtimeMs - fs.statSync(path.join(a, "report.json")).mtimeMs)[0];
  if (!dir) throw new Error(`No take to review: run tin-film rehearse ${file} first.`);
  const answer = await post(reviewRequest(file, dir));
  fs.writeFileSync(path.join(dir, "review.json"), `${JSON.stringify(answer, null, 2)}\n`);
  console.log(`review of ${dir}: ${answer.verdict}${answer.replayed ? " (already bought; replayed)" : ""}`);
  if (answer.summary) console.log(`  ${answer.summary}`);
  for (const item of answer.findings) console.log(`  - ${item.where}: ${item.what} Fix: ${item.fix}`);
}

// Copy the final take beside the note (re-encoded smaller when it is over Tin's limit), write the
// note, and check both as Tin will.
async function deliver(file, notePath) {
  if (!notePath?.endsWith(".md")) throw new Error("usage: tin-film deliver <film.json> <films/name.md>");
  const film = load(file);
  const format = formatOf(film, {});
  const dir = takeDir(file, "render", film, format);
  const reportFile = path.join(dir, "report.json");
  if (!fs.existsSync(reportFile) || !fs.existsSync(path.join(dir, "film.mp4"))) throw new Error(`No final take in ${dir}: run tin-film render ${file} first.`);
  const report = JSON.parse(fs.readFileSync(reportFile, "utf8"));
  const folder = notePath.slice(0, -".md".length) + ".assets";
  fs.mkdirSync(folder, {recursive: true});
  const video = path.join(folder, "film.mp4");
  fs.copyFileSync(path.join(dir, "film.mp4"), video);
  for (const crf of [23, 27, 31]) {
    if (fs.statSync(video).size <= MAX_FILM_BYTES) break;
    await ffmpeg(["-i", path.join(dir, "film.mp4"), "-c:v", "libx264", "-preset", "slow", "-crf", String(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", video]).done;
  }
  fs.copyFileSync(path.join(dir, "poster.png"), path.join(folder, "poster.png"));
  fs.copyFileSync(path.join(dir, "captions.vtt"), path.join(folder, "captions.vtt"));
  fs.copyFileSync(file, path.join(folder, "film.json"));
  // A staged film keeps its stage and storyboard, so a later run can change the cut.
  const kept = ["poster.png", "captions.vtt", "film.json"];
  if (film.stage) {
    const stage = path.resolve(here(file), film.stage);
    const others = fs.readdirSync(path.dirname(stage)).filter(name => name !== path.basename(stage) && !name.startsWith("."));
    if (others.length) console.log(`  only ${path.basename(stage)} is kept as the stage; inline ${others.join(", ")} to keep them`);
    fs.copyFileSync(stage, path.join(folder, "stage.html"));
    const board = path.join(here(file), "storyboard.md");
    fs.writeFileSync(path.join(folder, "storyboard.json"), `${JSON.stringify({storyboard: fs.existsSync(board) ? fs.readFileSync(board, "utf8") : ""}, null, 2)}\n`);
    kept.push("stage.html", "storyboard.json");
  }
  fs.writeFileSync(notePath, note({film, report, folder, format}));
  console.log(`wrote ${notePath} and ${folder}/ (film.mp4 ${(fs.statSync(video).size / 1e6).toFixed(1)} MB, ${kept.join(", ")})`);
  checkNote(notePath);
}

// The same contract Tin applies when it keeps the note: tin-studio in the sandbox, the
// repository's copy elsewhere.
function checkNote(notePath) {
  const local = fileURLToPath(new URL("../../sandbox/studio/tin_studio.py", import.meta.url));
  const command = fs.existsSync(local) ? ["python3", [local, "check", notePath]] : ["tin-studio", ["check", notePath]];
  const checked = spawnSync(command[0], command[1], {encoding: "utf8"});
  process.stdout.write(checked.stdout || "");
  if (checked.status !== 0) {
    process.stdout.write(checked.stderr || "");
    process.exitCode = 1;
  }
}

// The directions as a note to pick from: the sketches' stills and pages beside it, each with its
// name and what it shows, and the one the second look recommends (review --directions). The
// founder picks in Decisions; the note's next version is the film.
export function directionsNote({title, lede, sketches, review, folder}) {
  const name = path.basename(folder);
  const file = (sketch, ext) => `${sketch.n}-${slugOf(sketch.name)}${ext}`;
  const recommended = sketches.find(sketch => sketch.n === review.choice);
  const lines = [`# ${title}`, ""];
  if (lede) lines.push(lede, "");
  lines.push(`${sketches.length} ways to show it, each sketched as one frame.${recommended ? ` Tin recommends ${recommended.name}${review.summary ? `: ${review.summary.replace(/\s+/g, " ").trim()}` : "."}` : ""}`, "");
  lines.push("```tin-directions", `recommended: ${review.choice}`);
  if (review.runner_up && review.runner_up !== review.choice) lines.push(`runner_up: ${review.runner_up}`);
  if (review.summary) lines.push(`why: ${review.summary.replace(/\s+/g, " ").trim()}`);
  lines.push("```", "");
  for (const sketch of sketches) {
    lines.push(`## ${sketch.n}. ${sketch.name}`, "", `![${sketch.name}](./${name}/${file(sketch, path.extname(sketch.still))})`, "");
    if (sketch.about) lines.push(sketch.about, "");
    lines.push(`[The sketch's page](./${name}/${file(sketch, ".html")})`, "");
  }
  lines.push("## How they were found", "", `The kind of film, the research and the notes on each direction are in \`${name}/notes.json\`; the film is built from them and the sketch picked.`, "");
  return lines.join("\n");
}

const slugOf = text => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "sketch";

export function deliverDirections(dir, notePath) {
  if (!notePath?.endsWith(".md")) throw new Error("usage: tin-film deliver --directions <dir> <films/name.md>");
  const root = path.resolve(dir);
  const manifest = path.join(root, "sketches.json");
  const reviewed = path.join(root, "review.json");
  if (!fs.existsSync(manifest)) throw new Error(`No sketches in ${root}: run tin-film sketch ${dir} first.`);
  if (!fs.existsSync(reviewed)) throw new Error(`No recommendation for ${root}: run tin-film review --directions ${dir} first.`);
  const {sketches} = JSON.parse(fs.readFileSync(manifest, "utf8"));
  const review = JSON.parse(fs.readFileSync(reviewed, "utf8"));
  const board = [path.join(root, "..", "storyboard.md"), path.join(root, "..", "film", "storyboard.md")].find(item => fs.existsSync(item));
  const notes = board ? fs.readFileSync(board, "utf8") : "";
  const title = notes.match(/^# (.+)$/m)?.[1]?.trim() || path.basename(notePath, ".md");
  const folder = notePath.slice(0, -".md".length) + ".assets";
  fs.mkdirSync(folder, {recursive: true});
  for (const sketch of sketches) {
    const about = path.join(root, sketch.folder, "about.txt");
    sketch.about = fs.existsSync(about) ? fs.readFileSync(about, "utf8").replace(/\s+/g, " ").trim() : "";
    const base = path.join(folder, `${sketch.n}-${slugOf(sketch.name)}`);
    fs.copyFileSync(path.join(root, sketch.still), base + path.extname(sketch.still));
    fs.copyFileSync(path.join(root, sketch.folder, "index.html"), `${base}.html`);
  }
  // The kind, the research and the directions' notes, for the run that builds the pick.
  fs.writeFileSync(path.join(folder, "notes.json"), `${JSON.stringify({storyboard: notes}, null, 2)}\n`);
  fs.writeFileSync(notePath, directionsNote({title, sketches, review, folder}));
  console.log(`wrote ${notePath} and ${folder}/ (${sketches.length} sketches, each a still and its page; recommended ${review.choice})`);
  checkNote(notePath);
}

// A delivered film back as a staged film: its spec, stage and storyboard, from the note's folder.
export function restore(notePath, dir) {
  const folder = notePath.slice(0, -".md".length) + ".assets";
  for (const name of ["film.json", "stage.html", "storyboard.json"]) {
    if (!fs.existsSync(path.join(folder, name))) throw new Error(`${folder} has no ${name}: this film can't be restored; start it again from an example.`);
  }
  const root = path.resolve(dir);
  fs.mkdirSync(path.join(root, "stage"), {recursive: true});
  const spec = JSON.parse(fs.readFileSync(path.join(folder, "film.json"), "utf8"));
  spec.stage = "stage/index.html";
  fs.writeFileSync(path.join(root, "film.json"), `${JSON.stringify(spec, null, 2)}\n`);
  fs.copyFileSync(path.join(folder, "stage.html"), path.join(root, "stage", "index.html"));
  fs.writeFileSync(path.join(root, "storyboard.md"), JSON.parse(fs.readFileSync(path.join(folder, "storyboard.json"), "utf8")).storyboard || "");
  console.log(`restored ${root}: film.json, stage/index.html and storyboard.md; rehearse it, change it, render and deliver to ${notePath} again`);
}

// A storyboard (storyboard.md beside film.json): a title, a line saying what shape of feature it
// suits, the story, its directions (## Directions), then a "## N. Name" section per shot with its
// Mark and its Caption. A take's
// contact sheet is the storyboard drawn, one still per shot, so the film marks every shot and
// shows every caption, in the storyboard's order.
export function shotsOf(text) {
  return text.split(/^## /m).slice(1).filter(section => /^\d+\./.test(section)).map(section => {
    const [heading, ...lines] = section.split("\n");
    const field = name => lines.find(line => line.startsWith(`- ${name}:`))?.slice(name.length + 3).trim() || "";
    return {name: heading.replace(/^\d+\.\s*/, "").trim(), mark: field("Mark"), caption: field("Caption")};
  });
}

const flat = beats => beats.flatMap(beat => (beat.beats ? flat(beat.beats) : [beat]));

export function boardIssues(text, spec) {
  const shots = shotsOf(text);
  const beats = flat(spec.beats || []);
  const marks = beats.filter(beat => "mark" in beat).map(beat => beat.mark);
  const captions = beats.filter(beat => "caption" in beat).map(beat => beat.caption);
  if (!shots.length) return ['It has no shots: a "## 1. Name" section for each, with "- Mark:" and "- Caption:" lines.'];
  const issues = [];
  for (const shot of shots) {
    if (!shot.mark) issues.push(`Shot "${shot.name}" names no mark.`);
    else if (!marks.includes(shot.mark)) issues.push(`Shot "${shot.name}" is marked ${shot.mark}, and the film never marks ${shot.mark}.`);
    if (!shot.caption) issues.push(`Shot "${shot.name}" has no caption.`);
    else if (!captions.includes(shot.caption)) issues.push(`Shot "${shot.name}"'s caption is not one the film shows: “${shot.caption}”.`);
  }
  for (const mark of marks) if (!shots.some(shot => shot.mark === mark)) issues.push(`The film marks ${mark}, which no shot names.`);
  const order = shots.map(shot => marks.indexOf(shot.mark)).filter(index => index >= 0);
  if (order.some((index, i) => i && index < order[i - 1])) issues.push("The film marks its shots in another order than the storyboard.");
  if (!shots.some(shot => shot.mark === "poster")) issues.push("No shot is the poster (- Mark: poster).");
  return issues;
}

function storyboardIssues(file) {
  const board = path.join(here(file), "storyboard.md");
  if (!fs.existsSync(board)) return [`There is no storyboard.md beside ${path.basename(file)}; tin-film new writes the example's to rewrite.`];
  return boardIssues(fs.readFileSync(board, "utf8"), JSON.parse(fs.readFileSync(file, "utf8")));
}

// The worked examples (examples/<name>): a storyboard, a film.json, its stage and the contact
// sheet of its rehearsal (sheet.jpg; rehearse an example again after changing it and copy its
// sheet beside it), copied as the start of a new staged film, the way Stageshow starts from its
// closest gold example.
const EXAMPLES = fileURLToPath(new URL("./examples/", import.meta.url));

function examples() {
  return fs.readdirSync(EXAMPLES).filter(name => fs.existsSync(path.join(EXAMPLES, name, "storyboard.md"))).sort().map(name => {
    const text = fs.readFileSync(path.join(EXAMPLES, name, "storyboard.md"), "utf8");
    return {name, title: text.match(/^# (.+)$/m)?.[1] ?? name, shape: text.split(/\n\s*\n/)[1]?.trim() ?? ""};
  });
}

function start(dir, from = "onboarding") {
  const all = examples();
  const example = all.find(item => item.name === from);
  if (!example) throw new Error(`There is no worked example ${from}; there are ${all.map(item => item.name).join(", ")}.`);
  const target = path.resolve(dir);
  if (fs.existsSync(path.join(target, "film.json"))) throw new Error(`${target}/film.json is already there; tin-film new starts a film in an empty folder.`);
  fs.mkdirSync(target, {recursive: true});
  const source = path.join(EXAMPLES, from);
  for (const name of ["film.json", "storyboard.md"]) fs.copyFileSync(path.join(source, name), path.join(target, name));
  fs.cpSync(path.join(source, "stage"), path.join(target, "stage"), {recursive: true});
  // The example's contact sheet, one still per shot: the bar this film is held to.
  fs.copyFileSync(path.join(source, "sheet.jpg"), path.join(target, "example.jpg"));
  console.log(`wrote ${target}/storyboard.md, film.json and stage/: ${example.title}. Its contact sheet is ${target}/example.jpg, the bar; rewrite all three for this product.`);
  console.log("The worked examples (tin-film new <dir> --from <name>):");
  for (const item of all) console.log(`  ${item.name}: ${item.shape}`);
}

// Directions dealt from outside the model's habits (directions.js), printed to copy into the
// storyboard: one direction is written per card before any is chosen.
function directions(options) {
  const kind = options.kind || "idea";
  if (!["idea", "product", "both"].includes(kind)) throw new Error("--kind is idea (a general idea), product (what the product does) or both.");
  const seed = options.seed || crypto.randomBytes(4).toString("hex");
  const count = Math.max(3, Math.min(8, Number(options.count) || 5));
  console.log(`Directions dealt for a${kind === "idea" ? "n idea" : kind === "product" ? " product highlight" : "n idea that lands on the product"} (seed ${seed}). Write one direction for each card under ## Directions in storyboard.md, then sketch each:`);
  deal(kind, {count, seed}).forEach((card, i) => console.log(`${i + 1}. ${describe(card)}`));
  console.log("You may swap one card for a way of your own; say why. Keep the ideas far apart.");
}

// One still of each sketch: a folder per direction (1-iceberg/index.html, 2-orbit/index.html…)
// holding a stage page and its caption in caption.txt; stage.poster puts the page in its poster
// frame (the kit's shows every part). Each is
// filmed for a moment, as a staged film is, and the stills are tiled into <dir>/sketches.jpg in
// folder order, each caption numbered so a reader can name the one they mean.
// A sketch's folder, as the founder reads its name: "02-zoom-out" is "Zoom out".
const nameOf = folder => {
  const words = folder.replace(/^\d+[-_ ]*/, "").replace(/[-_]+/g, " ").trim() || folder;
  return words[0].toUpperCase() + words.slice(1);
};

export async function sketch(dir, options = {}) {
  const root = path.resolve(dir);
  const format = options.format || "16:9";
  const folders = fs.readdirSync(root, {withFileTypes: true}).filter(entry => entry.isDirectory() && fs.existsSync(path.join(root, entry.name, "index.html"))).map(entry => entry.name).sort();
  if (folders.length < 2) throw new Error(`Sketch at least two directions: ${root} needs a folder per direction, each with an index.html.`);
  const stills = [];
  const sketches = [];
  for (const [index, name] of folders.entries()) {
    const folder = path.join(root, name);
    const caption = fs.existsSync(path.join(folder, "caption.txt")) ? fs.readFileSync(path.join(folder, "caption.txt"), "utf8").trim() : name;
    const spec = {title: name, story: {before: "", after: ""}, stage: "index.html", screens: {"16:9": 1080}, now: new Date().toISOString(), format, ready: ".stage",
      // Numbered for the sheet the second look reads; the founder's still has the caption alone.
      beats: [{camera: "body", cut: true, pad: 0}, {caption: `${index + 1}. ${caption}`}, {do: ["poster"]}, {begin: true}, {hold: 1.2}, {mark: "poster"}, {caption}, {hold: 0.8}, {mark: "still"}, {end: true}]};
    const film = filmFromSpec(spec, path.join(folder, "film.json"));
    const {dir: take, report} = await shoot(film, {format, out: path.join(folder, "takes"), rehearse: true, check: false, gif: false});
    for (const line of report.issues.errors) console.log(`  ${index + 1}. ${name}: error: ${line}`);
    const marked = name => fs.readdirSync(path.join(take, "marks")).find(file => new RegExp(`${name}\\.(jpg|png)$`).test(file));
    const poster = marked("poster");
    const still = path.join(folder, `still${path.extname(poster)}`);
    fs.copyFileSync(path.join(take, "marks", marked("still") || poster), still);
    stills.push(path.join(take, "marks", poster));
    sketches.push({n: index + 1, folder: name, name: nameOf(name), caption, still: path.relative(root, still)});
    console.log(`${index + 1}. ${name}`);
  }
  fs.writeFileSync(path.join(root, "sketches.json"), `${JSON.stringify({sketches}, null, 2)}\n`);
  const tiles = fs.mkdtempSync(path.join(root, ".tiles-"));
  stills.forEach((file, i) => fs.copyFileSync(file, path.join(tiles, `${String(i + 1).padStart(2, "0")}${path.extname(file)}`)));
  const ext = path.extname(stills[0]);
  const columns = Math.min(3, stills.length);
  await ffmpeg(["-i", path.join(tiles, `%02d${ext}`), "-vf", `scale=960:-2:flags=lanczos,tile=${columns}x${Math.ceil(stills.length / columns)}:padding=24:margin=24:color=0x44403a`, "-frames:v", "1", "-q:v", "3", path.join(root, "sketches.jpg")]).done;
  fs.rmSync(tiles, {recursive: true, force: true});
  console.log(`look at: ${path.join(root, "sketches.jpg")}, the sketches in this order; each still and its name are in sketches.json`);
}

async function main(argv) {
  const [command, ...rest] = argv;
  const options = {};
  const args = [];
  for (let i = 0; i < rest.length; i++) {
    if (rest[i].startsWith("--")) options[rest[i].slice(2)] = rest[++i];
    else args.push(rest[i]);
  }
  if (command === "inspect" && args.length === 1) {
    const dir = path.resolve(options.dir || ".");
    const report = await inspect(args[0], {dir, format: options.format || "16:9"});
    console.log(`${report.title} (${report.url}), ${report.height}px tall, read at ${report.screen.width}×${report.screen.height}`);
    console.log(`look: ${report.look.background} ground, ${report.look.color} text, ${report.look.font}; as the stage kit's tokens:\n${report.look.tokens}`);
    if (report.look.stylesheets.length) console.log(`  its font stylesheets: ${report.look.stylesheets.join(", ")}`);
    for (const item of report.headings) console.log(`  h${item.level} at ${item.top}: ${item.text}  →  ${item.selector}`);
    for (const item of report.controls) console.log(`  ${item.kind} at ${item.top}: ${item.text || "(no text)"}${item.href ? ` [${item.href}]` : ""}  →  ${item.selector}`);
    if (report.recording.stopped.length) console.log(`  stopped (never sent): ${report.recording.stopped.join(", ")}`);
    console.log(`wrote ${path.join(dir, "inspect.json")}, screen.jpg, page.jpg and, if it was missing, film.json`);
    return;
  }
  if (command === "directions" && args.length === 0) return directions(options);
  if (command === "sketch" && args.length === 1) return sketch(args[0], options);
  if (command === "new" && args.length === 1) return start(args[0], options.from);
  if (TAKES[command] && args.length === 1) return take(command, args[0], options);
  if (command === "review" && options.directions && args.length === 0) return reviewDirections(options.directions);
  if (command === "review" && args.length === 1) return review(args[0]);
  if (command === "deliver" && options.directions && args.length === 1) return deliverDirections(options.directions, args[0]);
  if (command === "deliver" && args.length === 2) return deliver(args[0], args[1]);
  if (command === "restore" && args.length === 2) return restore(args[0], args[1]);
  const header = fs.readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n");
  console.log(header.slice(0, header.findIndex(line => !line.startsWith("//"))).join("\n").replaceAll("// ", "").replaceAll("//", ""));
  if (command && command !== "help") process.exitCode = 64;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

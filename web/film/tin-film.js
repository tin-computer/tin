// tin-film: the film kit as a command, for the feature-film workflow's agent in Tin's studio
// sandbox (and anyone filming a live site by hand). A film is a JSON file (film.json); its
// recording (world/) and takes (takes/) sit beside it, never in the project.
//
//   tin-film inspect <url> [--dir DIR] [--format 16:9]  outline, screenshots and a starter film.json
//   tin-film record <film.json>      play it once against the live site, saving what it loads
//   tin-film rehearse <film.json>    play it from the recording at low resolution, every frame checked
//   tin-film render <film.json>      the final take from the recording, at full resolution, checked
//   tin-film deliver <film.json> <note.md>   the final take beside its note, as Tin keeps films
//
// Every take prints what went wrong, the checks' findings and the files to look at; a take with
// an error exits 1. See docs/feature-films.md.
import {spawnSync} from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {FORMATS, ffmpeg, filmFromSpec, shoot, takeName} from "./film.js";
import {inspect} from "./inspect.js";

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
  console.log(`  look at: ${path.join(dir, "sheet.png")} and ${path.join(dir, "marks")}/`);
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
  const host = new URL(film.site.url).hostname;
  lines.push("", "## The film", "",
    `${format}, ${width}×${height}, ${report.seconds.toFixed(1)} s, filmed from a recording of ${host}. Its spec is \`${name}/film.json\`: change it and render again for a new cut.`, "");
  return lines.join("\n");
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
  fs.writeFileSync(notePath, note({film, report, folder, format}));
  console.log(`wrote ${notePath} and ${folder}/ (film.mp4 ${(fs.statSync(video).size / 1e6).toFixed(1)} MB, poster.png, captions.vtt, film.json)`);
  // The same contract Tin applies when it keeps the note: tin-studio in the sandbox, the
  // repository's copy elsewhere.
  const local = fileURLToPath(new URL("../../sandbox/studio/tin_studio.py", import.meta.url));
  const command = fs.existsSync(local) ? ["python3", [local, "check", notePath]] : ["tin-studio", ["check", notePath]];
  const checked = spawnSync(command[0], command[1], {encoding: "utf8"});
  process.stdout.write(checked.stdout || "");
  if (checked.status !== 0) {
    process.stdout.write(checked.stderr || "");
    process.exitCode = 1;
  }
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
    console.log(`look: ${report.look.background} ground, ${report.look.color} text, ${report.look.font}`);
    for (const item of report.headings) console.log(`  h${item.level} at ${item.top}: ${item.text}  →  ${item.selector}`);
    for (const item of report.controls) console.log(`  ${item.kind} at ${item.top}: ${item.text || "(no text)"}${item.href ? ` [${item.href}]` : ""}  →  ${item.selector}`);
    if (report.recording.stopped.length) console.log(`  stopped (never sent): ${report.recording.stopped.join(", ")}`);
    console.log(`wrote ${path.join(dir, "inspect.json")}, screen.png, page.png and, if it was missing, film.json`);
    return;
  }
  if (TAKES[command] && args.length === 1) return take(command, args[0], options);
  if (command === "deliver" && args.length === 2) return deliver(args[0], args[1]);
  console.log(fs.readFileSync(fileURLToPath(import.meta.url), "utf8").split("\n").slice(0, 12).join("\n").replaceAll("// ", "").replaceAll("//", ""));
  if (command && command !== "help") process.exitCode = 64;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

// A film of a public site from its recording: Sheepdogs, one of Tin's test projects, a drawing game
// at sheepdogs.io. Its world is the live site played once (`--record on`) and replayed from
// .films/worlds/sheepdogs for every take; the film is beats alone, as an agent would write it.

export const title = "Sheepdogs, from its site";
export const now = "2026-11-05T10:00:00Z";
export const timezone = "Europe/Berlin";

export function world() {
  return {site: {url: "https://sheepdogs.io/", recording: "sheepdogs"}, ready: "h1"};
}

export const beats = [
  {camera: "body", cut: true, pad: 0},
  {caption: "Sheepdogs: herd sheep by drawing, with other people online."},
  {begin: true},
  {hold: 2.5},
  {mark: "game"},
  {caption: "Its makers wrote down how herding became drawing."},
  {click: "a[href='/blog/how-herding-became-drawing']"},
  {ready: "h1"},
  {hold: 1.6},
  {mark: "poster"},
  {scroll: ["html", "h2"], duration: 1.2},
  {hold: 2},
  {mark: "story"},
  {end: true},
];

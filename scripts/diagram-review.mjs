// Shared by the developer gallery and the per-run, offline candidate checker.
import fs from "node:fs/promises";
import path from "node:path";

export async function embeddedFonts(assets) {
  return (await Promise.all(["sans", "mono"].flatMap((face) => [400, 700].map(async (weight) => {
    const font = `geist-${face}-${weight === 700 ? "bold" : "regular"}.woff2`;
    return `@font-face{font-family:"Tin Diagram ${face === "sans" ? "Sans" : "Mono"}";font-weight:${weight};src:url(data:font/woff2;base64,${(await fs.readFile(path.join(assets, "fonts", font))).toString("base64")}) format("woff2")}`;
  })))).join("\n");
}

export async function loadFonts() {
  await Promise.all([400, 700].flatMap((weight) => ["Tin Diagram Sans", "Tin Diagram Mono"].map((family) => document.fonts.load(`${weight} 12px "${family}"`))));
  await document.fonts.ready;
  if ([...document.fonts].some((face) => face.family.startsWith("Tin Diagram") && face.status !== "loaded")) throw new Error("Packaged diagram font unavailable");
}

export function exportDiagrams({ fontRules }) {
  return [...document.querySelectorAll("section")].map((section) => {
    const source = section.querySelector("svg"), svg = source.cloneNode(true);
    svg.setAttribute("text-rendering", getComputedStyle(source).textRendering);
    const originals = [source, ...source.querySelectorAll("*")], clones = [svg, ...svg.querySelectorAll("*")];
    for (let i = 0; i < originals.length; i++) {
      const computed = getComputedStyle(originals[i]);
      for (const attr of ["fill", "stroke", "font-family"]) {
        if (!clones[i].hasAttribute(attr)) continue;
        const value = computed.getPropertyValue(attr);
        const rgba = /^rgba\((\d+), (\d+), (\d+), ([\d.]+)\)$/.exec(value);
        clones[i].setAttribute(attr, rgba ? `rgb(${rgba[1]}, ${rgba[2]}, ${rgba[3]})` : value);
        if (rgba) clones[i].setAttribute(`${attr}-opacity`, rgba[4]);
      }
    }
    const { width, height } = source.viewBox.baseVal;
    svg.setAttribute("width", width + 48); svg.setAttribute("height", height + 48);
    svg.setAttribute("viewBox", `-24 -24 ${width + 48} ${height + 48}`); svg.removeAttribute("style");
    const style = document.createElementNS("http://www.w3.org/2000/svg", "style"); style.textContent = fontRules;
    const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [key, value] of Object.entries({ x: -24, y: -24, width: width + 48, height: height + 48, fill: getComputedStyle(source.hasAttribute("data-tin-brand") ? source : document.body).backgroundColor })) background.setAttribute(key, value);
    svg.prepend(style, background);
    return { id: section.id, svg: new XMLSerializer().serializeToString(svg) };
  });
}

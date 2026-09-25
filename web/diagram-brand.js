// A diagram carries its approved palette, never arbitrary CSS or remote font URLs.
const PREFIX = "%% tin:brand ";

function validateBrand(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      !["revision", "sha256", "light"].every((key) => Object.hasOwn(value, key)) ||
      Object.keys(value).some((key) => !["revision", "sha256", "light", "dark"].includes(key)) ||
      typeof value.revision !== "string" || typeof value.sha256 !== "string" ||
      !/^[0-9a-f]{40}$/.test(value.revision) || !/^[0-9a-f]{64}$/.test(value.sha256))
    throw new Error("Invalid diagram brand snapshot.");
  for (const mode of ["light", "dark"]) {
    if (!Object.hasOwn(value, mode)) continue;
    const palette = value[mode];
    if (!palette || typeof palette !== "object" || Array.isArray(palette) ||
        !["ink", "paper", "accent"].every((key) => Object.hasOwn(palette, key)) ||
        Object.keys(palette).some((key) => !["ink", "paper", "accent", "signal"].includes(key)) ||
        Object.values(palette).some((color) => typeof color !== "string" || !/^#[0-9a-fA-F]{6}$/.test(color)))
      throw new Error("Invalid diagram brand palette.");
  }
  return value;
}

function parseBrand(line) {
  const raw = line.slice(PREFIX.length);
  if (raw.length > 2000) throw new Error("Diagram brand snapshot is too large.");
  const value = validateBrand(JSON.parse(raw));
  // Requiring the compact form also rejects duplicate JSON keys.
  if (JSON.stringify(value) !== raw) throw new Error("Copy the compact diagram brand snapshot unchanged.");
  return value;
}

function mix(paper, ink, amount) {
  const rgb = (hex) => [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16));
  const foreground = rgb(ink);
  return "#" + rgb(paper).map((value, i) => Math.round(value + (foreground[i] - value) * amount).toString(16).padStart(2, "0")).join("");
}

function brandStyles(brand) {
  validateBrand(brand);
  return ["light", "dark"].flatMap((mode) => {
    // No invented dark palette: a light-only identity retains its paper in dark UI.
    const { ink, paper, accent } = brand[mode] || brand.light;
    const roles = {
      ink, paper, accent, secondary: mix(paper, ink, 0.8), muted: mix(paper, ink, 0.7),
      border: mix(paper, ink, 0.22), edge: mix(paper, ink, 0.55),
      frame: mix(paper, ink, 0.04), gate: mix(paper, ink, 0.06), receipt: mix(paper, accent, 0.09),
    };
    return Object.entries(roles).map(([role, color]) => `--diagram-brand-${mode}-${role}:${color}`);
  }).join(";");
}

export { PREFIX, validateBrand, parseBrand, brandStyles };

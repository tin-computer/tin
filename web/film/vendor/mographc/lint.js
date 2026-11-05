import { ranges, resolve } from "./config.js";
/** Run every enabled rule on a film and return its findings, suppressions applied. */
export const run = (film, config, options = {}) => {
    const kinds = options.kinds ?? ["check", "lint"];
    const findings = [];
    const timed = ranges(config, film.composition);
    for (const { rule, severity, options: ruleOptions } of resolve(config, film.composition).values()) {
        if (severity === "off" ||
            !kinds.includes(rule.kind) ||
            (rule.needsBaseline && !options.baseline))
            continue;
        rule.check({
            film,
            baseline: options.baseline,
            options: { ...rule.options, ...ruleOptions },
            report(r) {
                const from = film.seconds(r.frames[0]);
                const to = film.seconds(r.frames[1]);
                const finding = {
                    rule: rule.id,
                    severity: r.severity && severity !== "warn" ? r.severity : severity,
                    composition: film.composition,
                    element: r.element?.label,
                    from,
                    to,
                    frames: r.frames,
                    measured: r.measured,
                    need: r.need,
                    fix: rule.docs.fix,
                    docs: `mographc explain ${rule.id}`,
                };
                if (r.element?.ignored(rule.id))
                    finding.accepted =
                        r.element.ignores.get(rule.id) ||
                            r.element.ignores.get("*") ||
                            "ignored in code";
                for (const b of timed) {
                    const setting = b.rules?.[rule.id];
                    if (!setting || to < b.between[0] || from > b.between[1])
                        continue;
                    const sev = Array.isArray(setting) ? setting[0] : setting;
                    if (sev === "off")
                        finding.accepted = b.reason;
                    else
                        finding.severity = sev;
                }
                findings.push(finding);
            },
        });
    }
    return findings.sort((a, b) => a.from - b.from || a.rule.localeCompare(b.rule));
};
export const failing = (findings) => findings.filter((f) => f.severity === "error" && !f.accepted);

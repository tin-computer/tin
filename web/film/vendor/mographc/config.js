/**
 * Flat config, like ESLint's: an array of blocks applied in order. Presets are blocks.
 *
 *   export default defineConfig([
 *     recommended,
 *     acme, // your team's brand preset
 *     { rules: { "easing/weak-out": ["error", { long: 0.45 }] } },
 *     { compositions: ["Teaser"], rules: { "cursor/dead-exit": "off" } },
 *     { between: [9.5, 13], rules: { "regression/content-removed": "off" }, reason: "zoomed in by request" },
 *   ]);
 */
import { compilerRules } from "./rules/compiler.js";
import { activityRules } from "./rules/activity.js";
import { attentionRules } from "./rules/attention.js";
import { spacingRules } from "./rules/spacing.js";
import { motionRules } from "./rules/motion.js";
import { regressionRules } from "./rules/regression.js";
import { stagingRules } from "./rules/staging.js";
export const defineConfig = (config) => config;
const settings = (rules) => Object.fromEntries(rules.map((r) => [r.id, r.severity]));
/** The compiler: what's broken in any video. Always on. */
export const compiler = {
    name: "compiler",
    plugins: compilerRules,
    rules: settings(compilerRules),
};
const recommendedRules = [
    ...motionRules,
    ...activityRules,
    ...attentionRules,
    ...spacingRules,
    ...stagingRules,
    ...regressionRules,
];
/** General motion principles: easing, squash and stretch, staging, pace, attention, regressions. */
export const recommended = {
    name: "recommended",
    plugins: recommendedRules,
    rules: settings(recommendedRules),
};
export const allRules = () => [...compilerRules, ...recommendedRules];
/** Rule settings for one composition, outside any time range. */
export const resolve = (config, composition) => {
    const available = new Map();
    for (const r of compilerRules)
        available.set(r.id, r);
    for (const b of config)
        for (const r of b.plugins ?? [])
            available.set(r.id, r);
    const out = new Map();
    for (const r of compilerRules)
        out.set(r.id, { rule: r, severity: r.severity, options: {} });
    for (const b of config) {
        if (b.between || (b.compositions && !b.compositions.includes(composition)))
            continue;
        for (const [id, setting] of Object.entries(b.rules ?? {})) {
            const rule = available.get(id);
            if (!rule)
                throw new Error(`Unknown rule "${id}"${b.name ? ` in "${b.name}"` : ""}. If it's a custom rule, add it to a config block's "plugins". Run \`mographc rules\` to list the built-in rules.`);
            const [severity, options] = Array.isArray(setting) ? setting : [setting];
            const prev = out.get(id);
            out.set(id, {
                rule,
                severity,
                options: { ...prev?.options, ...(options ?? {}) },
            });
        }
    }
    return out;
};
/** Time-ranged blocks for one composition: rules switched off or re-graded between two times, with why. */
export const ranges = (config, composition) => config.filter((b) => b.between && (!b.compositions || b.compositions.includes(composition)));

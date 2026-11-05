const { NO_COLOR, FORCE_COLOR, COLUMNS } = process.env;
const colour = !NO_COLOR && (!!FORCE_COLOR || !!process.stdout.isTTY);
const paint = (code, s) => colour ? `\x1b[${code}m${s}\x1b[0m` : s;
export const red = (s) => paint("31", s);
export const green = (s) => paint("32", s);
const yellow = (s) => paint("33", s);
const cyan = (s) => paint("36", s);
export const dim = (s) => paint("2", s);
export const bold = (s) => paint("1", s);
/** Columns to wrap at: the terminal's width, or COLUMNS. Piped output isn't wrapped, so each finding stays on its own lines for tools. */
export const width = Number(COLUMNS) || (process.stdout.isTTY ? process.stdout.columns : 0);
/**
 * Word-wrap `text` so each line fits after its prefix: `first` columns on the first line,
 * `indent` on the rest. Returns one string per line, without the prefix.
 */
export const wrap = (text, indent, columns = width, first = indent) => {
    if (!columns || columns - indent < 30)
        return [text];
    const lines = [""];
    for (const word of text.split(" ")) {
        const line = lines[lines.length - 1];
        const room = columns - (lines.length === 1 ? first : indent);
        if (line && line.length + 1 + word.length > room)
            lines.push(word);
        else
            lines[lines.length - 1] = line ? `${line} ${word}` : word;
    }
    return lines;
};
const time = (f) => f.to - f.from < 0.05
    ? `${f.from.toFixed(2)}s`
    : `${f.from.toFixed(2)}-${f.to.toFixed(2)}s`;
// The columns before a finding's text (time, severity and rule id), and before its "Required" line.
const INDENT = 2 + 14 + 1 + 5 + 2 + 28 + 1;
const NEEDS_INDENT = 2 + 14 + 8;
/** Human output: grouped by composition, in time order, each finding with what to do about it. */
export const format = (findings, { warnings = true, accepted = false, columns = width } = {}) => {
    const lines = [];
    const shown = findings.filter((f) => (warnings || f.severity === "error") && (accepted || !f.accepted));
    for (const composition of [...new Set(shown.map((f) => f.composition))]) {
        lines.push("", bold(composition));
        for (const f of shown.filter((x) => x.composition === composition)) {
            const sev = f.accepted
                ? green("ok   ")
                : f.severity === "error"
                    ? bold(red("error"))
                    : bold(yellow("warn "));
            const [first, ...rest] = wrap(`${f.element ? `${f.element}: ` : ""}${f.measured}`, NEEDS_INDENT, columns, INDENT);
            lines.push(`  ${time(f).padEnd(14)} ${sev}  ${cyan(f.rule.padEnd(28))} ${first}`, ...rest.map((l) => `${" ".repeat(NEEDS_INDENT)}${l}`));
            const needs = `Required: ${f.need}.${f.accepted ? ` Accepted: ${f.accepted}` : ` Fix: ${f.fix}`}`;
            lines.push(...wrap(needs, NEEDS_INDENT, columns).map((l) => dim(`${" ".repeat(NEEDS_INDENT)}${l}`)));
        }
    }
    const errors = findings.filter((f) => f.severity === "error" && !f.accepted).length;
    const warns = findings.filter((f) => f.severity === "warn" && !f.accepted).length;
    const ok = findings.filter((f) => f.accepted).length;
    const warnText = `${warns} warning${warns === 1 ? "" : "s"}`;
    lines.push("", `${errors ? bold(red(`${errors} error${errors === 1 ? "" : "s"}`)) : green("0 errors")}, ${warns ? yellow(warnText) : warnText}${ok ? `, ${ok} accepted` : ""}`);
    return lines.join("\n");
};

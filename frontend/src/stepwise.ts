/** Per-step settings, as the engine reads them (backend stepwise.py): "value; Step: value; …", where each change
 * holds until the next, and Active in as step names and ranges ("Zenith to Horizon, Wind"). */

const MARK = /(?:^|;)\s*([A-Za-z][^;:]*?)\s*:/g;

export type StepValues = { base: string; changes: [string, string][] };

const trimSemis = (s: string) => s.trim().replace(/^;+|;+$/g, "").trim();

export function split(text: unknown): StepValues {
  const raw = text == null ? "" : String(text);
  const marks = [...raw.matchAll(MARK)];
  if (!marks.length) return { base: raw.trim(), changes: [] };
  const changes: [string, string][] = marks.map((m, k) => {
    const end = k + 1 < marks.length ? marks[k + 1].index! : raw.length;
    return [m[1].trim(), trimSemis(raw.slice(m.index! + m[0].length, end))];
  });
  return { base: trimSemis(raw.slice(0, marks[0].index!)), changes };
}

export function join({ base, changes }: StepValues): string {
  return [...(base.trim() ? [base.trim()] : []), ...changes.map(([s, v]) => `${s}: ${v}`.trimEnd())].join("; ");
}

/** The shortest Active in text for these steps, in model order (runs become ranges). */
export function formatActive(active: Set<string>, steps: string[]): string {
  const out: string[] = [];
  for (let i = 0; i < steps.length; i++) {
    if (!active.has(steps[i])) continue;
    let j = i;
    while (j + 1 < steps.length && active.has(steps[j + 1])) j++;
    out.push(j === steps.length - 1 && j > i ? `${steps[i]} to end` : j > i ? `${steps[i]} to ${steps[j]}` : steps[i]);
    i = j;
  }
  return out.join(", ");
}

/** A typed number stays a number (the setting's own kind); anything with step changes is text. */
export function asSetting(text: string, numeric: boolean): string | number {
  const t = text.trim();
  return numeric && t !== "" && /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t) ? parseFloat(t) : text;
}

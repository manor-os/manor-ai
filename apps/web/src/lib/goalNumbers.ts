const DECIMAL_PATTERN = /^([+-]?)(\d+)(?:\.(\d+))?$/;

interface DecimalParts {
  coefficient: bigint;
  scale: number;
}

function decimalParts(value: unknown): DecimalParts | null {
  const text = String(value).trim();
  const match = DECIMAL_PATTERN.exec(text);
  if (!match) return null;
  const [, sign, integer, fraction = ""] = match;
  const coefficient = BigInt(`${sign === "-" ? "-" : ""}${integer}${fraction}`);
  return { coefficient, scale: fraction.length };
}

function coefficientAtScale(value: DecimalParts, scale: number): bigint {
  return value.coefficient * (10n ** BigInt(scale - value.scale));
}

export function formatGoalNumber(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  const text = String(value).trim();
  const match = DECIMAL_PATTERN.exec(text);
  if (!match) return null;
  const [, sign, integer, fraction] = match;
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign === "+" ? "" : sign}${grouped}${fraction ? `.${fraction}` : ""}`;
}

export function goalProgressPercent(goal: {
  current_value?: unknown;
  target_value?: unknown;
  baseline_value?: unknown;
}): number {
  const current = decimalParts(goal.current_value ?? 0);
  const target = decimalParts(goal.target_value ?? 1);
  const baseline = decimalParts(goal.baseline_value ?? 0);
  if (!current || !target || !baseline) return 0;

  const scale = Math.max(current.scale, target.scale, baseline.scale);
  const currentValue = coefficientAtScale(current, scale);
  const targetValue = coefficientAtScale(target, scale);
  const baselineValue = coefficientAtScale(baseline, scale);
  const targetDelta = targetValue - baselineValue;
  if (targetDelta === 0n) return currentValue === targetValue ? 100 : 0;

  const progressScaled = ((currentValue - baselineValue) * 1_000_000n) / targetDelta;
  if (progressScaled <= 0n) return 0;
  if (progressScaled >= 1_000_000n) return 100;
  return Number(progressScaled) / 10_000;
}

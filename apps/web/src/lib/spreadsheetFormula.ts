export function parseSpreadsheetNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const text = String(value ?? "").normalize("NFKC").trim();
  if (!text || text.startsWith("=")) return null;

  const isPercent = /%$/.test(text);
  const normalized = text
    .replace(/[$¥€£₹₩₽₺₫₴₪₦₱฿₡₲₵₭₮₸₼₾₿,\s]/g, "")
    .replace(/%$/, "");
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed)) return null;
  return isPercent ? parsed / 100 : parsed;
}

function normalizeSpreadsheetFormulaExpression(formula: string): string {
  return formula
    .trim()
    .replace(/^=/, "")
    .normalize("NFKC")
    .replace(/[；;]/g, ",")
    .replace(/[×]/g, "*")
    .replace(/[÷]/g, "/");
}

interface SpreadsheetFormulaToken {
  type: "number" | "ref" | "ident" | "op" | "lparen" | "rparen" | "comma" | "colon";
  value: string;
  sheetName?: string;
}

export type SpreadsheetFormulaValue = number | string | boolean;

export interface SpreadsheetFormulaContext {
  sheets: Array<{ name: string; data: unknown[][] }>;
  currentSheetName: string;
}

export type SpreadsheetNumberFormatter = (value: number, numberFormat: string) => string;

export interface SpreadsheetFormulaEvaluationState {
  values: Map<string, SpreadsheetFormulaValue>;
  depth: number;
}

export interface SpreadsheetDisplayOptions {
  context?: SpreadsheetFormulaContext;
  numberFormat?: string;
  formatNumber?: SpreadsheetNumberFormatter;
  evaluationState?: SpreadsheetFormulaEvaluationState;
}

const MAX_SPREADSHEET_FORMULA_DEPTH = 256;

export function createSpreadsheetFormulaEvaluationState(): SpreadsheetFormulaEvaluationState {
  return { values: new Map(), depth: 0 };
}

function spreadsheetColumnIndex(label: string): number {
  let value = 0;
  for (const ch of label.toUpperCase()) {
    value = value * 26 + (ch.charCodeAt(0) - 64);
  }
  return value - 1;
}

function parseSpreadsheetRef(
  ref: string,
  sheetName?: string,
): { row: number; col: number; sheetName?: string } | null {
  const match = ref.match(/^\$?([A-Z]+)\$?(\d+)$/i);
  if (!match) return null;
  const row = Number(match[2]) - 1;
  const col = spreadsheetColumnIndex(match[1]);
  return row >= 0 && col >= 0 ? { row, col, sheetName } : null;
}

function readSpreadsheetCellReference(
  expression: string,
  start: number,
): { value: string; end: number } | null {
  const match = expression.slice(start).match(/^\$?[A-Z]+\$?\d+/i);
  if (!match) return null;
  const end = start + match[0].length;
  if (/[A-Z_\d]/i.test(expression[end] || "")) return null;
  return { value: match[0].toUpperCase(), end };
}

function tokenizeSpreadsheetFormula(expression: string): SpreadsheetFormulaToken[] | null {
  const tokens: SpreadsheetFormulaToken[] = [];
  let index = 0;
  while (index < expression.length) {
    const character = expression[index];
    if (/\s/.test(character)) {
      index += 1;
      continue;
    }
    if ("+-*/^".includes(character)) {
      tokens.push({ type: "op", value: character });
      index += 1;
      continue;
    }
    if (character === "(") { tokens.push({ type: "lparen", value: character }); index += 1; continue; }
    if (character === ")") { tokens.push({ type: "rparen", value: character }); index += 1; continue; }
    if (character === ",") { tokens.push({ type: "comma", value: character }); index += 1; continue; }
    if (character === ":") { tokens.push({ type: "colon", value: character }); index += 1; continue; }

    if (character === "'") {
      let cursor = index + 1;
      let sheetName = "";
      let closed = false;
      while (cursor < expression.length) {
        if (expression[cursor] !== "'") {
          sheetName += expression[cursor];
          cursor += 1;
          continue;
        }
        if (expression[cursor + 1] === "'") {
          sheetName += "'";
          cursor += 2;
          continue;
        }
        closed = true;
        cursor += 1;
        break;
      }
      if (!closed || expression[cursor] !== "!") return null;
      const reference = readSpreadsheetCellReference(expression, cursor + 1);
      if (!reference) return null;
      tokens.push({ type: "ref", value: reference.value, sheetName });
      index = reference.end;
      continue;
    }

    const unquotedSheetReference = expression.slice(index).match(
      /^([^'!+\-*/^(),:<>=\s]+)!(\$?[A-Z]+\$?\d+)/i,
    );
    if (unquotedSheetReference) {
      const sheetName = unquotedSheetReference[1];
      const reference = readSpreadsheetCellReference(expression, index + sheetName.length + 1);
      if (!reference) return null;
      tokens.push({ type: "ref", value: reference.value, sheetName });
      index = reference.end;
      continue;
    }

    if (/\d|\./.test(character)) {
      let end = index;
      while (end < expression.length && /[\d.]/.test(expression[end])) end += 1;
      const isPercent = expression[end] === "%";
      if (isPercent) end += 1;
      const raw = expression.slice(index, isPercent ? end - 1 : end);
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) return null;
      tokens.push({ type: "number", value: String(isPercent ? parsed / 100 : parsed) });
      index = end;
      continue;
    }

    if (/[A-Z_$]/i.test(character)) {
      let end = index;
      while (end < expression.length && /[A-Z_$\d.]/i.test(expression[end])) end += 1;
      const raw = expression.slice(index, end);
      if (expression[end] === "!") {
        const reference = readSpreadsheetCellReference(expression, end + 1);
        if (!reference || raw.includes("$")) return null;
        tokens.push({ type: "ref", value: reference.value, sheetName: raw });
        index = reference.end;
        continue;
      }
      tokens.push({ type: parseSpreadsheetRef(raw) ? "ref" : "ident", value: raw.toUpperCase() });
      index = end;
      continue;
    }

    return null;
  }
  return tokens;
}

function resolveSpreadsheetReference(
  data: unknown[][],
  ref: { row: number; col: number; sheetName?: string },
  context?: SpreadsheetFormulaContext,
): { data: unknown[][]; sheetName: string } | null {
  if (!ref.sheetName) {
    return { data, sheetName: context?.currentSheetName || "" };
  }
  if (!context) return null;
  const normalizedName = ref.sheetName.toLocaleLowerCase();
  const sheet = context.sheets.find((candidate) => candidate.name.toLocaleLowerCase() === normalizedName);
  return sheet ? { data: sheet.data, sheetName: sheet.name } : null;
}

function spreadsheetSeenKey(
  row: number,
  col: number,
  context?: SpreadsheetFormulaContext,
): string {
  return `${context?.currentSheetName || ""}!${row}:${col}`;
}

export function getSpreadsheetNumericValue(
  data: unknown[][],
  row: number,
  col: number,
  seen = new Set<string>(),
  context?: SpreadsheetFormulaContext,
  evaluationState = createSpreadsheetFormulaEvaluationState(),
): number | null {
  const key = spreadsheetSeenKey(row, col, context);
  if (evaluationState.values.has(key)) {
    const cached = evaluationState.values.get(key);
    return typeof cached === "number" ? cached : null;
  }
  if (seen.has(key)) return null;
  const value = data[row]?.[col];
  if (typeof value === "string" && value.trim().startsWith("=")) {
    if (evaluationState.depth >= MAX_SPREADSHEET_FORMULA_DEPTH) return null;
    const nextSeen = new Set(seen);
    nextSeen.add(key);
    evaluationState.depth += 1;
    try {
      const evaluated = evaluateSpreadsheetFormula(data, value, nextSeen, context, evaluationState);
      if (evaluated != null) evaluationState.values.set(key, evaluated);
      return evaluated;
    } finally {
      evaluationState.depth -= 1;
    }
  }
  return parseSpreadsheetNumber(value);
}

function getSpreadsheetFormulaValue(
  data: unknown[][],
  row: number,
  col: number,
  seen: Set<string>,
  context?: SpreadsheetFormulaContext,
  evaluationState = createSpreadsheetFormulaEvaluationState(),
): SpreadsheetFormulaValue | null {
  const key = spreadsheetSeenKey(row, col, context);
  if (evaluationState.values.has(key)) return evaluationState.values.get(key) ?? null;
  if (seen.has(key)) return null;
  const value = data[row]?.[col];
  if (typeof value === "string" && value.trim().startsWith("=")) {
    if (evaluationState.depth >= MAX_SPREADSHEET_FORMULA_DEPTH) return null;
    const nextSeen = new Set(seen);
    nextSeen.add(key);
    evaluationState.depth += 1;
    try {
      const evaluated = evaluateSpreadsheetFormulaValue(data, value, nextSeen, context, evaluationState);
      if (evaluated != null) evaluationState.values.set(key, evaluated);
      return evaluated;
    } finally {
      evaluationState.depth -= 1;
    }
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  return typeof value === "string" || typeof value === "boolean" ? value : null;
}

function evaluateSpreadsheetFormulaTokens(
  data: unknown[][],
  tokens: SpreadsheetFormulaToken[],
  seen: Set<string>,
  context?: SpreadsheetFormulaContext,
  evaluationState = createSpreadsheetFormulaEvaluationState(),
): number | null {
  if (tokens.length === 0) return null;
  let position = 0;

  const peek = () => tokens[position];
  const consume = () => tokens[position++];

  const numericValueForRef = (token: SpreadsheetFormulaToken): number | null => {
    const ref = parseSpreadsheetRef(token.value, token.sheetName);
    if (!ref) return null;
    const target = resolveSpreadsheetReference(data, ref, context);
    if (!target) return null;
    const targetContext = context
      ? { ...context, currentSheetName: target.sheetName }
      : undefined;
    return getSpreadsheetNumericValue(target.data, ref.row, ref.col, seen, targetContext, evaluationState);
  };

  const parseRangeValues = (firstToken: SpreadsheetFormulaToken): number[] | null => {
    const start = parseSpreadsheetRef(firstToken.value, firstToken.sheetName);
    if (!start || peek()?.type !== "colon") return null;
    consume();
    const endToken = consume();
    if (endToken?.type !== "ref") return null;
    const end = parseSpreadsheetRef(
      endToken.value,
      endToken.sheetName || start.sheetName,
    );
    if (!end || (start.sheetName && end.sheetName !== start.sheetName)) return null;
    const target = resolveSpreadsheetReference(data, start, context);
    if (!target) return null;
    const targetContext = context
      ? { ...context, currentSheetName: target.sheetName }
      : undefined;
    const values: number[] = [];
    const rowStart = Math.min(start.row, end.row);
    const rowEnd = Math.max(start.row, end.row);
    const colStart = Math.min(start.col, end.col);
    const colEnd = Math.max(start.col, end.col);
    for (let row = rowStart; row <= rowEnd; row += 1) {
      for (let col = colStart; col <= colEnd; col += 1) {
        const rawValue = target.data[row]?.[col];
        const isFormula = typeof rawValue === "string" && rawValue.trim().startsWith("=");
        const value = isFormula
          ? getSpreadsheetFormulaValue(target.data, row, col, seen, targetContext, evaluationState)
          : getSpreadsheetNumericValue(target.data, row, col, seen, targetContext, evaluationState);
        if (isFormula && value == null) return null;
        if (typeof value === "number") values.push(value);
      }
    }
    return values;
  };

  const parseExpression = (): number | null => parseAddSub();

  const parseFunctionArgs = (): number[] | null => {
    const values: number[] = [];
    if (peek()?.type === "rparen") {
      consume();
      return values;
    }
    while (position < tokens.length) {
      const token = peek();
      if (token?.type === "ref" && tokens[position + 1]?.type === "colon") {
        consume();
        const rangeValues = parseRangeValues(token);
        if (!rangeValues) return null;
        values.push(...rangeValues);
      } else {
        const value = parseExpression();
        if (value == null) return null;
        values.push(value);
      }
      if (peek()?.type === "comma") {
        consume();
        continue;
      }
      if (peek()?.type === "rparen") {
        consume();
        return values;
      }
      return null;
    }
    return null;
  };

  function parsePrimary(): number | null {
    const token = consume();
    if (!token) return null;
    if (token.type === "number") return Number(token.value);
    if (token.type === "ref") {
      if (peek()?.type === "colon") return null;
      return numericValueForRef(token);
    }
    if (token.type === "ident" && peek()?.type === "lparen") {
      consume();
      const values = parseFunctionArgs();
      if (!values) return null;
      if (token.value === "SUM") return values.reduce((sum, value) => sum + value, 0);
      if (token.value === "AVERAGE") return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
      if (token.value === "MIN") return values.length ? Math.min(...values) : 0;
      if (token.value === "MAX") return values.length ? Math.max(...values) : 0;
      if (token.value === "COUNT") return values.length;
      return null;
    }
    if (token.type === "lparen") {
      const value = parseExpression();
      if (peek()?.type !== "rparen") return null;
      consume();
      return value;
    }
    return null;
  }

  function parseUnary(): number | null {
    if (peek()?.type === "op" && (peek().value === "-" || peek().value === "+")) {
      const operator = consume().value;
      const value = parseUnary();
      return value == null ? null : operator === "-" ? -value : value;
    }
    return parsePrimary();
  }

  function parsePower(): number | null {
    let left = parseUnary();
    while (left != null && peek()?.type === "op" && peek().value === "^") {
      consume();
      const right = parseUnary();
      left = right == null ? null : Math.pow(left, right);
    }
    return left;
  }

  function parseMulDiv(): number | null {
    let left = parsePower();
    while (left != null && peek()?.type === "op" && (peek().value === "*" || peek().value === "/")) {
      const operator = consume().value;
      const right = parsePower();
      if (right == null) return null;
      left = operator === "*" ? left * right : right === 0 ? null : left / right;
    }
    return left;
  }

  function parseAddSub(): number | null {
    let left = parseMulDiv();
    while (left != null && peek()?.type === "op" && (peek().value === "+" || peek().value === "-")) {
      const operator = consume().value;
      const right = parseMulDiv();
      if (right == null) return null;
      left = operator === "+" ? left + right : left - right;
    }
    return left;
  }

  const result = parseExpression();
  return result != null && position === tokens.length && Number.isFinite(result) ? result : null;
}

export function evaluateSpreadsheetFormula(
  data: unknown[][],
  formula: string,
  seen = new Set<string>(),
  context?: SpreadsheetFormulaContext,
  evaluationState = createSpreadsheetFormulaEvaluationState(),
): number | null {
  const expression = normalizeSpreadsheetFormulaExpression(formula);
  const tokens = tokenizeSpreadsheetFormula(expression);
  return tokens ? evaluateSpreadsheetFormulaTokens(data, tokens, seen, context, evaluationState) : null;
}

export function evaluateSpreadsheetFormulaValue(
  data: unknown[][],
  formula: string,
  seen = new Set<string>(),
  context?: SpreadsheetFormulaContext,
  evaluationState = createSpreadsheetFormulaEvaluationState(),
): SpreadsheetFormulaValue | null {
  const expression = normalizeSpreadsheetFormulaExpression(formula);
  const tokens = tokenizeSpreadsheetFormula(expression);
  if (!tokens || tokens.length === 0) return null;
  if (tokens.length === 1 && tokens[0].type === "ref") {
    const ref = parseSpreadsheetRef(tokens[0].value, tokens[0].sheetName);
    if (!ref) return null;
    const target = resolveSpreadsheetReference(data, ref, context);
    if (!target) return null;
    const targetContext = context
      ? { ...context, currentSheetName: target.sheetName }
      : undefined;
    return getSpreadsheetFormulaValue(target.data, ref.row, ref.col, seen, targetContext, evaluationState);
  }
  return evaluateSpreadsheetFormulaTokens(data, tokens, seen, context, evaluationState);
}

export function getSpreadsheetDisplayValue(
  data: unknown[][],
  row: number,
  col: number,
  sourceDisplay?: string,
  options: SpreadsheetDisplayOptions = {},
): string {
  const value = data[row]?.[col];
  if (typeof value === "string" && value.trim().startsWith("=")) {
    const { context, numberFormat, formatNumber } = options;
    const evaluationState = options.evaluationState || createSpreadsheetFormulaEvaluationState();
    const evaluated = evaluateSpreadsheetFormulaValue(data, value, new Set([
      spreadsheetSeenKey(row, col, context),
    ]), context, evaluationState);
    if (evaluated == null) return sourceDisplay || "#ERROR";
    if (typeof evaluated !== "number") return String(evaluated);
    const cached = sourceDisplay ? parseSpreadsheetNumber(sourceDisplay) : null;
    if (cached != null && Object.is(cached, evaluated)) return sourceDisplay!;
    if (numberFormat && formatNumber) {
      try {
        return formatNumber(evaluated, numberFormat);
      } catch {
        // Fall back to a stable plain number for unsupported Excel formats.
      }
    }
    return Number.isInteger(evaluated) ? String(evaluated) : String(Number(evaluated.toFixed(6)));
  }
  return value != null ? String(value) : "";
}

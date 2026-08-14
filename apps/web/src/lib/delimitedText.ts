export interface DelimitedTextFormat {
  delimiter: "," | ";" | "\t";
  lineEnding: "\n" | "\r\n" | "\r";
  finalLineEnding: boolean;
}

function logicalDelimiterCounts(text: string, delimiter: DelimitedTextFormat["delimiter"]): number[] {
  const counts: number[] = [0];
  let quoted = false;
  for (let index = 0; index < text.length && counts.length <= 20; index += 1) {
    const character = text[index];
    if (character === '"') {
      if (quoted && text[index + 1] === '"') index += 1;
      else quoted = !quoted;
    } else if (!quoted && character === delimiter) {
      counts[counts.length - 1] += 1;
    } else if (!quoted && (character === "\n" || character === "\r")) {
      if (character === "\r" && text[index + 1] === "\n") index += 1;
      counts.push(0);
    }
  }
  return counts.filter((count) => count > 0);
}

export function detectDelimitedTextFormat(text: string): DelimitedTextFormat {
  const candidates = ([",", ";", "\t"] as const).map((delimiter) => {
    const counts = logicalDelimiterCounts(text, delimiter);
    const consistent = counts.length > 0 && counts.every((count) => count === counts[0]);
    return { delimiter, score: counts.reduce((sum, count) => sum + count, 0) + (consistent ? 1000 : 0) };
  });
  const delimiter = candidates.reduce((best, candidate) => candidate.score > best.score ? candidate : best).delimiter;
  const crlf = (text.match(/\r\n/g) || []).length;
  const withoutCrlf = text.replace(/\r\n/g, "");
  const lf = (withoutCrlf.match(/\n/g) || []).length;
  const cr = (withoutCrlf.match(/\r/g) || []).length;
  const lineEnding = crlf >= lf && crlf >= cr && crlf > 0 ? "\r\n" : cr > lf ? "\r" : "\n";
  return {
    delimiter,
    lineEnding,
    finalLineEnding: /(?:\r\n|\r|\n)$/.test(text),
  };
}

export function parseDelimitedText(
  rawText: string,
  format = detectDelimitedTextFormat(rawText),
): { rows: string[][]; format: DelimitedTextFormat } {
  const text = rawText.startsWith("\ufeff") ? rawText.slice(1) : rawText;
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let endedWithLineBreak = false;

  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    const next = text[index + 1];
    if (character === '"') {
      if (quoted && next === '"') {
        cell += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
      endedWithLineBreak = false;
    } else if (!quoted && character === format.delimiter) {
      row.push(cell);
      cell = "";
      endedWithLineBreak = false;
    } else if (!quoted && (character === "\n" || character === "\r")) {
      if (character === "\r" && next === "\n") index += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      endedWithLineBreak = true;
    } else {
      cell += character;
      endedWithLineBreak = false;
    }
  }

  if (!endedWithLineBreak || row.length > 0 || cell !== "" || rows.length === 0) {
    row.push(cell);
    rows.push(row);
  }
  return { rows, format };
}

function escapeDelimitedCell(value: unknown, delimiter: string): string {
  const text = value == null ? "" : String(value);
  return text.includes(delimiter)
    || text.includes('"')
    || /[\r\n]/.test(text)
    || text !== text.trim()
    ? `"${text.replace(/"/g, '""')}"`
    : text;
}

export function serializeDelimitedText(rows: unknown[][], format: DelimitedTextFormat): string {
  const body = rows
    .map((row) => row.map((cell) => escapeDelimitedCell(cell, format.delimiter)).join(format.delimiter))
    .join(format.lineEnding);
  return format.finalLineEnding && body ? `${body}${format.lineEnding}` : body;
}

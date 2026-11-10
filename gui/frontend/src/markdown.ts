import type { Observation } from "./types";

interface QueryResultPayload {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  complete: boolean;
  row_limit: number | null;
  total_row_count?: number | null;
}

interface ColumnInfo {
  name: string;
  type: string;
  nullable: boolean;
  default: string | null;
  primary_key: boolean;
}

interface TableDescriptionEntry {
  name: string;
  columns: ColumnInfo[];
  found: boolean;
}

interface TableSummary {
  name: string;
  column_count: number;
  columns: string[];
}

interface ErrorPayload {
  code: string;
  message: string;
}

interface EpisodeResultPayload {
  matched: boolean;
  message: string;
  reward: number;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  return String(value).replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

function table(headers: string[], rows: unknown[][]): string {
  const head = `| ${headers.join(" | ")} |`;
  const divider = `|${headers.map(() => "---").join("|")}|`;
  const body = rows.map((r) => `| ${r.map(cell).join(" | ")} |`).join("\n");
  return [head, divider, body].join("\n");
}

function queryResultMarkdown(payload: QueryResultPayload): string {
  const lines = ["## Query result", "", table(payload.columns, payload.rows), ""];
  if (payload.complete) {
    lines.push(`Result is complete: all ${payload.row_count} rows are shown.`);
  } else {
    lines.push(
      `Result is truncated: showing the first ${payload.row_count} rows (row limit ${payload.row_limit}); this is not the entire result.`,
    );
  }
  return lines.join("\n");
}

function tableDescriptionMarkdown(payload: { tables: TableDescriptionEntry[] }): string {
  const sections = ["## Table description", ""];
  for (const t of payload.tables) {
    sections.push(`### ${t.name}`, "");
    if (!t.found) {
      sections.push(`Table \`${t.name}\` was not found.`, "");
      continue;
    }
    const rows = t.columns.map((c) => [
      c.name,
      c.type,
      c.nullable ? "yes" : "no",
      c.default ?? "-",
      c.primary_key ? "PK" : "-",
    ]);
    sections.push(table(["Column", "Type", "Nullable", "Default", "Key"], rows), "");
  }
  return sections.join("\n").trimEnd() + "\n";
}

function schemaDescriptionMarkdown(payload: { tables: TableSummary[] }): string {
  const rows = payload.tables.map((t) => [t.name, t.column_count, t.columns.join(", ")]);
  return "## Schema description\n\n" + table(["Table", "Columns", "Column summary"], rows) + "\n";
}

function errorMarkdown(payload: ErrorPayload): string {
  return `**Error** (\`${payload.code}\`): ${payload.message}`;
}

function episodeResultMarkdown(payload: EpisodeResultPayload): string {
  const verdict = payload.matched ? "correct" : "incorrect";
  return `Episode ended. The submitted answer is **${verdict}**. Reward: ${payload.reward}.\n\n${payload.message}`;
}

function stepSignals(obs: Observation): string {
  if (obs.step_number === null || obs.step_number === undefined || obs.type === "episode_result") {
    return "";
  }
  const reward = obs.reward === null || obs.reward === undefined ? "-" : String(obs.reward);
  const status = obs.terminated ? "terminated" : obs.truncated ? "truncated" : "episode continues";
  return `\n\nStep ${obs.step_number} | reward ${reward} | ${status}`;
}

export function toMarkdown(obs: Observation): string {
  const payloadRenderers: Record<string, (p: unknown) => string> = {
    query_result: (p) => queryResultMarkdown(p as QueryResultPayload),
    table_description: (p) => tableDescriptionMarkdown(p as { tables: TableDescriptionEntry[] }),
    schema_description: (p) => schemaDescriptionMarkdown(p as { tables: TableSummary[] }),
    error: (p) => errorMarkdown(p as ErrorPayload),
    episode_result: (p) => episodeResultMarkdown(p as EpisodeResultPayload),
  };
  const renderer = payloadRenderers[obs.type];
  const body = renderer
    ? renderer(obs.payload)
    : `\`\`\`\n${JSON.stringify(obs.payload, null, 2)}\n\`\`\``;
  return body + stepSignals(obs);
}

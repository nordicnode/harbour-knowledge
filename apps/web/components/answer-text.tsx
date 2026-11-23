import type { ReactNode } from "react";

export type AnswerCitation = {
  evidence_id: string;
  source_title?: string;
};

const INLINE =
  /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(\s*<?((?:[^()<>\s]|\([^()\s]*\))+)>?\s*\)|<((?:https?:\/\/|mailto:)[^>\s]+)>|\[P:([^\]\s]+)\]|\[(\d{1,3})\]|\*([^*\n]+)\*|(?<![\w])_([^_\n]+)_(?![\w])|~~([^~\n]+)~~/g;

function safeHref(raw: string): string | null {
  const value = raw.trim().replace(/^<|>$/g, "");
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.toString() : null;
  } catch {
    return null;
  }
}

const linkClass =
  "font-medium text-pine underline decoration-pine/35 decoration-1 underline-offset-2 transition-colors hover:decoration-pine";

function CitationChip({ label, title }: { label: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      aria-label={title}
      className="mx-0.5 inline-flex h-[17px] min-w-[17px] items-center justify-center rounded-[4px] bg-pine-soft px-1 align-[1px] font-mono text-[9.5px] font-medium text-pine-dark"
    >
      {label}
    </span>
  );
}

function inline(
  text: string,
  keyPrefix: string,
  citationNumbers: ReadonlyMap<string, number>,
  depth = 0,
): ReactNode[] {
  if (depth > 4) return [text];

  const nodes: ReactNode[] = [];
  const pattern = new RegExp(INLINE.source, INLINE.flags);
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const key = `${keyPrefix}-${match.index}`;

    if (match[1] !== undefined) {
      nodes.push(<code key={key}>{match[1]}</code>);
    } else if (match[2] !== undefined) {
      nodes.push(<strong key={key}>{inline(match[2], key, citationNumbers, depth + 1)}</strong>);
    } else if (match[3] !== undefined && match[4] !== undefined) {
      const href = safeHref(match[4]);
      nodes.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer" className={linkClass}>
            {inline(match[3], key, citationNumbers, depth + 1)}
          </a>
        ) : (
          <span key={key}>{match[3]}</span>
        ),
      );
    } else if (match[5] !== undefined) {
      const href = safeHref(match[5]);
      nodes.push(
        href ? (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer" className={linkClass}>
            {match[5].replace(/^mailto:/, "")}
          </a>
        ) : (
          <span key={key}>{match[5]}</span>
        ),
      );
    } else if (match[6] !== undefined) {
      const number = citationNumbers.get(match[6]);
      nodes.push(
        <CitationChip
          key={key}
          label={number ?? "src"}
          title={number ? `Evidence ${number}` : "Source evidence"}
        />,
      );
    } else if (match[7] !== undefined) {
      nodes.push(<CitationChip key={key} label={match[7]} title={`Citation ${match[7]}`} />);
    } else if (match[8] !== undefined) {
      nodes.push(<em key={key}>{inline(match[8], key, citationNumbers, depth + 1)}</em>);
    } else if (match[9] !== undefined) {
      nodes.push(<em key={key}>{inline(match[9], key, citationNumbers, depth + 1)}</em>);
    } else if (match[10] !== undefined) {
      nodes.push(<del key={key}>{inline(match[10], key, citationNumbers, depth + 1)}</del>);
    }

    cursor = pattern.lastIndex;
  }

  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function alignmentClass(cell: string): string {
  if (/^:-+:$/.test(cell)) return "text-center";
  if (/^-+:$/.test(cell)) return "text-right";
  return "text-left";
}

function isTableLine(line: string): boolean {
  return /^\s*\|.*\|\s*$/.test(line);
}

function isSeparatorRow(line: string): boolean {
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function Table({
  lines,
  index,
  citationNumbers,
}: {
  lines: string[];
  index: number;
  citationNumbers: ReadonlyMap<string, number>;
}) {
  const separatorAt = lines.findIndex((line, position) => position > 0 && isSeparatorRow(line));
  const hasHeader = separatorAt > 0;
  const header = hasHeader ? splitRow(lines[separatorAt - 1]) : null;
  const alignments = hasHeader ? splitRow(lines[separatorAt]).map(alignmentClass) : [];
  const bodyStart = hasHeader ? separatorAt + 1 : 0;
  const rows = lines.slice(bodyStart).map(splitRow);
  const keyValue = !hasHeader && rows.every((row) => row.length === 2);
  const columns = header?.length ?? Math.max(...rows.map((row) => row.length), 0);

  if (!columns) return null;

  if (keyValue) {
    return (
      <div key={index} className="my-3 overflow-hidden rounded-lg border border-line">
        <table className="w-full border-collapse text-[12.5px]">
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b border-line/70 last:border-b-0">
                <th
                  scope="row"
                  className="w-[38%] bg-surface-2/60 px-3 py-2 text-left align-top font-medium text-muted"
                >
                  {inline(row[0] ?? "", `kv-${index}-${rowIndex}-0`, citationNumbers)}
                </th>
                <td className="px-3 py-2 align-top text-ink-2">
                  {inline(row[1] ?? "", `kv-${index}-${rowIndex}-1`, citationNumbers)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div key={index} className="my-3 overflow-x-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-[12.5px]">
        {header ? (
          <thead className="bg-surface-2">
            <tr>
              {header.map((cell, cellIndex) => (
                <th
                  key={cellIndex}
                  className={`border-b border-line px-2.5 py-1.5 font-semibold text-ink ${alignments[cellIndex] ?? "text-left"}`}
                >
                  {inline(cell, `th-${index}-${cellIndex}`, citationNumbers)}
                </th>
              ))}
            </tr>
          </thead>
        ) : null}
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-b border-line/60 last:border-b-0">
              {Array.from({ length: columns }, (_, cellIndex) => {
                const cell = row[cellIndex] ?? "";
                if (!header && rows.length > 1 && cellIndex === 0) {
                  return (
                    <th
                      key={cellIndex}
                      scope="row"
                      className="bg-surface-2/60 px-2.5 py-1.5 text-left align-top font-medium text-ink"
                    >
                      {inline(cell, `trh-${index}-${rowIndex}-${cellIndex}`, citationNumbers)}
                    </th>
                  );
                }
                return (
                  <td
                    key={cellIndex}
                    className={`px-2.5 py-1.5 align-top text-ink-2 ${alignments[cellIndex] ?? "text-left"}`}
                  >
                    {inline(cell, `td-${index}-${rowIndex}-${cellIndex}`, citationNumbers)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type ListItemNode = {
  content: string;
  ordered: boolean;
  indent: number;
  children: ListItemNode[];
};

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function listNodes(lines: string[]): ListItemNode[] {
  const root: ListItemNode[] = [];
  const stack: { indent: number; children: ListItemNode[] }[] = [{ indent: -1, children: root }];

  for (const line of lines) {
    const match = LIST_ITEM.exec(line);
    if (!match) continue;
    const indent = match[1].replace(/\t/g, "    ").length;
    const item: ListItemNode = {
      content: match[3],
      ordered: /^\d/.test(match[2]),
      indent,
      children: [],
    };
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
    stack[stack.length - 1].children.push(item);
    stack.push({ indent, children: item.children });
  }

  return root;
}

function renderItems(
  items: ListItemNode[],
  keyPrefix: string,
  citationNumbers: ReadonlyMap<string, number>,
): ReactNode {
  const groups: ListItemNode[][] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last[0].ordered === item.ordered) last.push(item);
    else groups.push([item]);
  }

  return groups.map((group, groupIndex) => {
    const children = group.map((item, itemIndex) => (
      <li key={`${keyPrefix}-${groupIndex}-${itemIndex}`}>
        {inline(item.content, `${keyPrefix}-${groupIndex}-${itemIndex}`, citationNumbers)}
        {item.children.length
          ? renderItems(item.children, `${keyPrefix}-${groupIndex}-${itemIndex}`, citationNumbers)
          : null}
      </li>
    ));
    return group[0].ordered ? (
      <ol key={`${keyPrefix}-group-${groupIndex}`}>{children}</ol>
    ) : (
      <ul key={`${keyPrefix}-group-${groupIndex}`}>{children}</ul>
    );
  });
}

function startsBlock(line: string): boolean {
  return (
    !line.trim() ||
    /^\s{0,3}(#{1,6})\s+/.test(line) ||
    /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line) ||
    /^\s{0,3}>/.test(line) ||
    /^\s*(```|~~~)/.test(line) ||
    isTableLine(line) ||
    LIST_ITEM.test(line)
  );
}

function renderBlocks(
  text: string,
  keyPrefix: string,
  citationNumbers: ReadonlyMap<string, number>,
): ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const nodes: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const key = `${keyPrefix}-${index}`;

    const fence = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      const marker = fence[1];
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      nodes.push(
        <pre key={key}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      nodes.push(
        <div key={key} role="heading" aria-level={level} className={`prose-h${level}`}>
          {inline(heading[2], `h-${key}`, citationNumbers)}
        </div>,
      );
      index += 1;
      continue;
    }

    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      nodes.push(<hr key={key} />);
      index += 1;
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
        index += 1;
      }
      nodes.push(
        <blockquote key={key}>
          {renderBlocks(quote.join("\n"), `q-${key}`, citationNumbers)}
        </blockquote>,
      );
      continue;
    }

    if (isTableLine(line)) {
      const table: string[] = [];
      while (index < lines.length && isTableLine(lines[index])) {
        table.push(lines[index]);
        index += 1;
      }
      nodes.push(
        <Table key={key} lines={table} index={index} citationNumbers={citationNumbers} />,
      );
      continue;
    }

    if (LIST_ITEM.test(line)) {
      const items: string[] = [];
      while (index < lines.length && LIST_ITEM.test(lines[index])) {
        items.push(lines[index]);
        index += 1;
      }
      nodes.push(
        <div key={key} className="prose-list">
          {renderItems(listNodes(items), key, citationNumbers)}
        </div>,
      );
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && !startsBlock(lines[index])) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    nodes.push(
      <p key={key}>{inline(paragraph.join(" "), `p-${key}`, citationNumbers)}</p>,
    );
  }

  return nodes;
}

export function AnswerText({
  text,
  citations,
  dense = false,
}: {
  text: string;
  citations?: readonly AnswerCitation[];
  dense?: boolean;
}) {
  const citationNumbers = new Map<string, number>();
  for (const [index, citation] of (citations ?? []).entries()) {
    if (!citationNumbers.has(citation.evidence_id)) {
      citationNumbers.set(citation.evidence_id, index + 1);
    }
  }

  const clean = (text ?? "").replace(/<!--[\s\S]*?-->/g, "").trim();
  const className = dense
    ? "prose-answer prose-dense text-[12px] leading-[1.65] text-ink-2"
    : "prose-answer text-[14px] leading-[1.72] text-ink-2";

  return <div className={className}>{renderBlocks(clean, "answer", citationNumbers)}</div>;
}

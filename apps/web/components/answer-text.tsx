import type { ReactNode } from "react";

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|\[\d+\])/g).filter(Boolean);
  return parts.map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (/^\[\d+\]$/.test(part)) {
      return (
        <span
          key={key}
          className="mx-0.5 inline-flex h-[17px] min-w-[17px] items-center justify-center rounded-[4px] bg-pine-soft px-1 align-[1px] font-mono text-[9.5px] font-medium text-pine-dark"
        >
          {part.slice(1, -1)}
        </span>
      );
    }
    return <span key={key}>{part}</span>;
  });
}

function block(text: string, index: number): ReactNode {
  const lines = text.split("\n");
  const heading = lines.length === 1 ? /^(#{1,4})\s+(.+)$/.exec(lines[0].trim()) : null;
  if (heading) {
    const level = heading[1].length;
    const className =
      level === 1
        ? "display mt-1 mb-2 text-[22px] leading-tight text-ink"
        : level === 2
          ? "display mt-5 mb-1.5 text-[18px] leading-tight text-ink"
          : "mt-4 mb-1 text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-2";
    return (
      <div key={index} className={className}>
        {inline(heading[2], `h-${index}`)}
      </div>
    );
  }
  if (lines.every((line) => line.trim().startsWith(">"))) {
    const content = lines
      .map((line) => line.replace(/^>\s?/, ""))
      .filter((line) => line.trim().length > 0)
      .join(" ");
    return (
      <blockquote
        key={index}
        className="my-3 rounded-r-md border-l-2 border-amber bg-amber-soft/70 px-3.5 py-2.5 text-[13px] leading-relaxed text-ink-2"
      >
        {inline(content, `q-${index}`)}
      </blockquote>
    );
  }

  if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
    return (
      <ul key={index}>
        {lines.map((line, lineIndex) => (
          <li key={lineIndex}>{inline(line.replace(/^\s*[-*]\s+/, ""), `li-${index}-${lineIndex}`)}</li>
        ))}
      </ul>
    );
  }

  if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
    return (
      <ol key={index}>
        {lines.map((line, lineIndex) => (
          <li key={lineIndex}>{inline(line.replace(/^\s*\d+[.)]\s+/, ""), `ol-${index}-${lineIndex}`)}</li>
        ))}
      </ol>
    );
  }

  return <p key={index}>{inline(text, `p-${index}`)}</p>;
}

export function AnswerText({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/).filter((part) => part.trim().length > 0);
  return <div className="prose-answer text-[14px] leading-[1.72] text-ink-2">{blocks.map(block)}</div>;
}

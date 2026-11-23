import Link from "next/link";
import { BookOpen, ChevronDown, FileSearch, Search as SearchIcon, SlidersHorizontal } from "lucide-react";

import { Badge, Button, Card, EmptyState, PageHeader, inputClass, selectClass } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { titleCase } from "@/lib/format";
import type { SearchResponse } from "@/lib/types";

function Highlight({ text, query }: { text: string; query: string }) {
  const terms = query
    .split(/\s+/)
    .map((term) => term.trim())
    .filter((term) => term.length > 2)
    .slice(0, 8);
  if (!terms.length) return <>{text}</>;
  const pattern = new RegExp(`(${terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  const pieces = text.split(pattern);
  return (
    <>
      {pieces.map((piece, index) =>
        terms.some((term) => piece.toLowerCase() === term.toLowerCase()) ? (
          <mark key={index} className="rounded bg-amber-soft px-0.5 text-ink">
            {piece}
          </mark>
        ) : (
          <span key={index}>{piece}</span>
        ),
      )}
    </>
  );
}

export default async function SearchPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; plan?: string; version?: string; exact?: string }>;
}) {
  const params = await searchParams;
  const q = (params.q ?? "").trim();
  const exact = params.exact === "1";
  let result: SearchResponse | null = null;
  let error: string | null = null;
  if (q.length >= 2) {
    const query = new URLSearchParams({ q, exact: String(exact) });
    if (params.plan) query.set("plan", params.plan);
    if (params.version) query.set("product_version", params.version);
    try {
      result = await apiGet<SearchResponse>(`/v1/search?${query.toString()}`);
    } catch (caught) {
      error = caught instanceof Error ? caught.message : "Search failed.";
    }
  }

  return (
    <div className="page-enter space-y-6">
      <PageHeader
        eyebrow="Search"
        title="Search sources."
        description="Find the exact passage that supports an answer. Internal material is visible here."
      />

      <Card className="px-4 py-4">
        <form method="GET" className="flex flex-col gap-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <SearchIcon size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input
                name="q"
                defaultValue={q}
                autoFocus
                placeholder="Search policies, error codes, procedures…"
                className={`${inputClass} h-10 pl-9 text-[14px]`}
              />
            </div>
            <Button type="submit" variant="primary" size="lg">
              Search
            </Button>
          </div>
          <details className="group">
            <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 text-[11.5px] text-muted hover:text-ink">
              <SlidersHorizontal size={12} /> Filters
              <ChevronDown size={12} className="transition-transform group-open:rotate-180" />
            </summary>
            <div className="flex flex-wrap items-end gap-3 pt-3">
              <label className="flex items-center gap-2 text-[11.5px] text-muted">
                <select
                  name="plan"
                  aria-label="Plan filter"
                  defaultValue={params.plan ?? ""}
                  className={`${selectClass} h-8 w-32`}
                >
                  <option value="">All plans</option>
                  <option value="team">Team</option>
                  <option value="business">Business</option>
                  <option value="enterprise">Enterprise</option>
                </select>
              </label>
              <input
                name="version"
                aria-label="Product version filter"
                defaultValue={params.version ?? ""}
                placeholder="Version (v1)"
                className={`${inputClass} h-8 w-32`}
              />
              <label className="flex h-8 items-center gap-1.5 text-[11.5px] text-muted">
                <input type="checkbox" name="exact" value="1" defaultChecked={exact} className="accent-pine" />
                Exact keyword only
              </label>
              {result ? (
                <span className="ml-auto text-[10.5px] text-faint">
                  {result.results.length} results · {result.candidates_scanned} candidates scanned · coverage{" "}
                  {Math.round(result.coverage * 100)}%
                </span>
              ) : null}
            </div>
          </details>
        </form>
      </Card>

      {error ? (
        <div className="rounded-xl border border-rust/25 bg-rust-soft px-4 py-3 text-[12px] text-rust">{error}</div>
      ) : null}

      {result?.index_warning ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber/25 bg-amber-soft/60 px-4 py-3 text-[11.5px] leading-relaxed text-ink-2">
          <SlidersHorizontal size={13} className="mt-0.5 shrink-0 text-amber" />
          <span>{result.index_warning}</span>
        </div>
      ) : null}

      {!q || q.length < 2 ? (
        <EmptyState
          title="Search the evidence library"
          description="Enter at least two characters. Try an error code, a policy phrase, or the way a customer would describe the problem."
          icon={<FileSearch size={19} />}
        />
      ) : result && result.results.length ? (
        <div className="space-y-3">
          {result.results.map((hit) => (
            <Card key={hit.passage_id} className="overflow-hidden">
              <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2/50 px-4 py-2.5">
                <FileSearch size={13} className="text-muted" />
                <Link
                  href={`/sources/${hit.source_id}`}
                  className="min-w-0 truncate text-[11.5px] font-medium text-ink hover:text-pine"
                >
                  {hit.document_title}
                </Link>
                <span className="text-[10px] text-faint">› {hit.section_path}</span>
                <div className="ml-auto flex items-center gap-2">
                  <Badge tone="outline">{titleCase(hit.source_tier)}</Badge>
                  <Badge tone={hit.visibility === "public" ? "pine" : "sky"}>
                    {hit.visibility === "public" ? "Public" : "Internal"}
                  </Badge>
                  <Badge tone="neutral">v{hit.document_version}</Badge>
                </div>
              </div>
              <div className="px-4 py-4">
                <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-ink-2">
                  <Highlight text={hit.matched_text.slice(0, 1200)} query={q} />
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line/70 pt-2.5 text-[10.5px] text-faint">
                  <span>Source: {hit.source_title}</span>
                  {hit.topic_name ? <span>Topic: {hit.topic_name}</span> : null}
                  <span className="ml-auto font-mono">score {hit.score.toFixed(3)}</span>
                  <span className="font-mono">query coverage {Math.round(hit.coverage * 100)}%</span>
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : result ? (
        <EmptyState
          title="No passage matched"
          description="A zero-result search is useful: it is the start of a content gap. Ask the question, abstain, and let the gap dashboard cluster it."
          action={
            <Link href={`/ask?q=${encodeURIComponent(q)}`} className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-pine hover:underline">
              <BookOpen size={13} /> Ask this instead
            </Link>
          }
          icon={<FileSearch size={19} />}
        />
      ) : null}
    </div>
  );
}

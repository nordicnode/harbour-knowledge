import Link from "next/link";
import { ArrowLeft, Sparkles } from "lucide-react";

import { AnswerComposer } from "@/components/answer-composer";
import { PageHeader } from "@/components/ui";
import { ApiError, apiGet } from "@/lib/api";
import type { SearchResponse, Topic } from "@/lib/types";

export default async function NewAnswerPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const query = (q ?? "").trim();
  const { topics } = await apiGet<{ topics: Topic[] }>("/v1/topics");
  let results: SearchResponse["results"] = [];
  let searchError: string | null = null;
  if (query.length >= 2) {
    try {
      const search = await apiGet<SearchResponse>(`/v1/search?${new URLSearchParams({ q: query })}`);
      results = search.results;
    } catch (error) {
      searchError = error instanceof ApiError ? error.message : "Search failed.";
    }
  }

  return (
    <div className="page-enter mx-auto max-w-4xl space-y-6">
      <Link href="/knowledge" className="inline-flex items-center gap-1.5 text-[11.5px] text-muted hover:text-ink">
        <ArrowLeft size={13} /> Knowledge
      </Link>
      <PageHeader
        eyebrow="Knowledge"
        title="Draft an answer."
        description="Pick the passages that should support the answer. The draft quotes them verbatim and stays in review until a person approves it."
      />
      <div className="flex items-start gap-2 rounded-xl border border-pine/20 bg-pine-soft/50 px-4 py-3 text-[11.5px] leading-relaxed text-pine-dark">
        <Sparkles size={14} className="mt-0.5 shrink-0" />
        <p>The draft quotes your selected passages. Edit the prose afterwards; the evidence stays pinned to this version.</p>
      </div>
      {searchError ? (
        <div className="rounded-xl border border-rust/25 bg-rust-soft px-4 py-3 text-[12px] text-rust">{searchError}</div>
      ) : null}
      <AnswerComposer query={query} results={results} topics={topics} searched={query.length >= 2} />
    </div>
  );
}

import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { AskPanel } from "@/components/ask-panel";
import { SourceUpload } from "@/components/source-upload";
import { Card, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import type { Overview } from "@/lib/types";

export default async function AskPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const overview = await apiGet<Overview>("/v1/overview");
  const { stats } = overview;

  if (stats.sources === 0) {
    return (
      <div className="page-enter mx-auto max-w-3xl space-y-6">
        <PageHeader
          eyebrow="Get started"
          title="Add your knowledge."
          description="Drop in the documents your team already relies on. Harbour reads them, connects the evidence, and prepares answers automatically."
        />
        <SourceUpload />
      </div>
    );
  }

  return (
    <div className="page-enter mx-auto max-w-4xl space-y-5">
      <PageHeader
        eyebrow="Ask"
        title="Ask Harbour."
        description="Ask in plain language. Every reply comes from your sources, with the exact evidence attached."
      />

      <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-[11.5px] text-muted">
        <span>
          <span className="font-semibold text-ink">{stats.sources}</span> source
          {stats.sources === 1 ? "" : "s"}
        </span>
        <span className="hidden h-3 w-px bg-line sm:block" />
        <span>
          <span className="font-semibold text-ink">{stats.approved_answers}</span> answers ready
        </span>
        {stats.pending_review > 0 ? (
          <>
            <span className="hidden h-3 w-px bg-line sm:block" />
            <Link href="/review" className="inline-flex items-center gap-1.5 hover:text-ink">
              <span className="font-semibold text-ink">{stats.pending_review}</span>
              {stats.pending_review === 1 ? "item needs" : "items need"} a look
              <ArrowRight size={11} className="text-faint" />
            </Link>
          </>
        ) : (
          <>
            <span className="hidden h-3 w-px bg-line sm:block" />
            <span className="text-pine">Everything is up to date</span>
          </>
        )}
        <Link href="/knowledge" className="ml-auto inline-flex items-center gap-1 font-medium text-pine hover:underline">
          Your knowledge <ArrowRight size={11} />
        </Link>
      </Card>

      <AskPanel initialQuestion={q ?? ""} />
    </div>
  );
}

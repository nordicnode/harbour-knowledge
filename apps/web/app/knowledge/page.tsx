import Link from "next/link";
import { ArrowRight, BookOpen, ChevronDown, Inbox } from "lucide-react";

import { SourceList } from "@/components/source-list";
import { SourceProgress } from "@/components/source-progress";
import { SourceUpload } from "@/components/source-upload";
import { Badge, Card, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { sourceState } from "@/lib/source-state";
import type { Answer, Meta, Overview, SourceRow } from "@/lib/types";

export default async function KnowledgePage({
  searchParams,
}: {
  searchParams: Promise<{ topic?: string }>;
}) {
  const { topic } = await searchParams;
  const [overview, answerData, sourceData, meta] = await Promise.all([
    apiGet<Overview>("/v1/overview"),
    apiGet<{ answers: Answer[] }>("/v1/answers"),
    apiGet<{ sources: SourceRow[] }>("/v1/sources"),
    apiGet<Meta>("/v1/meta"),
  ]);

  const sources = sourceData.sources;
  const canEdit = meta.member.role === "admin" || meta.member.role === "editor";
  const readyAnswers = answerData.answers.filter((answer) => answer.status === "approved");
  const answers = readyAnswers.filter((answer) => !topic || answer.topic_slug === topic);
  const pending = overview.stats.pending_review;
  const processing = sources.filter((source) => sourceState(source) === "reading").length;
  const topicName = topic ? answers[0]?.topic_name ?? null : null;

  return (
    <div className="page-enter mx-auto max-w-4xl space-y-7">
      <PageHeader
        eyebrow="Your knowledge"
        title="Add knowledge. We’ll handle the rest."
        description="Drop in the documents and pages your team already uses. Harbour reads them, organises them, and turns them into answers — no setup required."
      />

      <SourceUpload />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-1 text-[11.5px] text-muted">
        <span>
          <span className="font-semibold text-ink">{sources.length}</span> source
          {sources.length === 1 ? "" : "s"}
        </span>
        <span className="h-3 w-px bg-line" />
        <span>
          <span className="font-semibold text-ink">{readyAnswers.length}</span> answer
          {readyAnswers.length === 1 ? "" : "s"} ready
        </span>
        {!pending ? (
          <>
            <span className="h-3 w-px bg-line" />
            <span className="inline-flex items-center gap-1.5 text-pine">
              <span className="h-1.5 w-1.5 rounded-full bg-pine" /> Everything is handled
            </span>
          </>
        ) : null}
        <span className="ml-auto">
          <SourceProgress active={processing > 0} />
        </span>
      </div>

      {pending ? (
        <Link
          href="/review"
          className="group flex items-center gap-3 rounded-xl border border-amber/25 bg-amber-soft/60 px-4 py-3.5 transition-colors hover:border-amber/40 hover:bg-amber-soft"
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-amber/10 text-amber">
            <Inbox size={15} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[12.5px] font-medium text-ink">
              {pending} decision{pending === 1 ? "" : "s"} need{pending === 1 ? "s" : ""} you
            </span>
            <span className="mt-0.5 block text-[11px] leading-snug text-amber">
              Harbour handled everything else. These are the few calls it won’t make alone.
            </span>
          </span>
          <span className="inline-flex shrink-0 items-center gap-1 text-[11.5px] font-medium text-amber">
            Review <ArrowRight size={12} className="transition-transform group-hover:translate-x-0.5" />
          </span>
        </Link>
      ) : null}

      <SourceList sources={sources} canEdit={canEdit} processing={processing} />

      {answers.length ? (
        <details className="group" open={Boolean(topic)}>
          <summary className="flex cursor-pointer list-none items-center gap-2 px-1 py-1">
            <BookOpen size={15} className="text-muted" />
            <span className="display text-[20px] leading-tight text-ink">
              What Harbour can answer
            </span>
            <span className="text-[11.5px] text-faint">{answers.length}</span>
            {topicName ? <Badge tone="outline">{topicName}</Badge> : null}
            <ChevronDown
              size={15}
              className="ml-auto text-faint transition-transform group-open:rotate-180"
            />
          </summary>
          <Card className="mt-3 overflow-hidden">
            {answers.map((answer) => (
              <Link
                key={answer.id}
                href={`/knowledge/${answer.id}`}
                className="group flex items-center gap-3 border-b border-line/75 px-4 py-3.5 transition-colors last:border-b-0 hover:bg-surface-2 md:px-5"
              >
                <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink group-hover:text-pine">
                  {answer.question}
                </span>
                <span className="shrink-0 text-[10.5px] text-faint">{answer.topic_name ?? "General"}</span>
                <ArrowRight size={12} className="shrink-0 text-faint transition-transform group-hover:translate-x-0.5" />
              </Link>
            ))}
          </Card>
        </details>
      ) : null}
    </div>
  );
}

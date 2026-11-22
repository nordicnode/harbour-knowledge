import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  ChevronDown,
  FileCode2,
  FileImage,
  FileText,
  FileType2,
  Globe2,
  Inbox,
  Table2,
} from "lucide-react";

import { SourceProgress } from "@/components/source-progress";
import { SourceUpload } from "@/components/source-upload";
import { Badge, Card, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import type { Answer, Overview, SourceRow } from "@/lib/types";

const SOURCE_ICONS: Record<string, typeof FileText> = {
  pdf: FileText,
  docx: FileType2,
  pptx: FileType2,
  html: FileCode2,
  url: Globe2,
  image: FileImage,
  xlsx: Table2,
  text: FileText,
  markdown: FileText,
  epub: FileText,
};

type SourceState = "reading" | "ready" | "failed" | "removed";

function sourceState(source: SourceRow): SourceState {
  if (source.status === "withdrawn") return "removed";
  if (source.status === "failed" || source.status === "quarantined" || source.job_status === "failed") {
    return "failed";
  }
  if (
    source.status === "processing" ||
    source.job_status === "running" ||
    source.job_status === "queued"
  ) {
    return "reading";
  }
  return "ready";
}

const SOURCE_STATE_META: Record<SourceState, { label: string; tone: "pine" | "sky" | "rust" | "neutral" }> = {
  reading: { label: "Reading…", tone: "sky" },
  ready: { label: "Ready", tone: "pine" },
  failed: { label: "Couldn't read", tone: "rust" },
  removed: { label: "Removed", tone: "neutral" },
};

export default async function KnowledgePage({
  searchParams,
}: {
  searchParams: Promise<{ topic?: string }>;
}) {
  const { topic } = await searchParams;
  const [overview, answerData, sourceData] = await Promise.all([
    apiGet<Overview>("/v1/overview"),
    apiGet<{ answers: Answer[] }>("/v1/answers"),
    apiGet<{ sources: SourceRow[] }>("/v1/sources"),
  ]);

  const sources = sourceData.sources;
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

      <section className="space-y-3">
        <div className="flex items-end justify-between gap-4 px-1">
          <div>
            <h2 className="display text-[20px] leading-tight text-ink">Your sources</h2>
            <p className="mt-0.5 text-[11.5px] text-muted">
              Everything Harbour has read. Click one to see what it found.
            </p>
          </div>
          {processing ? (
            <Badge tone="sky">{processing} reading</Badge>
          ) : null}
        </div>

        {sources.length ? (
          <Card className="overflow-hidden">
            {sources.map((source) => {
              const Icon = SOURCE_ICONS[source.kind] ?? FileText;
              const state = sourceState(source);
              const meta = SOURCE_STATE_META[state];
              return (
                <Link
                  key={source.id}
                  href={`/sources/${source.id}`}
                  className="group flex items-center gap-3.5 border-b border-line/75 px-4 py-3.5 transition-colors last:border-b-0 hover:bg-surface-2 md:px-5"
                >
                  <span
                    className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${
                      state === "failed"
                        ? "border-rust/20 bg-rust-soft text-rust"
                        : "border-line bg-paper text-ink-2"
                    }`}
                  >
                    {state === "failed" ? <AlertTriangle size={15} /> : <Icon size={15} />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-medium text-ink group-hover:text-pine">
                      {source.title}
                    </span>
                    <span className="mt-0.5 block truncate text-[10.5px] text-faint">
                      {source.topic_name ? `${source.topic_name} · ` : ""}
                      Added {relativeTime(source.created_at)}
                      {source.job_error ? ` · ${source.job_error}` : ""}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-3">
                    {state === "ready" && source.passage_count ? (
                      <span className="hidden text-[10.5px] text-faint sm:inline">
                        {source.passage_count} passage{source.passage_count === 1 ? "" : "s"}
                      </span>
                    ) : null}
                    <Badge tone={meta.tone} dot>
                      {meta.label}
                    </Badge>
                  </span>
                </Link>
              );
            })}
          </Card>
        ) : (
          <div className="rounded-xl border border-dashed border-line-strong px-5 py-8 text-center">
            <p className="text-[13px] font-medium text-ink">No sources yet</p>
            <p className="mt-1 text-[11.5px] text-muted">
              Add your first document or page above. Harbour starts working immediately.
            </p>
          </div>
        )}
      </section>

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

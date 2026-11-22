import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, CheckCircle2, ChevronDown, Clock3, FileText, TriangleAlert } from "lucide-react";

import { AnswerEditor } from "@/components/answer-editor";
import { Badge, Card, PageHeader } from "@/components/ui";
import { ApiError, apiGet } from "@/lib/api";
import { formatDate, relativeTime, scopeLabel } from "@/lib/format";
import type { AnswerDetail, Topic } from "@/lib/types";

export default async function AnswerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let detail: AnswerDetail;
  let topics: Topic[];
  try {
    [detail, { topics }] = await Promise.all([
      apiGet<AnswerDetail>(`/v1/answers/${id}`),
      apiGet<{ topics: Topic[] }>("/v1/topics"),
    ]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const { answer, evidence, versions, review_items } = detail;
  const unverified = evidence.filter((item) => !item.quote_verified).length;
  const status =
    answer.status === "approved"
      ? { label: "Live", tone: "pine" as const }
      : answer.status === "needs_recheck"
        ? { label: "Needs a recheck", tone: "amber" as const }
        : answer.status === "in_review"
          ? { label: "Waiting to publish", tone: "sky" as const }
          : { label: "Draft", tone: "neutral" as const };

  return (
    <div className="page-enter mx-auto max-w-5xl space-y-6">
      <Link href="/knowledge" className="inline-flex items-center gap-1.5 text-[11.5px] text-muted hover:text-ink">
        <ArrowLeft size={13} /> Knowledge
      </Link>

      <PageHeader
        eyebrow="Answer"
        title={answer.question}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone={status.tone} dot>
              {status.label}
            </Badge>
            <Badge tone={answer.visibility === "public" ? "pine" : "sky"}>
              {answer.visibility === "public" ? "Customers can see this" : "Internal only"}
            </Badge>
            {answer.topic_name ? <Badge tone="outline">{answer.topic_name}</Badge> : null}
            {review_items.length ? (
              <Link href="/review" className="inline-flex items-center gap-1 text-[11px] font-medium text-amber hover:underline">
                <Clock3 size={11} /> In Review
              </Link>
            ) : null}
          </span>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_310px]">
        <div className="space-y-5">
          <AnswerEditor answer={answer} topics={topics} evidence={evidence} />

          <Card className="px-4 py-3">
            <details className="group">
              <summary className="flex cursor-pointer list-none items-center gap-2 text-[11.5px] font-medium text-muted hover:text-ink">
                <Clock3 size={13} />
                Version history
                <span className="text-faint">({versions.length})</span>
                <ChevronDown size={13} className="ml-auto transition-transform group-open:rotate-180" />
              </summary>
              <div className="mt-3 border-t border-line pt-2">
                {versions.map((version) => (
                  <div key={version.version} className="flex items-start gap-3 border-b border-line/60 py-2.5 last:border-b-0">
                    <span className="mt-0.5 font-mono text-[9.5px] text-faint">v{version.version}</span>
                    <div className="min-w-0 flex-1">
                      <p className="text-[11.5px] text-ink-2">
                        {version.change_reason ?? "Version saved"}
                        {version.version === answer.current_version ? (
                          <span className="ml-2 text-[9.5px] uppercase tracking-wider text-pine">current</span>
                        ) : null}
                      </p>
                      <p className="mt-0.5 text-[10px] text-faint">
                        {version.changed_by_name ?? "Harbour"} · {relativeTime(version.created_at)}
                      </p>
                    </div>
                  </div>
                ))}
                <p className="pt-2.5 text-[10px] text-faint">
                  {scopeLabel(answer.plan_scope, answer.version_scope)} · reviewed every{" "}
                  {answer.review_interval_days} days · next {formatDate(answer.next_review_at)}
                </p>
              </div>
            </details>
          </Card>
        </div>

        <aside>
          <Card className="overflow-hidden">
            <div className="flex items-center gap-2 border-b border-line px-4 py-3">
              <FileText size={14} className="text-muted" />
              <h2 className="text-[12.5px] font-semibold text-ink">Where this comes from</h2>
              <Badge tone={unverified ? "rust" : "pine"} className="ml-auto">
                {unverified ? `${unverified} to check` : `${evidence.length} verified`}
              </Badge>
            </div>
            {evidence.length ? (
              evidence.map((item) => (
                <div key={item.id} className="border-b border-line/70 px-4 py-3.5 last:border-b-0">
                  <p className="text-[12px] leading-relaxed text-ink-2">“{item.quote}”</p>
                  <div className="mt-2 flex items-center gap-1.5">
                    {item.quote_verified ? (
                      <CheckCircle2 size={11} className="text-pine" />
                    ) : (
                      <TriangleAlert size={11} className="text-rust" />
                    )}
                    <Link
                      href={`/sources/${item.source_id}`}
                      className="min-w-0 flex-1 truncate text-[10.5px] text-muted hover:text-pine"
                    >
                      {item.source_title} · {formatDate(item.effective_from)}
                    </Link>
                  </div>
                </div>
              ))
            ) : (
              <p className="px-4 py-5 text-[11.5px] leading-relaxed text-muted">
                No source quotes attached yet. Add some before publishing.
              </p>
            )}
            <div className="border-t border-line bg-surface-2/60 px-4 py-2.5 text-[10px] leading-snug text-faint">
              Each quote is checked against the source again at publish time.
            </div>
          </Card>
        </aside>
      </div>
    </div>
  );
}

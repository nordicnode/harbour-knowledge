import Link from "next/link";
import { notFound } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  ChevronDown,
  Clock3,
  FileText,
  Layers,
  Link2,
} from "lucide-react";

import { AnswerText } from "@/components/answer-text";
import { SourceActions } from "@/components/source-actions";
import { SourceProgress } from "@/components/source-progress";
import { Badge, Card, PageHeader } from "@/components/ui";
import { ApiError, apiGet } from "@/lib/api";
import { formatDate, relativeTime, titleCase } from "@/lib/format";
import type { Meta, SourceDetail, SourcePassage } from "@/lib/types";

function hasMarkdown(text: string): boolean {
  return /(^|\n)\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|\|)|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\n]+\)/.test(text);
}

function PassageBlock({ passage }: { passage: SourcePassage }) {
  return (
    <div className="border-t border-line/70 px-4 py-3 first:border-t-0">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className="rounded border border-line bg-paper px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-faint">
          {passage.level}
        </span>
        {passage.page_ref ? <span className="text-[10px] text-faint">{passage.page_ref}</span> : null}
        {passage.status !== "current" ? <Badge tone="rust">{passage.status}</Badge> : null}
      </div>
      {hasMarkdown(passage.text) ? (
        <AnswerText text={passage.text} dense />
      ) : (
        <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-ink-2">{passage.text}</p>
      )}
    </div>
  );
}

export default async function SourcePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let detail: SourceDetail;
  let meta: Meta;
  try {
    [detail, meta] = await Promise.all([apiGet<SourceDetail>(`/v1/sources/${id}`), apiGet<Meta>("/v1/meta")]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const { source, document, passages, jobs, alias_of } = detail;
  const sections = new Map<string, SourcePassage[]>();
  for (const passage of passages) {
    const key = passage.section_path || "Document";
    sections.set(key, [...(sections.get(key) ?? []), passage]);
  }
  const latestJob = jobs[0];
  const removed = source.status === "withdrawn";
  const failed =
    !removed &&
    (source.status === "failed" || source.status === "quarantined" || latestJob?.status === "failed");
  const processing =
    !removed &&
    !failed &&
    (source.status === "processing" || latestJob?.status === "running" || latestJob?.status === "queued");
  const canEdit = meta.member.role === "admin" || meta.member.role === "editor";
  const chunkCount = passages.filter((passage) => passage.level === "chunk").length;
  const statusLabel = alias_of
    ? "Duplicate"
    : removed
      ? "Removed"
      : failed
        ? "Couldn't read"
        : processing
          ? "Reading…"
          : "Ready";
  const statusTone = alias_of
    ? "neutral"
    : removed
      ? "neutral"
      : failed
        ? "rust"
        : processing
          ? "sky"
          : "pine";

  return (
    <div className="page-enter mx-auto max-w-5xl space-y-6">
      <Link href="/knowledge" className="inline-flex items-center gap-1.5 text-[11.5px] text-muted hover:text-ink">
        <ArrowLeft size={13} /> Knowledge
      </Link>

      <PageHeader
        eyebrow="Source"
        title={source.title}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone={statusTone as "pine" | "sky" | "rust" | "neutral"} dot>
              {statusLabel}
            </Badge>
            <Badge tone={source.visibility === "public" ? "pine" : "sky"}>
              {source.visibility === "public" ? "Customers can see this" : "Internal only"}
            </Badge>
            {document?.topic_name ? <Badge tone="outline">{document.topic_name}</Badge> : null}
            <span className="text-[11.5px] text-faint">Added {relativeTime(source.created_at)}</span>
          </span>
        }
        actions={<SourceActions id={source.id} canEdit={canEdit} />}
      />

      {alias_of ? (
        <div className="flex items-center gap-2 rounded-xl border border-sky/20 bg-sky-soft px-4 py-3 text-[12px] text-sky">
          <Link2 size={14} /> This is a duplicate of{" "}
          <Link href={`/sources/${alias_of.id}`} className="font-medium underline decoration-sky/30 underline-offset-2">
            {alias_of.title}
          </Link>
          . Harbour did not create a second copy.
        </div>
      ) : null}

      {failed ? (
        <div className="flex items-start gap-3 rounded-xl border border-rust/25 bg-rust-soft px-4 py-3.5">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rust" />
          <div>
            <p className="text-[12.5px] font-medium text-rust">Harbour couldn’t read this source</p>
            <p className="mt-0.5 text-[11.5px] leading-relaxed text-rust/85">
              {latestJob?.error ?? "The file may be a scan or use a format the parser cannot read."} Try again, or replace it
              with a text-based version.
            </p>
          </div>
        </div>
      ) : null}

      {processing ? (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-sky/20 bg-sky-soft px-4 py-3 text-[12px] text-sky">
          <Clock3 size={14} /> Harbour is reading this source. This page updates on its own.
          <SourceProgress active={processing} />
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
        <Card className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line px-4 py-3 md:px-5">
            <FileText size={14} className="text-muted" />
            <h2 className="text-[12.5px] font-semibold text-ink">What Harbour read</h2>
            <span className="ml-auto text-[10px] text-faint">{relativeTime(source.ingested_at)}</span>
          </div>
          {document ? (
            <div className="max-h-[620px] overflow-y-auto px-4 py-4 md:px-5">
              <AnswerText text={document.content_md} />
              {detail.content_truncated ? (
                <p className="mt-4 border-t border-line pt-3 text-[11px] text-faint">
                  Preview cut at 60,000 characters. The searchable version is complete.
                </p>
              ) : null}
            </div>
          ) : (
            <p className="px-5 py-10 text-center text-[12px] text-muted">
              {processing ? "Reading now…" : "No readable text was found in this source."}
            </p>
          )}
        </Card>

        <aside className="space-y-4">
          <Card className="px-4 py-4">
            <p className="text-[10px] font-semibold uppercase tracking-[0.13em] text-faint">Details</p>
            <dl className="mt-3 space-y-2.5 text-[11.5px]">
              <div className="flex items-start justify-between gap-4">
                <dt className="text-muted">Topic</dt>
                <dd className="text-right font-medium text-ink">
                  {source.topic_name ?? document?.topic_name ?? "Not filed"}
                </dd>
              </div>
              <div className="flex items-start justify-between gap-4">
                <dt className="text-muted">Searchable passages</dt>
                <dd className="text-right font-medium text-ink">{chunkCount}</dd>
              </div>
              <div className="flex items-start justify-between gap-4">
                <dt className="text-muted">Effective</dt>
                <dd className="text-right font-medium text-ink">{formatDate(document?.effective_from)}</dd>
              </div>
              <div className="flex items-start justify-between gap-4">
                <dt className="text-muted">Added</dt>
                <dd className="text-right text-ink-2">{formatDate(source.created_at)}</dd>
              </div>
            </dl>
            <details className="group mt-3 border-t border-line pt-3">
              <summary className="flex cursor-pointer list-none items-center text-[10.5px] font-medium text-muted hover:text-ink">
                Technical details
                <ChevronDown size={12} className="ml-auto transition-transform group-open:rotate-180" />
              </summary>
              <dl className="mt-2 space-y-2 text-[10.5px]">
                <div className="flex justify-between gap-3">
                  <dt className="text-faint">Type</dt>
                  <dd className="font-mono text-ink-2">{source.kind.toUpperCase()}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-faint">Size</dt>
                  <dd className="font-mono text-ink-2">
                    {source.size_bytes ? `${(source.size_bytes / 1024).toFixed(1)} KB` : "—"}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-faint">Version</dt>
                  <dd className="font-mono text-ink-2">{document ? `v${document.version}` : "—"}</dd>
                </div>
                <div>
                  <dt className="text-faint">SHA-256</dt>
                  <dd className="mt-0.5 break-all font-mono text-[9.5px] text-ink-2">{source.hash_sha256}</dd>
                </div>
                {source.origin_url ? (
                  <div>
                    <dt className="text-faint">Origin</dt>
                    <dd className="mt-0.5 break-all text-pine">{source.origin_url}</dd>
                  </div>
                ) : null}
              </dl>
              {jobs.length ? (
                <div className="mt-3 border-t border-line pt-2.5">
                  <p className="text-[9.5px] uppercase tracking-[0.1em] text-faint">Processing runs</p>
                  <div className="mt-1.5 space-y-1.5">
                    {jobs.slice(0, 4).map((job) => (
                      <div key={job.id} className="flex items-start justify-between gap-3 text-[10px]">
                        <span className="text-muted">
                          {job.stage ?? "—"}
                          {job.error ? <span className="mt-0.5 block text-rust">{job.error}</span> : null}
                        </span>
                        <span className="shrink-0 text-faint">{relativeTime(job.created_at)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </details>
          </Card>
        </aside>
      </div>

      <details className="group">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-1 py-1">
          <Layers size={15} className="text-muted" />
          <span className="display text-[19px] leading-tight text-ink">Searchable passages</span>
          <span className="text-[11px] text-faint">{chunkCount}</span>
          <ChevronDown
            size={15}
            className="ml-auto text-faint transition-transform group-open:rotate-180"
          />
        </summary>
        <Card className="mt-3 overflow-hidden">
          {sections.size ? (
            [...sections.entries()].map(([section, items], index) => {
              const sectionPassage = items.find((item) => item.level === "section");
              const chunks = items.filter((item) => item.level === "chunk");
              return (
                <details key={section} open={index === 0} className="group/section border-b border-line last:border-b-0">
                  <summary className="flex cursor-pointer list-none items-center gap-2.5 bg-surface-2/50 px-4 py-3 hover:bg-surface-2">
                    <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink">
                      {section || "Untitled section"}
                    </span>
                    <span className="text-[10px] text-faint">
                      {chunks.length || 1} passage{(chunks.length || 1) === 1 ? "" : "s"}
                    </span>
                    <ChevronDown
                      size={13}
                      className="text-faint transition-transform group-open/section:rotate-180"
                    />
                  </summary>
                  {chunks.length ? (
                    chunks.map((chunk) => <PassageBlock key={chunk.id} passage={chunk} />)
                  ) : sectionPassage ? (
                    <PassageBlock passage={sectionPassage} />
                  ) : null}
                </details>
              );
            })
          ) : (
            <p className="px-5 py-8 text-center text-[12px] text-muted">
              No searchable passages yet. A queued or failed run leaves this empty.
            </p>
          )}
        </Card>
      </details>
    </div>
  );
}

import Link from "next/link";
import { ArrowUpRight, MessageCircleQuestion, Target } from "lucide-react";

import { GapActions } from "@/components/gap-actions";
import { Badge, Card, EmptyState, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { channelLabel, formatDate, relativeTime } from "@/lib/format";
import type { Answer, Gap } from "@/lib/types";

const TABS = [
  { label: "Open", value: "open" },
  { label: "Answered", value: "answered" },
  { label: "Won't answer", value: "wont_answer" },
];

export default async function GapsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const { status: rawStatus } = await searchParams;
  const status = TABS.some((tab) => tab.value === rawStatus) ? rawStatus! : "open";
  const [data, answers] = await Promise.all([
    apiGet<{ gaps: Gap[]; totals: { status: string; count: number }[] }>(`/v1/gaps?status=${status}`),
    apiGet<{ answers: Answer[] }>("/v1/answers?status=approved"),
  ]);
  const totals = Object.fromEntries(data.totals.map((row) => [row.status, Number(row.count)]));
  const openQuestions = data.gaps.reduce((sum, gap) => sum + Number(gap.count), 0);

  return (
    <div className="page-enter space-y-6">
      <PageHeader
        eyebrow="Coverage"
        title="Coverage gaps."
        description="Questions the base could not answer, grouped by intent so one fix closes many conversations."
      />

      <Card className="flex flex-wrap items-center gap-2 px-4 py-3">
        {TABS.map((tab) => (
          <Link
            key={tab.value}
            href={`/gaps?status=${tab.value}`}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[11.5px] transition-colors ${
              status === tab.value
                ? "border-pine bg-pine text-white"
                : "border-line bg-surface text-ink-2 hover:border-line-strong hover:bg-surface-2"
            }`}
          >
            {tab.label}
            <span className={status === tab.value ? "text-white/70" : "text-faint"}>{totals[tab.value] ?? 0}</span>
          </Link>
        ))}
        <span className="ml-auto hidden items-center gap-1.5 text-[10.5px] text-faint sm:flex">
          <Target size={12} />
          {status === "open"
            ? `${openQuestions} questions across ${data.gaps.length} cluster${data.gaps.length === 1 ? "" : "s"}`
            : `${data.gaps.length} cluster${data.gaps.length === 1 ? "" : "s"}`}
        </span>
      </Card>

      {data.gaps.length ? (
        <div className="space-y-3">
          {data.gaps.map((gap, index) => (
            <Card key={gap.id} className="overflow-hidden">
              <div className="flex flex-col gap-4 px-5 py-4 md:flex-row md:items-start">
                <div className="flex shrink-0 items-center gap-3 md:w-12 md:flex-col md:items-center">
                  <div className="flex h-11 w-11 items-center justify-center rounded-full border border-rust/20 bg-rust-soft">
                    <span className="display text-[17px] text-rust">{gap.count}</span>
                  </div>
                  <span className="text-[9.5px] uppercase tracking-[0.08em] text-faint">asks</span>
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[9.5px] uppercase tracking-[0.08em] text-faint">
                      cluster {String(index + 1).padStart(2, "0")}
                    </span>
                    {gap.status === "answered" ? <Badge tone="pine">answered</Badge> : null}
                    {gap.status === "wont_answer" ? <Badge tone="neutral">won&apos;t answer</Badge> : null}
                  </div>
                  <p className="mt-1 text-[14px] font-medium leading-snug text-ink">{gap.representative_question}</p>
                  {gap.normalised_question !== gap.representative_question ? (
                    <p className="mt-1 text-[11px] text-muted">Normalised: {gap.normalised_question}</p>
                  ) : null}
                  <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[10.5px] text-faint">
                    <span className="inline-flex items-center gap-1">
                      <MessageCircleQuestion size={11} />
                      {gap.channels.length ? gap.channels.map(channelLabel).join(" · ") : "Unknown channel"}
                    </span>
                    <span>First seen {formatDate(gap.first_seen_at)}</span>
                    <span>Last seen {relativeTime(gap.last_seen_at)}</span>
                    {gap.linked_question ? (
                      <span className="inline-flex items-center gap-1 text-pine">
                        Closed by <span className="underline decoration-pine/25 underline-offset-2">{gap.linked_question}</span>
                      </span>
                    ) : null}
                  </div>
                </div>

                <div className="shrink-0 md:pt-1">
                  <GapActions gap={gap} approvedAnswers={answers.answers} />
                </div>
              </div>
              {gap.status === "open" ? (
                <div className="flex items-center gap-1.5 border-t border-line/70 bg-surface-2/40 px-5 py-2 text-[10px] text-faint">
                  <ArrowUpRight size={11} /> Fix the source, then write and approve an answer. New questions cluster here
                  until then.
                </div>
              ) : null}
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          title={status === "open" ? "No open gaps" : status === "answered" ? "No answered gaps yet" : "Nothing dismissed"}
          description={
            status === "open"
              ? "Every recent question either matched approved knowledge or has not been asked yet. Ask something the base cannot answer to see the loop close."
              : "Gaps you close or dismiss will appear here with the decision attached."
          }
          icon={<Target size={19} />}
        />
      )}
    </div>
  );
}

import Link from "next/link";
import { BookOpen, FolderTree, Hash, Network, UserRound } from "lucide-react";

import { Badge, ButtonLink, Card, EmptyState, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { titleCase } from "@/lib/format";
import type { Topic } from "@/lib/types";

export default async function TopicsPage() {
  const { topics } = await apiGet<{ topics: Topic[] }>("/v1/topics");
  const covered = topics.filter((topic) => (topic.approved_count ?? 0) > 0).length;
  const totalApproved = topics.reduce((sum, topic) => sum + (topic.approved_count ?? 0), 0);

  return (
    <div className="page-enter space-y-6">
      <PageHeader
        eyebrow="Knowledge"
        title="Topics."
        description={`${topics.length} topics · ${covered} with approved answers · ${totalApproved} approved across the workspace`}
      />

      {topics.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {topics.map((topic) => {
            const coverage =
              (topic.approved_count ?? 0) + (topic.answer_count ?? 0) > 0
                ? Math.round(((topic.approved_count ?? 0) / Math.max(1, topic.answer_count ?? 0)) * 100)
                : 0;
            return (
              <Card key={topic.id} className="flex flex-col overflow-hidden">
                <div className="border-b border-line bg-surface-2/50 px-4 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[14px] font-semibold text-ink">{topic.name}</p>
                      <p className="mt-0.5 flex items-center gap-1 font-mono text-[9.5px] text-faint">
                        <Hash size={9} /> {topic.slug}
                      </p>
                    </div>
                    <Badge tone={(topic.approved_count ?? 0) ? "pine" : "outline"}>
                      {topic.approved_count ?? 0} approved
                    </Badge>
                  </div>
                </div>
                <div className="flex flex-1 flex-col px-4 py-3.5">
                  <p className="line-clamp-2 min-h-[34px] text-[11.5px] leading-relaxed text-muted">
                    {topic.description || "No description."}
                  </p>
                  <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line/70 pt-3">
                    <div>
                      <p className="display text-[18px] leading-none text-ink">{topic.answer_count ?? 0}</p>
                      <p className="mt-1 text-[9px] uppercase tracking-[0.08em] text-faint">Answers</p>
                    </div>
                    <div>
                      <p className="display text-[18px] leading-none text-ink">{topic.source_count ?? 0}</p>
                      <p className="mt-1 text-[9px] uppercase tracking-[0.08em] text-faint">Sources</p>
                    </div>
                    <div>
                      <p className="display text-[18px] leading-none text-ink">{coverage}%</p>
                      <p className="mt-1 text-[9px] uppercase tracking-[0.08em] text-faint">Approved</p>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-faint">
                    <span className="inline-flex items-center gap-1">
                      <UserRound size={10} /> {topic.owner_name ?? "No owner"}
                    </span>
                    <span>Review every {topic.review_interval_days}d</span>
                  </div>
                </div>
                <div className="flex items-center justify-between border-t border-line px-4 py-2.5">
                  <Link
                    href={`/knowledge?topic=${topic.slug}`}
                    className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-pine hover:underline"
                  >
                    <BookOpen size={12} /> Open answers
                  </Link>
                  {topic.parent_id ? (
                    <span className="inline-flex items-center gap-1 text-[10px] text-faint">
                      <Network size={10} /> nested
                    </span>
                  ) : (
                    <span className="text-[10px] text-faint">{titleCase(topic.slug)}</span>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState
          title="No topics yet"
          description="Topics are created when sources are classified. Ingest a policy or help-centre page to populate the taxonomy."
          action={<ButtonLink href="/knowledge#add" variant="primary">Add a source</ButtonLink>}
          icon={<FolderTree size={19} />}
        />
      )}
    </div>
  );
}

import { Inbox, Plus } from "lucide-react";

import { ReviewQueue } from "@/components/review-queue";
import { ButtonLink, Card, EmptyState, PageHeader } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { reviewItemGroup } from "@/lib/format";
import type { Meta, ReviewItem } from "@/lib/types";

export default async function ReviewPage() {
  const [data, meta] = await Promise.all([
    apiGet<{ items: ReviewItem[]; counts: { kind: string; count: number }[] }>("/v1/review-items"),
    apiGet<Meta>("/v1/meta"),
  ]);
  const items = [...data.items].sort((a, b) => {
    const groupA = reviewItemGroup(a);
    const groupB = reviewItemGroup(b);
    if (groupA !== groupB) return groupA === "call" ? -1 : 1;
    return b.priority - a.priority || a.created_at.localeCompare(b.created_at);
  });
  const callCount = items.filter((item) => reviewItemGroup(item) === "call").length;
  const readyCount = items.length - callCount;
  const canDecide = meta.member.role === "admin" || meta.member.role === "editor";

  return (
    <div className="page-enter space-y-6">
      <PageHeader
        eyebrow="Review"
        title="A few decisions."
        description="Harbour reads, organises, and drafts by itself. Only the calls it can’t make alone land here."
      />

      <Card className="flex flex-wrap items-center gap-x-5 gap-y-2 px-5 py-3.5 text-[12px] text-muted">
        <span className="inline-flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-amber" />
          <span>
            <span className="font-semibold text-ink">{callCount}</span> only you can decide
          </span>
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-pine" />
          <span>
            <span className="font-semibold text-ink">{readyCount}</span> ready to approve
          </span>
        </span>
        <span className="ml-auto text-[10.5px] text-faint">Everything else is automatic.</span>
      </Card>

      <ReviewQueue
        items={items}
        canDecide={canDecide}
        emptyState={
          <EmptyState
            title="Nothing needs you"
            description="Harbour will surface an item here only when it genuinely needs a person."
            action={
              <ButtonLink href="/knowledge#add" variant="primary">
                <Plus size={14} /> Add knowledge
              </ButtonLink>
            }
            icon={<Inbox size={19} />}
          />
        }
      />
    </div>
  );
}

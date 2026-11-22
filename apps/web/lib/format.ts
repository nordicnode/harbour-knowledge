import type { AnswerStatus, ConfidenceBand, ReviewItem } from "./types";

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function formatDateShort(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function titleCase(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    new_answer: "Answer ready",
    edit_proposal: "Edit ready",
    conflict: "Sources disagree",
    dedup_merge: "Duplicate source",
    topic_change: "Topic change",
    stale_answer: "Answer may be stale",
    low_ocr: "Couldn't read source",
    change_impact: "Source changed",
    entity_merge: "Name overlap",
    glossary_promotion: "Glossary suggestion",
  };
  return labels[kind] ?? titleCase(kind);
}

export type ReviewGroup = "call" | "ready";

export function reviewGroup(kind: string): ReviewGroup {
  return kind === "conflict" || kind === "low_ocr" || kind === "stale_answer" ? "call" : "ready";
}

export function reviewItemGroup(item: ReviewItem): ReviewGroup {
  if (reviewGroup(item.kind) === "call") return "call";
  // A fallback question has no canonical draft yet, so there is nothing to approve.
  if (item.kind === "new_answer" && !item.answer && !item.payload?.answer_id) return "call";
  return "ready";
}

export function statusLabel(status: AnswerStatus | string): string {
  const labels: Record<string, string> = {
    draft: "Draft",
    in_review: "In review",
    approved: "Approved",
    needs_recheck: "Needs recheck",
    archived: "Archived",
    pending: "Pending",
    processing: "Processing",
    active: "Active",
    failed: "Failed",
    quarantined: "Quarantined",
    withdrawn: "Withdrawn",
    queued: "Queued",
    running: "Running",
    succeeded: "Succeeded",
    open: "Open",
    answered: "Answered",
    wont_answer: "Won't answer",
  };
  return labels[status] ?? titleCase(status);
}

export function confidenceLabel(band: ConfidenceBand | string | null | undefined): string {
  if (band === "high") return "High confidence";
  if (band === "medium") return "Review recommended";
  return "Low confidence";
}

export function confidenceSentence(band: ConfidenceBand | string | null | undefined, mode?: string): string {
  if (mode === "canonical") {
    return "Grounded in an approved canonical answer and verified evidence.";
  }
  if (band === "high") return "Strong evidence, citations verified.";
  if (band === "medium") return "Partial evidence. Check the sources before sending.";
  return "Not enough evidence to answer safely.";
}

export function scopeLabel(planScope: string[], versionScope: string[]): string {
  const plans = planScope.length ? planScope.map(titleCase).join(", ") : "All plans";
  const versions = versionScope.length ? versionScope.map((value) => value.toUpperCase()).join(", ") : "All versions";
  return `${plans} · ${versions}`;
}

export function channelLabel(channel: string): string {
  const labels: Record<string, string> = {
    web: "Agent assist",
    public: "Help centre",
    widget: "Widget",
    discord: "Discord",
  };
  return labels[channel] ?? titleCase(channel);
}

export function truncate(value: string, length = 160): string {
  if (value.length <= length) return value;
  return `${value.slice(0, length).trimEnd()}…`;
}

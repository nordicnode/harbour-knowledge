import type { SourceRow } from "./types";

export type SourceState = "reading" | "ready" | "failed" | "removed";

/**
 * A withdrawn or failed source is never "reading", even if it still carries a
 * stale job status. Keep this precedence shared so counters cannot disagree
 * with the badge shown on the row.
 */
export function sourceState(source: SourceRow): SourceState {
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

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronDown,
  Clock3,
  FilePenLine,
  GitPullRequestArrow,
  Inbox,
  Loader2,
  PenLine,
  Sparkles,
  X,
} from "lucide-react";

import { decideReviewAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import { formatDate, kindLabel, relativeTime } from "@/lib/format";
import type { ReviewItem } from "@/lib/types";
import { AnswerText } from "./answer-text";
import { Badge, Button, Card, textareaClass } from "./ui";

const ICONS: Record<string, typeof AlertTriangle> = {
  conflict: AlertTriangle,
  new_answer: Sparkles,
  edit_proposal: PenLine,
  change_impact: GitPullRequestArrow,
  stale_answer: Clock3,
  low_ocr: FilePenLine,
};

const HEADING: Record<string, string> = {
  conflict: "Which source is right?",
  "new_answer": "Answer ready to publish",
  edit_proposal: "Edit ready to publish",
  change_impact: "This answer may be out of date",
  stale_answer: "This answer needs a recheck",
  low_ocr: "Harbour couldn't read this source",
};

export function ReviewCard({
  item,
  canDecide,
  active = false,
  onDecided,
}: {
  item: ReviewItem;
  canDecide: boolean;
  active?: boolean;
  onDecided?: (result: {
    id: string;
    decision: "approve" | "reject" | "defer";
    expiresAt?: string;
  }) => void;
}) {
  const router = useRouter();
  const [note, setNote] = useState("");
  const [showNote, setShowNote] = useState(false);
  const [showResolution, setShowResolution] = useState(false);
  const [resolution, setResolution] = useState("");
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const [pending, startTransition] = useTransition();
  const busyRef = useRef(false);
  const Icon = ICONS[item.kind] ?? Inbox;
  const payload = item.payload ?? {};
  const answer = item.answer;

  function decide(decision: "approve" | "reject" | "defer", explicitResolution?: string) {
    if (busyRef.current) return;
    busyRef.current = true;
    startTransition(async () => {
      const response = await decideReviewAction({
        id: item.id,
        decision,
        note: note || undefined,
        resolution: (explicitResolution ?? resolution) || undefined,
      });
      if (response.ok) {
        setMessage({ text: "Decision recorded.", error: false });
        onDecided?.({
          id: item.id,
          decision,
          expiresAt: (response.data as { undo_expires_at?: string } | undefined)?.undo_expires_at,
        });
        router.refresh();
      } else {
        busyRef.current = false;
        setMessage({ text: response.error, error: true });
      }
    });
  }

  const conflict = item.conflict;
  const isConflict = item.kind === "conflict" && Boolean(conflict);
  const isNewAnswer = item.kind === "new_answer";
  const isEdit = item.kind === "edit_proposal";
  const resolutionReady = resolution.trim().length >= 8;
  const tierRank: Record<string, number> = {
    system_of_record: 4,
    official_doc: 3,
    wiki: 2,
    chat_export: 1,
  };
  const scoredSides = conflict
    ? [
        {
          tier: tierRank[conflict.source_tier_a ?? ""] ?? 0,
          date: conflict.effective_from_a ? Date.parse(conflict.effective_from_a) : 0,
        },
        {
          tier: tierRank[conflict.source_tier_b ?? ""] ?? 0,
          date: conflict.effective_from_b ? Date.parse(conflict.effective_from_b) : 0,
        },
      ]
    : [];
  const suggestedIndex =
    scoredSides.length === 2
      ? scoredSides[0].tier !== scoredSides[1].tier
        ? scoredSides[0].tier > scoredSides[1].tier
          ? 0
          : 1
        : scoredSides[0].date !== scoredSides[1].date
          ? scoredSides[0].date > scoredSides[1].date
            ? 0
            : 1
          : null
      : null;

  useEffect(() => {
    function onDecision(event: Event) {
      const detail = (event as CustomEvent<{ id: string; decision: "approve" | "reject" | "defer" }>).detail;
      if (!detail || detail.id !== item.id || busyRef.current) return;

      if (detail.decision === "approve" && isConflict && !resolutionReady) {
        setShowResolution(true);
        setMessage({ text: "Choose a source, or write why both apply.", error: false });
        requestAnimationFrame(() => {
          document.querySelector<HTMLElement>(`[data-review-note="${item.id}"]`)?.focus();
        });
        return;
      }
      if (detail.decision === "approve" && isNewAnswer && !answer) {
        setMessage({ text: "Harbour has not drafted this one yet.", error: false });
        return;
      }
      decide(detail.decision);
    }

    function onNote(event: Event) {
      const detail = (event as CustomEvent<{ id: string }>).detail;
      if (!detail || detail.id !== item.id) return;
      setShowNote(true);
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(`[data-review-note="${item.id}"]`)?.focus();
      });
    }

    window.addEventListener("review:decision", onDecision);
    window.addEventListener("review:note", onNote);
    return () => {
      window.removeEventListener("review:decision", onDecision);
      window.removeEventListener("review:note", onNote);
    };
  }, [answer, isConflict, isNewAnswer, item.id, note, resolution, resolutionReady]);

  const evidenceCount = answer?.evidence_count ?? item.suggested_evidence?.length ?? 0;
  const heading = HEADING[item.kind] ?? kindLabel(item.kind);

  return (
    <Card
      className={cn(
        "overflow-hidden transition-shadow",
        active && "border-pine/50 shadow-[0_6px_24px_rgba(29,91,73,0.07)] ring-2 ring-pine/10",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-2 border-b border-line px-4 py-2.5",
          active ? "bg-pine-soft/45" : "bg-surface-2/60",
        )}
      >
        <Icon size={13} strokeWidth={1.9} className="text-ink-2" />
        <span className="text-[10.5px] font-semibold uppercase tracking-[0.11em] text-ink-2">
          {kindLabel(item.kind)}
        </span>
        <span className="ml-auto text-[10px] text-faint">{relativeTime(item.created_at)}</span>
      </div>

      <div className="px-4 py-4 md:px-5">
        {isConflict ? (
          <>
            <h3 className="display text-[20px] leading-tight text-ink">Which source is right?</h3>
            <p className="mt-1 text-[12.5px] leading-relaxed text-muted">
              Two current sources disagree. Keep the one Harbour should use, or say that both apply.
              {suggestedIndex !== null ? " Harbour has marked the source it would keep." : ""}
            </p>
            <div className="mt-3.5 grid gap-2.5 sm:grid-cols-2">
              {[
                {
                  quote: conflict?.quote_a,
                  doc: conflict?.document_a,
                  source: conflict?.source_a,
                  date: conflict?.effective_from_a,
                },
                {
                  quote: conflict?.quote_b,
                  doc: conflict?.document_b,
                  source: conflict?.source_b,
                  date: conflict?.effective_from_b,
                },
              ].map((side, index) => (
                <div
                  key={index}
                  className={
                    index === suggestedIndex
                      ? "flex flex-col rounded-lg border border-pine/30 bg-pine-soft/25 p-3.5"
                      : "flex flex-col rounded-lg border border-line bg-surface-2 p-3.5"
                  }
                >
                  <div className="flex items-center gap-2">
                    <p className="text-[9px] font-semibold uppercase tracking-[0.12em] text-faint">
                      Option {index + 1}
                    </p>
                    {index === suggestedIndex ? <Badge tone="pine">Suggested</Badge> : null}
                  </div>
                  <p className="mt-1.5 text-[12.5px] font-medium leading-snug text-ink">
                    {side.source ?? side.doc ?? `Source ${index + 1}`}
                  </p>
                  <p className="mt-0.5 text-[10px] text-faint">
                    {side.doc && side.source ? `${side.doc} · ` : ""}
                    {side.date ? `Effective ${formatDate(side.date)}` : "Undated"}
                  </p>
                  <p className="mt-2 line-clamp-4 min-h-[48px] text-[11.5px] leading-relaxed text-ink-2">
                    {side.quote ?? "No quote recorded."}
                  </p>
                  {canDecide ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() =>
                        decide(
                          "approve",
                          `Keep "${side.source ?? side.doc ?? `source ${index + 1}`}" as the current source.`,
                        )
                      }
                      disabled={pending}
                      className="mt-3 w-full"
                      aria-label={`Keep ${side.source ?? side.doc ?? `source ${index + 1}`}`}
                    >
                      {pending ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                      Keep this
                    </Button>
                  ) : null}
                </div>
              ))}
            </div>
            {showResolution ? (
              <div className="mt-3">
                <textarea
                  value={resolution}
                  onChange={(event) => setResolution(event.target.value)}
                  rows={2}
                  placeholder="Why both sources apply, or how you settled the difference…"
                  aria-label="Conflict resolution"
                  data-review-note={item.id}
                  className={textareaClass}
                />
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => decide("approve")}
                  disabled={pending || !resolutionReady}
                  className="mt-2"
                >
                  {pending ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                  Save resolution
                </Button>
              </div>
            ) : null}
          </>
        ) : isNewAnswer ? (
          <>
            <h3 className="display text-[21px] leading-tight text-ink">
              {answer?.question ?? payload.question ?? "New answer"}
            </h3>
            <p className="mt-1 text-[12px] leading-relaxed text-muted">
              {payload.summary ?? "Harbour drafted this from your sources."}
            </p>
            <div className="mt-3 rounded-lg border border-line bg-surface-2 px-3.5 py-3">
              <AnswerText text={answer?.answer_md ?? payload.answer_md ?? "No draft attached yet."} />
            </div>
            {item.suggested_evidence?.length ? (
              <details className="group mt-2.5">
                <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[11px] font-medium text-muted hover:text-ink">
                  Where this came from
                  <ChevronDown size={12} className="transition-transform group-open:rotate-180" />
                </summary>
                <div className="mt-2 space-y-2">
                  {item.suggested_evidence.slice(0, 3).map((passage) => (
                    <div key={passage.id} className="rounded-md border border-line px-3 py-2">
                      <p className="line-clamp-2 text-[11px] leading-snug text-ink-2">{passage.text}</p>
                      <p className="mt-1 text-[10px] text-faint">{passage.document_title}</p>
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
          </>
        ) : isEdit ? (
          <>
            <h3 className="display text-[21px] leading-tight text-ink">
              {answer?.question ?? payload.question ?? "Answer edit"}
            </h3>
            <p className="mt-1 text-[12px] leading-relaxed text-muted">
              {payload.summary ?? "An edited version is waiting to go live."}
            </p>
            <div className="mt-3 rounded-lg border border-line bg-surface-2 px-3.5 py-3">
              <AnswerText text={item.suggested_md ?? answer?.answer_md ?? "No answer text attached."} />
            </div>
            {item.suggested_md && answer?.answer_md ? (
              <details className="group mt-2.5">
                <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[11px] font-medium text-muted hover:text-ink">
                  Compare with the live version
                  <ChevronDown size={12} className="transition-transform group-open:rotate-180" />
                </summary>
                <div className="mt-2 rounded-md border border-line px-3 py-2.5">
                  <p className="mb-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-faint">
                    Live now
                  </p>
                  <AnswerText text={answer.answer_md} />
                </div>
              </details>
            ) : null}
          </>
        ) : (
          <>
            <h3 className="display text-[20px] leading-tight text-ink">
              {heading}
            </h3>
            <p className="mt-1 text-[12.5px] leading-relaxed text-muted">
              {answer?.question ?? item.summary ?? payload.summary}
            </p>
            {item.kind === "change_impact" || item.kind === "stale_answer" ? (
              <p className="mt-2 text-[11.5px] leading-relaxed text-amber">
                {payload.detail
                  ? String(payload.detail)
                  : "A source changed. Check the answer still matches, then republish it."}
              </p>
            ) : null}
            {payload.error ? (
              <p className="mt-2 rounded-md border border-rust/20 bg-rust-soft px-3 py-2 font-mono text-[10.5px] leading-snug text-rust">
                {String(payload.error)}
              </p>
            ) : null}
          </>
        )}

        {canDecide ? (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line/80 pt-3.5">
            {isConflict ? (
              <>
                {!showResolution ? (
                  <button
                    type="button"
                    onClick={() => setShowResolution(true)}
                    className="rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:text-ink"
                  >
                    Both apply…
                  </button>
                ) : null}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => decide("reject")}
                  disabled={pending}
                  aria-keyshortcuts="r"
                  title="Dismiss this conflict (R)"
                >
                  <X size={13} /> Not a conflict
                </Button>
              </>
            ) : isNewAnswer ? (
              <>
                {answer ? (
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => decide("approve")}
                    disabled={pending}
                    aria-keyshortcuts="a"
                    title="Publish this answer (A)"
                  >
                    {pending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                    Publish answer
                  </Button>
                ) : (
                  <Link
                    href={`/knowledge/new?q=${encodeURIComponent(String(payload.question ?? ""))}`}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-pine px-3 text-[12.5px] font-medium text-white hover:bg-pine-dark"
                  >
                    Draft the answer <ArrowRight size={12} />
                  </Link>
                )}
                {answer ? (
                  <Link
                    href={`/knowledge/${answer.id}`}
                    className="inline-flex h-8 items-center rounded-lg px-2.5 text-[12px] font-medium text-ink-2 hover:bg-paper-deep"
                  >
                    Edit first
                  </Link>
                ) : null}
                <Button variant="ghost" size="sm" onClick={() => decide("reject")} disabled={pending}>
                  <X size={13} /> Discard
                </Button>
              </>
            ) : isEdit ? (
              <>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => decide("approve")}
                  disabled={pending}
                  aria-keyshortcuts="a"
                  title="Publish this edit (A)"
                >
                  {pending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                  Publish edit
                </Button>
                {answer ? (
                  <Link
                    href={`/knowledge/${answer.id}`}
                    className="inline-flex h-8 items-center rounded-lg px-2.5 text-[12px] font-medium text-ink-2 hover:bg-paper-deep"
                  >
                    Edit first
                  </Link>
                ) : null}
                <Button variant="ghost" size="sm" onClick={() => decide("reject")} disabled={pending}>
                  <X size={13} /> Discard
                </Button>
              </>
            ) : item.kind === "change_impact" || item.kind === "stale_answer" ? (
              <>
                {answer ? (
                  <Link
                    href={`/knowledge/${answer.id}`}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-pine px-3 text-[12.5px] font-medium text-white hover:bg-pine-dark"
                  >
                    Check the answer <ArrowRight size={12} />
                  </Link>
                ) : null}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => decide("approve")}
                  disabled={pending}
                  aria-keyshortcuts="a"
                  title="Mark this answer as checked (A)"
                >
                  {pending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                  Still accurate
                </Button>
              </>
            ) : item.kind === "low_ocr" ? (
              <Button variant="ghost" size="sm" onClick={() => decide("reject")} disabled={pending}>
                <X size={13} /> Dismiss
              </Button>
            ) : (
              <>
                <Button variant="primary" size="sm" onClick={() => decide("approve")} disabled={pending}>
                  {pending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                  Looks right
                </Button>
                <Button variant="ghost" size="sm" onClick={() => decide("reject")} disabled={pending}>
                  <X size={13} /> No
                </Button>
              </>
            )}

            <Button
              variant="quiet"
              size="sm"
              onClick={() => decide("defer")}
              disabled={pending}
              aria-keyshortcuts="d"
              title="Leave this for later (D)"
            >
              Later
            </Button>
            {!isConflict && !showNote ? (
              <button
                type="button"
                onClick={() => setShowNote(true)}
                className="rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:text-ink"
              >
                Add a note
              </button>
            ) : null}
          </div>
        ) : (
          <div className="mt-4 border-t border-line/80 pt-3 text-[11px] text-muted">
            An editor or admin needs to decide this one.
          </div>
        )}

        {showNote && !isConflict && canDecide ? (
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Optional note for the audit trail"
            aria-label="Decision note"
            data-review-note={item.id}
            autoFocus
            className="mt-2.5 h-8 w-full rounded-md border border-line bg-surface px-2.5 text-[11.5px]"
          />
        ) : null}

        {isNewAnswer && evidenceCount ? (
          <p className="mt-2.5 text-[10.5px] text-faint">
            Grounded in {evidenceCount} source quote{evidenceCount === 1 ? "" : "s"}.
          </p>
        ) : null}

        {message ? (
          <p
            role="status"
            aria-live="polite"
            className={cn("mt-2 text-[11px]", message.error ? "text-rust" : "text-pine")}
          >
            {message.text}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

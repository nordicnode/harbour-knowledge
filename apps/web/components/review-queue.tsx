"use client";

import { Fragment, useCallback, useEffect, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Check, Clock3, Keyboard, Loader2, Undo2, X } from "lucide-react";

import { undoReviewAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import { reviewItemGroup } from "@/lib/format";
import type { ReviewItem } from "@/lib/types";
import { ReviewCard } from "./review-item";

type Decision = "approve" | "reject" | "defer";

type UndoToast = {
  id: string;
  decision: Decision;
  label: string;
  startedAt: number;
  expiresAt: number;
};

const DECISION_TONE: Record<Decision, string> = {
  approve: "bg-pine-soft/15 text-[#8fd0b3]",
  reject: "bg-rust/20 text-[#f0a696]",
  defer: "bg-amber/20 text-[#e9c18c]",
};

function Key({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-[10px] leading-none text-ink-2">
      {children}
    </kbd>
  );
}

function itemLabel(item: ReviewItem): string {
  return String(
    item.answer?.question ??
      item.payload?.question ??
      item.summary ??
      item.kind.replace(/_/g, " "),
  );
}

export function ReviewQueue({
  items,
  canDecide,
  emptyState,
}: {
  items: ReviewItem[];
  canDecide: boolean;
  emptyState?: React.ReactNode;
}) {
  const router = useRouter();
  const [activeId, setActiveId] = useState<string | null>(items[0]?.id ?? null);
  const [showHelp, setShowHelp] = useState(false);
  const [toast, setToast] = useState<UndoToast | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [undoPending, startUndo] = useTransition();
  const lastIndex = useRef(0);
  const lastDecisionAt = useRef(0);

  const focusCard = useCallback((id: string) => {
    requestAnimationFrame(() => {
      const element = document.querySelector<HTMLElement>(`[data-review-id="${id}"]`);
      if (!element) return;
      element.scrollIntoView({ block: "center", behavior: "smooth" });
      element.focus({ preventScroll: true });
    });
  }, []);

  const select = useCallback(
    (id: string, scroll = true) => {
      setActiveId(id);
      if (scroll) focusCard(id);
    },
    [focusCard],
  );

  const move = useCallback(
    (delta: number) => {
      if (!items.length) return;
      const currentIndex = items.findIndex((item) => item.id === activeId);
      const base = currentIndex >= 0 ? currentIndex : lastIndex.current;
      const nextIndex = (base + delta + items.length) % items.length;
      lastIndex.current = nextIndex;
      select(items[nextIndex].id);
    },
    [activeId, items, select],
  );

  const handleDecided = useCallback(
    (result: { id: string; decision: Decision; expiresAt?: string }) => {
      const now = Date.now();
      const parsed = result.expiresAt ? Date.parse(result.expiresAt) : Number.NaN;
      const expiresAt = Number.isFinite(parsed) ? parsed : now + 15_000;
      const decidedItem = items.find((item) => item.id === result.id);
      setUndoError(null);
      setClock(now);
      setToast({
        id: result.id,
        decision: result.decision,
        label: decidedItem ? itemLabel(decidedItem) : "Review decision",
        startedAt: now,
        expiresAt,
      });
    },
    [items],
  );

  useEffect(() => {
    if (!toast) return;
    const tick = () => {
      const now = Date.now();
      setClock(now);
      if (now >= toast.expiresAt) {
        setToast(null);
        setUndoError(null);
      }
    };
    tick();
    const timer = window.setInterval(tick, 250);
    return () => window.clearInterval(timer);
  }, [toast]);

  const undoDecision = useCallback(() => {
    if (!toast || undoPending) return;
    const target = toast;
    setUndoError(null);
    startUndo(async () => {
      const response = await undoReviewAction(target.id);
      if (response.ok) {
        setToast(null);
        router.refresh();
      } else {
        setUndoError(response.error);
      }
    });
  }, [router, startUndo, toast, undoPending]);

  const secondsLeft = toast ? Math.max(0, Math.ceil((toast.expiresAt - clock) / 1000)) : 0;
  const remainingPercent = toast
    ? Math.max(0, Math.min(100, ((toast.expiresAt - clock) / Math.max(1, toast.expiresAt - toast.startedAt)) * 100))
    : 0;
  const ToastIcon = toast?.decision === "approve" ? Check : toast?.decision === "reject" ? X : Clock3;
  const toastDecision = toast?.decision === "approve" ? "Approved" : toast?.decision === "reject" ? "Rejected" : "Deferred";

  useEffect(() => {
    if (!items.length) {
      if (activeId !== null) setActiveId(null);
      return;
    }

    const index = items.findIndex((item) => item.id === activeId);
    if (index >= 0) {
      lastIndex.current = index;
      return;
    }

    const nextIndex = Math.min(lastIndex.current, items.length - 1);
    const nextId = items[nextIndex]?.id ?? items[0].id;
    setActiveId(nextId);
    focusCard(nextId);
  }, [activeId, focusCard, items]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const inField = Boolean(target?.closest("input, textarea, select, [contenteditable='true']"));

      if (event.key === "Escape") {
        if (showHelp) {
          setShowHelp(false);
          return;
        }
        if (inField) {
          target?.blur();
          if (activeId) focusCard(activeId);
        }
        return;
      }

      if (inField) return;
      if (event.altKey || event.ctrlKey || event.metaKey) return;

      if (event.key === "?") {
        event.preventDefault();
        setShowHelp((value) => !value);
        return;
      }

      const key = event.key.toLowerCase();
      if (key === "u" && toast) {
        event.preventDefault();
        undoDecision();
        return;
      }
      if (key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        move(1);
        return;
      }
      if (key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        move(-1);
        return;
      }

      if (!activeId) return;

      if (key === "n") {
        event.preventDefault();
        window.dispatchEvent(new CustomEvent("review:note", { detail: { id: activeId } }));
        return;
      }

      if (!canDecide) return;

      if (key === "a" || key === "r" || key === "d") {
        // A decision re-renders and moves the cursor to the next item. Ignore
        // key repeat and rapid re-presses so a held key cannot clear the queue.
        if (event.repeat || Date.now() - lastDecisionAt.current < 500) return;
        lastDecisionAt.current = Date.now();
        event.preventDefault();
        const decision: Decision = key === "a" ? "approve" : key === "r" ? "reject" : "defer";
        window.dispatchEvent(
          new CustomEvent("review:decision", {
            detail: { id: activeId, decision },
          }),
        );
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeId, canDecide, focusCard, move, showHelp, toast, undoDecision]);

  return (
    <div className="space-y-3">
      {items.length ? (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-surface-2/60 px-4 py-2.5 text-[11px] text-muted">
            <span className="inline-flex items-center gap-1.5 font-medium text-ink-2">
              <Keyboard size={13} /> Keyboard
            </span>
            <span className="hidden items-center gap-1.5 sm:inline-flex">
              <Key>J</Key>
              <Key>K</Key> move
            </span>
            {canDecide ? (
              <>
                <span className="hidden items-center gap-1.5 sm:inline-flex">
                  <Key>A</Key> publish
                </span>
                <span className="hidden items-center gap-1.5 sm:inline-flex">
                  <Key>R</Key> discard
                </span>
                <span className="hidden items-center gap-1.5 sm:inline-flex">
                  <Key>D</Key> later
                </span>
              </>
            ) : null}
            <span className="hidden items-center gap-1.5 md:inline-flex">
              <Key>N</Key> note
            </span>
            <button
              type="button"
              onClick={() => setShowHelp((value) => !value)}
              aria-expanded={showHelp}
              className="ml-auto inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-pine transition-colors hover:bg-pine-soft/60"
            >
              {showHelp ? "Hide shortcuts" : "All shortcuts"}
            </button>
          </div>

          {showHelp ? (
            <div className="grid gap-2 rounded-xl border border-line bg-surface px-4 py-3 text-[11.5px] text-muted sm:grid-cols-2">
              <p>
                <Key>J</Key> / <Key>K</Key> or <Key>↓</Key> / <Key>↑</Key> — move between items
              </p>
              <p>
                <Key>A</Key> — publish or approve{canDecide ? "" : " (editors only)"}
              </p>
              <p>
                <Key>R</Key> — discard or reject{canDecide ? "" : " (editors only)"}
              </p>
              <p>
                <Key>D</Key> — leave it for later{canDecide ? "" : " (editors only)"}
              </p>
              <p>
                <Key>N</Key> — jump to the decision note
              </p>
              <p>
                <Key>Esc</Key> — leave a note, close this panel
              </p>
              <p>
                <Key>U</Key> — undo the last decision, while the toast is showing
              </p>
            </div>
          ) : null}
        </>
      ) : (
        emptyState
      )}

      {items.length ? (
        <div className="space-y-4">
          {items.map((item, index) => {
            const active = item.id === activeId;
            const group = reviewItemGroup(item);
            const showGroup = index === 0 || reviewItemGroup(items[index - 1]) !== group;
            return (
              <Fragment key={item.id}>
                {showGroup ? (
                  <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 px-1 pt-2">
                    <h2 className="display text-[19px] leading-tight text-ink">
                      {group === "call" ? "Only you can decide" : "Harbour handled the rest"}
                    </h2>
                    <p className="text-[11px] text-muted">
                      {group === "call"
                        ? "Contradictions or sources Harbour couldn't read."
                        : "Prepared answers, edits, and checks. One click each."}
                    </p>
                  </div>
                ) : null}
                <div
                  id={item.id}
                  data-review-id={item.id}
                  tabIndex={-1}
                  role="group"
                  aria-label={`Review item: ${itemLabel(item)}`}
                  aria-current={active ? "true" : undefined}
                  onMouseDown={() => select(item.id, false)}
                  onFocusCapture={() => select(item.id, false)}
                  className="scroll-mt-24 rounded-[14px] outline-none"
                >
                  <ReviewCard item={item} canDecide={canDecide} active={active} onDecided={handleDecided} />
                </div>
              </Fragment>
            );
          })}
        </div>
      ) : null}

      {toast ? (
        <div className="pointer-events-none fixed inset-x-0 bottom-5 z-50 flex justify-center px-4 sm:px-6">
          <div
            role="status"
            aria-live="polite"
            className="pointer-events-auto w-full max-w-[430px] overflow-hidden rounded-[14px] border border-white/10 bg-[#0b100c] text-white shadow-[0_18px_50px_rgba(0,0,0,0.55)]"
          >
            <div className="flex items-center gap-3 px-4 py-3">
              <span
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
                  DECISION_TONE[toast.decision],
                )}
              >
                <ToastIcon size={15} strokeWidth={2.2} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-[12.5px] font-medium leading-snug">
                  {toastDecision} — {toast.label}
                </p>
                <p className="text-[11px] leading-snug text-white/55">
                  Decision saved. Undo available for <span aria-hidden="true">{secondsLeft}s</span>
                  <span className="sr-only">15 seconds</span>.
                </p>
                {undoError ? (
                  <p role="alert" className="mt-1 text-[11px] leading-snug text-[#f0a696]">
                    {undoError}
                  </p>
                ) : null}
              </div>
              <button
                type="button"
                onClick={undoDecision}
                disabled={undoPending}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-white/10 px-2.5 py-1.5 text-[11.5px] font-medium text-white transition-colors hover:bg-white/20 disabled:opacity-60"
              >
                {undoPending ? <Loader2 size={13} className="animate-spin" /> : <Undo2 size={13} />}
                {undoPending ? "Undoing" : "Undo"}
                <span aria-hidden="true" className="ml-0.5 rounded border border-white/20 px-1 font-mono text-[9px] leading-[14px] text-white/60">
                  U
                </span>
              </button>
              <button
                type="button"
                onClick={() => setToast(null)}
                aria-label="Dismiss undo"
                className="rounded-md p-1 text-white/45 transition-colors hover:bg-white/10 hover:text-white"
              >
                <X size={14} />
              </button>
            </div>
            <div className="h-1 bg-white/10">
              <div
                className="h-full bg-[#7fbfa0] transition-[width] duration-200 ease-linear"
                style={{ width: `${remainingPercent}%` }}
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

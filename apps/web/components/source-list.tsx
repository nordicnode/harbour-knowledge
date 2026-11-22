"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import {
  AlertTriangle,
  Check,
  FileCode2,
  FileImage,
  FileText,
  FileType2,
  Globe2,
  Loader2,
  Table2,
  Trash2,
  X,
} from "lucide-react";

import { deleteSourceAction, deleteSourcesAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/format";
import type { SourceRow } from "@/lib/types";
import { Badge, Card } from "./ui";

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

function SelectionMark({ checked }: { checked: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
        checked ? "border-pine bg-pine text-white" : "border-line-strong bg-surface text-transparent",
      )}
    >
      <Check size={11} strokeWidth={3} />
    </span>
  );
}

function SourceListItem({
  source,
  canEdit,
  selecting,
  selected,
  onToggle,
}: {
  source: SourceRow;
  canEdit: boolean;
  selecting: boolean;
  selected: boolean;
  onToggle: () => void;
}) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [removed, setRemoved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const Icon = SOURCE_ICONS[source.kind] ?? FileText;
  const state = sourceState(source);
  const meta = SOURCE_STATE_META[state];

  if (removed) return null;

  function remove() {
    setError(null);
    startTransition(async () => {
      const result = await deleteSourceAction(source.id, null);
      if (result.ok) {
        setRemoved(true);
        router.refresh();
      } else {
        setError(result.error);
        setConfirming(false);
      }
    });
  }

  const content = (
    <>
      {selecting ? <SelectionMark checked={selected} /> : null}
      <span
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border",
          state === "failed"
            ? "border-rust/20 bg-rust-soft text-rust"
            : "border-line bg-paper text-ink-2",
        )}
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
    </>
  );

  return (
    <div
      className={cn(
        "border-b border-line/75 last:border-b-0",
        confirming && "bg-rust-soft/35",
        selecting && selected && "bg-pine-soft/40",
        pending && "opacity-75",
      )}
    >
      <div className="group flex items-center gap-2 px-4 py-3.5 transition-colors hover:bg-surface-2 md:px-5">
        {selecting ? (
          <button
            type="button"
            onClick={onToggle}
            aria-pressed={selected}
            aria-label={`${selected ? "Deselect" : "Select"} ${source.title}`}
            className="flex min-w-0 flex-1 items-center gap-3.5 text-left"
          >
            {content}
          </button>
        ) : (
          <Link href={`/sources/${source.id}`} className="flex min-w-0 flex-1 items-center gap-3.5">
            {content}
          </Link>
        )}

        {canEdit && !selecting ? (
          confirming ? (
            <span className="flex shrink-0 items-center gap-1">
              <span className="text-[10.5px] font-medium text-rust">Remove?</span>
              <button
                type="button"
                onClick={remove}
                disabled={pending}
                className="inline-flex h-7 items-center gap-1 rounded-md bg-rust px-2 text-[10.5px] font-medium text-white transition-colors hover:bg-rust/90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {pending ? <Loader2 size={11} className="animate-spin" /> : null}
                {pending ? "Removing…" : "Remove"}
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                disabled={pending}
                aria-label="Cancel removal"
                className="rounded-md p-1 text-faint transition-colors hover:bg-paper-deep hover:text-ink disabled:opacity-50"
              >
                <X size={13} />
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              aria-label={`Remove ${source.title}`}
              title="Remove source"
              className="shrink-0 rounded-md p-1.5 text-faint transition-colors hover:bg-rust-soft hover:text-rust"
            >
              <Trash2 size={14} />
            </button>
          )
        ) : null}
      </div>
      {error ? (
        <p role="alert" className="px-4 pb-2.5 text-[10.5px] text-rust md:px-5">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function SourceList({
  sources,
  canEdit,
  processing,
}: {
  sources: SourceRow[];
  canEdit: boolean;
  processing: number;
}) {
  const router = useRouter();
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const visible = sources.filter((source) => !removed.has(source.id));
  const allSelected = visible.length > 0 && visible.every((source) => selected.has(source.id));

  function toggleSelecting() {
    setSelecting((value) => !value);
    setSelected(new Set());
    setConfirming(false);
    setError(null);
  }

  function toggle(sourceId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(sourceId)) next.delete(sourceId);
      else next.add(sourceId);
      return next;
    });
    setConfirming(false);
  }

  function selectAll() {
    setSelected(new Set(visible.map((source) => source.id)));
    setConfirming(false);
  }

  function clearSelection() {
    setSelected(new Set());
    setConfirming(false);
  }

  function removeSelected() {
    const ids = visible.filter((source) => selected.has(source.id)).map((source) => source.id);
    if (!ids.length) return;
    setError(null);
    startTransition(async () => {
      const result = await deleteSourcesAction(ids);
      if (result.ok) {
        // A source can vanish between selection and deletion (another editor,
        // or a stale tab). Hide only the ones the API actually withdrew.
        const missing = new Set(result.data?.missing ?? []);
        const deleted = ids.filter((id) => !missing.has(id));
        setRemoved((current) => new Set([...current, ...deleted]));
        setSelected(new Set());
        setSelecting(false);
        setConfirming(false);
        if (missing.size) {
          setError(
            `${missing.size} selected source${missing.size === 1 ? " was" : "s were"} already gone.`,
          );
        }
        router.refresh();
      } else {
        setError(result.error);
        setConfirming(false);
      }
    });
  }

  return (
    <section className="space-y-3">
      <div className="flex items-end justify-between gap-4 px-1">
        <div>
          <h2 className="display text-[20px] leading-tight text-ink">Your sources</h2>
          <p className="mt-0.5 text-[11.5px] text-muted">
            Everything Harbour has read. Open one to see what it found, or remove it here.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {processing ? <Badge tone="sky">{processing} reading</Badge> : null}
          {canEdit && sources.length > 1 ? (
            <button
              type="button"
              onClick={toggleSelecting}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-[11px] font-medium transition-colors",
                selecting
                  ? "border-pine/30 bg-pine-soft text-pine"
                  : "border-line bg-surface text-muted hover:border-line-strong hover:text-ink",
              )}
            >
              {selecting ? "Done" : "Select"}
            </button>
          ) : null}
        </div>
      </div>

      {selecting ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-line bg-surface px-3 py-2">
          <span className="text-[11.5px] text-muted">
            <span className="font-semibold text-ink">{selected.size}</span> selected
          </span>
          <button
            type="button"
            onClick={allSelected ? clearSelection : selectAll}
            className="text-[11px] font-medium text-pine hover:underline"
          >
            {allSelected ? "Clear" : "Select all"}
          </button>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {confirming ? (
              <>
                <span className="text-[11px] font-medium text-rust">
                  Remove {selected.size} source{selected.size === 1 ? "" : "s"}?
                </span>
                <button
                  type="button"
                  onClick={removeSelected}
                  disabled={pending || !selected.size}
                  className="inline-flex h-7 items-center gap-1 rounded-md bg-rust px-2.5 text-[10.5px] font-medium text-white transition-colors hover:bg-rust/90 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {pending ? <Loader2 size={11} className="animate-spin" /> : null}
                  {pending ? "Removing…" : "Remove"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  disabled={pending}
                  className="rounded-md px-1.5 py-1 text-[10.5px] text-muted transition-colors hover:bg-paper-deep hover:text-ink disabled:opacity-50"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setConfirming(true)}
                disabled={!selected.size}
                className="inline-flex h-7 items-center gap-1 rounded-md border border-rust/20 bg-rust-soft px-2.5 text-[10.5px] font-medium text-rust transition-colors hover:border-rust/35 disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Trash2 size={12} /> Remove selected
              </button>
            )}
          </div>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="rounded-lg border border-rust/20 bg-rust-soft px-3 py-2 text-[11px] text-rust">
          {error}
        </p>
      ) : null}

      {visible.length ? (
        <Card className="overflow-hidden">
          {visible.map((source) => (
            <SourceListItem
              key={source.id}
              source={source}
              canEdit={canEdit}
              selecting={selecting}
              selected={selected.has(source.id)}
              onToggle={() => toggle(source.id)}
            />
          ))}
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
  );
}

"use client";

import { useState, useTransition } from "react";
import {
  Check,
  CheckCheck,
  ChevronDown,
  CircleSlash2,
  Clipboard,
  FilePenLine,
  MessageSquareWarning,
  ShieldAlert,
  ShieldCheck,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import { feedbackAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import type { AskResult } from "@/lib/types";
import { ConfidenceMeter } from "./confidence";
import { AnswerText } from "./answer-text";
import { Badge, Button, Card, textareaClass } from "./ui";

function modePill(mode: AskResult["mode"]) {
  if (mode === "canonical") {
    return (
      <Badge tone="pine">
        <ShieldCheck size={11} /> Approved answer
      </Badge>
    );
  }
  if (mode === "fallback") {
    return (
      <Badge tone="amber">
        <FilePenLine size={11} /> Unapproved draft
      </Badge>
    );
  }
  return (
    <Badge tone="rust">
      <CircleSlash2 size={11} /> No approved answer
    </Badge>
  );
}

function IndexWarning({ warning }: { warning: string | null }) {
  if (!warning) return null;
  return (
    <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber/25 bg-amber-soft/60 px-3 py-2 text-[11px] leading-relaxed text-ink-2">
      <ShieldAlert size={13} className="mt-0.5 shrink-0 text-amber" />
      <span>{warning}</span>
    </div>
  );
}

function Feedback({ result }: { result: AskResult }) {
  const [vote, setVote] = useState<1 | -1 | 0>(0);
  const [pending, startTransition] = useTransition();
  const [saved, setSaved] = useState(false);
  const [editing, setEditing] = useState(false);
  const [edited, setEdited] = useState(result.answer_md ?? "");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  function send(value: 1 | -1) {
    setVote(value);
    startTransition(async () => {
      const response = await feedbackAction(result.qa_log_id, value);
      setSaved(response.ok);
      if (!response.ok) setMessage(response.error);
    });
  }

  function proposeEdit() {
    startTransition(async () => {
      const response = await feedbackAction(result.qa_log_id, 0, edited, note || undefined);
      if (response.ok) {
        setMessage("Edit proposed to the review queue.");
        setEditing(false);
      } else {
        setMessage(response.error);
      }
    });
  }

  return (
    <div className="mt-5 border-t border-line/80 pt-3.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-[10.5px] font-medium uppercase tracking-[0.1em] text-faint">Useful?</span>
        <button
          type="button"
          onClick={() => send(1)}
          disabled={pending}
          aria-label="This answer was useful"
          className={cn(
            "flex h-7 items-center gap-1 rounded-md px-2 text-[11px] transition-colors",
            vote === 1 ? "bg-pine-soft text-pine-dark" : "text-muted hover:bg-paper-deep",
          )}
        >
          <ThumbsUp size={12} /> Yes
        </button>
        <button
          type="button"
          onClick={() => send(-1)}
          disabled={pending}
          aria-label="This answer was not useful"
          className={cn(
            "flex h-7 items-center gap-1 rounded-md px-2 text-[11px] transition-colors",
            vote === -1 ? "bg-rust-soft text-rust" : "text-muted hover:bg-paper-deep",
          )}
        >
          <ThumbsDown size={12} /> No
        </button>
        <span className="mx-1 h-3 w-px bg-line" />
        <button
          type="button"
          onClick={() => setEditing((value) => !value)}
          className="flex h-7 items-center gap-1 rounded-md px-2 text-[11px] text-muted transition-colors hover:bg-paper-deep"
        >
          <MessageSquareWarning size={12} /> Suggest an edit
        </button>
        {saved ? (
          <span className="ml-auto flex items-center gap-1 text-[10.5px] text-pine">
            <CheckCheck size={12} /> logged
          </span>
        ) : null}
      </div>

      {editing ? (
        <div className="mt-3 rounded-lg border border-line bg-surface-2 p-3">
          <p className="mb-2 text-[11px] text-muted">
            A material edit becomes a proposal. It never changes the live answer on its own.
          </p>
          <textarea
            value={edited}
            onChange={(event) => setEdited(event.target.value)}
            rows={5}
            className={textareaClass}
          />
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Why is this better? (optional)"
            className="mt-2 h-8 w-full rounded-md border border-line bg-surface px-2.5 text-[12px]"
          />
          <div className="mt-2 flex items-center gap-2">
            <Button size="sm" variant="primary" onClick={proposeEdit} disabled={pending || !edited.trim()}>
              Propose edit
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
      {message ? <p className="mt-2 text-[11px] text-muted">{message}</p> : null}
    </div>
  );
}

export function AnswerCard({ result }: { result: AskResult }) {
  const [copied, setCopied] = useState(false);
  const [showIgnored, setShowIgnored] = useState(false);
  const unverifiedCitations = result.citations.filter((citation) => !citation.verified).length;

  if (result.abstained) {
    return (
      <Card className="overflow-hidden">
        <div className="border-l-[3px] border-rust px-5 py-5 md:px-6">
          <div className="flex flex-wrap items-center gap-2">
            {modePill("abstained")}
            <Badge tone="neutral">Gap logged</Badge>
          </div>
          <h3 className="display mt-3 text-[23px] leading-tight text-ink">I could not answer that safely.</h3>
          <p className="mt-2 max-w-2xl text-[13.5px] leading-relaxed text-ink-2">
            {result.abstention_reason ?? "No approved answer covers this question for the selected scope."}
          </p>
          <IndexWarning warning={result.index_warning} />
          <div className="mt-4 rounded-lg border border-line bg-surface-2 px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Searched</p>
            <p className="mt-1 text-[12px] text-ink-2">{result.searched_scope}</p>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {result.next_steps.map((step) => (
              <span
                key={step}
                className="rounded-md border border-line bg-surface px-2.5 py-1.5 text-[11.5px] text-ink-2"
              >
                {step}
              </span>
            ))}
          </div>
          <p className="mt-4 text-[11px] text-faint">
            This question is now clustered on the gap dashboard. An owner can draft an answer from source.
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="border-l-[3px] border-pine px-5 py-5 md:px-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {modePill(result.mode)}
            <ConfidenceMeter band={result.confidence_band} mode={result.mode} compact />
          </div>
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard.writeText(result.answer_md ?? "");
              setCopied(true);
              setTimeout(() => setCopied(false), 1800);
            }}
            className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-muted transition-colors hover:bg-paper-deep hover:text-ink"
          >
            {copied ? <Check size={12} /> : <Clipboard size={12} />}
            {copied ? "Copied" : "Copy reply"}
          </button>
        </div>

        <IndexWarning warning={result.index_warning} />

        <div className="mt-4">
          <AnswerText text={result.answer_md ?? ""} citations={result.citations} />
        </div>

        {result.citations.length ? (
          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">
                Evidence · {result.citations.length}
              </p>
              {unverifiedCitations ? (
                <span className="flex items-center gap-1 text-[10.5px] text-rust">
                  <ShieldAlert size={12} /> {unverifiedCitations} unverified
                </span>
              ) : (
                <span className="flex items-center gap-1 text-[10.5px] text-pine">
                  <ShieldCheck size={12} /> quotes verified
                </span>
              )}
            </div>
            <div className="overflow-hidden rounded-lg border border-line bg-surface-2">
              {result.citations.map((citation, index) => (
                <details
                  key={citation.evidence_id}
                  id={`evidence-${citation.evidence_id}`}
                  className="group scroll-mt-24 border-b border-line/80 last:border-b-0"
                >
                  <summary className="flex cursor-pointer list-none items-center gap-2.5 px-3.5 py-2.5 text-[11.5px] hover:bg-surface">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[5px] bg-pine-soft font-mono text-[10px] text-pine-dark">
                      {index + 1}
                    </span>
                    <span className="min-w-0 flex-1 truncate font-medium text-ink-2">{citation.source_title}</span>
                    <span className="hidden shrink-0 text-[10.5px] text-faint sm:block">{citation.section_path}</span>
                    {citation.verified ? <ShieldCheck size={12} className="shrink-0 text-pine" /> : <Badge tone="rust">unverified</Badge>}
                    <ChevronDown size={13} className="shrink-0 text-faint transition-transform group-open:rotate-180" />
                  </summary>
                  <div className="border-t border-line/70 bg-surface px-4 py-3">
                    <div className="border-l-2 border-line pl-3">
                      <AnswerText text={citation.quote} dense />
                    </div>
                    <p className="mt-2 text-[10.5px] text-faint">
                      {citation.source_title}
                      {citation.source_date ? ` · ${citation.source_date}` : ""} · {citation.section_path}
                    </p>
                  </div>
                </details>
              ))}
            </div>
          </div>
        ) : null}

        {result.sources_ignored.length ? (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setShowIgnored((value) => !value)}
              className="text-[11px] font-medium text-amber hover:underline"
            >
              {result.sources_ignored.length} source{result.sources_ignored.length === 1 ? "" : "s"} held back
              {showIgnored ? " —" : " +"}
            </button>
            {showIgnored ? (
              <ul className="mt-2 space-y-1.5">
                {result.sources_ignored.map((source) => (
                  <li key={`${source.title}-${source.reason}`} className="text-[11.5px] text-muted">
                    <span className="font-medium text-ink-2">{source.title}</span> — {source.reason}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        <Feedback result={result} />

        <div className="mt-3 flex flex-wrap items-center gap-3 text-[10px] text-faint">
          <span>{result.provider === "dev" ? "Dev intelligence" : result.provider}</span>
          <span>·</span>
          <span>{result.latency_ms} ms</span>
          <span>·</span>
          <span>{result.mode === "canonical" ? `version ${result.answer_version}` : "passage-grounded"}</span>
          {result.confidence ? (
            <>
              <span>·</span>
              <span>
                retrieval {Math.round(result.confidence.retrieval * 100)} · faithfulness{" "}
                {Math.round(result.confidence.faithfulness * 100)}
              </span>
            </>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

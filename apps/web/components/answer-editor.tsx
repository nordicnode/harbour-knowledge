"use client";

import { useActionState, useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Archive, Check, ChevronDown, Loader2, PencilLine, ShieldCheck } from "lucide-react";

import {
  ActionResult,
  approveAnswerAction,
  archiveAnswerAction,
  saveAnswerAction,
} from "@/app/actions";
import type { Answer, AnswerEvidence, Topic } from "@/lib/types";
import { AnswerText } from "./answer-text";
import { Badge, Button, Card, Field, FieldGroup, inputClass, selectClass, textareaClass } from "./ui";

const PLANS = [
  { value: "team", label: "Team" },
  { value: "business", label: "Business" },
  { value: "enterprise", label: "Enterprise" },
];

const VERSIONS = [
  { value: "v1", label: "Version 1" },
  { value: "v2", label: "Version 2" },
];

export function AnswerEditor({
  answer,
  topics,
  evidence,
}: {
  answer: Answer;
  topics: Topic[];
  evidence: AnswerEvidence[];
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [state, formAction, formPending] = useActionState<ActionResult | null, FormData>(
    saveAnswerAction.bind(null, answer.id),
    null,
  );
  const [actionPending, startAction] = useTransition();
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const approved = answer.status === "approved";
  const hasInternalEvidence = evidence.some((item) => item.source_visibility !== "public");

  useEffect(() => {
    if (state?.ok) {
      setEditing(false);
      router.refresh();
    }
  }, [router, state]);

  function approve() {
    startAction(async () => {
      const result = await approveAnswerAction(answer.id);
      setActionMessage(result.ok ? "Published. This answer is live for its scope." : result.error);
      if (result.ok) router.refresh();
    });
  }

  function archive() {
    startAction(async () => {
      const result = await archiveAnswerAction(answer.id);
      setActionMessage(result.ok ? "Answer archived." : result.error);
      if (result.ok) router.refresh();
    });
  }

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line bg-surface-2/50 px-4 py-2.5">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">The answer</span>
        {approved ? (
          <Badge tone="pine">
            <ShieldCheck size={10} /> Live
          </Badge>
        ) : (
          <Badge tone="amber">Not published</Badge>
        )}
        <span className="ml-auto text-[10px] text-faint">version {answer.current_version}</span>
      </div>

      {editing ? (
        <form action={formAction} className="px-4 py-4 md:px-5">
          <textarea
            name="answer_md"
            defaultValue={answer.answer_md}
            rows={14}
            className={`${textareaClass} min-h-[280px] font-[450]`}
            placeholder="Write the answer in plain language."
          />
          <p className="mt-1.5 text-[10.5px] text-faint">
            Editing saves a new version. Nothing changes for customers until you publish it.
          </p>

          <details className="group mt-4 rounded-lg border border-line bg-surface-2/40">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5 text-[12px] font-medium text-ink-2">
              <span>Answer settings</span>
              <span className="flex items-center gap-1.5 text-[10.5px] text-faint">
                Topic, visibility, review, scope
                <ChevronDown size={13} className="transition-transform group-open:rotate-180" />
              </span>
            </summary>
            <div className="grid gap-4 border-t border-line px-4 py-4 md:grid-cols-2">
              <Field label="Topic">
                <select name="topic_slug" defaultValue={answer.topic_slug ?? ""} className={selectClass}>
                  <option value="">No topic</option>
                  {topics.map((topic) => (
                    <option key={topic.id} value={topic.slug}>
                      {topic.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Visibility">
                <select name="visibility" defaultValue={answer.visibility} className={selectClass}>
                  <option value="internal">Internal only</option>
                  <option value="public">Customers can see this</option>
                </select>
              </Field>
              <Field label="Review interval (days)">
                <input
                  type="number"
                  name="review_interval_days"
                  min={1}
                  max={730}
                  defaultValue={answer.review_interval_days}
                  className={inputClass}
                />
              </Field>
              <Field label="What changed?" hint="Optional. Shown in the history.">
                <input name="change_reason" placeholder="e.g. Updated the refund window" className={inputClass} />
              </Field>
              <FieldGroup label="Applies to plans" hint="Leave empty for every plan.">
                <div className="flex flex-wrap gap-3 pt-1">
                  {PLANS.map((plan) => (
                    <label key={plan.value} className="flex items-center gap-1.5 text-[12px] text-ink-2">
                      <input
                        type="checkbox"
                        name="plan_scope"
                        value={plan.value}
                        defaultChecked={answer.plan_scope.includes(plan.value)}
                        className="accent-pine"
                      />
                      {plan.label}
                    </label>
                  ))}
                </div>
              </FieldGroup>
              <FieldGroup label="Applies to versions" hint="Leave empty for every version.">
                <div className="flex flex-wrap gap-3 pt-1">
                  {VERSIONS.map((version) => (
                    <label key={version.value} className="flex items-center gap-1.5 text-[12px] text-ink-2">
                      <input
                        type="checkbox"
                        name="version_scope"
                        value={version.value}
                        defaultChecked={answer.version_scope.includes(version.value)}
                        className="accent-pine"
                      />
                      {version.label}
                    </label>
                  ))}
                </div>
              </FieldGroup>
            </div>
          </details>

          {hasInternalEvidence && answer.visibility === "public" ? (
            <p className="mt-3 rounded-md border border-rust/25 bg-rust-soft px-3 py-2 text-[11.5px] text-rust">
              This answer quotes an internal source. Make it internal or replace the evidence.
            </p>
          ) : null}

          {state && !state.ok ? (
            <p className="mt-3 rounded-md border border-rust/25 bg-rust-soft px-3 py-2 text-[11.5px] text-rust">
              {state.error}
            </p>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
            <Button type="submit" variant="primary" disabled={formPending}>
              {formPending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
              {approved ? "Save as new version" : "Save changes"}
            </Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(false)} disabled={formPending}>
              Cancel
            </Button>
            <span className="ml-auto text-[10.5px] text-faint">
              {evidence.length} source quote{evidence.length === 1 ? "" : "s"}
            </span>
          </div>
        </form>
      ) : (
        <div className="px-4 py-4 md:px-5">
          <div className="min-h-[140px]">
            <AnswerText text={answer.answer_md || "No answer text yet."} />
          </div>

          {!approved ? (
            <p className="mt-3 rounded-md border border-amber/20 bg-amber-soft/60 px-3 py-2 text-[11.5px] leading-relaxed text-amber">
              This answer is not live yet. Publish it when it reads right.
            </p>
          ) : null}

          {actionMessage ? (
            <p className="mt-2 text-[11px] text-pine" role="status" aria-live="polite">
              {actionMessage}
            </p>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
            {!approved ? (
              <Button
                type="button"
                variant="primary"
                onClick={approve}
                disabled={actionPending || evidence.length === 0}
                title={evidence.length === 0 ? "Attach a source quote before publishing" : undefined}
              >
                {actionPending ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
                {answer.status === "needs_recheck" ? "Recheck & publish" : "Publish answer"}
              </Button>
            ) : null}
            <Button type="button" variant={approved ? "secondary" : "ghost"} onClick={() => setEditing(true)}>
              <PencilLine size={13} /> Edit answer
            </Button>
            <Button type="button" variant="ghost" onClick={archive} disabled={actionPending}>
              <Archive size={13} /> Archive
            </Button>
            <span className="ml-auto text-[10.5px] text-faint">
              {evidence.length} source quote{evidence.length === 1 ? "" : "s"} attached
            </span>
          </div>
        </div>
      )}
    </Card>
  );
}

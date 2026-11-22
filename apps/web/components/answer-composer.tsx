"use client";

import { useActionState, useState } from "react";
import Link from "next/link";
import { Check, ChevronDown, FileSearch, Loader2, Search, Sparkles, X } from "lucide-react";

import { ActionResult, createAnswerAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import type { Topic } from "@/lib/types";
import { Badge, Button, Card, Field, FieldGroup, inputClass, selectClass } from "./ui";

type Hit = {
  passage_id: string;
  source_id: string;
  document_title: string;
  source_title: string;
  section_path: string;
  matched_text: string;
  visibility: "internal" | "public";
  document_version: number;
};

const PLANS = ["team", "business", "enterprise"];
const VERSIONS = ["v1", "v2"];
const MAX_SELECTED = 5;

export function AnswerComposer({
  query,
  results,
  topics,
  searched,
}: {
  query: string;
  results: Hit[];
  topics: Topic[];
  searched: boolean;
}) {
  const [selected, setSelected] = useState<Record<string, Hit>>({});
  const [question, setQuestion] = useState(query);
  const [state, formAction, pending] = useActionState<ActionResult | null, FormData>(
    createAnswerAction,
    null,
  );
  const chosen = Object.values(selected);

  function toggle(hit: Hit) {
    setSelected((current) => {
      const next = { ...current };
      if (next[hit.passage_id]) {
        delete next[hit.passage_id];
      } else if (Object.keys(next).length < MAX_SELECTED) {
        next[hit.passage_id] = hit;
      }
      return next;
    });
  }

  return (
    <div className="space-y-5">
      <Card className="px-4 py-4">
        <form method="GET" action="/knowledge/new" className="flex flex-col gap-2 sm:flex-row">
          <div className="relative flex-1">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
            <input
              name="q"
              defaultValue={query}
              placeholder="Search for the evidence that should ground this answer…"
              className={`${inputClass} h-10 pl-9 text-[14px]`}
            />
          </div>
          <Button type="submit" variant="secondary" size="lg">
            <FileSearch size={14} /> Find passages
          </Button>
        </form>
        <p className="mt-2 text-[10.5px] text-faint">
          Select up to {MAX_SELECTED} current passages. The draft will quote them verbatim; nothing is invented.
        </p>
      </Card>

      {chosen.length ? (
        <Card className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line bg-pine-soft/60 px-4 py-2.5">
            <Check size={13} className="text-pine" />
            <p className="text-[11.5px] font-medium text-pine-dark">
              {chosen.length} passage{chosen.length === 1 ? "" : "s"} selected
            </p>
            <button
              type="button"
              onClick={() => setSelected({})}
              className="ml-auto inline-flex items-center gap-1 text-[10.5px] text-pine/80 hover:text-pine"
            >
              <X size={11} /> Clear
            </button>
          </div>
          <div className="divide-y divide-line/70">
            {chosen.map((hit, index) => (
              <div key={hit.passage_id} className="flex items-start gap-2.5 px-4 py-2.5">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-pine text-[9.5px] font-semibold text-white">
                  {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[11px] font-medium text-ink">{hit.document_title}</p>
                  <p className="truncate text-[10px] text-faint">{hit.section_path}</p>
                </div>
                <button
                  type="button"
                  onClick={() => toggle(hit)}
                  className="mt-0.5 text-muted hover:text-rust"
                  aria-label="Remove passage"
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-line bg-surface-2/60 px-4 py-2.5">
          <p className="text-[11.5px] font-semibold text-ink">Evidence candidates</p>
          <span className="text-[10px] text-faint">{searched ? `${results.length} matched passages` : "Search to begin"}</span>
        </div>
        {results.length ? (
          <div className="max-h-[420px] divide-y divide-line/70 overflow-y-auto">
            {results.map((hit) => {
              const active = Boolean(selected[hit.passage_id]);
              return (
                <button
                  key={hit.passage_id}
                  type="button"
                  onClick={() => toggle(hit)}
                  className={cn(
                    "flex w-full items-start gap-3 px-4 py-3 text-left transition-colors",
                    active ? "bg-pine-soft/50" : "hover:bg-surface-2",
                  )}
                >
                  <span
                    className={cn(
                      "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                      active ? "border-pine bg-pine text-white" : "border-line-strong bg-surface",
                    )}
                  >
                    {active ? <Check size={11} /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-[11.5px] font-medium text-ink">{hit.document_title}</span>
                      <Badge tone={hit.visibility === "public" ? "pine" : "sky"}>
                        {hit.visibility === "public" ? "public" : "internal"}
                      </Badge>
                      <span className="font-mono text-[9px] text-faint">v{hit.document_version}</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[10px] text-faint">{hit.section_path}</span>
                    <span className="mt-1 line-clamp-2 text-[11.5px] leading-relaxed text-ink-2">{hit.matched_text}</span>
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="px-5 py-9 text-center">
            <p className="text-[12.5px] text-muted">
              {searched ? "No passage matched. Broaden the search or ingest a source first." : "Search the evidence library above."}
            </p>
            {searched ? (
              <Link href="/knowledge#add" className="mt-2 inline-block text-[11.5px] font-medium text-pine hover:underline">
                Add a source →
              </Link>
            ) : null}
          </div>
        )}
      </Card>

      <Card className="px-4 py-4 md:px-5">
        <form action={formAction} className="space-y-4">
          {chosen.map((hit) => (
            <input key={hit.passage_id} type="hidden" name="passage_ids" value={hit.passage_id} />
          ))}

          <Field label="Canonical question" hint="Write it the way a customer would ask it.">
            <input
              name="question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="What is the refund window?"
              className={inputClass}
              required
            />
          </Field>

          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Topic">
              <select name="topic_slug" defaultValue="" className={selectClass}>
                <option value="">No topic</option>
                {topics.map((topic) => (
                  <option key={topic.id} value={topic.slug}>
                    {topic.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Visibility">
              <select name="visibility" defaultValue="internal" className={selectClass}>
                <option value="internal">Internal — agents only</option>
                <option value="public">Public — help centre</option>
              </select>
            </Field>
          </div>

          <details className="group rounded-lg border border-line bg-surface-2/40">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5 text-[12px] font-medium text-ink-2">
              <span>Advanced scope</span>
              <span className="flex items-center gap-1.5 text-[10.5px] text-faint">
                Plans and product versions
                <ChevronDown size={13} className="transition-transform group-open:rotate-180" />
              </span>
            </summary>
            <div className="grid gap-4 border-t border-line px-4 py-4 md:grid-cols-2">
              <FieldGroup label="Plan scope">
                <div className="flex flex-wrap gap-3 pt-2">
                  {PLANS.map((plan) => (
                    <label key={plan} className="flex items-center gap-1.5 text-[12px] capitalize text-ink-2">
                      <input type="checkbox" name="plan_scope" value={plan} className="accent-pine" />
                      {plan}
                    </label>
                  ))}
                </div>
              </FieldGroup>
              <FieldGroup label="Product version">
                <div className="flex flex-wrap gap-3 pt-2">
                  {VERSIONS.map((version) => (
                    <label key={version} className="flex items-center gap-1.5 text-[12px] uppercase text-ink-2">
                      <input type="checkbox" name="version_scope" value={version} className="accent-pine" />
                      {version}
                    </label>
                  ))}
                </div>
              </FieldGroup>
            </div>
          </details>

          {state && !state.ok ? (
            <p className="rounded-md border border-rust/25 bg-rust-soft px-3 py-2 text-[11.5px] text-rust">
              {state.error}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <Button type="submit" variant="primary" disabled={pending || chosen.length === 0}>
              {pending ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
              {pending ? "Drafting from evidence…" : "Draft with evidence"}
            </Button>
            <span className="text-[10.5px] text-faint">
              {chosen.length ? "The draft goes to review, not straight to customers." : "Select at least one passage first."}
            </span>
          </div>
        </form>
      </Card>
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, CornerDownLeft, Loader2, Settings2 } from "lucide-react";

import { askAction } from "@/app/actions";
import { channelLabel } from "@/lib/format";
import type { AskResult } from "@/lib/types";
import { AnswerCard } from "./answer-card";
import { Button, Card, selectClass, textareaClass } from "./ui";

type Message =
  | { id: string; role: "user"; content: string }
  | { id: string; role: "assistant"; result?: AskResult; error?: string; pending?: boolean };

const SUGGESTIONS = [
  "What is the refund window for annual plans?",
  "Do I need approval for team expenses?",
  "What does the Business plan cost per user?",
];

const PLANS = ["team", "business", "enterprise"];

function newId(): string {
  return Math.random().toString(36).slice(2);
}

export function AskPanel({ initialQuestion = "" }: { initialQuestion?: string }) {
  const [scopeOpen, setScopeOpen] = useState(false);
  const [channel, setChannel] = useState("web");
  const [plan, setPlan] = useState("");
  const [version, setVersion] = useState("");
  const [input, setInput] = useState(initialQuestion);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!messages.length) return;
    const frame = requestAnimationFrame(() => {
      const anchor = bottom.current;
      if (!anchor) return;
      // Keep the end of the newest reply above the sticky composer. A plain
      // scrollIntoView aligns it to the viewport edge and the composer covers it.
      const composerHeight = composer.current?.getBoundingClientRect().height ?? 0;
      const target =
        window.scrollY + anchor.getBoundingClientRect().bottom - window.innerHeight + composerHeight + 24;
      window.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [messages]);

  async function submit(question: string) {
    const trimmed = question.trim();
    if (!trimmed || busy) return;

    const history = messages.flatMap((message) => {
      if (message.role === "user") return [{ role: "user", content: message.content }];
      if (message.role === "assistant" && message.result?.answer_md) {
        return [{ role: "assistant", content: message.result.answer_md }];
      }
      return [];
    });

    const userMessage: Message = { id: newId(), role: "user", content: trimmed };
    const pendingId = newId();
    setMessages((current) => [...current, userMessage, { id: pendingId, role: "assistant", pending: true }]);
    setInput("");
    setBusy(true);

    const response = await askAction({
      question: trimmed,
      channel,
      plan: plan || null,
      version: version || null,
      history,
    });

    setMessages((current) =>
      current.map((message) => {
        if (message.id !== pendingId) return message;
        if (response.ok && response.data) {
          return { id: pendingId, role: "assistant", result: response.data };
        }
        return {
          id: pendingId,
          role: "assistant",
          error: response.ok ? "The answer could not be loaded." : response.error,
        };
      }),
    );
    setBusy(false);
    inputRef.current?.focus();
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit(input);
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-4">
        {messages.length === 0 ? (
          <Card className="px-5 py-5">
            <p className="max-w-xl text-[12.5px] leading-relaxed text-ink-2">
              Ask in plain language. Approved knowledge comes first, with exact quotes attached. If nothing supports an
              answer, it abstains and logs the gap.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => void submit(suggestion)}
                  className="rounded-full border border-line bg-paper px-3 py-1.5 text-left text-[11.5px] text-ink-2 transition-colors hover:border-pine/30 hover:bg-pine-soft/40"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </Card>
        ) : (
          messages.map((message) =>
            message.role === "user" ? (
              <div key={message.id} className="flex justify-end">
                <div className="max-w-[86%] rounded-[14px] rounded-br-[4px] border border-pine/20 bg-pine-soft px-4 py-3 text-[13px] leading-relaxed text-ink">
                  {message.content}
                </div>
              </div>
            ) : message.pending ? (
              <Card key={message.id} className="px-5 py-5">
                <div className="flex items-center gap-2 text-[11.5px] text-muted">
                  <Loader2 size={14} className="animate-spin text-pine" />
                  Searching approved answers, verifying evidence…
                </div>
                <div className="mt-4 space-y-2">
                  <div className="h-2.5 w-4/5 animate-pulse rounded bg-paper-deep" />
                  <div className="h-2.5 w-3/5 animate-pulse rounded bg-paper-deep" />
                  <div className="h-2.5 w-2/3 animate-pulse rounded bg-paper-deep" />
                </div>
              </Card>
            ) : message.error ? (
              <Card key={message.id} className="border-rust/25 px-5 py-4 text-[12.5px] text-rust">
                {message.error}
              </Card>
            ) : message.result ? (
              <AnswerCard key={message.id} result={message.result} />
            ) : null,
          )
        )}
        <div ref={bottom} aria-hidden="true" />
      </div>

      <div ref={composer} className="sticky bottom-4">
        <Card className="px-3 py-2.5 shadow-[0_8px_30px_rgba(0,0,0,0.45)]">
        {scopeOpen ? (
          <div className="mb-2 grid gap-3 rounded-lg border border-line bg-surface-2/70 px-3 py-3 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint">Channel</span>
              <select value={channel} onChange={(event) => setChannel(event.target.value)} className={selectClass}>
                <option value="web">Agent assist</option>
                <option value="public">Public help centre</option>
                <option value="widget">Embeddable widget</option>
                <option value="discord">Discord bot</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint">Plan</span>
              <select value={plan} onChange={(event) => setPlan(event.target.value)} className={selectClass}>
                <option value="">Any plan</option>
                {PLANS.map((value) => (
                  <option key={value} value={value}>
                    {value[0].toUpperCase() + value.slice(1)}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint">Version</span>
              <select value={version} onChange={(event) => setVersion(event.target.value)} className={selectClass}>
                <option value="">Any version</option>
                <option value="v1">Version 1</option>
                <option value="v2">Version 2</option>
              </select>
            </label>
            <div className="flex flex-wrap items-center justify-between gap-2 sm:col-span-3">
              <p className="text-[10.5px] leading-snug text-faint">
                Customer-facing channels only see approved, public answers.
              </p>
              <Button type="button" size="sm" variant="secondary" onClick={() => setScopeOpen(false)}>
                Done
              </Button>
            </div>
          </div>
        ) : null}
        <textarea
          ref={inputRef}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          placeholder="Ask a support question…"
          className={`${textareaClass} resize-none border-0 bg-transparent px-1 focus:outline-none`}
        />
        <div className="flex items-center justify-between gap-3 px-1">
          <button
            type="button"
            onClick={() => setScopeOpen((value) => !value)}
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2 py-1 text-[10px] text-ink-2 transition-colors hover:border-line-strong hover:text-ink"
          >
            <Settings2 size={11} />
            {channelLabel(channel)}
            {plan ? ` · ${plan}` : " · any plan"}
            {version ? ` · ${version}` : " · any version"}
            <span className="font-medium text-pine">{scopeOpen ? "Close" : "Change"}</span>
          </button>
          <div className="flex items-center gap-2">
            <span className="hidden items-center gap-1 text-[10px] text-faint sm:flex">
              <CornerDownLeft size={11} /> to send
            </span>
            <button
              type="button"
              onClick={() => void submit(input)}
              disabled={busy || !input.trim()}
              aria-label="Ask the knowledge base"
              className="flex h-8 w-8 items-center justify-center rounded-full bg-pine text-[#0c1711] transition-colors hover:bg-pine-dark disabled:bg-line-strong disabled:text-ink-2"
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <ArrowUp size={15} />}
            </button>
          </div>
        </div>
        </Card>
      </div>
    </div>
  );
}

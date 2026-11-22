"use client";

import { useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, Loader2, PenLine, RotateCcw, X } from "lucide-react";

import { setGapStatusAction } from "@/app/actions";
import type { Answer, Gap } from "@/lib/types";
import { Button, selectClass } from "./ui";

export function GapActions({ gap, approvedAnswers }: { gap: Gap; approvedAnswers: Answer[] }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [linking, setLinking] = useState(false);
  const [answerId, setAnswerId] = useState("");

  function change(status: "open" | "answered" | "wont_answer", linkedAnswerId?: string) {
    setError(null);
    startTransition(async () => {
      const result = await setGapStatusAction(gap.id, status, undefined, linkedAnswerId);
      if (result.ok) {
        setLinking(false);
        router.refresh();
      } else {
        setError(result.error);
      }
    });
  }

  return (
    <div className="flex flex-col items-end gap-2">
      {gap.status === "open" ? (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Link
            href={`/knowledge/new?q=${encodeURIComponent(gap.representative_question)}`}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-pine px-3 text-[11.5px] font-medium text-white hover:bg-pine-dark"
          >
            <PenLine size={12} /> Answer this
          </Link>
          {approvedAnswers.length ? (
            <Button type="button" size="sm" variant="secondary" onClick={() => setLinking((value) => !value)}>
              <Check size={12} /> Link answer
            </Button>
          ) : null}
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => change("wont_answer")}
            disabled={pending}
            title="Close this cluster without writing an answer"
          >
            {pending ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />} Won&apos;t answer
          </Button>
        </div>
      ) : (
        <Button type="button" size="sm" variant="secondary" onClick={() => change("open")} disabled={pending}>
          {pending ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />} Reopen
        </Button>
      )}

      {linking && gap.status === "open" ? (
        <div className="flex items-center gap-2">
          <select value={answerId} onChange={(event) => setAnswerId(event.target.value)} className={`${selectClass} h-8 w-64 text-[11.5px]`}>
            <option value="">Choose an approved answer…</option>
            {approvedAnswers.map((answer) => (
              <option key={answer.id} value={answer.id}>
                {answer.question}
              </option>
            ))}
          </select>
          <Button
            type="button"
            size="sm"
            variant="primary"
            disabled={!answerId || pending}
            onClick={() => change("answered", answerId)}
          >
            Close
          </Button>
        </div>
      ) : null}

      {error ? <p className="max-w-xs text-right text-[10.5px] text-rust">{error}</p> : null}
    </div>
  );
}

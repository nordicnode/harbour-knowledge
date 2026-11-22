"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Loader2, RefreshCw, Trash2 } from "lucide-react";

import { deleteSourceAction, reingestSourceAction } from "@/app/actions";
import { Button } from "./ui";

export function SourceActions({ id, canEdit }: { id: string; canEdit: boolean }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  if (!canEdit) return null;

  function reingest() {
    setMessage(null);
    startTransition(async () => {
      const result = await reingestSourceAction(id);
      if (result.ok) {
        setMessage("Harbour is reading it again. This page updates on its own.");
        router.refresh();
      } else {
        setMessage(result.error);
      }
    });
  }

  function remove() {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    startTransition(async () => {
      const result = await deleteSourceAction(id);
      if (result && !result.ok) setMessage(result.error);
    });
  }

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <Button type="button" size="sm" variant="secondary" onClick={reingest} disabled={pending}>
          {pending ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          Read again
        </Button>
        <Button type="button" size="sm" variant={confirming ? "danger" : "ghost"} onClick={remove} disabled={pending}>
          <Trash2 size={13} /> {confirming ? "Confirm remove" : "Remove"}
        </Button>
        {confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(false)}
            className="text-[11px] text-muted hover:text-ink"
          >
            Cancel
          </button>
        ) : null}
      </div>
      {message ? <p className="max-w-xs text-right text-[10.5px] text-muted">{message}</p> : null}
    </div>
  );
}

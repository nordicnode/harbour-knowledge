"use client";

import { useEffect } from "react";
import { AlertTriangle, RefreshCw, Terminal } from "lucide-react";

import { Button, Card, buttonStyles } from "@/components/ui";

export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <Card className="max-w-lg px-8 py-9">
        <div className="mb-5 flex h-11 w-11 items-center justify-center rounded-full bg-amber-soft text-amber">
          <AlertTriangle size={19} />
        </div>
        <p className="display text-[26px] text-ink">The workspace could not load.</p>
        <p className="mt-2 text-[13.5px] leading-relaxed text-muted">
          {error.message || "The knowledge API did not respond."}
        </p>
        <div className="mt-5 rounded-lg border border-line bg-paper px-4 py-3 font-mono text-[11.5px] text-ink-2">
          <div className="flex items-center gap-2 text-faint">
            <Terminal size={12} /> start the services
          </div>
          <p className="mt-2">make db &amp;&amp; make migrate</p>
          <p className="mt-1">make api</p>
          <p className="mt-1">make web</p>
        </div>
        <div className="mt-6 flex gap-2">
          <Button onClick={reset} variant="primary">
            <RefreshCw size={14} /> Try again
          </Button>
          <a href="/" className={buttonStyles("secondary")}>
            Reload workspace
          </a>
        </div>
      </Card>
    </div>
  );
}

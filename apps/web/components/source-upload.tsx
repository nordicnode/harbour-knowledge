"use client";

import { useActionState, useRef, useState } from "react";
import { FileText, Link2, Loader2, UploadCloud, X } from "lucide-react";

import { ActionResult, uploadSourceAction } from "@/app/actions";
import { cn } from "@/lib/cn";
import { Button, Card, inputClass } from "./ui";

const ACCEPT = ".pdf,.docx,.pptx,.html,.htm,.txt,.md,.markdown,.csv,.log";

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function SourceUpload() {
  const formRef = useRef<HTMLFormElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [dragging, setDragging] = useState(false);
  const [clientError, setClientError] = useState<string | null>(null);
  const [state, formAction, pending] = useActionState<ActionResult | null, FormData>(
    uploadSourceAction,
    null,
  );

  function choose(next: File | null) {
    setClientError(null);
    if (next && next.size > 15 * 1024 * 1024) {
      setClientError("Files are limited to 15 MB.");
      return;
    }
    setFile(next);
    if (next) {
      // Adding knowledge is one step: choosing a file starts the work.
      requestAnimationFrame(() => formRef.current?.requestSubmit());
    }
  }

  function onDrop(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const next = event.dataTransfer.files?.[0] ?? null;
    if (!next) return;
    if (inputRef.current) inputRef.current.files = event.dataTransfer.files;
    choose(next);
  }

  return (
    <Card id="add" className="scroll-mt-24 overflow-hidden border-pine/20">
      <form ref={formRef} action={formAction} className="p-3 sm:p-4">
        <input
          ref={inputRef}
          type="file"
          name="file"
          accept={ACCEPT}
          tabIndex={-1}
          aria-hidden="true"
          className="sr-only"
          onChange={(event) => choose(event.target.files?.[0] ?? null)}
        />
        <div className="grid gap-2.5 md:grid-cols-2">
          <div
            onClick={() => !pending && inputRef.current?.click()}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                inputRef.current?.click();
              }
            }}
            role="button"
            tabIndex={pending ? -1 : 0}
            aria-label="Choose a document to add"
            className={cn(
              "flex min-h-[76px] cursor-pointer items-center gap-3 rounded-xl border border-dashed px-3.5 py-3 transition-colors",
              dragging
                ? "border-pine bg-pine-soft/60"
                : "border-line-strong bg-surface-2/60 hover:border-pine/40 hover:bg-pine-soft/25",
              pending && "pointer-events-none opacity-70",
            )}
          >
            <div
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border bg-surface",
                file ? "border-pine/25 text-pine" : "border-line text-muted",
              )}
            >
              {pending ? (
                <Loader2 size={16} className="animate-spin" />
              ) : file ? (
                <FileText size={16} />
              ) : (
                <UploadCloud size={17} />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[12.5px] font-medium text-ink">
                {pending ? "Reading your source…" : file ? file.name : "Drop a document, or click to choose"}
              </p>
              <p className="mt-0.5 truncate text-[10.5px] text-faint">
                {file ? `${humanSize(file.size)} · keeping the original` : "PDF, Word, slides, HTML, or text · up to 15 MB"}
              </p>
            </div>
            {file && !pending ? (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  setFile(null);
                  if (inputRef.current) inputRef.current.value = "";
                }}
                aria-label="Choose a different document"
                className="rounded-md p-1 text-faint transition-colors hover:bg-paper-deep hover:text-ink"
              >
                <X size={14} />
              </button>
            ) : null}
          </div>

          <div className="flex items-center gap-2 rounded-xl border border-line bg-surface-2/60 px-3 py-2.5">
            <Link2 size={15} className="shrink-0 text-muted" />
            <input
              name="url"
              type="url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="…or paste a page link"
              aria-label="Source URL"
              className={`${inputClass} h-8 border-0 bg-transparent px-0 focus:outline-none`}
            />
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={pending || (!file && !url.trim())}
              className="shrink-0"
            >
              {pending ? <Loader2 size={12} className="animate-spin" /> : null}
              Add
            </Button>
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 px-1">
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-[10.5px] text-muted">
            <input type="checkbox" name="visibility" value="public" className="accent-pine" />
            Customers can see this source
          </label>
          <p className="text-[10.5px] text-faint">
            Harbour classifies it, checks it against the rest, and prepares answers on its own.
          </p>
        </div>

        {clientError ? (
          <p className="mt-2 rounded-md border border-rust/25 bg-rust-soft px-3 py-2 text-[11.5px] text-rust">
            {clientError}
          </p>
        ) : null}
        {state && !state.ok ? (
          <p className="mt-2 rounded-md border border-rust/25 bg-rust-soft px-3 py-2 text-[11.5px] text-rust">
            {state.error}
          </p>
        ) : null}
      </form>
    </Card>
  );
}

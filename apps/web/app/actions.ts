"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, apiForm, apiJson } from "@/lib/api";
import type { AskResult, ReviewItem } from "@/lib/types";

export type ActionResult<T = unknown> = { ok: true; data?: T } | { ok: false; error: string };

function toError(error: unknown): { ok: false; error: string } {
  if (error instanceof ApiError) return { ok: false, error: error.message };
  if (error instanceof Error) return { ok: false, error: error.message };
  return { ok: false, error: "Something went wrong." };
}

function listFrom(form: FormData, name: string): string[] {
  const raw = form.getAll(name).flatMap((value) => String(value).split(","));
  return [...new Set(raw.map((value) => value.trim().toLowerCase()).filter(Boolean))];
}

export async function askAction(input: {
  question: string;
  channel: string;
  plan?: string | null;
  version?: string | null;
  history?: { role: string; content: string }[];
}): Promise<ActionResult<AskResult>> {
  try {
    const result = await apiJson<AskResult>("/v1/ask", "POST", {
      question: input.question,
      channel: input.channel,
      plan: input.plan || null,
      product_version: input.version || null,
      history: input.history ?? [],
    });
    return { ok: true, data: result };
  } catch (error) {
    return toError(error);
  }
}

export async function feedbackAction(
  qaLogId: string,
  thumbs: -1 | 0 | 1,
  editedAnswerMd?: string,
  note?: string,
): Promise<ActionResult> {
  try {
    await apiJson(`/v1/ask/${qaLogId}/feedback`, "POST", {
      thumbs,
      edited_answer_md: editedAnswerMd ?? null,
      note: note ?? null,
    });
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function uploadSourceAction(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const file = form.get("file");
  const url = String(form.get("url") ?? "").trim();
  const hasFile = file instanceof File && file.size > 0;

  if (!hasFile && !url) {
    return { ok: false, error: "Choose a file or paste a link." };
  }

  try {
    if (hasFile) {
      const payload = new FormData();
      payload.set("file", file);
      payload.set("visibility", String(form.get("visibility") ?? "internal"));
      await apiForm("/v1/sources", payload);
    } else {
      await apiJson("/v1/sources", "POST", {
        url,
        visibility: form.get("visibility") ?? "internal",
      });
    }
    revalidatePath("/knowledge");
    revalidatePath("/");
    redirect("/knowledge");
  } catch (error) {
    if (error instanceof Error && error.message === "NEXT_REDIRECT") throw error;
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return toError(error);
  }
}

export async function reingestSourceAction(id: string): Promise<ActionResult> {
  try {
    await apiJson(`/v1/sources/${id}/reingest`, "POST");
    revalidatePath(`/sources/${id}`);
    revalidatePath("/knowledge");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function deleteSourceAction(
  id: string,
  redirectTo: string | null = "/knowledge",
): Promise<ActionResult> {
  try {
    // Deleting a source that is still being read waits for the per-source
    // ingest lock, so allow more than the default request budget.
    await apiJson(`/v1/sources/${id}`, "DELETE", undefined, 120_000);
    revalidatePath(`/sources/${id}`);
    revalidatePath("/knowledge");
    revalidatePath("/review");
    revalidatePath("/");
    revalidatePath("/", "layout");
    if (redirectTo) redirect(redirectTo);
    return { ok: true };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return toError(error);
  }
}

export async function deleteSourcesAction(
  ids: string[],
): Promise<ActionResult<{ deleted: number; missing: string[] }>> {
  try {
    const result = await apiJson<{ deleted: number; missing: string[] }>(
      "/v1/sources/bulk-delete",
      "POST",
      { ids },
      120_000,
    );
    revalidatePath("/knowledge");
    revalidatePath("/review");
    revalidatePath("/");
    revalidatePath("/", "layout");
    return { ok: true, data: result };
  } catch (error) {
    return toError(error);
  }
}

export async function saveAnswerAction(
  id: string,
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  try {
    await apiJson(`/v1/answers/${id}`, "PATCH", {
      answer_md: String(form.get("answer_md") ?? ""),
      change_reason: String(form.get("change_reason") ?? "") || null,
      topic_slug: form.get("topic_slug") || null,
      visibility: form.get("visibility"),
      plan_scope: listFrom(form, "plan_scope"),
      version_scope: listFrom(form, "version_scope"),
      review_interval_days: Number(form.get("review_interval_days") ?? 180),
    });
    revalidatePath(`/knowledge/${id}`);
    revalidatePath("/knowledge");
    revalidatePath("/review");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function approveAnswerAction(id: string): Promise<ActionResult> {
  try {
    await apiJson(`/v1/answers/${id}/approve`, "POST");
    revalidatePath(`/knowledge/${id}`);
    revalidatePath("/knowledge");
    revalidatePath("/review");
    revalidatePath("/");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function archiveAnswerAction(id: string): Promise<ActionResult> {
  try {
    await apiJson(`/v1/answers/${id}/archive`, "POST");
    revalidatePath(`/knowledge/${id}`);
    revalidatePath("/knowledge");
    revalidatePath("/review");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function createAnswerAction(
  _previous: ActionResult | null,
  form: FormData,
): Promise<ActionResult> {
  const question = String(form.get("question") ?? "").trim();
  const answerMd = String(form.get("answer_md") ?? "").trim();
  const passageIds = form.getAll("passage_ids").map(String).filter(Boolean);
  if (!question) return { ok: false, error: "Give the canonical answer a question." };
  if (!answerMd && passageIds.length === 0) {
    return { ok: false, error: "Select at least one source passage to draft from." };
  }

  try {
    const answer = await apiJson<{ id: string }>("/v1/answers", "POST", {
      question,
      answer_md: answerMd,
      topic_slug: form.get("topic_slug") || null,
      visibility: form.get("visibility") ?? "internal",
      plan_scope: listFrom(form, "plan_scope"),
      version_scope: listFrom(form, "version_scope"),
      review_interval_days: Number(form.get("review_interval_days") ?? 90),
      evidence: passageIds.map((passage_id) => ({ passage_id, quote: "" })),
    });
    revalidatePath("/knowledge");
    revalidatePath("/review");
    redirect(`/knowledge/${answer.id}`);
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return toError(error);
  }
}

export async function decideReviewAction(input: {
  id: string;
  decision: "approve" | "reject" | "defer";
  note?: string;
  resolution?: string;
}): Promise<ActionResult> {
  try {
    const result = await apiJson(`/v1/review-items/${input.id}/decide`, "POST", {
      decision: input.decision,
      note: input.note ?? null,
      resolution: input.resolution ?? null,
    });
    revalidatePath("/review");
    revalidatePath("/");
    revalidatePath("/knowledge");
    revalidatePath("/gaps");
    return { ok: true, data: result };
  } catch (error) {
    return toError(error);
  }
}

export async function undoReviewAction(id: string): Promise<ActionResult> {
  try {
    await apiJson(`/v1/review-items/${id}/undo`, "POST");
    revalidatePath("/review");
    revalidatePath("/");
    revalidatePath("/knowledge");
    revalidatePath("/gaps");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function setGapStatusAction(
  id: string,
  status: "open" | "answered" | "wont_answer",
  note?: string,
  linkedAnswerId?: string,
): Promise<ActionResult> {
  try {
    await apiJson(`/v1/gaps/${id}/status`, "POST", {
      status,
      note: note ?? null,
      linked_answer_id: linkedAnswerId ?? null,
    });
    revalidatePath("/gaps");
    revalidatePath("/");
    return { ok: true };
  } catch (error) {
    return toError(error);
  }
}

export async function loadReviewItem(id: string): Promise<ReviewItem | null> {
  try {
    const result = await apiJson<{ items: ReviewItem[] }>("/v1/review-items", "GET");
    return result.items.find((item) => item.id === id) ?? null;
  } catch {
    return null;
  }
}

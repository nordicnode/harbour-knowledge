const API_URL = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const TENANT = process.env.DEV_TENANT_SLUG ?? "harbour";
const USER = process.env.DEV_USER_EMAIL ?? "maya@harbour.example";

export class ApiError extends Error {
  status: number;
  code: string;
  correlationId: string | null;

  constructor(message: string, status: number, code: string, correlationId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

function headers(extra?: HeadersInit): Headers {
  const result = new Headers(extra);
  result.set("x-tenant-slug", TENANT);
  result.set("x-user-email", USER);
  return result;
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = await response.json();
    return new ApiError(
      body?.error?.message ?? `Request failed with ${response.status}`,
      response.status,
      body?.error?.code ?? "internal",
      body?.error?.correlation_id ?? null,
    );
  } catch {
    return new ApiError(`Request failed with ${response.status}`, response.status, "internal", null);
  }
}

async function request(path: string, init: RequestInit, timeoutMs = 45_000): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: headers(init.headers),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const timedOut =
      error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError");
    throw new ApiError(
      timedOut
        ? "The knowledge API took too long to answer. Try again in a moment."
        : "The knowledge API is not reachable. Start it with `make api`.",
      503,
      timedOut ? "timeout" : "provider_unavailable",
      null,
    );
  }
  if (!response.ok) {
    throw await parseError(response);
  }
  return response;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await request(path, { method: "GET" });
  return (await response.json()) as T;
}

export async function apiJson<T>(
  path: string,
  method: string,
  body?: unknown,
  timeoutMs = 45_000,
): Promise<T> {
  const response = await request(
    path,
    {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    timeoutMs,
  );
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiForm<T>(path: string, form: FormData): Promise<T> {
  const response = await request(path, { method: "POST", body: form });
  return (await response.json()) as T;
}

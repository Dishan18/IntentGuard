export type DecisionKind = "ALLOW" | "REVIEW" | "BLOCK";

export type DecisionRow = {
  id: string;
  time: string;
  agent_id: string | null;
  customer_id: string | null;
  customer_name: string | null;
  tool: string;
  amount: string | null;
  decision: DecisionKind;
  rule: string | null;
  codes: string[];
  paypal_call: "SENT" | "NOT_SENT" | "WAITING_FOR_OWNER" | "SEE_REVIEW";
  paypal_id: string | null;
  status: string;
  review_id: string | null;
  review_status: string | null;
  owner_approved: boolean;
};

export type Check = { rule: string; result: "PASS" | "FAIL" | "REVIEW"; detail: string; code: string | null };

export type Review = {
  review_id: string;
  status: string;
  created_at: string;
  agent_id: string;
  customer_id: string | null;
  tool: string;
  arguments: Record<string, unknown>;
  amount: string | null;
  codes: string[];
  reasons: { rule: string; code: string; detail: string }[];
  resolved_at: string | null;
  resolved_by: string | null;
  note: string | null;
};

export type DecisionDetail = DecisionRow & {
  mandate: {
    id: string;
    version: number;
    status: string;
    source_text: string;
    refunds: Record<string, string | number | null> | null;
    invoices: Record<string, string | number | null> | null;
  } | null;
  arguments: Record<string, unknown>;
  paypal_response: unknown;
  checks: Check[];
  ledger: { status: string; amount: string; refund_id: string | null; paypal_status: string | null } | null;
  review: Review | null;
  timeline: { at: string; what: string }[];
};

export type Summary = {
  day: string;
  refunds_auto: { count: number; amount: string };
  refunds_owner_approved: { count: number; amount: string };
  held_for_review: { count: number; amount: string };
  pending_reviews: number;
  blocked: { count: number };
  protected: string;
  daily_caps: { mandate_id: string; agent_id: string; used: string; cap: string }[];
  calls_today: number;
};

export type Snapshot = { summary: Summary; decisions: DecisionRow[]; reviews: Review[] };

export type ReviewResult = {
  review_id: string;
  status: string;
  codes?: string[];
  message?: string;
  paypal_call?: string;
  paypal?: { id?: string; status?: string } | string;
  error?: string;
};

export class Unauthorized extends Error {}

const KEY_STORAGE = "intentguard.ownerKey";

export function savedKey(): string {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function saveKey(key: string | null) {
  try {
    if (key) sessionStorage.setItem(KEY_STORAGE, key);
    else sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    /* private mode: the key just isn't remembered */
  }
}

async function call<T>(key: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
  });
  if (response.status === 401) throw new Unauthorized("That owner key isn't valid.");
  const body = await response.json().catch(() => ({}));
  if (!response.ok && response.status !== 409) throw new Error(body.message ?? body.error ?? `HTTP ${response.status}`);
  return body as T;
}

export const getSummary = (key: string) => call<Summary>(key, "/owner/summary");
export const getDetail = (key: string, id: string) => call<DecisionDetail>(key, `/owner/decisions/${id}`);
export const decide = (key: string, reviewId: string, action: "approve" | "reject", note: string) =>
  call<ReviewResult>(key, `/owner/reviews/${reviewId}/${action}`, { method: "POST", body: JSON.stringify({ note }) });

/** Follows the live stream, reconnecting with backoff, until `signal` aborts. */
export async function follow(
  key: string,
  onSnapshot: (snapshot: Snapshot) => void,
  onConnection: (live: boolean) => void,
  signal: AbortSignal,
): Promise<void> {
  let delay = 1000;
  while (!signal.aborted) {
    try {
      const response = await fetch("/owner/stream", { headers: { Authorization: `Bearer ${key}` }, signal });
      if (response.status === 401) throw new Unauthorized("That owner key isn't valid.");
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      onConnection(true);
      delay = 1000;
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let end: number;
        while ((end = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const data = block.split("\n").find((line) => line.startsWith("data: "));
          if (block.startsWith("event: snapshot") && data) onSnapshot(JSON.parse(data.slice(6)));
        }
      }
    } catch (error) {
      if (error instanceof Unauthorized) throw error;
      if (signal.aborted) return;
    }
    onConnection(false);
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 15000);
  }
}

// ---------------------------------------------------------------- mandates

export type Example = { scenario: string; decision: DecisionKind; codes: string[]; why: string };

export type MandateVersion = {
  mandate_id: string;
  version: number;
  agent_id: string;
  status: "DRAFT" | "ACTIVE" | "SUPERSEDED" | "INVALID";
  source_text: string;
  compiled_by: string;
  created_at: string;
  activated_at: string | null;
  activated_by: string | null;
  rules: string[];
  examples: Example[];
  warnings: string[];
  blocking: string[];
  unsupported: string[];
  mapping: { text: string; becomes: string }[];
  changes: { field: string; from: string | null; to: string | null }[];
};

export type AgentMandates = {
  agent: { id: string; name: string; purpose: string; status: string; allowed_tools: string[] };
  active: MandateVersion | null;
  history: {
    version: number;
    status: string;
    compiled_by: string;
    created_at: string;
    activated_at: string | null;
    activated_by: string | null;
    source_text: string;
  }[];
};

export const getMandates = (key: string) => call<AgentMandates[]>(key, "/owner/mandates");
export const getMandateVersion = (key: string, id: string, version: number) =>
  call<MandateVersion>(key, `/owner/mandates/${id}/${version}`);
export const compileMandate = (key: string, agentId: string, text: string) =>
  call<MandateVersion & { error?: string; message?: string }>(key, "/owner/mandates/compile", {
    method: "POST",
    body: JSON.stringify({ agent_id: agentId, text }),
  });
export const restoreMandate = (key: string, id: string, version: number) =>
  call<MandateVersion & { error?: string; message?: string }>(key, `/owner/mandates/${id}/${version}/restore`, {
    method: "POST",
  });
export const activateMandate = (key: string, id: string, version: number) =>
  call<MandateVersion & { error?: string; message?: string }>(key, `/owner/mandates/${id}/${version}/activate`, {
    method: "POST",
  });

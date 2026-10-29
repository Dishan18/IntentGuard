export type Customer = { id: string; name: string };

export type Order = {
  order_id: string;
  item: string;
  amount: string;
  currency: string;
  refunded: string | null;
  /** The latest refund request for this order that needed the owner's approval. */
  review: { id: string; status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "FAILED"; amount: string | null; note: string | null } | null;
};

export type Session = { customer: Customer | null; orders: Order[]; suggestions: string[] };

/** IntentGuard's decision on one tool call the agent made, as streamed by the store's API. */
export type Decision = {
  tool: string;
  amount: string | null;
  decision: "ALLOW" | "REVIEW" | "BLOCK" | "ERROR";
  codes?: string[];
  message?: string;
  paypal_call?: "NOT_SENT" | "NOT_SENT_YET";
  paypal_status?: string;
  paypal_id?: string;
  review_id?: string;
};

export type ChatEvent =
  | { kind: "tool_start"; tool: string; amount: string | null }
  | ({ kind: "tool" } & Decision)
  | { kind: "reply"; text: string; model: string; degraded: boolean }
  | { kind: "error"; message: string };

export type Turn = { role: "user" | "model"; text: string };

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json() as Promise<T>;
}

export const getCustomers = () => fetch("/api/customers").then((r) => json<Customer[]>(r));
export const getSession = () => fetch("/api/session").then((r) => json<Session>(r));
export const login = (customerId: string) =>
  fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customer_id: customerId }),
  }).then((r) => json<Customer>(r));

/** One chat turn. Calls `onEvent` for each server-sent event as it arrives. */
export async function chat(
  message: string,
  history: Turn[],
  compromised: boolean,
  onEvent: (event: ChatEvent) => void,
): Promise<void> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, history, compromised }),
  });
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    onEvent({ kind: "error", message: detail.error ?? `The store returned ${response.status}.` });
    return;
  }
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
      let kind = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) kind = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (kind && data) onEvent({ kind, ...JSON.parse(data) } as ChatEvent);
    }
  }
}

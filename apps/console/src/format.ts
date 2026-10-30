export const TOOL_LABELS: Record<string, string> = {
  create_refund: "Refund",
  get_order: "Order lookup",
  get_refund: "Refund lookup",
  create_invoice: "Create invoice",
  send_invoice: "Send invoice",
  send_invoice_reminder: "Invoice reminder",
  get_invoice: "Invoice lookup",
  list_invoices: "List invoices",
  cancel_sent_invoice: "Cancel invoice",
};

export const AGENT_LABELS: Record<string, string> = {
  "support-agent": "Support Agent",
  "billing-agent": "Billing Agent",
};

export const DECISION_LABELS = { ALLOW: "Allowed", REVIEW: "Held", BLOCK: "Blocked" } as const;

export function money(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-US", { style: "currency", currency: "USD" })
    : `$${value}`;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
}

/** "max_order_age_days" → "Max order age (days)" */
export function ruleLabel(key: string): string {
  const words = key.replace(/_days$/, " (days)").replace(/_max$/, " max").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

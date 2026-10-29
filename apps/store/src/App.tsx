import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { chat, Customer, Decision, getCustomers, getSession, login, Order, Turn } from "./api";

type Item =
  | { id: number; type: "user"; text: string }
  | { id: number; type: "agent"; text: string; model: string; degraded: boolean }
  | { id: number; type: "check"; tool: string; amount: string | null; result?: Decision }
  | { id: number; type: "error"; text: string }
  | { id: number; type: "notice"; text: string; tone: "ok" | "bad" | "neutral" };

const TOOL_LABELS: Record<string, string> = {
  create_refund: "Refund",
  get_order: "Order lookup",
  get_refund: "Refund lookup",
};

let nextId = 1;

function money(value: string | null | undefined) {
  return value ? `$${value}` : "";
}

/** A held refund is settled once the owner has decided and, if approved, PayPal has refunded it. */
function settled(order: Order): boolean {
  const review = order.review;
  if (!review || review.status === "PENDING") return false;
  return review.status !== "APPROVED" || order.refunded !== null;
}

/** What the store tells the customer when the owner decides on their held refund. */
function decisionNotice(order: Order): { text: string; tone: "ok" | "bad" | "neutral" } {
  const review = order.review!;
  const amount = money(review.amount);
  switch (review.status) {
    case "APPROVED":
      return { text: `Maya approved your refund of ${amount} for the ${order.item}. It's on its way to your PayPal account.`, tone: "ok" };
    case "REJECTED":
      return {
        text: `Maya reviewed your refund request for the ${order.item} and declined it.${review.note ? ` Her note: "${review.note}"` : ""}`,
        tone: "bad",
      };
    case "EXPIRED":
      return { text: `Your refund request for the ${order.item} expired before it was reviewed. You're welcome to ask again.`, tone: "neutral" };
    default:
      return { text: `Your approved refund for the ${order.item} couldn't be completed. The store will contact you.`, tone: "bad" };
  }
}

export default function App() {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [history, setHistory] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [compromised, setCompromised] = useState(false);
  const [showChecks, setShowChecks] = useState(true);
  const log = useRef<HTMLDivElement>(null);
  // Held refunds seen on this page that the owner hadn't settled yet, so we tell the customer
  // when that changes (and not about decisions made before they opened the chat).
  const awaiting = useRef(new Set<string>());

  const refresh = useCallback(
    () =>
      getSession().then((session) => {
        setCustomer(session.customer);
        setOrders(session.orders);
        setSuggestions(session.suggestions);
        for (const order of session.orders) {
          const review = order.review;
          if (!review) continue;
          if (!settled(order)) {
            awaiting.current.add(review.id);
          } else if (awaiting.current.delete(review.id)) {
            const notice = decisionNotice(order);
            setItems((current) => [...current, { id: nextId++, type: "notice", ...notice }]);
            // Let the agent know too, so a follow-up question gets a consistent answer.
            setHistory((current) => {
              const last = current[current.length - 1];
              if (!last) return current;
              const text = `[Store update] ${notice.text}`;
              return last.role === "model"
                ? [...current.slice(0, -1), { role: "model", text: `${last.text}\n\n${text}` }]
                : [...current, { role: "model", text }];
            });
          }
        }
      }),
    [],
  );

  // While a refund waits for the owner, check back every few seconds.
  const waiting = orders.some((order) => order.review && !settled(order));
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [waiting, refresh]);

  useEffect(() => {
    getCustomers().then(setCustomers);
    refresh();
  }, [refresh]);

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [items, busy]);

  async function switchCustomer(id: string) {
    await login(id);
    awaiting.current.clear();
    setItems([]);
    setHistory([]);
    setDraft("");
    await refresh();
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy || !customer) return;
    setDraft("");
    setBusy(true);
    setItems((current) => [...current, { id: nextId++, type: "user", text: message }]);
    let replied = "";
    await chat(message, history, compromised, (event) => {
      if (event.kind === "tool_start") {
        setItems((current) => [...current, { id: nextId++, type: "check", tool: event.tool, amount: event.amount }]);
      } else if (event.kind === "tool") {
        const { kind: _, ...result } = event;
        setItems((current) => {
          const index = current.findLastIndex((item) => item.type === "check" && !item.result);
          if (index === -1) return current;
          const updated = [...current];
          updated[index] = { ...(current[index] as Extract<Item, { type: "check" }>), result };
          return updated;
        });
      } else if (event.kind === "reply") {
        replied = event.text;
        setItems((current) => [
          ...current,
          { id: nextId++, type: "agent", text: event.text, model: event.model, degraded: event.degraded },
        ]);
      } else {
        setItems((current) => [...current, { id: nextId++, type: "error", text: event.message }]);
      }
    }).catch((error: Error) =>
      setItems((current) => [...current, { id: nextId++, type: "error", text: error.message }]),
    );
    if (replied) setHistory((current) => [...current, { role: "user", text: message }, { role: "model", text: replied }]);
    setBusy(false);
    refresh();
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    send(draft);
  }

  return (
    <div className="page">
      <header className="masthead">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            ◍
          </span>
          <div>
            <h1>Maya's Pottery</h1>
            <p>Small-batch stoneware, thrown in Portland</p>
          </div>
        </div>
        <label className="login">
          <span>Logged in as</span>
          <select value={customer?.id ?? ""} onChange={(e) => switchCustomer(e.target.value)}>
            <option value="" disabled>
              Choose a customer
            </option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      </header>

      <div className="demo-bar" role="group" aria-label="Demo controls">
        <span className="demo-tag">PayPal sandbox demo</span>
        <label className="toggle">
          <input type="checkbox" checked={showChecks} onChange={(e) => setShowChecks(e.target.checked)} />
          Show IntentGuard decisions
        </label>
        <label className={`toggle ${compromised ? "danger" : ""}`}>
          <input type="checkbox" checked={compromised} onChange={(e) => setCompromised(e.target.checked)} />
          Simulate a compromised agent
        </label>
      </div>

      {!customer ? (
        <main className="empty">
          <h2>Pick a customer to log in as</h2>
          <p>
            The support assistant is an AI agent that can refund orders through PayPal. Every refund it attempts goes
            through IntentGuard, which applies the owner's rules before anything reaches PayPal.
          </p>
          <div className="customer-grid">
            {customers.map((c) => (
              <button key={c.id} onClick={() => switchCustomer(c.id)}>
                {c.name}
              </button>
            ))}
          </div>
        </main>
      ) : (
        <main className="layout">
          <section className="orders" aria-label="Your orders">
            <h2>Your orders</h2>
            {orders.length === 0 && <p className="muted">No orders yet.</p>}
            <ul>
              {orders.map((order) => (
                <li key={order.order_id} className="order">
                  <span className="order-thumb" aria-hidden>
                    {order.item[0]}
                  </span>
                  <div className="order-body">
                    <strong>{order.item}</strong>
                    <span className="muted mono">#{order.order_id.slice(-8)}</span>
                  </div>
                  <div className="order-side">
                    <span className="order-amount">{money(order.amount)}</span>
                    <OrderBadge order={order} />
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="chat" aria-label="Customer support chat">
            <div className="chat-head">
              <div>
                <h2>Support</h2>
                <p className="muted">AI assistant · refunds checked by IntentGuard</p>
              </div>
              {compromised && <span className="badge bad">Agent compromised (simulated)</span>}
            </div>

            <div className="log" ref={log} aria-live="polite">
              {items.length === 0 && (
                <p className="muted intro">Hi {customer.name.split(" ")[0]}! Ask about an order or a refund.</p>
              )}
              {items.map((item) => {
                if (item.type === "user") return <div key={item.id} className="bubble user">{item.text}</div>;
                if (item.type === "agent")
                  return (
                    <div key={item.id} className="bubble agent">
                      {item.text}
                      {item.degraded && <span className="fineprint">Assistant offline: reply from IntentGuard's record</span>}
                    </div>
                  );
                if (item.type === "error") return <div key={item.id} className="bubble error">{item.text}</div>;
                if (item.type === "notice")
                  return (
                    <div key={item.id} className={`notice ${item.tone}`} role="status">
                      <span className="notice-from">Maya's Pottery</span>
                      {item.text}
                    </div>
                  );
                return showChecks ? <Check key={item.id} item={item} /> : null;
              })}
              {busy && !items.some((item) => item.type === "check" && !item.result) && (
                <div className="typing" aria-label="Assistant is typing">
                  <span />
                  <span />
                  <span />
                </div>
              )}
            </div>

            <div className="suggestions">
              {suggestions.map((text) => (
                <button key={text} disabled={busy} onClick={() => send(text)} title={text}>
                  {text}
                </button>
              ))}
            </div>

            <form className="composer" onSubmit={onSubmit}>
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Type a message"
                maxLength={2000}
                aria-label="Message"
              />
              <button type="submit" disabled={busy || !draft.trim()}>
                Send
              </button>
            </form>
          </section>
        </main>
      )}
    </div>
  );
}

function OrderBadge({ order }: { order: Order }) {
  if (order.refunded) return <span className="badge ok">Refunded {money(order.refunded)}</span>;
  const status = order.review?.status;
  if (status === "PENDING" || status === "APPROVED") return <span className="badge wait">Refund awaiting approval</span>;
  if (status === "REJECTED") return <span className="badge bad">Refund declined</span>;
  if (status === "FAILED") return <span className="badge bad">Refund not completed</span>;
  return <span className="badge">Paid</span>;
}

function Check({ item }: { item: Extract<Item, { type: "check" }> }) {
  const label = `${TOOL_LABELS[item.tool] ?? item.tool}${item.amount ? ` ${money(item.amount)}` : ""}`;
  const result = item.result;
  if (!result)
    return (
      <div className="check pending">
        <span className="check-head">
          <Shield /> IntentGuard · {label}
        </span>
        <span className="check-status">Checking against the owner's rules…</span>
      </div>
    );

  const tone = { ALLOW: "ok", REVIEW: "wait", BLOCK: "bad", ERROR: "bad" }[result.decision];
  const verdict = {
    ALLOW: "Allowed",
    REVIEW: "Held for the owner's approval",
    BLOCK: "Blocked",
    ERROR: "PayPal returned an error",
  }[result.decision];
  const paypal =
    result.decision === "BLOCK"
      ? "PayPal call not sent"
      : result.decision === "REVIEW"
        ? "PayPal call not sent yet"
        : result.paypal_status
          ? `PayPal: ${result.paypal_status}`
          : "";

  return (
    <div className={`check ${tone}`}>
      <span className="check-head">
        <Shield /> IntentGuard · {label}
      </span>
      <span className="check-status">
        <strong>{verdict}</strong>
        {paypal && <span className="check-paypal">{paypal}</span>}
      </span>
      {result.codes && (
        <span className="codes">
          {result.codes.map((code) => (
            <code key={code}>{code}</code>
          ))}
        </span>
      )}
      {result.message && <span className="check-message">{result.message}</span>}
    </div>
  );
}

function Shield() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden>
      <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3z" fill="currentColor" />
    </svg>
  );
}

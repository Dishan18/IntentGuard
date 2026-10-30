import { FormEvent, useCallback, useEffect, useState } from "react";
import { decide, follow, getSummary, type Review, type ReviewResult, saveKey, savedKey, type Snapshot, Unauthorized } from "./api";
import { DetailPanel } from "./Detail";
import { DecisionGrid } from "./Grid";
import { Mandates } from "./Mandates";
import { RedTeam } from "./RedTeam";
import { AGENT_LABELS, clock, money, TOOL_LABELS } from "./format";

export default function App() {
  const [ownerKey, setOwnerKey] = useState(savedKey);
  return ownerKey ? (
    <Console ownerKey={ownerKey} onSignOut={() => (saveKey(null), setOwnerKey(""))} />
  ) : (
    <Login onKey={(key) => (saveKey(key), setOwnerKey(key))} />
  );
}

function Shield({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3z" fill="currentColor" />
      <path d="m8.5 12 2.4 2.4 4.6-4.8" fill="none" stroke="var(--brand-ink)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Login({ onKey }: { onKey: (key: string) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await getSummary(key.trim());
      onKey(key.trim());
    } catch (e) {
      setError(e instanceof Unauthorized ? "That owner key isn't valid." : `Can't reach the gateway: ${(e as Error).message}`);
    }
    setBusy(false);
  }

  return (
    <main className="login">
      <form className="panel login-card" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark">
            <Shield />
          </span>
          <div>
            <h1>IntentGuard</h1>
            <p className="muted">Owner console · Maya's Pottery</p>
          </div>
        </div>
        <label htmlFor="key">Owner key</label>
        <input id="key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="igo_…" />
        <p className="muted small">
          The <code>owner</code> entry in <code>data/agent_keys.json</code> (from <code>scripts/register_agents.py</code>). Agent keys don't work here.
        </p>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={!key.trim() || busy}>
          {busy ? "Checking…" : "Open console"}
        </button>
      </form>
    </main>
  );
}

function Console({ ownerKey, onSignOut }: { ownerKey: string; onSignOut: () => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [live, setLive] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [view, setView] = useState<"activity" | "mandates" | "redteam">("activity");

  const notify = useCallback((notice: Omit<Notice, "id">) => {
    const id = Date.now() + Math.random();
    setNotices((current) => [...current, { ...notice, id }]);
    setTimeout(() => setNotices((current) => current.filter((n) => n.id !== id)), 10000);
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    follow(
      ownerKey,
      (next) => (setSnapshot(next), setVersion((v) => v + 1)),
      setLive,
      abort.signal,
    ).catch((error) => error instanceof Unauthorized && onSignOut());
    return () => abort.abort();
  }, [ownerKey, onSignOut]);

  const close = useCallback(() => setSelected(null), []);

  return (
    <div className={`app ${selected ? "with-detail" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            <Shield />
          </span>
          <div>
            <h1>IntentGuard</h1>
            <p className="muted">Owner console · Maya's Pottery · PayPal sandbox</p>
          </div>
        </div>
        <nav className="tabs" aria-label="Console sections">
          <button className={view === "activity" ? "active" : ""} onClick={() => setView("activity")}>
            Activity
            {snapshot && snapshot.summary.pending_reviews > 0 && <span className="badge">{snapshot.summary.pending_reviews}</span>}
          </button>
          <button className={view === "mandates" ? "active" : ""} onClick={() => setView("mandates")}>
            Mandates
          </button>
          <button className={view === "redteam" ? "active" : ""} onClick={() => setView("redteam")}>
            Red team
          </button>
        </nav>
        <div className="topbar-right">
          <span className={`live ${live ? "on" : ""}`} role="status">
            <span className="live-dot" aria-hidden />
            {live ? "Live" : "Reconnecting…"}
          </span>
          <button className="ghost" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      <div className="notices" aria-live="polite">
        {notices.map((notice) => (
          <div key={notice.id} className={`notice notice-${notice.tone}`} role="status">
            {notice.text}
            <button className="icon-button" onClick={() => setNotices((c) => c.filter((n) => n.id !== notice.id))} aria-label="Dismiss">
              ✕
            </button>
          </div>
        ))}
      </div>

      {view === "mandates" ? (
        <main className="content">
          <Mandates ownerKey={ownerKey} onNotice={(text, tone) => notify({ text, tone })} />
        </main>
      ) : view === "redteam" ? (
        <main className="content">
          <RedTeam ownerKey={ownerKey} onNotice={(text, tone) => notify({ text, tone })} />
        </main>
      ) : !snapshot ? (
        <p className="muted loading">Connecting to the gateway…</p>
      ) : (
        <main className="content">
          <Kpis snapshot={snapshot} />
          {snapshot.reviews.length > 0 && (
            <ReviewQueue ownerKey={ownerKey} reviews={snapshot.reviews} onOpen={setSelected} onResult={notify} snapshot={snapshot} />
          )}
          <div className="workspace">
            <DecisionGrid rows={snapshot.decisions} selectedId={selected} onSelect={setSelected} />
            {selected && <DetailPanel ownerKey={ownerKey} id={selected} version={version} onClose={close} />}
          </div>
        </main>
      )}
    </div>
  );
}

function Kpis({ snapshot }: { snapshot: Snapshot }) {
  const s = snapshot.summary;
  const cap = s.daily_caps[0];
  const used = cap ? Number(cap.used) : 0;
  const limit = cap ? Number(cap.cap) : 0;
  const share = limit ? Math.min(used / limit, 1) : 0;
  return (
    <section className="kpis" aria-label={`Today (${s.day}, UTC)`}>
      <Kpi label="Refunded automatically" value={String(s.refunds_auto.count)} sub={money(s.refunds_auto.amount)} />
      <Kpi label="Approved by you" value={String(s.refunds_owner_approved.count)} sub={money(s.refunds_owner_approved.amount)} />
      <Kpi label="Waiting for you" value={String(s.pending_reviews)} sub={`${s.held_for_review.count} held today`} tone={s.pending_reviews ? "wait" : undefined} />
      <Kpi label="Blocked" value={String(s.blocked.count)} sub={`${s.calls_today} agent actions today`} tone={s.blocked.count ? "bad" : undefined} />
      <Kpi label="Money protected" value={money(s.protected)} sub="refunds agents tried that the rules stopped" tone="good" />
      {cap && (
        <div className="kpi kpi-cap">
          <span className="kpi-label">Daily refund cap · {AGENT_LABELS[cap.agent_id] ?? cap.agent_id}</span>
          <span className="kpi-value">
            {money(cap.used)} <span className="muted kpi-of">of {money(cap.cap)}</span>
          </span>
          <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={limit} aria-valuenow={used}>
            <span className={share >= 0.9 ? "high" : ""} style={{ width: `${share * 100}%` }} />
          </div>
        </div>
      )}
    </section>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: "good" | "bad" | "wait" }) {
  return (
    <div className={`kpi ${tone ? `kpi-${tone}` : ""}`}>
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">{value}</span>
      <span className="kpi-sub muted">{sub}</span>
    </div>
  );
}

type Notice = { id: number; text: string; tone: "ok" | "bad" };

/** What happened when the owner decided, in one sentence. */
function describe(result: ReviewResult, amount: string, tool: string): Omit<Notice, "id"> {
  if (result.status === "APPROVED") {
    const paypal = typeof result.paypal === "object" ? result.paypal : undefined;
    if (tool === "create_refund")
      return { text: `Approved: PayPal refund ${paypal?.id ?? ""} ${paypal?.status?.toLowerCase() ?? "sent"} for ${amount}.`, tone: "ok" };
    return { text: `Approved: ${TOOL_LABELS[tool] ?? tool} for ${amount} sent to PayPal.`, tone: "ok" };
  }
  if (result.status === "REJECTED") return { text: `Rejected. Nothing was sent to PayPal.`, tone: "ok" };
  if (result.status === "FAILED")
    return {
      text: `Not sent: the request no longer passes your rules (${(result.codes ?? []).join(", ") || result.message || "PayPal error"}).`,
      tone: "bad",
    };
  return { text: result.message ?? `This review is already ${String(result.status).toLowerCase()}.`, tone: "bad" };
}

function ReviewQueue({
  ownerKey,
  reviews,
  onOpen,
  onResult,
  snapshot,
}: {
  ownerKey: string;
  reviews: Review[];
  onOpen: (id: string) => void;
  onResult: (notice: Omit<Notice, "id">) => void;
  snapshot: Snapshot;
}) {
  return (
    <section className="panel queue" aria-label="Waiting for your approval">
      <div className="panel-head">
        <div>
          <h2>Waiting for your approval</h2>
          <p className="muted">Approving re-checks the request against PayPal and your rules, then sends exactly what's shown.</p>
        </div>
      </div>
      <div className="queue-list">
        {reviews.map((review) => (
          <ReviewCard
            key={review.review_id}
            ownerKey={ownerKey}
            review={review}
            customerName={snapshot.decisions.find((d) => d.review_id === review.review_id)?.customer_name ?? review.customer_id}
            callId={snapshot.decisions.find((d) => d.review_id === review.review_id && d.decision === "REVIEW")?.id}
            onOpen={onOpen}
            onResult={onResult}
          />
        ))}
      </div>
    </section>
  );
}

function ReviewCard({
  ownerKey,
  review,
  customerName,
  callId,
  onOpen,
  onResult,
}: {
  ownerKey: string;
  review: Review;
  customerName: string | null;
  callId?: string;
  onOpen: (id: string) => void;
  onResult: (notice: Omit<Notice, "id">) => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"" | "approve" | "reject">("");
  const [error, setError] = useState("");

  async function act(action: "approve" | "reject") {
    setBusy(action);
    setError("");
    try {
      // The card leaves the queue on the next live update, so the outcome is reported up.
      onResult(describe(await decide(ownerKey, review.review_id, action, note), money(review.amount), review.tool));
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy("");
  }

  const target = String(review.arguments.capture_id ?? review.arguments.invoice_id ?? "");
  return (
    <article className="review-card">
      <div className="review-main">
        <div className="review-title">
          <strong>
            {TOOL_LABELS[review.tool] ?? review.tool} {money(review.amount)}
          </strong>
          <span className="muted">
            for {customerName} · {AGENT_LABELS[review.agent_id] ?? review.agent_id} · {clock(review.created_at)}
          </span>
        </div>
        <ul className="reasons">
          {review.reasons.map((reason) => (
            <li key={reason.code}>
              <code>{reason.code}</code> {reason.detail}
            </li>
          ))}
        </ul>
        <p className="muted small">
          Capture <code>{target}</code>
          {callId && (
            <>
              {" · "}
              <button className="link" onClick={() => onOpen(callId)}>
                Full audit trail
              </button>
            </>
          )}
        </p>
      </div>
      <div className="review-actions">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (shown to the customer)"
          maxLength={500}
          aria-label="Note for the customer"
          disabled={!!busy}
        />
        <div className="buttons">
          <button className="primary" disabled={!!busy} onClick={() => act("approve")}>
            {busy === "approve" ? "Checking and sending…" : "Approve"}
          </button>
          <button className="danger" disabled={!!busy} onClick={() => act("reject")}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </button>
        </div>
        {error && <p className="error small">{error}</p>}
      </div>
    </article>
  );
}

import { useEffect, useState } from "react";
import { type DecisionDetail, getDetail } from "./api";
import { DecisionChip } from "./Grid";
import { AGENT_LABELS, dateTime, money, ruleLabel, TOOL_LABELS } from "./format";

const RESULT_ICON = { PASS: "✓", FAIL: "✕", REVIEW: "!" } as const;

export function DetailPanel({ ownerKey, id, version, onClose }: { ownerKey: string; id: string; version: number; onClose: () => void }) {
  const [detail, setDetail] = useState<DecisionDetail | null>(null);
  const [error, setError] = useState("");

  // Refetch when the live snapshot changes, so the timeline keeps up (an approval, a webhook).
  useEffect(() => {
    let cancelled = false;
    getDetail(ownerKey, id)
      .then((d) => !cancelled && (setDetail(d), setError("")))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [ownerKey, id, version]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside className="detail" aria-label="Decision detail">
      <div className="detail-head">
        <div>
          <p className="eyebrow mono">{id}</p>
          {detail && (
            <h2>
              {TOOL_LABELS[detail.tool] ?? detail.tool} {detail.amount && money(detail.amount)}
              {detail.customer_name && <span className="muted"> · {detail.customer_name}</span>}
            </h2>
          )}
        </div>
        <button className="icon-button" onClick={onClose} aria-label="Close detail">
          ✕
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {!detail && !error && <p className="muted">Loading…</p>}
      {detail && (
        <div className="detail-body">
          <div className="detail-verdict">
            <DecisionChip decision={detail.decision} approved={detail.owner_approved} />
            <span className={`tag ${detail.paypal_call === "SENT" ? "tag-sent" : "tag-stopped"}`}>
              {detail.paypal_call === "SENT" ? "PayPal call sent" : detail.paypal_call === "WAITING_FOR_OWNER" ? "Waiting for you" : detail.paypal_call === "SEE_REVIEW" ? "Sent after your approval" : "PayPal call not sent"}
            </span>
            <span className="muted">
              {AGENT_LABELS[detail.agent_id ?? ""] ?? detail.agent_id} · {dateTime(detail.time)}
            </span>
          </div>

          <p className="summary">{detail.summary}</p>

          <section>
            <h3>
              <span className="step">1</span> What you authorized
            </h3>
            {detail.mandate ? (
              <>
                <blockquote>{detail.mandate.source_text}</blockquote>
                {detail.mandate.refunds && (
                  <dl className="limits">
                    {Object.entries(detail.mandate.refunds)
                      .filter(([, value]) => value !== null)
                      .map(([key, value]) => (
                        <div key={key}>
                          <dt>{ruleLabel(key)}</dt>
                          <dd>{key.endsWith("_max") ? money(String(value)) : String(value)}</dd>
                        </div>
                      ))}
                  </dl>
                )}
                <p className="muted small">
                  Mandate <code>{detail.mandate.id}</code> v{detail.mandate.version}, {detail.mandate.status.toLowerCase()}
                </p>
              </>
            ) : (
              <p className="muted">No mandate for this agent: everything it tries is blocked.</p>
            )}
          </section>

          <section>
            <h3>
              <span className="step">2</span> What the agent tried
            </h3>
            <p>
              <code>{detail.tool}</code> with exactly these arguments:
            </p>
            <pre>{JSON.stringify(detail.arguments, null, 2)}</pre>
          </section>

          <section>
            <h3>
              <span className="step">3</span> What PayPal says
            </h3>
            {detail.paypal_response ? (
              <PayPalResponse response={detail.paypal_response} />
            ) : (
              <p className="muted">Nothing was sent to PayPal for this call. The facts IntentGuard read from PayPal are in the checks below.</p>
            )}
            {detail.ledger && (
              <p className="small">
                Ledger: <strong>{detail.ledger.status.toLowerCase()}</strong> {money(detail.ledger.amount)}
                {detail.ledger.paypal_status && <>, PayPal reports <strong>{detail.ledger.paypal_status.toLowerCase()}</strong></>}
              </p>
            )}
          </section>

          <section>
            <h3>
              <span className="step">4</span> Rule by rule
            </h3>
            <ol className="checks">
              {detail.checks.map((check, index) => (
                <li key={index} className={`check check-${check.result.toLowerCase()}`}>
                  <span className="check-icon" aria-label={check.result}>
                    {RESULT_ICON[check.result]}
                  </span>
                  <span className="check-rule">{ruleLabel(check.rule)}</span>
                  <span className="check-detail">
                    {check.detail}
                    {check.code && <code>{check.code}</code>}
                  </span>
                </li>
              ))}
            </ol>
          </section>

          {detail.review && (
            <section>
              <h3>Your review</h3>
              <p>
                <strong>{detail.review.status.toLowerCase()}</strong>
                {detail.review.resolved_by && <> by {detail.review.resolved_by}</>}
                {detail.review.note && <> — “{detail.review.note}”</>}
              </p>
            </section>
          )}

          <section>
            <h3>Timeline</h3>
            <ol className="timeline">
              {detail.timeline.map((item, index) => (
                <li key={index}>
                  <time>{dateTime(item.at)}</time>
                  <span>{item.what}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </aside>
  );
}

function PayPalResponse({ response }: { response: unknown }) {
  if (typeof response === "object" && response !== null && "id" in response) {
    const body = response as { id: string; status?: string; amount?: { value: string } };
    return (
      <p>
        PayPal refund <code>{body.id}</code> {body.status && <strong>{body.status.toLowerCase()}</strong>}
        {body.amount && <> for {money(body.amount.value)}</>}
      </p>
    );
  }
  return <pre>{typeof response === "string" ? response : JSON.stringify(response, null, 2)}</pre>;
}

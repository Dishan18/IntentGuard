import { useCallback, useEffect, useState } from "react";
import { getRedTeam, type RedTeamReport, type RedTeamResult, startRedTeam, type Verdict } from "./api";
import { dateTime, money, TOOL_LABELS } from "./format";
import { DecisionChip } from "./Grid";

const VERDICTS: { key: Verdict; label: string; tone: string; help: string }[] = [
  { key: "BREACH", label: "Breaches", tone: "bad", help: "Something allowed on another customer's order. Should always be 0." },
  { key: "STOPPED", label: "Stopped", tone: "good", help: "Every attempt was blocked before PayPal." },
  { key: "HELD_FOR_YOU", label: "Held for you", tone: "wait", help: "It would have waited for your approval." },
  { key: "ALLOWED_BY_POLICY", label: "Allowed by your rules", tone: "info", help: "On Ben's own order, within your mandate: what your rules let through without evidence." },
  { key: "MODEL_REFUSED", label: "Model refused", tone: "muted", help: "The agent didn't try." },
  { key: "ERROR", label: "Didn't run", tone: "muted", help: "The LLM was unavailable for this attack." },
];

export function RedTeam({ ownerKey, onNotice }: { ownerKey: string; onNotice: (text: string, tone: "ok" | "bad") => void }) {
  const [report, setReport] = useState<RedTeamReport | null>(null);
  const [compromised, setCompromised] = useState(true);
  const [generated, setGenerated] = useState(0);
  const [starting, setStarting] = useState(false);

  const load = useCallback(() => getRedTeam(ownerKey).then(setReport).catch(() => undefined), [ownerKey]);

  useEffect(() => {
    load();
  }, [load]);

  const running = report?.status === "RUNNING";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [running, load]);

  async function start() {
    setStarting(true);
    try {
      const result = await startRedTeam(ownerKey, compromised, generated);
      if (result.error) onNotice(result.message ?? result.error, "bad");
      else await load();
    } catch (e) {
      onNotice((e as Error).message, "bad");
    }
    setStarting(false);
  }

  return (
    <div className="redteam">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Attack my policy</h2>
            <p className="muted">
              Plays known attacks as Ben, a real customer, against the real Support Agent and IntentGuard, with your current
              mandate and live PayPal facts. It's a dry run: nothing is ever sent to PayPal, reserved or queued, and none of it
              shows up in your activity.
            </p>
          </div>
        </div>
        <div className="redteam-controls">
          <label className="check-label">
            <input type="checkbox" checked={compromised} onChange={(e) => setCompromised(e.target.checked)} disabled={running} />
            The agent obeys the attacker (tests IntentGuard, not the model)
          </label>
          <label className="check-label">
            Gemini-written attacks
            <select value={generated} onChange={(e) => setGenerated(Number(e.target.value))} disabled={running}>
              {[0, 3, 5].map((n) => (
                <option key={n} value={n}>
                  {n === 0 ? "none" : `+${n}`}
                </option>
              ))}
            </select>
          </label>
          <button className="primary" onClick={start} disabled={running || starting}>
            {running ? `Running… ${report?.done ?? 0} of ${report?.total ?? "?"}` : starting ? "Starting…" : "Run attacks"}
          </button>
        </div>
      </section>

      {report?.run_id && <Report report={report} />}
      {report && !report.run_id && <p className="muted">No runs yet.</p>}
    </div>
  );
}

function Report({ report }: { report: RedTeamReport }) {
  const counts = report.counts ?? {};
  const results = report.results ?? [];
  return (
    <>
      <section className="verdicts" aria-label="Results">
        {VERDICTS.filter((v) => v.key === "BREACH" || counts[v.key]).map((v) => (
          <div key={v.key} className={`kpi verdict-kpi tone-${v.tone}`} title={v.help}>
            <span className="kpi-label">{v.label}</span>
            <span className="kpi-value">{counts[v.key] ?? 0}</span>
            <span className="kpi-sub muted">{v.help}</span>
          </div>
        ))}
      </section>
      <p className="muted small run-meta">
        Run {report.run_id} · {report.compromised ? "agent obeying the attacker" : "agent with its normal prompt"} ·{" "}
        {report.status === "RUNNING" ? `${report.done} of ${report.total} done` : `${results.length} attacks`} · started{" "}
        {dateTime(report.started_at)}
        {report.error && <span className="error"> · {report.error}</span>}
      </p>
      <div className="attacks">
        {results.map((result) => (
          <AttackCard key={result.idx} result={result} />
        ))}
      </div>
    </>
  );
}

function AttackCard({ result }: { result: RedTeamResult }) {
  const verdict = VERDICTS.find((v) => v.key === result.verdict);
  return (
    <article className={`panel attack tone-${verdict?.tone ?? "muted"}`}>
      <div className="attack-head">
        <div>
          <strong>{result.title}</strong>
          <span className="muted small">
            {" "}
            · {result.category}
            {result.source === "generated" && " · written by Gemini"}
          </span>
        </div>
        <span className={`verdict tone-${verdict?.tone ?? "muted"}`}>{verdict?.label ?? result.verdict}</span>
      </div>
      {result.messages.map((message, i) => (
        <blockquote key={i} className="attacker">
          <span className="muted small">Ben:</span> {message}
        </blockquote>
      ))}
      {result.events.length > 0 && (
        <ol className="attempts">
          {result.events.map((event, i) => {
            const amount = (event.arguments.amount as { value?: string } | undefined)?.value;
            return (
              <li key={i}>
                <span className="attempt-what">
                  {TOOL_LABELS[event.tool] ?? event.tool}
                  {amount ? ` ${money(amount)}` : ""}
                  {event.arguments.capture_id ? <code>{String(event.arguments.capture_id)}</code> : null}
                </span>
                {event.decision === "ALLOW" || event.decision === "REVIEW" || event.decision === "BLOCK" ? (
                  <DecisionChip decision={event.decision} />
                ) : (
                  <span className="tag">{event.decision}</span>
                )}
                {event.codes.length > 0 && (
                  <span className="codes">
                    {event.codes.map((code) => (
                      <code key={code}>{code}</code>
                    ))}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
      <p className="attack-detail">{result.detail}</p>
      {result.reply && (
        <details>
          <summary className="small">What the agent told Ben</summary>
          <p className="muted small">{result.reply}</p>
        </details>
      )}
    </article>
  );
}

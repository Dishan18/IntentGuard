import { useCallback, useEffect, useState } from "react";
import {
  activateMandate,
  type AgentMandates,
  compileMandate,
  type Example,
  getMandates,
  type MandateVersion,
  restoreMandate,
} from "./api";
import { dateTime, TOOL_LABELS } from "./format";
import { DecisionChip } from "./Grid";

export function Mandates({ ownerKey, onNotice }: { ownerKey: string; onNotice: (text: string, tone: "ok" | "bad") => void }) {
  const [agents, setAgents] = useState<AgentMandates[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(
    () =>
      getMandates(ownerKey)
        .then((data) => (setAgents(data), setError("")))
        .catch((e: Error) => setError(e.message)),
    [ownerKey],
  );

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <p className="error">{error}</p>;
  if (!agents) return <p className="muted">Loading mandates…</p>;
  return (
    <div className="mandates">
      <p className="muted intro">
        A mandate is what you allow an agent to do. Write it in plain English; the compiler turns it into rules, checks that every
        number came from your text, and shows what it would decide. Nothing changes until you activate it.
      </p>
      {agents.map((entry) => (
        <AgentMandate key={entry.agent.id} ownerKey={ownerKey} entry={entry} onChanged={load} onNotice={onNotice} />
      ))}
    </div>
  );
}

function AgentMandate({
  ownerKey,
  entry,
  onChanged,
  onNotice,
}: {
  ownerKey: string;
  entry: AgentMandates;
  onChanged: () => void;
  onNotice: (text: string, tone: "ok" | "bad") => void;
}) {
  const active = entry.active;
  const [text, setText] = useState(active?.source_text ?? "");
  const [draft, setDraft] = useState<MandateVersion | null>(null);
  const [busy, setBusy] = useState<"" | "compile" | "activate">("");
  const [error, setError] = useState("");

  async function compile() {
    setBusy("compile");
    setError("");
    setDraft(null);
    try {
      const result = await compileMandate(ownerKey, entry.agent.id, text);
      if (result.error) setError(result.message ?? result.error);
      else setDraft(result);
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy("");
  }

  async function restore(version: number) {
    setBusy("compile");
    setError("");
    try {
      const result = await restoreMandate(ownerKey, active?.mandate_id ?? "", version);
      if (result.error) setError(result.message ?? result.error);
      else (setDraft(result), setText(result.source_text));
    } catch (e) {
      setError((e as Error).message);
    }
    setBusy("");
  }

  async function activate() {
    if (!draft) return;
    setBusy("activate");
    try {
      const result = await activateMandate(ownerKey, draft.mandate_id, draft.version);
      if (result.error) {
        onNotice(result.message ?? result.error, "bad");
      } else {
        onNotice(`${entry.agent.name} now follows mandate v${result.version}.`, "ok");
        setDraft(null);
        onChanged();
      }
    } catch (e) {
      onNotice((e as Error).message, "bad");
    }
    setBusy("");
  }

  return (
    <section className="panel mandate" aria-label={`${entry.agent.name} mandate`}>
      <div className="panel-head">
        <div>
          <h2>{entry.agent.name}</h2>
          <p className="muted">{entry.agent.purpose}</p>
          <div className="tools">
            {entry.agent.allowed_tools.map((tool) => (
              <span key={tool} className="tag" title={tool}>
                {TOOL_LABELS[tool] ?? tool}
              </span>
            ))}
          </div>
        </div>
        {active && (
          <span className="muted small">
            Active: v{active.version}
            {active.activated_by && active.activated_by !== "seed" ? `, activated by ${active.activated_by}` : ""}
            {active.activated_at ? ` · ${dateTime(active.activated_at)}` : ""}
          </span>
        )}
      </div>

      <div className="mandate-body">
        <div className="mandate-col">
          <h3>Enforced now</h3>
          {active ? (
            <>
              <ul className="rules">
                {active.rules.map((rule) => (
                  <li key={rule}>{rule}</li>
                ))}
              </ul>
              <Examples examples={active.examples} />
            </>
          ) : (
            <p className="muted">No active mandate: this agent can't do anything.</p>
          )}
        </div>

        <div className="mandate-col">
          <h3>Change it in plain English</h3>
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={5} maxLength={2000} aria-label="Mandate text" />
          <div className="buttons">
            <button className="primary" onClick={compile} disabled={!text.trim() || !!busy}>
              {busy === "compile" ? "Compiling with Gemini…" : "Compile"}
            </button>
            {text !== (active?.source_text ?? "") && (
              <button className="ghost" onClick={() => (setText(active?.source_text ?? ""), setDraft(null))} disabled={!!busy}>
                Reset
              </button>
            )}
          </div>
          {error && <p className="error small">{error}</p>}
          {draft && <Draft draft={draft} busy={busy === "activate"} onActivate={activate} onDiscard={() => setDraft(null)} />}
        </div>
      </div>

      {entry.history.length > 1 && (
        <details className="history">
          <summary>Version history ({entry.history.length})</summary>
          <ol>
            {entry.history.map((h) => (
              <li key={h.version}>
                <span className={`tag ${h.status === "ACTIVE" ? "tag-sent" : ""}`}>v{h.version} {h.status.toLowerCase()}</span>
                <span className="muted small">
                  {dateTime(h.created_at)} · {h.compiled_by === "seed" ? "built in" : `drafted by ${h.compiled_by}`}
                  {h.activated_by && h.activated_by !== "seed" ? ` · activated by ${h.activated_by}` : ""}
                </span>
                {h.status === "SUPERSEDED" && active && (
                  <button className="link small" onClick={() => restore(h.version)} disabled={!!busy}>
                    Restore as a new draft
                  </button>
                )}
                <q>{h.source_text}</q>
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}

function Draft({ draft, busy, onActivate, onDiscard }: { draft: MandateVersion; busy: boolean; onActivate: () => void; onDiscard: () => void }) {
  const invalid = draft.status === "INVALID";
  return (
    <div className="draft">
      <div className="draft-head">
        <strong>{invalid ? "Couldn't compile" : `Draft v${draft.version}`}</strong>
        {!invalid && <span className="muted small">drafted by {draft.compiled_by}, not enforced yet</span>}
      </div>

      {draft.blocking.length > 0 && (
        <ul className="issues issues-bad">
          {draft.blocking.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}
      {draft.warnings.length > 0 && (
        <ul className="issues issues-warn">
          {draft.warnings.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}

      {!invalid && (
        <>
          {draft.changes.length > 0 ? (
            <ul className="changes">
              {draft.changes.map((change) => (
                <li key={change.field}>
                  <code>{change.field}</code> {change.from ?? "none"} → <strong>{change.to ?? "none"}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">Same rules as the active version.</p>
          )}
          <ul className="rules">
            {draft.rules.map((rule) => (
              <li key={rule}>{rule}</li>
            ))}
          </ul>
          <Examples examples={draft.examples} open />
        </>
      )}

      <div className="buttons">
        {!invalid && (
          <button className="primary" onClick={onActivate} disabled={busy || draft.blocking.length > 0}>
            {busy ? "Activating…" : `Activate v${draft.version}`}
          </button>
        )}
        <button className="ghost" onClick={onDiscard} disabled={busy}>
          {invalid ? "Close" : "Not now"}
        </button>
      </div>
    </div>
  );
}

function Examples({ examples, open = false }: { examples: Example[]; open?: boolean }) {
  if (!examples.length) return null;
  return (
    <details className="examples" open={open}>
      <summary>What it would decide ({examples.length} examples)</summary>
      <table>
        <tbody>
          {examples.map((example) => (
            <tr key={example.scenario}>
              <td>{example.scenario}</td>
              <td>
                <DecisionChip decision={example.decision} />
              </td>
              <td className="muted small">{example.why}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

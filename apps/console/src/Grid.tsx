import {
  CellStyleModule,
  ClientSideRowModelModule,
  colorSchemeDark,
  colorSchemeLight,
  type ColDef,
  type ICellRendererParams,
  ModuleRegistry,
  QuickFilterModule,
  type RowClickedEvent,
  RowStyleModule,
  themeQuartz,
  TooltipModule,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { DecisionKind, DecisionRow } from "./api";
import { AGENT_LABELS, clock, DECISION_LABELS, dateTime, money, TOOL_LABELS } from "./format";

// Only what this grid uses, to keep the bundle small.
ModuleRegistry.registerModules([CellStyleModule, ClientSideRowModelModule, QuickFilterModule, RowStyleModule, TooltipModule]);

export function DecisionChip({ decision, approved }: { decision: DecisionKind; approved?: boolean }) {
  return (
    <span className={`chip chip-${decision.toLowerCase()}`}>
      <span className="chip-dot" aria-hidden />
      {approved ? "Approved by you" : DECISION_LABELS[decision]}
    </span>
  );
}

function DecisionCell(params: ICellRendererParams<DecisionRow>) {
  if (!params.data) return null;
  return <DecisionChip decision={params.data.decision} approved={params.data.owner_approved} />;
}

function PayPalCell(params: ICellRendererParams<DecisionRow>) {
  const row = params.data;
  if (!row) return null;
  if (row.paypal_call === "SENT")
    return (
      <span className="paypal-cell">
        <span className="tag tag-sent">Sent</span>
        {row.paypal_id && <code>{row.paypal_id}</code>}
      </span>
    );
  if (row.paypal_call === "WAITING_FOR_OWNER") return <span className="tag tag-wait">Waiting for you</span>;
  if (row.paypal_call === "SEE_REVIEW") return <span className="tag">Sent after approval</span>;
  return <span className="tag tag-stopped">Not sent</span>;
}

function RuleCell(params: ICellRendererParams<DecisionRow>) {
  const codes = params.data?.codes ?? [];
  if (!codes.length) return <span className="muted">—</span>;
  return (
    <span className="codes" title={codes.join(", ")}>
      <code>{codes[0]}</code>
      {codes.length > 1 && <span className="muted">+{codes.length - 1}</span>}
    </span>
  );
}

const FILTERS: { key: "ALL" | DecisionKind; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "ALLOW", label: "Allowed" },
  { key: "REVIEW", label: "Held" },
  { key: "BLOCK", label: "Blocked" },
];

function usePrefersDark() {
  const query = "(prefers-color-scheme: dark)";
  const [dark, setDark] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const listener = () => setDark(media.matches);
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, []);
  return dark;
}

export function DecisionGrid({
  rows,
  selectedId,
  onSelect,
}: {
  rows: DecisionRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [filter, setFilter] = useState<"ALL" | DecisionKind>("ALL");
  const [search, setSearch] = useState("");
  const dark = usePrefersDark();
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  // Highlight rows that arrive while the console is open (not the ones there on first load).
  useEffect(() => {
    const ids = new Set(rows.map((row) => row.id));
    if (seen.current) {
      const added = rows.filter((row) => !seen.current!.has(row.id)).map((row) => row.id);
      if (added.length) {
        setFresh((current) => new Set([...current, ...added]));
        setTimeout(() => setFresh((current) => new Set([...current].filter((id) => !added.includes(id)))), 4000);
      }
    }
    seen.current = ids;
  }, [rows]);

  const theme = useMemo(
    () =>
      themeQuartz.withPart(dark ? colorSchemeDark : colorSchemeLight).withParams({
        fontFamily: "Inter, system-ui, sans-serif",
        headerFontWeight: 600,
        accentColor: dark ? "#7aa7ff" : "#1f5eff",
        borderRadius: 8,
        wrapperBorderRadius: 12,
        rowHeight: 44,
        headerHeight: 40,
        ...(dark ? { backgroundColor: "#151a23", headerBackgroundColor: "#1b2230" } : {}),
      }),
    [dark],
  );

  const columns = useMemo<ColDef<DecisionRow>[]>(
    () => [
      {
        field: "time",
        headerName: "Time",
        width: 130,
        sort: "desc",
        valueFormatter: (p) => clock(p.value),
        tooltipValueGetter: (p) => dateTime(p.value),
      },
      { field: "agent_id", headerName: "Agent", width: 140, valueFormatter: (p) => AGENT_LABELS[p.value] ?? p.value ?? "" },
      {
        field: "customer_name",
        headerName: "Customer",
        width: 130,
        valueGetter: (p) => p.data?.customer_name ?? p.data?.customer_id ?? "—",
      },
      { field: "tool", headerName: "Action", width: 130, valueFormatter: (p) => TOOL_LABELS[p.value] ?? p.value },
      {
        field: "amount",
        headerName: "Amount",
        width: 110,
        type: "rightAligned",
        valueGetter: (p) => (p.data?.amount ? Number(p.data.amount) : null),
        valueFormatter: (p) => (p.value === null ? "—" : money(String(p.value))),
      },
      { field: "decision", headerName: "Decision", width: 170, cellRenderer: DecisionCell },
      { field: "rule", headerName: "Rule", flex: 1, minWidth: 190, cellRenderer: RuleCell, getQuickFilterText: (p) => p.data?.codes.join(" ") ?? "" },
      { field: "paypal_call", headerName: "PayPal", width: 230, cellRenderer: PayPalCell, getQuickFilterText: (p) => p.data?.paypal_id ?? "" },
      { field: "status", headerName: "Status", width: 140, valueFormatter: (p) => String(p.value ?? "").replaceAll("_", " ").toLowerCase() },
    ],
    [],
  );

  const visible = useMemo(() => (filter === "ALL" ? rows : rows.filter((row) => row.decision === filter)), [rows, filter]);
  const counts = useMemo(() => {
    const result: Record<string, number> = { ALL: rows.length };
    for (const row of rows) result[row.decision] = (result[row.decision] ?? 0) + 1;
    return result;
  }, [rows]);

  return (
    <section className="panel grid-panel" aria-label="Decisions">
      <div className="panel-head">
        <div>
          <h2>Every agent action</h2>
          <p className="muted">Each call an agent made, what IntentGuard decided, and whether PayPal was called.</p>
        </div>
        <div className="grid-tools">
          <div className="segmented" role="group" aria-label="Filter by decision">
            {FILTERS.map(({ key, label }) => (
              <button key={key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>
                {label} <span className="count">{counts[key] ?? 0}</span>
              </button>
            ))}
          </div>
          <input
            className="search"
            type="search"
            placeholder="Search customer, rule, PayPal ID"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search decisions"
          />
        </div>
      </div>
      <div className="grid-wrap">
        <AgGridReact<DecisionRow>
          theme={theme}
          rowData={visible}
          columnDefs={columns}
          getRowId={(p) => p.data.id}
          quickFilterText={search}
          animateRows
          tooltipShowDelay={300}
          rowClassRules={{
            "row-fresh": (p) => !!p.data && fresh.has(p.data.id),
            "row-selected": (p) => !!p.data && p.data.id === selectedId,
          }}
          onRowClicked={(event: RowClickedEvent<DecisionRow>) => event.data && onSelect(event.data.id)}
          overlayNoRowsTemplate="No agent actions yet. Try the store's chat."
        />
      </div>
    </section>
  );
}

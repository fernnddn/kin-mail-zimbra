import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { theme } from "../styles/theme";
import { Button, Hint, Skeleton, SkeletonCard, WarnBox } from "../ui";
import { formatValue } from "./chart";

/** What happened last month, without leaving the console.
 *
 * The Monitoring tab answers "what is happening now". This answers the
 * question somebody asks once a month, and which until now meant either
 * reading Postfix logs by hand or not answering at all.
 *
 * Everything here is read-only: it runs range queries through the same
 * loopback Prometheus proxy the charts use.
 */

type Column = { key: string; heading: string; kind: string };
type Row = Record<string, string | number | null>;
type Report = {
  period: string;
  label: string;
  start: string;
  end: string;
  generated_at: string;
  columns: Column[];
  totals: Record<string, number | null>;
  delivered_pct: number | null;
  bounced_pct: number | null;
  daily: Row[];
  busiest_day: string | null;
  busiest_day_accepted: number;
  days: number;
  unavailable: string[];
};

const PERIODS: { id: string; label: string }[] = [
  { id: "this_month", label: "This month" },
  { id: "last_month", label: "Last month" },
  { id: "last_7d", label: "Last 7 days" },
  { id: "last_30d", label: "Last 30 days" },
  { id: "last_90d", label: "Last 90 days" },
];

// The four an operator would read out loud, in the order they would say them.
const HEADLINE = ["accepted", "delivered_in", "delivered_out", "bounced"];

/* A monthly report gets printed or saved as a PDF and handed to somebody.
   On paper the controls are noise, a table that scrolls inside a 460px box
   loses every row past the twelfth, and a card with no background is a box of
   numbers with no structure. */
const Printable = styled.div`
  @media print {
    .no-print {
      display: none !important;
    }
    /* Backgrounds are dropped by default, which erases the header banding and
       the total row - the two things that make the table readable. */
    * {
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
  }
`;

/* Only exists on paper: on screen the period is in the panel heading and the
   freshness is obvious, but a printed page has to say what it is and when it
   was taken or it is just a table of numbers. */
const PrintHead = styled.header`
  display: none;
  @media print {
    display: block;
    margin-bottom: 14px;
    h2 {
      margin: 0 0 2px;
      font-size: 1.1rem;
      color: ${theme.ink};
    }
    p {
      margin: 0;
      font-size: 0.72rem;
      color: ${theme.muted};
    }
  }
`;

const Bar = styled.div`
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
`;

const PeriodGroup = styled.div`
  display: inline-flex;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  overflow: hidden;
  background: ${theme.bgElev};
`;

const PeriodBtn = styled.button<{ $on: boolean }>`
  border: 0;
  padding: 7px 13px;
  font-size: 0.8rem;
  font-weight: ${(p) => (p.$on ? 650 : 500)};
  cursor: pointer;
  background: ${(p) => (p.$on ? theme.accent : "transparent")};
  color: ${(p) => (p.$on ? "#fff" : theme.ink)};
  transition: background 120ms ease, color 120ms ease;
  &:hover:not(:disabled) {
    background: ${(p) => (p.$on ? theme.accent : theme.surface[100])};
  }
  &:disabled {
    cursor: default;
    opacity: 0.6;
  }
`;

const Spacer = styled.div`
  flex: 1 1 auto;
`;

const Headline = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
`;

const Stat = styled.div`
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.lg};
  padding: 14px 16px;
`;

const StatLabel = styled.p`
  margin: 0 0 6px;
  font-size: 0.66rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: ${theme.muted};
`;

const StatValue = styled.p`
  margin: 0;
  font-size: 1.55rem;
  font-weight: 660;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: ${theme.ink};
`;

const StatSub = styled.p`
  margin: 5px 0 0;
  font-size: 0.72rem;
  color: ${theme.muted};
`;

const Panel = styled.div`
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.lg};
  overflow: hidden;
`;

const PanelHead = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 16px;
  border-bottom: 1px solid ${theme.line};
`;

const PanelTitle = styled.h3`
  margin: 0;
  font-size: 0.85rem;
  font-weight: 650;
  color: ${theme.ink};
`;

/* The table scrolls inside its own box; the page must never scroll sideways. */
const TableWrap = styled.div`
  overflow-x: auto;
  max-height: 460px;
  overflow-y: auto;
  @media print {
    /* Otherwise the print stops at whatever fits the box on screen. */
    overflow: visible;
    max-height: none;
  }
`;

const Table = styled.table`
  width: 100%;
  /* separate, not collapse: a collapsed border does not travel with a sticky
     cell, so the pinned column loses its dividing line as soon as it pins. */
  border-collapse: separate;
  border-spacing: 0;
  font-size: 0.78rem;
  font-variant-numeric: tabular-nums;

  /* The date column stays put while the figures scroll under it. Without this
     the first thing to leave the screen on a narrow window is the one column
     that says which day you are reading. */
  th:first-of-type,
  td:first-of-type {
    position: sticky;
    left: 0;
    z-index: 2;
    box-shadow: 1px 0 0 ${theme.surface[200]};
  }
  @media print {
    /* Nothing scrolls on paper, and a sticky cell prints on top of the row
       it was pinned over. */
    th,
    td,
    tfoot td {
      position: static !important;
    }
    tr {
      break-inside: avoid;
    }
    thead {
      display: table-header-group;
    }
  }
  /* The header's first cell is pinned in both directions, so it has to sit
     above the row headers it crosses. */
  thead th:first-of-type {
    z-index: 4;
  }
  tfoot td:first-of-type {
    z-index: 4;
  }
`;

const Th = styled.th<{ $num?: boolean }>`
  position: sticky;
  top: 0;
  z-index: 1;
  background: ${theme.surface[100]};
  text-align: ${(p) => (p.$num ? "right" : "left")};
  font-weight: 650;
  font-size: 0.68rem;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: ${theme.muted};
  padding: 9px 12px;
  white-space: nowrap;
  box-shadow: inset 0 -1px 0 ${theme.line};
`;

const Td = styled.td<{ $num?: boolean; $muted?: boolean }>`
  text-align: ${(p) => (p.$num ? "right" : "left")};
  padding: 7px 12px;
  white-space: nowrap;
  color: ${(p) => (p.$muted ? theme.muted : theme.ink)};
  /* Opaque on purpose: a sticky cell with a transparent background shows the
     scrolled figures sliding underneath it. */
  background: ${theme.bgElev};
  box-shadow: inset 0 -1px 0 ${theme.surface[100]};
`;

const Tr = styled.tr`
  /* The hover has to reach the pinned cell too, or the highlighted row breaks
     in half at the frozen column. */
  &:hover td {
    background: ${theme.surface[50]};
  }
  &:last-of-type td {
    box-shadow: none;
  }
`;

const Foot = styled.tr`
  td {
    position: sticky;
    bottom: 0;
    /* stays a total row on paper, just not a floating one */
    background: ${theme.surface[100]};
    font-weight: 660;
    box-shadow: inset 0 1px 0 ${theme.line};
    z-index: 3;
  }
  td:first-of-type {
    box-shadow: inset 0 1px 0 ${theme.line}, 1px 0 0 ${theme.surface[200]};
  }
`;

const Empty = styled.p`
  margin: 0;
  padding: 28px 16px;
  text-align: center;
  color: ${theme.muted};
  font-size: 0.82rem;
`;

function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return Math.round(value).toLocaleString();
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${value}%`;
}

function dayLabel(iso: string): string {
  // The dates come from the server already formatted as its own local days;
  // parsing them as UTC here would shift every row by the offset.
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    day: "2-digit",
    month: "short",
  });
}

export function ReportsTab() {
  const [period, setPeriod] = useState("last_month");
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  useEffect(() => () => {
    alive.current = false;
  }, []);

  const load = useCallback(async (want: string) => {
    setError("");
    try {
      const data = await api<Report>(`/api/reports/mail?period=${encodeURIComponent(want)}`);
      if (alive.current) setReport(data);
    } catch (e) {
      if (alive.current) {
        setReport(null);
        setError(
          e instanceof Error
            ? e.message
            : "Could not build the report. Metrics are collected by the built-in monitoring stack.",
        );
      }
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void load(period);
  }, [period, load]);

  const columns = report?.columns || [];
  const rows = report?.daily || [];

  if (loading && !report) {
    return (
      <Headline>
        <SkeletonCard>
          <Skeleton $h="0.7rem" $w="50%" style={{ marginBottom: 12 }} />
          <Skeleton $h="1.6rem" $w="40%" />
        </SkeletonCard>
        <SkeletonCard>
          <Skeleton $h="0.7rem" $w="50%" style={{ marginBottom: 12 }} />
          <Skeleton $h="1.6rem" $w="40%" />
        </SkeletonCard>
      </Headline>
    );
  }

  return (
    <Printable>
      <PrintHead>
        <h2>KIN Mail - mail report</h2>
        <p>
          {report ? `${report.label} · ` : ""}
          {report
            ? `${report.start.slice(0, 10)} to ${report.end.slice(0, 10)}`
            : ""}
          {report ? ` · generated ${new Date(report.generated_at).toLocaleString()}` : ""}
        </p>
      </PrintHead>
      <Bar className="no-print">
        <PeriodGroup role="group" aria-label="Report period">
          {PERIODS.map((p) => (
            <PeriodBtn
              key={p.id}
              type="button"
              $on={p.id === period}
              disabled={loading}
              onClick={() => setPeriod(p.id)}
            >
              {p.label}
            </PeriodBtn>
          ))}
        </PeriodGroup>
        <Spacer />
        <Button
          type="button"
          variant="ghost"
          disabled={!report || rows.length === 0}
          onClick={() => window.print()}
        >
          Print / PDF
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={!report || rows.length === 0}
          onClick={() => {
            // A plain navigation, so the browser handles the download and the
            // Content-Disposition filename rather than us inventing one.
            window.location.href = `/api/reports/mail.csv?period=${encodeURIComponent(period)}`;
          }}
        >
          Export CSV
        </Button>
      </Bar>

      {error ? <WarnBox className="no-print">{error}</WarnBox> : null}

      {report ? (
        <>
          <Headline>
            {HEADLINE.map((key) => {
              const col = columns.find((c) => c.key === key);
              if (!col) return null;
              return (
                <Stat key={key}>
                  <StatLabel>{col.heading}</StatLabel>
                  <StatValue>{count(report.totals[key])}</StatValue>
                  {key === "delivered_in" && report.delivered_pct !== null ? (
                    <StatSub>{pct(report.delivered_pct)} of final outcomes</StatSub>
                  ) : null}
                  {key === "bounced" && report.bounced_pct !== null ? (
                    <StatSub>{pct(report.bounced_pct)} of final outcomes</StatSub>
                  ) : null}
                  {key === "accepted" && report.busiest_day ? (
                    <StatSub>
                      Busiest {dayLabel(report.busiest_day)} ({count(report.busiest_day_accepted)})
                    </StatSub>
                  ) : null}
                </Stat>
              );
            })}
          </Headline>

          {report.unavailable.length ? (
            <WarnBox>
              Some figures could not be read from Prometheus and are shown as blank:{" "}
              {report.unavailable
                .map((k) => columns.find((c) => c.key === k)?.heading || k)
                .join(", ")}
              . The rest of the report is complete.
            </WarnBox>
          ) : null}

          <Panel>
            <PanelHead>
              <PanelTitle>{report.label} - day by day</PanelTitle>
              <Hint style={{ margin: 0 }}>
                {report.days} day{report.days === 1 ? "" : "s"}. Days are this
                server's own days, and queue figures are the worst point in each
                one rather than an average.
              </Hint>
            </PanelHead>
            {rows.length ? (
              <TableWrap>
                <Table>
                  <thead>
                    <tr>
                      <Th scope="col">Date</Th>
                      {columns.map((c) => (
                        <Th key={c.key} scope="col" $num>
                          {c.heading}
                        </Th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <Tr key={String(row.date)}>
                        <Td>{dayLabel(String(row.date))}</Td>
                        {columns.map((c) => {
                          const raw = row[c.key];
                          const value =
                            raw === null || raw === undefined ? null : Number(raw);
                          return (
                            <Td key={c.key} $num $muted={!value}>
                              {c.key === "queue_oldest_peak" && value
                                ? formatValue(value, "seconds")
                                : count(value)}
                            </Td>
                          );
                        })}
                      </Tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <Foot>
                      <Td>Total</Td>
                      {columns.map((c) => (
                        <Td key={c.key} $num>
                          {c.key === "queue_oldest_peak" && report.totals[c.key]
                            ? formatValue(Number(report.totals[c.key]), "seconds")
                            : count(report.totals[c.key])}
                        </Td>
                      ))}
                    </Foot>
                  </tfoot>
                </Table>
              </TableWrap>
            ) : (
              <Empty>
                No mail figures for this period.
                <br />
                On an appliance deployed before mail reporting existed, the
                collector that produces these numbers is installed by re-running
                the monitoring step of the deploy; figures start from the day it
                first runs, not before. On a recent appliance there is simply
                nothing further back to show yet.
              </Empty>
            )}
          </Panel>
        </>
      ) : null}
    </Printable>
  );
}

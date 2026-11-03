"use client";

import { useEffect, useState } from "react";
import {
  api,
  type CrossCheckResponse,
  type DocumentSummary,
} from "@/lib/api";

function Picker({
  title,
  documents,
  selected,
  onToggle,
}: {
  title: string;
  documents: DocumentSummary[];
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="panel" style={{ flex: 1, minWidth: 280 }}>
      <h2>{title}</h2>
      <div style={{ maxHeight: 280, overflowY: "auto" }}>
        {documents.map((doc) => (
          <label
            key={doc.id}
            className="row"
            style={{ padding: "4px 0", cursor: "pointer" }}
          >
            <input
              type="checkbox"
              checked={selected.has(doc.id)}
              onChange={() => onToggle(doc.id)}
              style={{ width: "auto" }}
            />
            <span>{doc.filename}</span>
            <span className="badge muted">
              {doc.source_kind.replace(/_/g, " ")}
            </span>
          </label>
        ))}
      </div>
      <p className="muted" style={{ marginBottom: 0 }}>
        {selected.size} selected
      </p>
    </div>
  );
}

export default function CrossCheckPage() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [left, setLeft] = useState<Set<string>>(new Set());
  const [right, setRight] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<CrossCheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api
      .listDocuments()
      .then((docs) =>
        setDocuments(docs.filter((d) => d.status !== "failed")),
      )
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void) => (
    id: string,
  ) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setter(next);
  };

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.crossCheck([...left], [...right]));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>Cross-verification</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Reconcile two sets of documents against each other — PDF against PDF,
          or PDF against a spreadsheet register. Invoices are matched on number,
          then on an OCR-normalised number with corroborating evidence, then on
          a supplier/date/total fingerprint.
        </p>
      </div>

      <div className="row" style={{ alignItems: "stretch" }}>
        <Picker
          title="Side A"
          documents={documents}
          selected={left}
          onToggle={toggle(left, setLeft)}
        />
        <Picker
          title="Side B"
          documents={documents}
          selected={right}
          onToggle={toggle(right, setRight)}
        />
      </div>

      <div className="panel">
        <button
          className="primary"
          disabled={left.size === 0 || right.size === 0 || loading}
          onClick={() => void run()}
        >
          {loading ? "Comparing…" : "Run cross-check"}
        </button>
        {error && <p className="err-text">{error}</p>}
      </div>

      {result && (
        <div className="panel">
          <h2>Result</h2>
          <div className="counts" style={{ marginBottom: 14 }}>
            {Object.entries(result.counts).map(([label, n]) => (
              <div className="count" key={label}>
                <div className="n">{n}</div>
                <div className="l">{label.replace(/_/g, " ")}</div>
              </div>
            ))}
          </div>
          <table>
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Status</th>
                <th>Matched by</th>
                <th>Differences</th>
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, index) => (
                <tr key={`${row.key}-${index}`}>
                  <td className="mono">{row.key}</td>
                  <td>
                    <span
                      className={
                        row.status === "match"
                          ? "badge ok"
                          : row.status === "mismatch"
                            ? "badge err"
                            : "badge warn"
                      }
                    >
                      {row.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="muted">{row.matched_by}</td>
                  <td>
                    {row.differences.length === 0 ? (
                      <span className="muted">—</span>
                    ) : (
                      row.differences.map((d) => (
                        <div key={d.field} className="mono">
                          <strong>{d.field}</strong>: {String(d.left)} ≠{" "}
                          {String(d.right)}
                          {d.note && <span className="muted"> ({d.note})</span>}
                        </div>
                      ))
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

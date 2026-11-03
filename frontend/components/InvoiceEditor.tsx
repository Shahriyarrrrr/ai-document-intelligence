"use client";

import { useMemo, useState } from "react";
import { api, type Invoice } from "@/lib/api";

const FIELDS = [
  ["invoice_number", "Invoice number"],
  ["invoice_date", "Invoice date"],
  ["supplier_name", "Supplier"],
  ["customer_name", "Customer"],
  ["currency", "Currency"],
  ["subtotal", "Subtotal"],
  ["tax", "Tax"],
  ["total", "Total"],
] as const;

type FieldName = (typeof FIELDS)[number][0];

function confidenceLabel(score: number | undefined) {
  if (score === undefined) return null;
  const percent = Math.round(score * 100);
  const cls = score >= 0.8 ? "ok" : score >= 0.55 ? "warn" : "err";
  return (
    <div className={`conf badge ${cls}`}>{percent}% confidence</div>
  );
}

export default function InvoiceEditor({
  invoice,
  onUpdated,
}: {
  invoice: Invoice;
  onUpdated: (updated: Invoice) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const current = (name: FieldName): string => {
    if (name in draft) return draft[name];
    const value = invoice[name];
    return value === null || value === undefined ? "" : String(value);
  };

  const dirtyFields = useMemo(
    () =>
      Object.keys(draft).filter((name) => {
        const original = invoice[name as FieldName];
        const originalText =
          original === null || original === undefined ? "" : String(original);
        return draft[name] !== originalText;
      }),
    [draft, invoice],
  );

  // Fields the validator specifically complained about, so the reviewer's eye
  // goes to the one number that is actually wrong rather than scanning eight
  // identical inputs.
  const flagged = useMemo(
    () => new Set(invoice.issues.map((i) => i.field).filter(Boolean) as string[]),
    [invoice.issues],
  );

  const save = async () => {
    if (dirtyFields.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api.correct(
        invoice.id,
        dirtyFields.map((field) => ({ field, value: draft[field] })),
      );
      onUpdated(result.invoice);
      setDraft({});
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const errors = invoice.issues.filter((i) => i.severity === "error");
  const warnings = invoice.issues.filter((i) => i.severity === "warning");

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Extracted invoice</h2>
        <span className="badge muted">{invoice.extraction_method}</span>
        {invoice.needs_review ? (
          <span className="badge err">needs review</span>
        ) : (
          <span className="badge ok">validated</span>
        )}
        <div className="spacer" />
        {dirtyFields.length > 0 && (
          <>
            <button onClick={() => setDraft({})} disabled={saving}>
              Discard
            </button>
            <button className="primary" onClick={save} disabled={saving}>
              {saving
                ? "Saving…"
                : `Save ${dirtyFields.length} correction${dirtyFields.length > 1 ? "s" : ""}`}
            </button>
          </>
        )}
      </div>

      {error && <p className="err-text">{error}</p>}

      <div className="grid2">
        {FIELDS.map(([name, label]) => {
          const isDirty = dirtyFields.includes(name);
          return (
            <div className="field" key={name}>
              <label>
                {label}
                {flagged.has(name) && (
                  <span className="badge err" style={{ marginLeft: 6 }}>
                    flagged
                  </span>
                )}
              </label>
              <input
                className={isDirty ? "dirty" : ""}
                value={current(name)}
                placeholder="not extracted"
                onChange={(e) =>
                  setDraft({ ...draft, [name]: e.target.value })
                }
              />
              {confidenceLabel(invoice.field_confidence?.[name])}
            </div>
          );
        })}
      </div>

      {invoice.line_items.length > 0 && (
        <>
          <h2 style={{ marginTop: 20 }}>Line items</h2>
          <table>
            <thead>
              <tr>
                <th>Description</th>
                <th className="right">Qty</th>
                <th className="right">Unit price</th>
                <th className="right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {invoice.line_items.map((item, index) => (
                <tr key={index}>
                  <td>{item.description ?? <span className="muted">—</span>}</td>
                  <td className="right mono">{item.quantity ?? "—"}</td>
                  <td className="right mono">{item.unit_price ?? "—"}</td>
                  <td className="right mono">{item.amount ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {(errors.length > 0 || warnings.length > 0) && (
        <>
          <h2 style={{ marginTop: 20 }}>
            Validation ({errors.length} error
            {errors.length === 1 ? "" : "s"}, {warnings.length} warning
            {warnings.length === 1 ? "" : "s"})
          </h2>
          {[...errors, ...warnings].map((issue) => (
            <div className={`issue ${issue.severity}`} key={issue.id}>
              <div>{issue.message}</div>
              <code>
                {issue.code}
                {issue.field ? ` · ${issue.field}` : ""}
              </code>
            </div>
          ))}
        </>
      )}

      {invoice.corrections.length > 0 && (
        <>
          <h2 style={{ marginTop: 20 }}>Correction history</h2>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>From</th>
                <th>To</th>
                <th>By</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {invoice.corrections.map((c) => (
                <tr key={c.id}>
                  <td>{c.field}</td>
                  <td className="mono muted">{c.old_value ?? "—"}</td>
                  <td className="mono">{c.new_value ?? "—"}</td>
                  <td className="muted">{c.corrected_by}</td>
                  <td className="muted mono">
                    {new Date(c.at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

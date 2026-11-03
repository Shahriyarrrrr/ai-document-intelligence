"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type DocumentDetail, type Invoice } from "@/lib/api";
import InvoiceEditor from "@/components/InvoiceEditor";

export default function DocumentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDoc(await api.getDocument(id));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep polling while the document is still being processed, so opening a
  // detail page straight after upload shows it fill in rather than sitting
  // on an empty shell.
  useEffect(() => {
    if (!doc) return;
    if (["completed", "needs_review", "failed"].includes(doc.status)) return;
    const timer = setInterval(() => void load(), 1500);
    return () => clearInterval(timer);
  }, [doc, load]);

  const onInvoiceUpdated = (updated: Invoice) => {
    setDoc((current) =>
      current
        ? {
            ...current,
            invoices: current.invoices.map((i) =>
              i.id === updated.id ? updated : i,
            ),
          }
        : current,
    );
  };

  if (error) return <p className="err-text">{error}</p>;
  if (!doc) return <p className="muted">Loading…</p>;

  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <Link href="/">← Documents</Link>
        <div className="spacer" />
        <button
          onClick={async () => {
            await api.reprocess(doc.id);
            await load();
          }}
        >
          Reprocess
        </button>
      </div>

      <div className="panel">
        <h2>{doc.filename}</h2>
        <div className="counts">
          {(
            [
              ["status", doc.status.replace(/_/g, " ")],
              ["type", doc.source_kind.replace(/_/g, " ")],
              ["pages", String(doc.page_count)],
              ["ocr pages", String(doc.ocr_page_count)],
              ["size", `${(doc.byte_size / 1024).toFixed(0)} KB`],
              [
                "duration",
                doc.duration_s != null ? `${doc.duration_s.toFixed(2)}s` : "—",
              ],
            ] as const
          ).map(([label, value]) => (
            <div className="count" key={label}>
              <div className="n" style={{ fontSize: 15 }}>
                {value}
              </div>
              <div className="l">{label}</div>
            </div>
          ))}
        </div>
        {doc.error_message && (
          <div className="issue error" style={{ marginTop: 12 }}>
            {doc.error_message}
          </div>
        )}
        {Array.isArray(doc.metrics?.warnings) &&
          (doc.metrics.warnings as string[]).map((w) => (
            <div className="issue warning" style={{ marginTop: 8 }} key={w}>
              {w}
            </div>
          ))}
      </div>

      {doc.invoices.map((invoice) => (
        <InvoiceEditor
          key={invoice.id}
          invoice={invoice}
          onUpdated={onInvoiceUpdated}
        />
      ))}

      <div className="panel">
        <h2>Processing history</h2>
        <table>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Status</th>
              <th className="right">Duration</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {doc.events.map((event, index) => (
              <tr key={index}>
                <td>{event.stage}</td>
                <td>
                  <span
                    className={
                      event.status === "ok" ? "badge ok" : "badge err"
                    }
                  >
                    {event.status}
                  </span>
                </td>
                <td className="right mono">
                  {event.duration_ms != null ? `${event.duration_ms} ms` : "—"}
                </td>
                <td className="mono muted">
                  {JSON.stringify(event.detail).slice(0, 110)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

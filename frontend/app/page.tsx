"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  type DocumentSummary,
  type StatusCounts,
} from "@/lib/api";

const TERMINAL = new Set(["completed", "needs_review", "failed"]);

function statusBadge(status: string, needsReview: boolean) {
  if (status === "failed") return <span className="badge err">failed</span>;
  if (status === "needs_review" || needsReview)
    return <span className="badge warn">needs review</span>;
  if (status === "completed") return <span className="badge ok">completed</span>;
  if (status === "pending") return <span className="badge muted">queued</span>;
  return <span className="badge muted">{status.replace(/_/g, " ")}</span>;
}

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [counts, setCounts] = useState<StatusCounts | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rejected, setRejected] = useState<
    Array<{ filename: string; reason: string }>
  >([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [docs, c] = await Promise.all([
        api.listDocuments(),
        api.statusCounts(),
      ]);
      setDocuments(docs);
      setCounts(c);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is actually in flight. A fixed interval that
  // keeps hammering the API after everything has settled is the usual way
  // these dashboards become the noisiest client on the network.
  useEffect(() => {
    const inFlight = documents.some((d) => !TERMINAL.has(d.status));
    if (!inFlight) return;
    const timer = setInterval(() => void refresh(), 1500);
    return () => clearInterval(timer);
  }, [documents, refresh]);

  const handleFiles = useCallback(
    async (fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return;
      setUploading(true);
      setError(null);
      setRejected([]);
      try {
        const result = await api.upload(Array.from(fileList));
        setRejected(result.rejected);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [refresh],
  );

  const failedCount = counts?.failed ?? 0;

  return (
    <>
      <div className="panel">
        <h2>Upload</h2>
        <div
          className={dragOver ? "drop over" : "drop"}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            void handleFiles(e.dataTransfer.files);
          }}
          onClick={() => inputRef.current?.click()}
        >
          {uploading
            ? "Uploading…"
            : "Drop files here, or click to choose. PDF, JPG, PNG, TIFF, XLSX, XLS, CSV."}
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.xlsm,.csv,.tsv"
            onChange={(e) => void handleFiles(e.target.files)}
          />
        </div>
        {rejected.length > 0 && (
          <div style={{ marginTop: 12 }}>
            {rejected.map((r) => (
              <div key={r.filename} className="issue error">
                <strong>{r.filename}</strong> rejected — {r.reason}
              </div>
            ))}
          </div>
        )}
        {error && (
          <p className="err-text" style={{ marginBottom: 0 }}>
            {error}
          </p>
        )}
      </div>

      {counts && (
        <div className="counts" style={{ marginBottom: 18 }}>
          {(
            [
              ["queued", counts.pending],
              ["processing", counts.processing],
              ["completed", counts.completed],
              ["needs review", counts.needs_review],
              ["failed", counts.failed],
            ] as const
          ).map(([label, n]) => (
            <div className="count" key={label}>
              <div className="n">{n}</div>
              <div className="l">{label}</div>
            </div>
          ))}
        </div>
      )}

      <div className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Documents</h2>
          <div className="spacer" />
          <button onClick={() => void refresh()}>Refresh</button>
          <button
            disabled={failedCount === 0}
            onClick={async () => {
              await api.reprocessAllFailed();
              await refresh();
            }}
          >
            Reprocess failed ({failedCount})
          </button>
        </div>

        {documents.length === 0 ? (
          <p className="muted">Nothing uploaded yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th>Type</th>
                <th className="right">Pages</th>
                <th className="right">OCR</th>
                <th className="right">Issues</th>
                <th className="right">Time</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td>
                    <Link href={`/documents/${doc.id}`}>{doc.filename}</Link>
                    {doc.error_message && (
                      <div className="err-text mono">{doc.error_message}</div>
                    )}
                    {!TERMINAL.has(doc.status) && <progress />}
                  </td>
                  <td>{statusBadge(doc.status, doc.needs_review)}</td>
                  <td className="muted">{doc.source_kind.replace(/_/g, " ")}</td>
                  <td className="right">{doc.page_count || "—"}</td>
                  <td className="right">{doc.ocr_page_count || "—"}</td>
                  <td className="right">{doc.issue_count || "—"}</td>
                  <td className="right muted mono">
                    {doc.duration_s != null ? `${doc.duration_s.toFixed(1)}s` : "—"}
                  </td>
                  <td className="right">
                    <button
                      onClick={async () => {
                        await api.reprocess(doc.id);
                        await refresh();
                      }}
                    >
                      Reprocess
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

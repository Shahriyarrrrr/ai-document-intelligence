"use client";

import { useState } from "react";
import Link from "next/link";
import { api, type SearchResponse } from "@/lib/api";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"keyword" | "semantic" | "hybrid">("hybrid");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await api.search(query, mode));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="panel">
        <h2>Search processed documents</h2>
        <div className="row">
          <input
            style={{ flex: 1, minWidth: 220 }}
            value={query}
            placeholder="Invoice number, supplier, or a description of what you need…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void run()}
          />
          <select
            value={mode}
            onChange={(e) =>
              setMode(e.target.value as "keyword" | "semantic" | "hybrid")
            }
          >
            <option value="hybrid">Hybrid</option>
            <option value="keyword">Keyword</option>
            <option value="semantic">Semantic</option>
          </select>
          <button className="primary" onClick={() => void run()} disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
        {error && <p className="err-text">{error}</p>}
        {result && !result.semantic_available && mode !== "keyword" && (
          <div className="issue warning" style={{ marginTop: 12 }}>
            Semantic search is unavailable in this deployment (pgvector or the
            embedding model is not configured), so these results are keyword
            matches only.
          </div>
        )}
      </div>

      {result && (
        <div className="panel">
          <h2>
            {result.total} result{result.total === 1 ? "" : "s"} for “
            {result.query}”
          </h2>
          {result.hits.length === 0 ? (
            <p className="muted">No matches.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Invoice</th>
                  <th>Supplier</th>
                  <th className="right">Total</th>
                  <th className="right">Score</th>
                </tr>
              </thead>
              <tbody>
                {result.hits.map((hit, index) => (
                  <tr key={`${hit.document_id}-${hit.page_number}-${index}`}>
                    <td>
                      <Link href={`/documents/${hit.document_id}`}>
                        {hit.filename}
                      </Link>
                      {hit.page_number != null && (
                        <span className="muted"> · p{hit.page_number}</span>
                      )}
                      {hit.snippet && (
                        <div
                          className="snippet"
                          // ts_headline returns <b>-wrapped terms; the content
                          // is our own extracted text, and Postgres escapes
                          // everything else in the fragment.
                          dangerouslySetInnerHTML={{ __html: hit.snippet }}
                        />
                      )}
                    </td>
                    <td className="mono">{hit.invoice_number ?? "—"}</td>
                    <td>{hit.supplier_name ?? "—"}</td>
                    <td className="right mono">{hit.total ?? "—"}</td>
                    <td className="right mono muted">{hit.score.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  );
}

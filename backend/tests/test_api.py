"""API tests against a live Postgres.

Skipped automatically when no database is reachable, so `pytest` still passes
on a machine with only Python installed - the ingestion, extraction and
validation suites cover the logic either way.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from .conftest import CORRUPT_DIR, FILES_DIR


def _database_reachable() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(), reason="no database available"
)


@pytest.fixture(scope="module")
def client():
    from sqlalchemy import text

    from app.db import engine
    from app.main import app

    with TestClient(app) as test_client:
        with engine.begin() as conn:
            for table in (
                "validation_issues", "corrections", "extracted_invoices",
                "processing_events", "pages", "crosscheck_results", "documents",
            ):
                conn.execute(text(f"TRUNCATE {table} CASCADE"))
        yield test_client


def upload(client, *names, directory=FILES_DIR):
    files = [
        ("files", (name, open(os.path.join(directory, name), "rb").read(),
                   "application/octet-stream"))
        for name in names
    ]
    return client.post("/api/documents/upload", files=files)


class TestHealth:
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] in {"ok", "degraded"}
        assert "extraction_mode" in body


class TestUpload:
    def test_accepts_supported_file(self, client):
        response = upload(client, "digital_INV-2026-00001.pdf")
        assert response.status_code == 200
        assert len(response.json()["accepted"]) == 1

    def test_rejects_unsupported_extension(self, client):
        response = client.post(
            "/api/documents/upload",
            files=[("files", ("notes.rtf", b"junk", "application/rtf"))],
        )
        rejected = response.json()["rejected"]
        assert len(rejected) == 1
        assert "unsupported" in rejected[0]["reason"]

    def test_rejects_empty_file(self, client):
        response = client.post(
            "/api/documents/upload",
            files=[("files", ("empty.pdf", b"", "application/pdf"))],
        )
        assert response.json()["rejected"][0]["reason"] == "file is empty"

    def test_reupload_is_flagged_as_duplicate_not_rejected(self, client):
        upload(client, "digital_INV-2026-00002.pdf")
        again = upload(client, "digital_INV-2026-00002.pdf")
        assert again.json()["accepted"][0]["duplicate_of"] is not None


class TestProcessing:
    def test_document_completes_and_extracts(self, client, ground_truth):
        name = "digital_INV-2026-00003.pdf"
        document_id = upload(client, name).json()["accepted"][0]["document_id"]
        detail = client.get(f"/api/documents/{document_id}").json()
        assert detail["status"] in {"completed", "needs_review"}
        assert detail["invoices"]
        invoice = detail["invoices"][0]
        assert invoice["invoice_number"] == ground_truth[name]["invoice_number"]

    def test_corrupt_file_fails_with_a_reason(self, client):
        document_id = upload(
            client, "truncated.pdf", directory=CORRUPT_DIR
        ).json()["accepted"][0]["document_id"]
        detail = client.get(f"/api/documents/{document_id}").json()
        assert detail["status"] == "failed"
        assert "pdf_corrupt" in detail["error_message"]

    def test_events_recorded_for_each_stage(self, client):
        document_id = upload(
            client, "digital_INV-2026-00004.pdf"
        ).json()["accepted"][0]["document_id"]
        detail = client.get(f"/api/documents/{document_id}").json()
        stages = {e["stage"] for e in detail["events"]}
        assert {"ingest", "extract", "validate", "index"} <= stages

    def test_inconsistent_invoice_is_flagged(self, client):
        document_id = upload(
            client, "inconsistent_INV-2026-99001.pdf"
        ).json()["accepted"][0]["document_id"]
        detail = client.get(f"/api/documents/{document_id}").json()
        invoice = detail["invoices"][0]
        assert invoice["needs_review"]
        assert any(i["code"] == "total_mismatch" for i in invoice["issues"])

    def test_reprocess_failed(self, client):
        document_id = upload(
            client, "truncated.pdf", directory=CORRUPT_DIR
        ).json()["accepted"][0]["document_id"]
        response = client.post(f"/api/documents/{document_id}/reprocess")
        assert response.status_code == 200

    def test_status_counts(self, client):
        body = client.get("/api/documents/status").json()
        assert set(body) == {
            "pending", "processing", "completed", "needs_review", "failed"
        }


class TestCorrections:
    def test_correction_applies_and_revalidates(self, client):
        document_id = upload(
            client, "inconsistent_INV-2026-99001.pdf"
        ).json()["accepted"][0]["document_id"]
        invoice = client.get(f"/api/documents/{document_id}").json()["invoices"][0]
        assert invoice["needs_review"]

        corrected_total = round(
            (invoice["subtotal"] or 0) + (invoice["tax"] or 0), 2
        )
        response = client.patch(
            f"/api/documents/invoices/{invoice['id']}",
            json=[{"field": "total", "value": corrected_total}],
        )
        assert response.status_code == 200
        updated = response.json()["invoice"]
        assert updated["total"] == corrected_total
        assert not any(i["code"] == "total_mismatch" for i in updated["issues"])
        assert updated["corrections"][0]["field"] == "total"
        # A hand-corrected field is known-good.
        assert updated["field_confidence"]["total"] == 1.0

    def test_rejects_uncorrectable_field(self, client):
        document_id = upload(
            client, "digital_INV-2026-00005.pdf"
        ).json()["accepted"][0]["document_id"]
        invoice = client.get(f"/api/documents/{document_id}").json()["invoices"][0]
        response = client.patch(
            f"/api/documents/invoices/{invoice['id']}",
            json=[{"field": "extraction_method", "value": "hacked"}],
        )
        assert response.status_code == 400

    def test_rejects_invalid_amount(self, client):
        document_id = upload(
            client, "digital_INV-2026-00006.pdf"
        ).json()["accepted"][0]["document_id"]
        invoice = client.get(f"/api/documents/{document_id}").json()["invoices"][0]
        response = client.patch(
            f"/api/documents/invoices/{invoice['id']}",
            json=[{"field": "total", "value": "not a number"}],
        )
        assert response.status_code == 400

    def test_unknown_invoice_404s(self, client):
        response = client.patch(
            "/api/documents/invoices/00000000-0000-0000-0000-000000000000",
            json=[{"field": "total", "value": 1}],
        )
        assert response.status_code == 404


class TestSearch:
    def test_keyword_search_finds_invoice_number(self, client):
        upload(client, "digital_INV-2026-00007.pdf")
        body = client.get(
            "/api/search", params={"q": "INV-2026-00007", "mode": "keyword"}
        ).json()
        assert body["total"] >= 1

    def test_structured_filter(self, client):
        body = client.get("/api/search/invoices", params={"currency": "USD"}).json()
        assert isinstance(body, list)

    def test_empty_query_rejected(self, client):
        assert client.get("/api/search", params={"q": ""}).status_code == 422


class TestCrossCheckEndpoint:
    def test_pdf_against_excel(self, client):
        pdfs = [
            name for name in sorted(os.listdir(FILES_DIR))
            if name.startswith("crosscheck_")
        ]
        pdf_ids = [
            item["document_id"]
            for item in upload(client, *pdfs).json()["accepted"]
        ]
        xlsx_id = upload(
            client, "register_multi_sheet.xlsx"
        ).json()["accepted"][0]["document_id"]

        response = client.post(
            "/api/search/crosscheck",
            json={"left_document_ids": pdf_ids, "right_document_ids": [xlsx_id]},
        )
        assert response.status_code == 200
        counts = response.json()["counts"]
        assert counts["match"] == len(pdfs)
        assert counts["mismatch"] == 0

    def test_unknown_document_404s(self, client):
        response = client.post(
            "/api/search/crosscheck",
            json={
                "left_document_ids": ["00000000-0000-0000-0000-000000000000"],
                "right_document_ids": ["00000000-0000-0000-0000-000000000000"],
            },
        )
        assert response.status_code == 404

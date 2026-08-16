"""Unit tests for src/docproc/ingest.py -- the document router. Only exercises
the .edi and .txt paths directly (no LLM, no Docling needed); the Docling
path is tested for its ImportError-with-install-hint behavior only, since
actually invoking Docling requires the optional heavy dependency and a real
model download (covered by manual verification in LEARNING.md instead)."""

from __future__ import annotations

import builtins

import pytest

from src.docproc.ingestion.ingest import ingest


class TestEdiRouting:
    def test_edi_extension_routes_to_deterministic_parser(self, tmp_path):
        sample = tmp_path / "sample.edi"
        sample.write_text(
            "ISA*00*          *00*          *ZZ*A*ZZ*B*260812*1200*^*00501*1*0*P*:~\n"
            "N1*PR*TEST PAYER~\n"
            "CLP*CLM1*1*100.00*80.00*20.00*12*1*11*1~\n"
        )
        result = ingest(str(sample))
        assert result.kind == "structured"
        assert result.extraction is not None
        assert result.extraction.claim_number.value == "CLM1"
        assert "no LLM call needed" in result.source_note

    def test_x12_extension_alias_also_routes_to_edi_parser(self, tmp_path):
        sample = tmp_path / "sample.x12"
        sample.write_text("N1*PR*TEST~\nCLP*CLM2*1*50.00*40.00*10.00*12*1*11*1~\n")
        result = ingest(str(sample))
        assert result.kind == "structured"


class TestTextRouting:
    def test_txt_extension_is_read_as_is(self, tmp_path):
        sample = tmp_path / "sample.txt"
        sample.write_text("Plain prose document content.")
        result = ingest(str(sample))
        assert result.kind == "text"
        assert result.text == "Plain prose document content."
        assert result.source_note == "Read as plain text."

    def test_unknown_extension_falls_back_to_plain_text(self, tmp_path):
        """Anything not explicitly EDI or Docling-handled is read as text --
        the original, conservative default behavior."""
        sample = tmp_path / "sample.unknown"
        sample.write_text("some content")
        result = ingest(str(sample))
        assert result.kind == "text"


class TestDoclingImportGuard:
    def test_missing_docling_raises_helpful_runtime_error(self, tmp_path, monkeypatch):
        """If docling isn't installed, ingest() must raise a clear,
        actionable error -- not an opaque ImportError deep in a traceback."""
        sample = tmp_path / "sample.pdf"
        sample.write_bytes(b"%PDF-1.4 fake")

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "docling.document_converter" or name.startswith("docling"):
                raise ImportError("simulated: docling not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(RuntimeError, match="pip install docling"):
            ingest(str(sample))

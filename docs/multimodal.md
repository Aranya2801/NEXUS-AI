# Multimodal intelligence (Phase 11)

## What it does
| Input | Handling |
|---|---|
| Image upload (`.png .jpg .jpeg .webp .gif .tif .tiff .bmp`) | magic-byte check → Pillow decode under a pixel budget → re-encode to PNG (drops metadata) → OCR (+ optional vision caption) → indexed as `figure` chunks |
| PDF | text (as before); embedded images; ruled tables via pdfplumber (`table` chunks); scanned pages via `NEXUS_OCR_ENABLED` |
| DOCX / PPTX | embedded pictures extracted (icons below `figure_min_pixels` skipped, duplicates removed by hash) |
| Query by image | `POST /search/multimodal` (image + optional text): OCR/caption become the retrieval query. The image is not stored |

Chunks carry a `kind` (`text | table | figure`, DB column, Alembic 0002). Figures and tables never merge with prose chunks.
`/search` accepts `kinds`; hits report `kind`. Figures are served at `GET /documents/{id}/figures/{n}` with the document's access rule
(other tenants / hidden collections get 404) and `Content-Security-Policy: default-src 'none'`.

## Safety decisions
- **SVG is refused** (can carry script). Every image is decoded and re-encoded; a corrupt body behind valid magic bytes is rejected.
- Decompression-bomb defence: `NEXUS_MAX_IMAGE_PIXELS` (default 40 MP) checked before decode.
- Enrichment never aborts ingestion: missing tesseract → `ocr.status = "unavailable"`; a failing vision provider → `caption.status = "failed"`.
  If nothing at all can be extracted the job fails with an explicit message.
- A real vision provider receives image bytes: enable it deliberately (`NEXUS_VISION_PROVIDER`).

## Honest limitations
- Vision captioning was tested with the deterministic mock and with mock HTTP transports (request shape, retries). **No real VLM was called.**
- OCR was tested on clean generated text; accuracy on real scans, handwriting or low resolution is unmeasured. Only English language data is installed.
- Borderless tables are not detected; table text also appears in the flowing page text (duplicate indexing, accepted).
- Charts/diagrams are indexed only through OCR text and vision captions; there is no chart-to-data extraction.
- No retrieval-quality benchmark exists for multimodal content (the evaluation corpus is text-only).

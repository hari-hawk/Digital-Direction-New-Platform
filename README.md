# Digital Direction

Telecom document extraction pipeline. Extracts 60 standardized fields from carrier invoices, CSRs, contracts, and reports into a structured inventory.

- **Backend** — Python 3.11+, FastAPI, SQLAlchemy async
- **Frontend** — Next.js 16, Tailwind, shadcn/ui, next-themes
- **Database** — PostgreSQL 16 + pgvector (Docker)
- **LLM** — Gemini 2.5 (extraction) on Vertex AI or AI Studio, Claude (merge + eval)
- **Observability** — LangFuse (self-hosted, optional)

## Quick start (local)

### Prerequisites
- Python 3.11+
- Node.js 18+ (or Bun)
- Docker (any runtime — Docker Desktop, Colima, OrbStack)

### One-time setup
```bash
# Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Extras not in pyproject yet
pip install redis google-genai python-dateutil

# Frontend
cd frontend && npm install && cd ..

# API keys
cat > .env << 'EOF'
GEMINI_API_KEY=your-key
ANTHROPIC_API_KEY=your-key
# Optional — flip to Vertex for better 503 reliability
# LLM_BACKEND=vertex
# GCP_PROJECT_ID=your-gcp-project
# GCP_REGION=us-central1
EOF
```

### Run
```bash
# Start Postgres + Redis + Langfuse
docker-compose up -d

# Terminal 1 — backend
source .venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — frontend
cd frontend
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npm run dev
```

Open http://localhost:3000 — login passphrase `dd2026`.

## Pipeline

```
Upload → Classify → Parse → Extract → Validate → (Merge) → (Compliance) → Review → Export
         filename    text     Gemini   format +    cross-    contract     human     Excel
         + content   OCR      prompts  math        doc       checks       UI
         + LLM       Excel                         merge                  corrections
```

**Zero-template approach**: works on unseen documents. Known carriers (AT&T, Windstream, Spectrum, Peerless) have tuned prompts; unknown carriers (Frontier, Lumen, etc.) are auto-detected by the LLM and routed through a generic prompt.

## Key directories

- `backend/` — FastAPI app
  - `pipeline/` — classify / parse / extract / merge / validate / compliance
  - `api/` — REST endpoints (uploads, review, exports, dashboard, analytics, carriers)
  - `services/` — LLM clients, storage, feedback, spend ledger
- `configs/carriers/{name}/` — per-carrier metadata, prompts, domain knowledge
- `configs/processing/` — generic fallback prompts (work for any carrier)
- `db/init.sql` — Postgres schema (+ `00_create_langfuse_db.sh` for the Langfuse DB)
- `evals/` — golden-data comparison + LLM fuzzy judge
- `frontend/` — Next.js app

## Features

- **Multi-carrier**: 4 pre-configured + LLM auto-detect for any other carrier
- **Multi-format**: PDF (text + OCR via Docling + multimodal Gemini), Excel, CSV, `.msg`/`.eml`, Word
- **Auto-validation**: every extraction gets format + cross-field checks; rows flagged `Needs Review`
- **Auto-compliance** (post-merge): rate mismatch, expired contract, MTM inconsistency
- **Spend cap**: hard stop at configurable cumulative cost (default $100), meter in sidebar
- **Bin**: soft-delete with restore / purge / download
- **Re-extract**: re-run any completed project with updated prompts/config
- **Data grid**: compact 8-column or full 68-column view with horizontal scroll
- **Billing sidecar columns**: when CSR and invoice addresses diverge, both are preserved
- **Source-order guarantee**: extracted rows appear in PDF reading order

## Useful commands

```bash
# Extract a single file (CLI)
python -m backend.cli extract path/to/file.pdf --carrier att --doc-type invoice

# Run evals on a known-golden dataset
python -m evals.runner --extracted out.json --golden golden.xlsx --carrier att

# Health check
curl http://127.0.0.1:8000/health

# Current LLM spend
curl http://127.0.0.1:8000/api/spend
```

## Deployment

See `docs/design/` for architecture notes. Designed for eventual Cloud Run deployment (see Rajat's handoff for the GCP plan) or Vercel for the frontend + a separate Python host for the backend.

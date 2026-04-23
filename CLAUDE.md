# Digital Direction

Telecom document extraction pipeline. Extracts 60 standardized fields from carrier invoices, CSRs, contracts, and reports.

## Tech Stack
- Backend: Python 3.11+, FastAPI, SQLAlchemy async
- Frontend: Next.js, Tailwind, shadcn/ui
- Database: PostgreSQL 16 + pgvector (Docker)
- LLM: Gemini 2.5 (extraction), Claude (merge + eval)
- Document parsing: Docling (visual), pdfplumber (text), pandas (structured)

## Project Structure
- `backend/` — Python FastAPI application
- `backend/pipeline/` — 5-stage pipeline: classify → parse → extract → merge → validate
- `backend/config_loader.py` — Carrier config loading (CarrierConfig, MergeRulesConfig, etc.)
- `frontend/` — Next.js application
- `configs/` — Carrier YAML configs (version controlled)
- `configs/carriers/{carrier}/carrier.yaml` — Carrier metadata + merge_rules
- `configs/carriers/{carrier}/prompts/` — LLM extraction prompts per doc type
- `configs/carriers/{carrier}/domain_knowledge/` — USOC codes, field codes
- `scripts/` — QA and utility scripts
- `db/` — Database schema
- `evals/` — Golden data and eval framework
- `docs/design/` — Design documents

## Commands
- `docker compose up -d` — Start PostgreSQL + Redis
- `cd backend && uvicorn main:app --reload` — Start backend
- `cd frontend && NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 bun dev` — Start frontend
- `python -m backend.cli extract --input-dir <path>` — Run extraction CLI
- `python scripts/merge_qa.py` — Cross-doc merge QA (uses cached results)
- `python scripts/merge_qa.py --fresh` — Re-extract + merge QA ($0.34)

## Design Doc
See `docs/design/2026-04-16-extraction-pipeline-design.md` for full architecture.
See `docs/design/implementation-plan.md` for implementation tasks.

## Iron Rules

### No Hardcoding (applies to ALL client data)
- NEVER hardcode values from POC files, golden data, or sample outputs into code or configs
- Everything must be dynamically extracted from source documents or driven by carrier config
- If you see a specific value in golden data (account number, amount, address, etc.), you MUST NOT embed that value anywhere — the pipeline must discover it from the document

### Golden Data Rules
- Golden data is for EVALUATION ONLY — measuring accuracy of our extraction against known-correct output
- Golden data must NEVER influence extraction logic. Do not reverse-engineer prompts or configs to replicate golden data values
- Some golden data fields are ANALYST JUDGMENTS — values an analyst wrote based on their interpretation, context, or business rules (e.g., notes, status descriptions, analyst-chosen labels). These are NOT extractable from documents and we do NOT attempt to replicate them
- Our job: extract what the source documents contain. The analyst's job: add judgment on top
- When comparing extraction output to golden data, distinguish between:
  - **Extractable fields** — values that exist in the source document (account numbers, amounts, addresses, phone numbers, USOCs). Mismatches here are extraction bugs to fix
  - **Analyst fields** — values the analyst added from external knowledge or judgment (status notes, recommendations, contract interpretations). Mismatches here are expected and acceptable
- Never tune prompts to match analyst judgment fields — that's overfitting to opinion, not improving extraction

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health

## Working Preferences
- Model: Claude Opus (latest available, e.g. opus-4-6 or higher) with high thinking budget
- Always update docs and memory after design changes
- Carrier-specific logic in YAML configs, not code
- Read real data before designing extraction strategies

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

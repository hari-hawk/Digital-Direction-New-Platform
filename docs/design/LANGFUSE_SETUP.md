# LangFuse Integration Setup Guide

**Date:** April 22, 2026  
**Purpose:** Enable LLM observability for the Digital Direction extraction pipeline

## Overview

LangFuse is a self-hosted, open-source LLM observability platform. This guide walks you through:
1. Starting the LangFuse service (already in docker-compose.yml)
2. Configuring the backend to send traces to LangFuse
3. Testing the integration end-to-end
4. Using the LangFuse dashboard for debugging

---

## Quick Start

### 1. Start All Services

```bash
cd /path/to/Digital\ Direction/Platform
docker compose up -d
```

This starts:
- PostgreSQL (port 5432 → `localhost:5433`)
- Redis (port 6379)
- LangFuse (port 3100 → `localhost:3100`)

### 2. Verify LangFuse is Running

```bash
curl -s http://localhost:3100/health || echo "LangFuse not ready yet"
```

Wait for HTTP 200 response. LangFuse initializes its database on first start (~30 seconds).

### 3. Access LangFuse Dashboard

Open your browser:
```
http://localhost:3100
```

Default credentials (if first login):
- Email: `admin@langfuse.com`
- Password: `admin`

### 4. Backend Configuration

Ensure your `.env` has these settings (defaults are correct for local POC):

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-test-key
LANGFUSE_SECRET_KEY=sk-lf-test-key
LANGFUSE_HOST=http://localhost:3100
```

### 5. Start the Backend

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

---

## What Gets Traced

### Extraction Calls (Gemini)
Every LLM call to Gemini for document extraction is automatically traced:
- **Model:** gemini-2.5-flash or gemini-2.5-pro
- **Data:** Prompt (truncated to 2000 chars), response, token counts, latency
- **Metadata:** Model name, call type ("extraction")

### Eval Calls (Claude)
Every Claude call in the eval framework is traced:
- **Model:** claude-opus-4-6
- **Data:** Eval prompt, LLM judgment, fuzzy matching results
- **Metadata:** Carrier name, eval type ("fuzzy_match")

---

## Using the LangFuse Dashboard

### View All Traces

1. Go to http://localhost:3100
2. Click **"Traces"** in the sidebar
3. See all LLM calls in chronological order

### Search & Filter

Filter by:
- **Model:** Filter for "gemini" or "claude" calls
- **Name:** Filter by "extraction" or "eval_judge"
- **Metadata:** Filter by carrier (if you add carrier metadata)

Example: "Show me all eval calls for Windstream"
```
metadata.carrier = "Windstream" AND name = "eval_judge"
```

### View a Trace

Click any trace to see:
- **Input:** The full prompt (truncated in list view, full here)
- **Output:** The LLM response
- **Tokens:** Input + output token counts
- **Latency:** How long the call took
- **Model:** Which LLM was used
- **Metadata:** Custom fields (carrier, call type)

### Add Annotations

Developers can click "Add Note" to document findings:
- "USOC field missing from prompt"
- "Good extraction, eval scored 92%"
- "Root cause: missing component name list"

Team members see these notes on all future views.

---

## Debugging Example: Find Why USOC Extraction Failed

**Scenario:** An analyst reports low confidence on USOC extraction for a Windstream CSR.

**Traditional approach (15–20 min):**
1. Find the CSR PDF in storage
2. Read it manually
3. Find the extraction code + config
4. Reconstruct the prompt
5. Re-run extraction
6. Compare output
7. Make an educated guess about the fix

**With LangFuse (1–2 min):**
1. Open LangFuse dashboard
2. Filter: `metadata.carrier = "Windstream" AND name = "extraction"`
3. Look for low-confidence USOC fields
4. Click the trace
5. Read the exact prompt + response
6. See: "Prompt doesn't mention USOC codes → add component name list to context"
7. Update `configs/carriers/windstream/carrier.yaml`
8. Re-run extraction

---

## Integration with Eval Framework

When eval judge runs, it:
1. Compares extraction output to golden data
2. Produces field-level accuracy scores
3. Traces each Claude eval call to LangFuse

**Correlation workflow:**
```
Extraction trace (Gemini) → eval_judge trace (Claude) → score (CORRECT/WRONG/MISSING)
                ↓                          ↓
            LangFuse dashboard links them together
            Drill down: "Which extraction calls scored < 70%?"
```

---

## Cost Tracking

LangFuse automatically tracks:
- **Input tokens** per call
- **Output tokens** per call
- **Model** used (Gemini vs Claude)
- **Latency** in milliseconds

To analyze costs:
1. Go to **"Pricing"** tab in LangFuse dashboard
2. See token usage by model
3. Combine with `configs/processing/llm_costs.yaml` to calculate USD

Manual calculation:
```python
from backend.services.spend_ledger import current_total
print(f"Total spent: ${current_total():.2f}")
```

---

## Common Issues & Troubleshooting

### LangFuse Not Accessible at localhost:3100

**Problem:** Browser shows "connection refused"

**Solution:**
```bash
# Check if service is running
docker ps | grep langfuse

# If not running, start it
docker compose up langfuse -d

# Check logs
docker compose logs langfuse
```

### Traces Not Appearing in Dashboard

**Problem:** Run extraction but no traces show up in LangFuse.

**Checklist:**
1. Verify `LANGFUSE_ENABLED=true` in your environment
2. Verify backend is using the correct settings:
   ```bash
   curl http://127.0.0.1:8000/health  # Should work
   ```
3. Check backend logs for LangFuse errors:
   ```bash
   # In the terminal running uvicorn, look for:
   # "Tracing is best-effort, never block extraction"
   ```
4. Verify LangFuse connectivity:
   ```bash
   curl -s http://localhost:3100/health
   ```

### Database Connection Error

**Problem:** LangFuse container exits with database error.

**Solution:**
```bash
# Wait for postgres to be ready, then restart langfuse
docker compose up -d postgres
sleep 10
docker compose up -d langfuse

# Or reset everything
docker compose down -v
docker compose up -d
```

---

## Next Steps: Moving to LangSmith (Phase 7)

Once you're comfortable with LangFuse observability:

1. **Export your learnings:** Use LangFuse traces to identify common failure patterns
2. **Document patterns:** "AT&T USOC extraction needs component name list"
3. **Plan Phase 7:** Self-healing feedback loop
4. **Migrate to LangSmith:** When ready for cloud-based, production-grade observability

Migration is straightforward (different SDK, same trace data).

---

## Production Checklist

For production deployment (beyond POC):

- [ ] Configure NEXTAUTH credentials (not "local" defaults)
- [ ] Use managed PostgreSQL (not local docker)
- [ ] Enable HTTPS for LangFuse dashboard
- [ ] Set up regular database backups
- [ ] Configure trace retention policy (how many days to keep traces)
- [ ] Document access control (who can view traces)
- [ ] Monitor LangFuse storage usage

---

## References

- **LangFuse Docs:** https://langfuse.com/docs
- **LangFuse Self-Hosted:** https://langfuse.com/docs/self-host
- **Backend Tracing Code:** `backend/services/llm.py` (`_trace_llm_call`)
- **Eval Tracing Code:** `evals/judge.py` (`_trace_eval_call`)

---

**Last Updated:** April 22, 2026  
**Status:** LangFuse POC active, ready for team use

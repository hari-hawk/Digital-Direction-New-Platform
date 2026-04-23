# LangFuse Quick Start — Team Reference Card

**Last Updated:** April 22, 2026  
**Status:** Live and ready to use

---

## ⚡ 30-Second Setup

```bash
# 1. Start services (if not already running)
docker compose up -d

# 2. Start backend
cd backend && uvicorn main:app --reload --host 127.0.0.1 --port 8000

# 3. Open LangFuse dashboard
open http://localhost:3100
```

**That's it!** Traces will appear automatically as you run extractions.

---

## 🎯 Access Dashboard

| What | Where |
|------|-------|
| **URL** | http://localhost:3100 |
| **Traces** | Click "Traces" in left sidebar |
| **Models** | Gemini (extraction) + Claude (eval) |
| **Filter** | By model, name, or metadata |

---

## 🔍 Common Tasks

### View All Extraction Traces
```
Dashboard → Traces
Filter: name = "extraction"
```

### Find Failed USOC Extractions
```
Dashboard → Traces
Filter: name = "extraction" AND model = "gemini"
Scan for low confidence
Click trace → See exact prompt + response
```

### Check Eval Judge Results
```
Dashboard → Traces
Filter: name = "eval_judge"
See which fields were judged CORRECT/WRONG/PARTIAL
Click → See Claude's reasoning
```

### View Token Usage by Model
```
Dashboard → Analytics (or Pricing tab)
See input + output tokens per model
Multiply by config/processing/llm_costs.yaml rates
```

---

## 📊 Dashboard Features

| Feature | How |
|---------|-----|
| **View Trace** | Click any row in Traces tab |
| **Search** | Use filter box (supports fuzzy search) |
| **Sort** | Click column header |
| **Add Note** | Click "Add annotation" on trace detail |
| **Export** | Copy JSON from trace view |

---

## ⚙️ If Something Breaks

### Traces not showing up?
```bash
# 1. Check LangFuse is running
curl http://localhost:3100

# 2. Check backend config
grep LANGFUSE backend/settings.py

# 3. Check backend logs
# (In the terminal running uvicorn, look for "LangFuse" messages)

# 4. Restart backend
# (Ctrl+C, then re-run uvicorn)
```

### LangFuse dashboard loading forever?
```bash
# Wait 30 seconds (first init of database)
# Or check docker logs
docker compose logs langfuse
```

### Getting errors about "LangFuse not found"?
```bash
# Make sure you're in the right directory
cd /path/to/Digital\ Direction/Platform

# Make sure Python path includes backend
export PYTHONPATH=$PWD:$PYTHONPATH
```

---

## 📚 Full Documentation

See these files for detailed info:
- **Setup & Usage:** `docs/design/LANGFUSE_SETUP.md`
- **Integration Details:** `docs/design/LANGFUSE_INTEGRATION_COMPLETE.md`
- **Testing:** `scripts/test_langfuse_integration.py`

---

## 🔧 Config Reference

### Backend Settings (`backend/settings.py`)
```python
langfuse_enabled: bool = True
langfuse_public_key: str = "pk-lf-test-key"
langfuse_secret_key: str = "sk-lf-test-key"
langfuse_host: str = "http://localhost:3100"
```

### Docker Compose (`docker-compose.yml`)
```yaml
langfuse:
  image: langfuse/langfuse:2
  ports:
    - "3100:3000"  # Access on localhost:3100
  environment:
    DATABASE_URL: postgresql://...
```

---

## 🚀 Tips & Tricks

**💡 Tip 1:** Use LangFuse filters to find patterns
```
"Show me all Windstream CSR extractions that took > 1000ms"
Filter: metadata.carrier = "Windstream" AND latency_ms > 1000
```

**💡 Tip 2:** Add notes to document findings
- Click a trace
- Click "Add annotation"
- Write: "USOC missing from CSR variant X"
- Team sees this on future traces of same type

**💡 Tip 3:** Export traces for offline analysis
- Click trace → "..." menu → "Export JSON"
- Use in evals framework or analysis

**💡 Tip 4:** Watch real-time as you test
- Open dashboard in one window
- Run extraction in another
- Traces appear in real-time

---

## 📞 Questions?

- **Setup issues?** → `docs/design/LANGFUSE_SETUP.md` section "Troubleshooting"
- **How to use dashboard?** → `docs/design/LANGFUSE_SETUP.md` section "Using the LangFuse Dashboard"
- **Integration details?** → `docs/design/LANGFUSE_INTEGRATION_COMPLETE.md`
- **Want to move to LangSmith?** → Ask team lead about Phase 7 planning

---

**Remember:** Tracing is best-effort and non-blocking. It never slows down extraction.

🎯 **Start now:** `open http://localhost:3100`

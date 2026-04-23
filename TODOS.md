# TODOS

## ✅ LangFuse LLM Observability Integration (COMPLETED)
**Phase:** Phase 6 (Eval Framework) + Phase 7 Prep
**Status:** ACTIVE and running
**What:** LangFuse integration for tracing Gemini extraction calls and Claude eval judge calls.
**Completed:** April 22, 2026
**Details:**
- ✅ Backend LangFuse client enabled (backend/services/llm.py)
- ✅ Gemini extraction traces active
- ✅ Claude eval judge traces active (evals/judge.py)
- ✅ Docker container running (port 3100)
- ✅ Environment configuration template (.env.example)
- ✅ Setup documentation (docs/design/LANGFUSE_SETUP.md)
- ✅ Integration test script (scripts/test_langfuse_integration.py)

**Access Dashboard:** http://localhost:3100  
**Documentation:** docs/design/LANGFUSE_INTEGRATION_COMPLETE.md

---

## LangSmith Migration (Phase 7 Candidate)
**Phase:** Post-Phase 6, Phase 7 (Self-Healing Loop)
**Status:** Planned for future (after LangFuse POC exploration)
**What:** Migrate from LangFuse to LangSmith for cloud-based observability.
**Why:** LangSmith enables advanced pattern detection, automatic regression alerts, and built-in API for self-healing feedback loops.
**Dependencies:** LangFuse POC learnings + Phase 7 architecture planning
**Timeline:** Q2 2026 (after Phase 7 feedback loop is implemented)

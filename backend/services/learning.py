"""Self-learning layer — turn analyst corrections into searchable memory.

Architecture choice (May-2026): we use the existing pgvector column on the
`corrections` table rather than a separate vector DB. Reasons:
  - The column + ivfflat index were already scaffolded
  - At the scale we operate (<10K corrections per customer) pgvector is fine
  - One fewer external service to operate / secret to rotate
  - get_relevant_corrections_db() already does two-tier (exact → vector) recall

What this module adds:
  1. `embed_synthesis()` — build the embeddable sentence for a correction using
     strategy (c) (full context: carrier + doc_type + field + before/after +
     source snippet). Picked over (a) raw snippet or (b) snippet+field+value
     because it gives the retriever the most actionable context per record.
  2. `store_correction_embedding()` — generate embedding + persist via raw SQL
     (pgvector type isn't natively supported by SQLAlchemy).
  3. `schedule_embedding()` — fire-and-forget background task wrapper so the
     correction-save HTTP path stays snappy.

Failure mode is graceful: if the embedding write fails (LLM down, DB hiccup),
the correction row still lands without an embedding. Tier-1 exact-match
recall still works. Tier-2 vector recall just skips that record. A backfill
job (commit 3 in the plan) will sweep up the gaps.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import async_session

logger = logging.getLogger(__name__)


# ── Synthesis strategy (option C from the brain-rollout plan) ────────────────


def embed_synthesis(
    *,
    carrier: str | None,
    doc_type: str | None,
    field_name: str,
    extracted_value: str | None,
    corrected_value: str | None,
    source_text_snippet: str | None,
    format_variant: str | None = None,
) -> str:
    """Build the natural-language sentence we embed for retrieval.

    Strategy (c) — full-context synthesis. Retrieval matches "similar
    correction situations" rather than just similar raw text. The cost is
    ~3× tokens per embedding vs. raw-snippet, but at <$0.0001/embedding it's
    irrelevant. The win: queries like "what should service_type be when the
    invoice says 'broadband internet 100mbps'" match the right corrections.

    Empty / None inputs are tolerated; the sentence degrades gracefully.
    """
    carrier_part = (carrier or "unknown carrier").strip()
    doc_part = (doc_type or "document").strip()
    field_part = field_name.strip() if field_name else "(field)"
    snippet = (source_text_snippet or "").strip()
    if len(snippet) > 400:
        snippet = snippet[:400] + "…"

    extracted = (extracted_value or "(blank)").strip()
    corrected = (corrected_value or "(blank)").strip()
    fmt = f" ({format_variant})" if format_variant else ""

    # The sentence reads naturally so the embedding sits in the same
    # semantic neighbourhood as questions like "what's the right service_type
    # for this comcast invoice line?". Don't change the wording without
    # re-running backfill — old embeddings won't re-cluster.
    return (
        f"On a {carrier_part} {doc_part}{fmt}, the {field_part} should be "
        f"'{corrected}' not '{extracted}' when the source text says: "
        f"\"{snippet}\""
    )


# ── Storage ──────────────────────────────────────────────────────────────────


async def store_correction_embedding(
    correction_id: uuid.UUID | str,
    *,
    carrier: str | None,
    doc_type: str | None,
    field_name: str,
    extracted_value: str | None,
    corrected_value: str | None,
    source_text_snippet: str | None,
    format_variant: str | None = None,
    db: AsyncSession | None = None,
) -> bool:
    """Generate the synthesis embedding for a correction and persist it.

    Returns True on success, False on any failure (always non-raising — the
    correction row itself stays valid regardless).
    """
    if not field_name:
        return False
    sess, owned = (db, False) if db is not None else (async_session(), True)
    try:
        # Build sentence + embed
        sentence = embed_synthesis(
            carrier=carrier, doc_type=doc_type, field_name=field_name,
            extracted_value=extracted_value, corrected_value=corrected_value,
            source_text_snippet=source_text_snippet, format_variant=format_variant,
        )
        try:
            from backend.services.llm import get_gemini
            gemini = get_gemini()
            vec = await gemini.embed(sentence)
        except Exception as e:
            logger.warning(f"learning.embed failed for correction {correction_id}: {e}")
            return False

        if not vec or not isinstance(vec, list):
            return False

        # Persist via raw SQL — pgvector's `vector` type isn't natively
        # supported by SQLAlchemy, but Postgres accepts a stringified array.
        # The column is `embedding vector(768)`.
        cid = uuid.UUID(str(correction_id)) if not isinstance(correction_id, uuid.UUID) else correction_id
        emb_literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        await sess.execute(
            sql_text(
                "UPDATE corrections SET embedding = CAST(:emb AS vector) "
                "WHERE id = :cid"
            ),
            {"emb": emb_literal, "cid": cid},
        )
        if owned:
            await sess.commit()
        logger.info(
            "learning: embedded correction %s (carrier=%s, field=%s, dim=%d)",
            correction_id, carrier, field_name, len(vec),
        )
        return True
    except Exception as e:
        logger.exception(f"store_correction_embedding failed for {correction_id}: {e}")
        if owned:
            try:
                await sess.rollback()
            except Exception:
                pass
        return False
    finally:
        if owned:
            await sess.close()


# ── Convenience wrapper for the API path ────────────────────────────────────


def schedule_embedding(background_tasks: Any, **kwargs) -> None:
    """Fire-and-forget scheduler. Pass either a FastAPI BackgroundTasks (will
    use add_task) or None / anything-async-compatible (will use asyncio).

    Used from review.py + exports.py + master_data.py so the analyst's save
    path returns in <100ms while the embedding write runs in the background.
    """
    if background_tasks is not None and hasattr(background_tasks, "add_task"):
        background_tasks.add_task(store_correction_embedding, **kwargs)
        return
    # Fall back to fire-and-forget on the running event loop
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(store_correction_embedding(**kwargs))
    except RuntimeError:
        # No running loop (e.g. called from sync path); run synchronously as a
        # best-effort. This is a slow path — try to avoid it.
        asyncio.run(store_correction_embedding(**kwargs))

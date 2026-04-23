"""LLM client wrappers — Gemini + Claude with retry, rate limiting, cost tracking.

Uses google.genai SDK (new) and anthropic SDK.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types
import anthropic
import yaml

from backend.settings import settings
from backend.services.spend_ledger import check_budget, record as record_spend


# ── LangFuse observability (optional, self-hosted) ──

_langfuse = None


def get_langfuse():
    """Lazy-init LangFuse client. Returns None if not configured."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        return _langfuse
    except Exception:
        return None


def _trace_llm_call(
    model: str,
    prompt: str,
    response: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    call_type: str = "extraction",
):
    """Log an LLM call to LangFuse if enabled. Non-blocking, best-effort."""
    lf = get_langfuse()
    if not lf:
        return
    try:
        trace = lf.trace(name=call_type, metadata={"model": model})
        trace.generation(
            name=f"{call_type}_{model}",
            model=model,
            input=prompt[:2000],  # Truncate for storage (full prompts are huge)
            output=response[:2000],
            usage={"input": input_tokens, "output": output_tokens},
            metadata={"latency_ms": latency_ms},
        )
    except Exception:
        pass  # Tracing is best-effort, never block extraction


def _load_cost_table() -> dict[str, dict[str, float]]:
    """Load LLM token costs from config file, fall back to defaults."""
    cost_file = Path(settings.configs_dir) / "processing" / "llm_costs.yaml"
    if cost_file.exists():
        return yaml.safe_load(cost_file.read_text()) or {}
    return {
        "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
        "gemini-2.5-pro": {"input": 1.25, "output": 5.00},
        "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
        "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    }


_COST_TABLE = _load_cost_table()


@dataclass
class LLMResponse:
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    raw_response: Any = None

    @property
    def estimated_cost_usd(self) -> float:
        model_key = self.model.split("/")[-1] if "/" in self.model else self.model
        for key, rates in _COST_TABLE.items():
            if key in model_key.lower():
                return (self.input_tokens * rates["input"] + self.output_tokens * rates["output"]) / 1_000_000
        return 0.0


class GeminiClient:
    def __init__(self, max_concurrent: int | None = None, retry_delays: list[int] | None = None):
        if settings.llm_backend == "vertex":
            if not settings.gcp_project_id:
                raise RuntimeError(
                    "llm_backend=vertex requires GCP_PROJECT_ID. "
                    "Run `gcloud auth application-default login` and set GCP_PROJECT_ID in .env."
                )
            self._client = genai.Client(
                vertexai=True,
                project=settings.gcp_project_id,
                location=settings.gcp_region,
            )
            logger.info(f"Gemini backend: Vertex AI ({settings.gcp_project_id}/{settings.gcp_region})")
        else:
            self._client = genai.Client(api_key=settings.gemini_api_key)
            logger.info("Gemini backend: AI Studio (api key)")
        self._semaphore = asyncio.Semaphore(max_concurrent or settings.gemini_max_concurrent)
        self._retry_delays = retry_delays or settings.llm_retry_delays

    async def extract(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.1,
        response_mime_type: str = "application/json",
    ) -> LLMResponse:
        check_budget()
        model = model or settings.gemini_extraction_model
        async with self._semaphore:
            return await self._call_with_retry(prompt, model, temperature, response_mime_type)

    async def extract_multimodal(
        self,
        prompt: str,
        pdf_path: str,
        model: str | None = None,
        temperature: float = 0.1,
    ) -> LLMResponse:
        check_budget()
        model = model or settings.gemini_complex_model
        async with self._semaphore:
            return await self._call_multimodal_with_retry(prompt, pdf_path, model, temperature)

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        model = model or settings.gemini_embedding_model
        result = await self._client.aio.models.embed_content(model=model, contents=text)
        return result.embeddings[0].values

    async def _call_with_retry(
        self, prompt: str, model: str, temperature: float, response_mime_type: str
    ) -> LLMResponse:
        last_error = None
        for attempt, delay in enumerate(self._retry_delays):
            try:
                start = time.monotonic()
                # Use streaming to avoid total-time timeouts.
                # Gemini 2.5 thinking can take minutes before output starts.
                # Streaming lets us detect idle (stuck) vs slow (thinking).
                chunks = []
                input_tokens = 0
                output_tokens = 0

                config_kwargs = dict(
                    temperature=temperature,
                    response_mime_type=response_mime_type,
                    max_output_tokens=65536,
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
                )
                # Disable "thinking" only on Flash — adds 10-30s per call with no benefit
                # for pattern extraction. Pro does not support thinking_budget=0.
                if "flash" in model.lower():
                    config_kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)

                stream = await self._client.aio.models.generate_content_stream(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(**config_kwargs),
                )
                async for chunk in stream:
                    if chunk.text:
                        chunks.append(chunk.text)
                    # Track token counts from the final chunk's usage metadata
                    if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                        input_tokens = getattr(chunk.usage_metadata, "prompt_token_count", 0) or 0
                        output_tokens = getattr(chunk.usage_metadata, "candidates_token_count", 0) or 0

                latency = int((time.monotonic() - start) * 1000)
                content = "".join(chunks)

                _trace_llm_call(model, prompt[:500], content[:500],
                                input_tokens, output_tokens, latency, "extraction")
                resp = LLMResponse(
                    content=content,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency,
                )
                record_spend(resp.estimated_cost_usd)
                return resp
            except Exception as e:
                last_error = e
                err_str = str(e)
                is_retryable = ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                                or "503" in err_str or "UNAVAILABLE" in err_str)
                if is_retryable:
                    wait = delay + (attempt * 2)
                    logger.warning(f"Gemini retryable error (attempt {attempt+1}/{len(self._retry_delays)}), waiting {wait}s: {err_str[:100]}")
                    await asyncio.sleep(wait)
                    continue
                if attempt < len(self._retry_delays) - 1:
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_error

    async def _call_multimodal_with_retry(
        self, prompt: str, pdf_path: str, model: str, temperature: float
    ) -> LLMResponse:
        last_error = None
        for attempt, delay in enumerate(self._retry_delays):
            try:
                start = time.monotonic()
                uploaded = await asyncio.to_thread(self._client.files.upload, file=pdf_path)
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=[prompt, uploaded],
                    config=genai_types.GenerateContentConfig(temperature=temperature),
                )
                latency = int((time.monotonic() - start) * 1000)

                usage = response.usage_metadata
                resp = LLMResponse(
                    content=response.text,
                    model=model,
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    latency_ms=latency,
                    raw_response=response,
                )
                record_spend(resp.estimated_cost_usd)
                return resp
            except Exception as e:
                last_error = e
                err_str = str(e)
                is_retryable = ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                                or "503" in err_str or "UNAVAILABLE" in err_str)
                if is_retryable:
                    wait = delay + (attempt * 2)
                    logger.warning(f"Gemini multimodal retryable error (attempt {attempt+1}/{len(self._retry_delays)}), waiting {wait}s: {err_str[:100]}")
                    await asyncio.sleep(wait)
                    continue
                if attempt < len(self._retry_delays) - 1:
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_error


class ClaudeClient:
    def __init__(self, max_concurrent: int | None = None, retry_delays: list[int] | None = None):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._semaphore = asyncio.Semaphore(max_concurrent or settings.claude_max_concurrent)
        self._retry_delays = retry_delays or settings.llm_retry_delays

    async def call(
        self,
        prompt: str,
        system: str = "",
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        check_budget()
        model = model or settings.claude_merge_model
        max_tokens = max_tokens or settings.claude_max_tokens
        async with self._semaphore:
            return await self._call_with_retry(prompt, system, model, temperature, max_tokens)

    async def _call_with_retry(
        self, prompt: str, system: str, model: str, temperature: float, max_tokens: int
    ) -> LLMResponse:
        last_error = None
        for attempt, delay in enumerate(self._retry_delays):
            try:
                start = time.monotonic()
                kwargs = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if system:
                    kwargs["system"] = system

                response = await self._client.messages.create(**kwargs)
                latency = int((time.monotonic() - start) * 1000)

                resp = LLMResponse(
                    content=response.content[0].text,
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    latency_ms=latency,
                    raw_response=response,
                )
                record_spend(resp.estimated_cost_usd)
                return resp
            except Exception as e:
                last_error = e
                if "rate_limit" in str(e).lower() or "429" in str(e):
                    await asyncio.sleep(delay + (attempt * 0.5))
                    continue
                if attempt < len(self._retry_delays) - 1:
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_error


# Singletons
_gemini: GeminiClient | None = None
_claude: ClaudeClient | None = None


def get_gemini() -> GeminiClient:
    global _gemini
    if _gemini is None:
        _gemini = GeminiClient()
    return _gemini


def get_claude() -> ClaudeClient:
    global _claude
    if _claude is None:
        _claude = ClaudeClient()
    return _claude

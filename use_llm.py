"""
use_llm.py — thin OpenRouter client for PlainWatch.

Dependencies: httpx, python-dotenv. No SDK (openai adds ~34 MB RSS for one endpoint).

Async-first, because Activity runs under asyncio.Semaphore(4) and every call is
I/O-bound. Sync wrappers (`*_sync`) exist for REPL/testing only — never call them
from inside the Executor's event loop.

    llm = UseLLM()
    text = await llm.get_llm_response(query="summarise this", system="be terse")
    data = await llm.get_json_response(query="...", schema={...})
    await llm.aclose()

    # or as a context manager
    async with UseLLM() as llm:
        text = await llm.get_llm_response(query="hello")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# 429/503 carry Retry-After; 502/504 are transient provider failures.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# error_type values worth retrying (OpenRouter's stable typed vocabulary).
RETRYABLE_ERROR_TYPES = {
    "rate_limit_exceeded",
    "provider_overloaded",
    "provider_unavailable",
    "timeout",
    "server",
}

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMError(RuntimeError):
    """Any non-recoverable failure from OpenRouter."""

    def __init__(self, message: str, *, status: int | None = None,
                 error_type: str | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.body = body or {}

    @property
    def retryable(self) -> bool:
        return (
            (self.status in RETRYABLE_STATUS)
            or (self.error_type in RETRYABLE_ERROR_TYPES)
        )


@dataclass
class LLMResponse:
    """Full result, when you want cost/usage rather than just text."""

    text: str
    model: str
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float | None = None
    generation_id: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        return self.text


class UseLLM:
    """OpenRouter chat-completions client.

    Reads from .env (all optional except the key):
        OPENROUTER_API_KEY        required
        OPENROUTER_MODEL          default model slug
        OPENROUTER_FALLBACK_MODELS  comma-separated slugs, used via route=fallback
        OPENROUTER_SITE_URL       HTTP-Referer, for openrouter.ai app attribution
        OPENROUTER_APP_NAME       X-OpenRouter-Title
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_models: Sequence[str] | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        site_url: str | None = None,
        app_name: str | None = None,
    ):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not set (env or api_key argument)")

        self.model = model or os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.0-flash-vl:free")

        if fallback_models is None:
            raw = os.getenv("OPENROUTER_FALLBACK_MODELS", "")
            fallback_models = [m.strip() for m in raw.split(",") if m.strip()]
        self.fallback_models = list(fallback_models)

        self.timeout = timeout
        self.max_retries = max_retries
        self.site_url = site_url or os.getenv("OPENROUTER_SITE_URL")
        self.app_name = app_name or os.getenv("OPENROUTER_APP_NAME", "PlainWatch")

        self._client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- client

    @property
    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.site_url:
            h["HTTP-Referer"] = self.site_url
        if self.app_name:
            h["X-OpenRouter-Title"] = self.app_name
        return h

    def _get_client(self) -> httpx.AsyncClient:
        # One pooled client across calls — 4 concurrent Activities share it.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "UseLLM":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------- internals

    @staticmethod
    def _build_messages(
        query: str,
        system: str | None = None,
        history: Sequence[dict] | None = None,
        prefill: str | None = None,
    ) -> list[dict]:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": query})
        if prefill:
            # Assistant prefill: model continues from this partial response.
            messages.append({"role": "assistant", "content": prefill})
        return messages

    @staticmethod
    def _raise_for_body_error(body: dict, status: int) -> None:
        """OpenRouter can return HTTP 200 with an `error` object and no choices.

        Headers are committed as soon as the provider accepts the request, so any
        failure after that point lands in the body. Checking status alone is not
        enough.
        """
        err = body.get("error")
        if not err and body.get("choices"):
            # Per-choice error: partial content with finish_reason "error".
            choice_err = body["choices"][0].get("error")
            if choice_err:
                err = choice_err
        if not err:
            return
        meta = err.get("metadata") or {}
        raise LLMError(
            err.get("message", "unknown OpenRouter error"),
            status=err.get("code", status),
            error_type=meta.get("error_type"),
            body=body,
        )

    def _payload(
        self,
        messages: list[dict],
        model: str | None,
        max_tokens: int | None,
        temperature: float | None,
        top_p: float | None,
        seed: int | None,
        stop: str | Sequence[str] | None,
        response_format: dict | None,
        plugins: Sequence[dict] | None,
        extra: dict | None,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        if self.fallback_models:
            payload["models"] = [payload["model"], *self.fallback_models]
            payload["route"] = "fallback"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if seed is not None:
            payload["seed"] = seed
        if stop is not None:
            payload["stop"] = stop
        if response_format is not None:
            payload["response_format"] = response_format
        if plugins:
            payload["plugins"] = list(plugins)
        if extra:
            payload.update(extra)
        return payload

    async def _post(self, payload: dict) -> dict:
        """POST with retry on transient failures, honouring Retry-After."""
        client = self._get_client()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                r = await client.post(API_URL, headers=self._headers, json=payload)

                if r.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = self._retry_delay(r, attempt)
                    logger.warning(
                        "OpenRouter %s on attempt %d/%d, retrying in %.1fs",
                        r.status_code, attempt + 1, self.max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                try:
                    body = r.json()
                except ValueError:
                    raise LLMError(
                        f"non-JSON response (HTTP {r.status_code}): {r.text[:300]}",
                        status=r.status_code,
                    )

                # HTTP 200 does not mean success — check the body.
                self._raise_for_body_error(body, r.status_code)
                r.raise_for_status()
                return body

            except LLMError as e:
                if e.retryable and attempt < self.max_retries:
                    delay = self._backoff(attempt)
                    logger.warning(
                        "OpenRouter error_type=%s on attempt %d/%d, retrying in %.1fs: %s",
                        e.error_type, attempt + 1, self.max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)
                    last_exc = e
                    continue
                raise

            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = self._backoff(attempt)
                    logger.warning(
                        "OpenRouter transport error on attempt %d/%d, retrying in %.1fs: %s",
                        attempt + 1, self.max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"transport failure after {self.max_retries} retries: {e}")

        raise LLMError(f"exhausted retries: {last_exc}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        # Exponential with jitter: ~1s, 2s, 4s.
        return (3 ** attempt) + random.uniform(0, 0.5)

    def _retry_delay(self, r: httpx.Response, attempt: int) -> float:
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self._backoff(attempt)

    @staticmethod
    def _parse(body: dict) -> LLMResponse:
        choices = body.get("choices") or []
        if not choices:
            raise LLMError("response contained no choices", body=body)

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish = choice.get("finish_reason")

        if not text and finish == "length":
            # Reasoning model burned the whole budget on reasoning. Retrying
            # does not help — raise max_tokens or cap reasoning instead.
            raise LLMError(
                "empty content with finish_reason=length — raise max_tokens "
                "or cap reasoning effort",
                error_type="max_tokens_exceeded",
                body=body,
            )

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            model=body.get("model", ""),
            finish_reason=finish,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            cost=usage.get("cost"),
            generation_id=body.get("id"),
            raw=body,
        )

    # ------------------------------------------------------------ public API

    async def get_llm_response(
        self,
        query: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        stop: str | Sequence[str] | None = None,
        history: Sequence[dict] | None = None,
        prefill: str | None = None,
        web_search: bool = False,
        response_format: dict | None = None,
        extra: dict | None = None,
        full: bool = False,
    ) -> str | LLMResponse:
        """Send a prompt, get text back.

        full=True returns an LLMResponse (usage, cost, model actually used).
        web_search=True enables OpenRouter's `web` plugin for grounding.
        prefill seeds the assistant's reply so it continues from your text.
        """
        messages = self._build_messages(query, system, history, prefill)
        plugins = [{"id": "web"}] if web_search else None
        payload = self._payload(
            messages, model, max_tokens, temperature, top_p, seed,
            stop, response_format, plugins, extra,
        )

        body = await self._post(payload)
        resp = self._parse(body)

        logger.info(
            "llm ok model=%s tokens=%d/%d cost=%s",
            resp.model, resp.prompt_tokens, resp.completion_tokens, resp.cost,
        )
        return resp if full else resp.text

    # Short alias.
    get_resp = get_llm_response

    async def get_json_response(
        self,
        query: str,
        *,
        schema: dict | None = None,
        schema_name: str = "response",
        strict: bool = True,
        heal: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Force a JSON response and return the parsed object.

        schema=None uses basic json_object mode. Passing a JSON Schema uses
        strict json_schema mode, which more models honour exactly.
        heal=True adds the response-healing plugin, which repairs malformed JSON
        server-side before it reaches you.
        """
        if schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": strict,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}

        if heal:
            extra = dict(kwargs.pop("extra", None) or {})
            plugins = list(extra.get("plugins") or [])
            plugins.append({"id": "response-healing"})
            extra["plugins"] = plugins
            kwargs["extra"] = extra

        text = await self.get_llm_response(
            query, response_format=response_format, **kwargs
        )
        return self._loads(text)

    @staticmethod
    def _loads(text: str) -> Any:
        """Parse JSON, stripping ``` fences models add unprompted."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = _JSON_FENCE.sub("", text).strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                raise LLMError(f"model did not return valid JSON: {text[:300]}") from e

    async def classify(
        self,
        text: str,
        criteria: str,
        *,
        scale: int = 10,
        **kwargs: Any,
    ) -> dict:
        """Score one item against criteria. Returns {"score": int, "reason": str}.

        Built for the relevance pass: cheap model, tight schema, no prose.
        """
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": scale},
                "reason": {"type": "string"},
            },
            "required": ["score", "reason"],
            "additionalProperties": False,
        }
        kwargs.setdefault("temperature", 0)
        kwargs.setdefault("max_tokens", 300)
        kwargs.setdefault(
            "system",
            f"Score how well the text matches the criteria, 0 to {scale}. "
            "Reply only with JSON. Keep `reason` under 20 words.",
        )
        return await self.get_json_response(
            f"CRITERIA:\n{criteria}\n\nTEXT:\n{text}",
            schema=schema,
            schema_name="classification",
            **kwargs,
        )

    async def summarize(
        self,
        text: str,
        *,
        instructions: str | None = None,
        max_words: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Summarize text. `instructions` shapes the output format."""
        system = instructions or "Summarize the text. No preamble, no meta-commentary."
        if max_words:
            system += f" Keep it under {max_words} words."
        kwargs.setdefault("temperature", 0.3)
        return await self.get_llm_response(text, system=system, **kwargs)

    async def batch(
        self,
        queries: Iterable[str],
        *,
        concurrency: int = 4,
        return_exceptions: bool = True,
        **kwargs: Any,
    ) -> list:
        """Run many prompts concurrently under a local semaphore.

        Note this is a second semaphore, nested inside the Executor's. Keep
        `concurrency` low so N topics x M queries doesn't blow past rate limits.
        """
        sem = asyncio.Semaphore(concurrency)

        async def one(q: str):
            async with sem:
                return await self.get_llm_response(q, **kwargs)

        return await asyncio.gather(
            *(one(q) for q in queries), return_exceptions=return_exceptions
        )

    # ------------------------------------------------------ sync convenience

    def get_llm_response_sync(self, query: str, **kwargs: Any):
        """Blocking wrapper. REPL and scripts only — never inside the Executor."""
        return asyncio.run(self._run_and_close(self.get_llm_response(query, **kwargs)))

    def get_json_response_sync(self, query: str, **kwargs: Any):
        return asyncio.run(self._run_and_close(self.get_json_response(query, **kwargs)))

    async def _run_and_close(self, coro):
        try:
            return await coro
        finally:
            await self.aclose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    llm = UseLLM()
    print(llm.get_llm_response_sync(
        query="search over internet and return about BRICS summit 2026.",
        max_tokens=2000,
        extra={
        "reasoning": {
            "effort": "low",
        }
        },
        web_search=True
    ))
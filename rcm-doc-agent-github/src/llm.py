"""LLM client abstraction.

A thin `LLMClient.complete(system, messages, on_token) -> str` interface backed
by LiteLLM: ONE provider-agnostic backend. LiteLLM normalizes ~100 providers
behind a single call, so the provider is chosen by the *model string*
(`groq/llama-3.3-70b-versatile`, `gemini/gemini-2.5-flash`,
`anthropic/claude-sonnet-4-5`, `ollama/llama3.1`, ...) plus an optional
`API_BASE` for any OpenAI-compatible endpoint. Swapping or adding a provider
is a config change, never a code change.

Robustness: bounded retries with exponential backoff on transient errors, a
per-call timeout, and a typed error so the loop can fall back gracefully. We
set `num_retries=0` on LiteLLM so retry policy lives in one place — here.
"""

from __future__ import annotations

import logging
import time

from .config import Settings

log = logging.getLogger("agent.llm")


class LLMError(RuntimeError):
    """Non-recoverable LLM failure surfaced to the loop for graceful fallback."""


class LLMTransientError(LLMError):
    """Recoverable (rate limit / 5xx / timeout); eligible for retry."""


# --------------------------------------------------------------------------- #
# Public client
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(self, settings: Settings):
        """Build the LiteLLM-backed client from settings (model, keys, timeouts)."""
        self.s = settings
        self._backend = _LiteLLMBackend(settings)
        self.last_usage: dict[str, int] | None = None

    def complete(self, system: str, messages: list[dict], on_token=None) -> str:
        """Return raw model text, with retries/backoff on transient errors.

        If `on_token` is provided, tokens stream to it as they arrive while
        still returning the full accumulated string for structured parsing.

        After this returns, `self.last_usage` holds the real token counts
        (`prompt_tokens`/`completion_tokens`/`total_tokens`) for THIS call, as
        reported by the provider -- not an estimate. `None` if the provider
        didn't return usage (rare, but some streaming configs omit it).
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.s.max_retries + 1):
            try:
                text = self._backend.complete(system, messages, on_token=on_token)
                self.last_usage = self._backend.last_usage
                return text
            except LLMTransientError as exc:
                last_exc = exc
                delay = self.s.retry_base_delay * (2 ** (attempt - 1))
                log.warning("LLM transient error (attempt %d/%d): %s; retrying in %.1fs",
                            attempt, self.s.max_retries, exc, delay)
                time.sleep(delay)
            except LLMError:
                raise  # non-transient: don't burn retries
        raise LLMError(f"exhausted {self.s.max_retries} retries: {last_exc}")


# --------------------------------------------------------------------------- #
# Unified backend: LiteLLM (provider-agnostic)
# --------------------------------------------------------------------------- #
class _LiteLLMBackend:
    """One backend for every provider LiteLLM supports.

    The model string carries the provider, e.g.:
        anthropic/claude-sonnet-4-5
        openai/gpt-4.1-mini
        gemini/gemini-2.5-flash
        groq/llama-3.3-70b-versatile
        deepseek/deepseek-chat
        openrouter/meta-llama/llama-3.3-70b-instruct
        ollama/llama3.1                      (local)

    Any OpenAI-compatible endpoint also works by setting `api_base`, so adding a
    provider is a config change, not a code change.
    """

    def __init__(self, settings: Settings):
        """Import litellm lazily and lock down its own retry behavior in favor
        of ours."""
        try:
            import litellm  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise LLMError("`litellm` package not installed") from exc
        self.s = settings
        self._litellm = litellm
        self.last_usage: dict[str, int] | None = None
        # Don't let LiteLLM retry behind our back; we own the retry policy.
        litellm.drop_params = True          # ignore params a provider doesn't support
        litellm.suppress_debug_info = True

    def complete(self, system: str, messages: list[dict], on_token=None) -> str:
        """One completion call; maps provider exceptions to `LLMTransientError`
        (retryable) vs `LLMError` (fatal) so `LLMClient` can decide whether to
        back off and retry or surface the failure immediately."""
        litellm = self._litellm
        from litellm.exceptions import (  # noqa: PLC0415
            APIConnectionError,
            APIError,
            InternalServerError,
            RateLimitError,
            ServiceUnavailableError,
            Timeout,
        )

        kwargs = {
            "model": self.s.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.s.max_tokens,
            "temperature": self.s.temperature,
            "timeout": self.s.request_timeout,
            "num_retries": 0,  # our LLMClient owns backoff/retries
        }
        if self.s.api_base:
            kwargs["api_base"] = self.s.api_base
        if self.s.api_key:
            kwargs["api_key"] = self.s.api_key

        self.last_usage = None
        try:
            if on_token:
                chunks: list[str] = []
                for part in litellm.completion(stream=True, stream_options={"include_usage": True}, **kwargs):
                    delta = (part.choices[0].delta.content or "") if part.choices else ""
                    if delta:
                        on_token(delta)
                        chunks.append(delta)
                    usage = getattr(part, "usage", None)
                    if usage:  # the final chunk of a stream, when the provider reports it
                        self.last_usage = {"prompt_tokens": usage.prompt_tokens,
                                           "completion_tokens": usage.completion_tokens,
                                           "total_tokens": usage.total_tokens}
                return "".join(chunks)
            resp = litellm.completion(**kwargs)
            usage = getattr(resp, "usage", None)
            if usage:
                self.last_usage = {"prompt_tokens": usage.prompt_tokens,
                                   "completion_tokens": usage.completion_tokens,
                                   "total_tokens": usage.total_tokens}
            return resp.choices[0].message.content or ""
        except (RateLimitError, Timeout, APIConnectionError,
                ServiceUnavailableError, InternalServerError) as exc:
            raise LLMTransientError(str(exc)) from exc
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            if status in (408, 409, 429) or (status and status >= 500):
                raise LLMTransientError(str(exc)) from exc
            raise LLMError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc

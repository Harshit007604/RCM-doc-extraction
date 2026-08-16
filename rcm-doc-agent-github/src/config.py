"""Externalized configuration.

All tunables (model, temperature, timeouts, budgets, keys) come from
environment variables or a `.env` file — nothing is hard-coded in the agent
logic. See `.env.example` for the full list.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables for both agents, loaded from environment / `.env` and
    overridable via CLI flags (see `get_settings`). One class, so every
    surface (CLI, Streamlit UI, eval harness) configures itself identically.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM provider ---
    # LiteLLM routes to ~100 providers; the PROVIDER IS CHOSEN BY `model`,
    # e.g. groq/llama-3.3-70b-versatile, gemini/gemini-2.5-flash,
    # anthropic/claude-sonnet-4-5, deepseek/deepseek-chat, ollama/llama3.1,
    # openai/gpt-4.1. Every call goes to a real provider; there is no offline
    # mode, so a valid API key for the chosen provider is always required.
    model: str = Field(default="gemini/gemini-2.5-flash")
    temperature: float = Field(default=0.0)
    max_tokens: int = Field(default=1500)

    # Generic credentials. Leave `api_key` unset to let LiteLLM read the
    # provider's conventional env var (GROQ_API_KEY, GEMINI_API_KEY,
    # ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). Set `api_base` to point at any
    # OpenAI-compatible endpoint (self-hosted vLLM, Ollama, a gateway, ...).
    api_key: str | None = Field(default=None)
    api_base: str | None = Field(default=None)

    # --- Robustness knobs ---
    request_timeout: float = Field(default=60.0)   # per-call wall clock (s)
    max_retries: int = Field(default=3)            # LLM transient-error retries
    retry_base_delay: float = Field(default=1.0)   # exponential backoff base (s)

    # --- Agent-loop budgets (guard-rails against runaway loops) ---
    max_steps: int = Field(default=8)              # hard cap on loop iterations
    max_code_retries: int = Field(default=2)       # self-correction attempts per exec

    # --- Input guard-rail ---
    # Reject an oversized document BEFORE it's ever sent to the LLM, rather
    # than silently forwarding an arbitrarily large payload. 100,000 chars
    # (~25k tokens at the project's own ~4 chars/token estimate, see
    # `queue/ratelimit.py::estimate_tokens`) is generous relative to this
    # corpus's real documents (a few hundred to ~2,000 chars) -- it's sized
    # to catch a genuinely pathological input (a misrouted binary file, an
    # accidentally-concatenated multi-document dump), not to constrain
    # legitimate payer correspondence.
    max_input_chars: int = Field(default=100_000)

    # --- Sandbox ---
    sandbox_timeout: float = Field(default=10.0)   # subprocess wall clock (s)

    # --- Observability ---
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")

    # --- Memory / conversation persistence ---
    memory_dir: str = Field(default="memory")


def get_settings(**overrides) -> Settings:
    """Build settings, allowing explicit overrides (e.g. from CLI flags)."""
    base = Settings()
    if overrides:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return base.model_copy(update=clean)
    return base

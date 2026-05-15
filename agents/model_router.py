"""
QualityEngine AI — Model Router
Provides LLM instances with automatic fallback chains:
  Groq llama-3.3-70b → Groq mixtral-8x7b → Groq llama3-70b → OpenRouter (free)

Each Groq model has its OWN independent quota — so hitting one limit
automatically falls back to the next model. OpenRouter is the final safety net.
"""
from __future__ import annotations
import os
import logging
from typing import Any

from langchain_groq import ChatGroq
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("qualityengine.router")

# ─── Model configs ─────────────────────────────────────────────────────────────

GROQ_MODELS = [
    "llama-3.3-70b-versatile",           # Best quality — primary
    "qwen/qwen3-32b",                     # Strong 32B fallback
    "meta-llama/llama-4-scout-17b-16e-instruct",  # Llama 4 Scout
    "openai/gpt-oss-120b",               # GPT OSS 120B
    "openai/gpt-oss-20b",                # GPT OSS 20B
    "llama-3.1-8b-instant",              # Fastest — last resort
]


def _get_groq_keys() -> list[str]:
    """Return all available Groq API keys (supports key rotation)."""
    keys = []
    for var in ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"]:
        k = os.environ.get(var, "")
        if k:
            keys.append(k)
    return keys or [""]


def _make_groq(model: str, temperature: float = 0.2, key: str = None, **kwargs) -> ChatGroq:
    return ChatGroq(
        model=model,
        api_key=key or os.environ["GROQ_API_KEY"],
        temperature=temperature,
        **kwargs,
    )


def _make_openrouter(temperature: float = 0.2) -> BaseChatModel | None:
    """OpenRouter free tier — no credit card needed, just email signup."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model="meta-llama/llama-3.3-70b-instruct:free",
            openai_api_key=key,
            openai_api_base="https://openrouter.ai/api/v1",
            temperature=temperature,
            default_headers={
                "HTTP-Referer": "https://github.com/chhhee10/ANVIL",
                "X-Title": "QualityEngine AI",
            },
        )
    except ImportError:
        logger.warning("langchain-openai not installed — OpenRouter unavailable")
        return None


def get_llm(temperature: float = 0.2, structured_output=None) -> BaseChatModel:
    """
    Returns a LLM with automatic fallback chain:
    key1/model1 → key1/model2 → key2/model1 → key2/model2 → ... → OpenRouter
    """
    keys = _get_groq_keys()

    # Build list: primary model with key1, then all models×keys, then openrouter
    all_variants = []
    for key in keys:
        for model in GROQ_MODELS:
            all_variants.append(_make_groq(model, temperature, key=key))

    if not all_variants:
        raise ValueError("No GROQ_API_KEY found in environment")

    primary = all_variants[0]
    fallbacks = all_variants[1:]

    # Add OpenRouter if key is set
    openrouter = _make_openrouter(temperature)
    if openrouter:
        fallbacks.append(openrouter)

    n_keys   = len(keys)
    n_models = len(GROQ_MODELS)
    logger.info("Model router: %d key(s) × %d models = %d variants in fallback chain",
                n_keys, n_models, len(all_variants))

    if structured_output:
        primary_s   = primary.with_structured_output(structured_output)
        fallbacks_s = [f.with_structured_output(structured_output) for f in fallbacks]
        return primary_s.with_fallbacks(fallbacks_s)

    return primary.with_fallbacks(fallbacks)


def get_str_llm(temperature: float = 0.3) -> BaseChatModel:
    """Returns a plain string-output LLM with full fallback chain (for test generator)."""
    return get_llm(temperature=temperature, structured_output=None)

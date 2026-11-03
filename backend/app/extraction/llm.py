"""LLM extraction with strict-JSON parsing, retries and graceful degradation.

Provider-agnostic on purpose: the brief says "an LLM API", and binding the
whole pipeline to one vendor's SDK is the kind of decision that is cheap to
make and expensive to undo. The `LLMClient` protocol below is the only
surface the rest of the system sees.

Failure policy, in order:
  1. Call the model, ask for JSON.
  2. On a malformed response, retry with the parse error fed back in - models
     correct their own JSON reliably when shown the error.
  3. On repeated failure or no API key, fall back to the deterministic
     extractor rather than failing the document.

Every path records which method was used, so the evaluation report can break
accuracy down by extraction method instead of averaging them into a
meaningless single number.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

from pydantic import ValidationError

from ..config import settings
from . import fallback
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .schema import InvoiceExtraction, json_schema

log = logging.getLogger(__name__)

# Characters of document text sent to the model. Roughly 15k tokens, which
# fits every mainstream context window with room for the schema and reply.
MAX_PROMPT_CHARS = 60_000


class LLMUnavailable(Exception):
    pass


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> tuple[str, dict]:
        """Return (raw_text, usage_metadata)."""
        ...


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("openai package not installed") from exc
        self._client = OpenAI(api_key=api_key, timeout=settings.llm_timeout_s)
        self._model = model

    def complete(self, system: str, user: str) -> tuple[str, dict]:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Forces syntactically valid JSON, which removes the most common
            # class of parse failure entirely.
            response_format={"type": "json_object"},
            temperature=0,
        )
        usage = response.usage
        return response.choices[0].message.content or "", {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        }


class AnthropicClient:
    def __init__(self, api_key: str, model: str) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("anthropic package not installed") from exc
        self._client = anthropic.Anthropic(api_key=api_key, timeout=settings.llm_timeout_s)
        self._model = model

    def complete(self, system: str, user: str) -> tuple[str, dict]:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
        }


def build_client() -> LLMClient | None:
    """None means "run without an LLM" - a supported mode, not an error."""
    if settings.llm_provider == "none" or not settings.llm_api_key:
        return None
    try:
        if settings.llm_provider == "openai":
            return OpenAIClient(settings.llm_api_key, settings.llm_model)
        if settings.llm_provider == "anthropic":
            return AnthropicClient(settings.llm_api_key, settings.llm_model)
    except LLMUnavailable as exc:
        log.warning("LLM client unavailable, falling back: %s", exc)
    return None


def _strip_fences(text: str) -> str:
    """Remove markdown fences some models add despite instructions."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first complete JSON object in the text.

    Brace-counting rather than a greedy regex, so a nested object in
    line_items does not truncate the match.
    """
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model response")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : index + 1])
    raise ValueError("unterminated JSON object in model response")


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_PROMPT_CHARS:
        return text, False
    # Keep the head and the tail: invoice identity is at the top, totals at
    # the bottom. Cutting only the tail loses exactly the fields that matter.
    head = text[: int(MAX_PROMPT_CHARS * 0.6)]
    tail = text[-int(MAX_PROMPT_CHARS * 0.4) :]
    return f"{head}\n\n[... middle of document truncated ...]\n\n{tail}", True


def extract_invoice(
    *,
    text: str,
    filename: str,
    source_kind: str,
    ocr_confidence: float | None = None,
    client: LLMClient | None = None,
) -> tuple[InvoiceExtraction, dict]:
    """Extract one invoice. Always returns a result; never raises on LLM error.

    The returned metadata records method, attempts, timing and token usage so
    the evaluation report can be built from real numbers.
    """
    meta: dict[str, Any] = {
        "method": "llm",
        "prompt_version": PROMPT_VERSION,
        "attempts": 0,
        "errors": [],
        "truncated": False,
    }

    if client is None:
        client = build_client()

    if client is None:
        started = time.perf_counter()
        result = fallback.extract(text)
        meta.update(
            method="rules",
            duration_s=round(time.perf_counter() - started, 3),
            reason="no LLM client configured",
        )
        return result, meta

    prompt_text, truncated = _truncate(text)
    meta["truncated"] = truncated
    schema_json = json.dumps(json_schema(), indent=2)
    user_prompt = build_user_prompt(
        filename=filename,
        source_kind=source_kind,
        text=prompt_text,
        schema=schema_json,
        ocr_confidence=ocr_confidence,
    )

    started = time.perf_counter()
    last_error: str | None = None

    for attempt in range(1, settings.llm_max_retries + 1):
        meta["attempts"] = attempt
        prompt = user_prompt
        if last_error:
            # Feeding the error back is far more effective than a blind retry.
            prompt = (
                f"{user_prompt}\n\nYour previous response could not be parsed.\n"
                f"Error: {last_error}\nReturn corrected JSON only."
            )
        try:
            raw, usage = client.complete(SYSTEM_PROMPT, prompt)
            payload = _extract_json_object(raw)
            result = InvoiceExtraction.model_validate(payload)
            meta.update(
                duration_s=round(time.perf_counter() - started, 3),
                usage=usage,
                raw_response=payload,
            )
            return result, meta
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            meta["errors"].append(last_error)
            log.warning("LLM JSON parse failed (attempt %s): %s", attempt, exc)
        except ValidationError as exc:
            last_error = f"schema validation failed: {exc.errors()[:3]}"
            meta["errors"].append(last_error)
            log.warning("LLM schema validation failed (attempt %s)", attempt)
        except Exception as exc:
            # Network, rate limit, auth. Back off, then retry.
            last_error = f"{type(exc).__name__}: {exc}"
            meta["errors"].append(last_error)
            log.warning("LLM call failed (attempt %s): %s", attempt, exc)
            time.sleep(min(2 ** attempt, 8))

    # Every attempt failed - degrade rather than drop the document.
    log.error("LLM extraction failed for %s after %s attempts; using rules",
              filename, settings.llm_max_retries)
    result = fallback.extract(text)
    meta.update(
        method="rules_after_llm_failure",
        duration_s=round(time.perf_counter() - started, 3),
        reason=last_error,
    )
    return result, meta

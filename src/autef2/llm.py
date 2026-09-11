"""Thin OpenAI wrapper with per-call token and cost accounting.

Cost per fix is one of the reported metrics, so accounting cannot be an
afterthought: every call made on behalf of a repair attempt is attributed back
to that attempt. ``LLMClient.scoped()`` gives a child recorder that shares the
transport but tallies separately.

v1 pulled in ``autogen`` to do this and then bypassed it anyway, calling
``generate_oai_reply`` directly with a hand-built message list. We talk to the
OpenAI SDK straight, which removes a dependency and makes usage observable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import MODEL_PRICING, AutefConfig

logger = logging.getLogger(__name__)

Messages = List[Dict[str, str]]


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or refuses to answer usefully."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(self, prompt: int, completion: int, cost: float) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cost_usd += cost
        self.calls += 1

    def merge(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd
        self.calls += other.calls

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 6),
        }


class LLMClient:
    """Chat-completions client that records what every call cost."""

    def __init__(self, config: AutefConfig, parent: Optional["LLMClient"] = None):
        self.config = config
        self.usage = Usage()
        self._parent = parent
        self._client = parent._client if parent is not None else None
        if parent is None:
            self._client = self._build_client()

    # -- transport --------------------------------------------------------

    def _build_client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise LLMError(
                "The 'openai' package is not installed. Run: pip install openai"
            ) from exc

        if not self.config.api_key:
            raise LLMError(
                "No OpenAI API key found. Set OPENAI_API_KEY, or put a real key "
                "in OAI_CONFIG_LIST.json (the shipped file contains only the "
                "placeholder 'ENV_OPENAI_API_KEY')."
            )
        kwargs: Dict[str, Any] = {"api_key": self.config.api_key}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        return OpenAI(**kwargs)

    def scoped(self) -> "LLMClient":
        """A child client sharing the connection but tallying separately."""
        return LLMClient(self.config, parent=self)

    # -- calls ------------------------------------------------------------

    def complete(
        self,
        messages: Messages,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        retries: int = 3,
    ) -> str:
        """One chat completion. Returns the message text."""
        temperature = (
            self.config.temperature if temperature is None else temperature
        )
        max_tokens = max_tokens or self.config.max_output_tokens

        model = self.config.model
        last_error: Optional[Exception] = None
        attempt = 0
        while attempt < retries:
            kwargs = self._request_kwargs(
                messages, temperature, max_tokens, json_mode
            )
            try:
                response = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                last_error = exc
                if _learn_quirk(model, exc):
                    # The model rejected a parameter rather than failing the
                    # work. Rebuild without it; this does not use up a retry.
                    logger.info(
                        "Adapting request shape for %s: %s", model, exc
                    )
                    continue
                if _is_ssl_error(exc):
                    raise LLMError(
                        "TLS handshake failed talking to the OpenAI API. On a "
                        "machine running TLS-intercepting antivirus this is "
                        "expected: disable HTTPS scanning, or point "
                        "SSL_CERT_FILE / REQUESTS_CA_BUNDLE at the "
                        f"interceptor's root certificate. Original error: {exc}"
                    ) from exc
                attempt += 1
                if attempt >= retries:
                    break
                delay = 2 ** attempt
                logger.warning(
                    "LLM call failed (%s), retrying in %ss", exc, delay
                )
                time.sleep(delay)
                continue

            self._record(response)
            choice = response.choices[0]
            return (choice.message.content or "").strip()

        raise LLMError(f"LLM call failed after {retries} attempts: {last_error}")

    def complete_json(
        self,
        messages: Messages,
        *,
        required_keys: Tuple[str, ...] = (),
        retries: int = 2,
    ) -> Dict[str, Any]:
        """A completion constrained to JSON, parsed and key-checked.

        Falls back to extracting the first JSON object from the text when the
        model wraps it in prose despite json_mode.
        """
        for attempt in range(retries + 1):
            raw = self.complete(messages, json_mode=True)
            data = _parse_json(raw)
            if data is not None and all(k in data for k in required_keys):
                return data
            if attempt == retries:
                break
            messages = list(messages) + [
                {"role": "assistant", "content": raw[:2000]},
                {
                    "role": "user",
                    "content": (
                        "That was not valid JSON with the required keys "
                        f"{list(required_keys)}. Reply with a single JSON "
                        "object and nothing else."
                    ),
                },
            ]
        raise LLMError(
            f"Model did not return JSON with keys {list(required_keys)}"
        )

    def _request_kwargs(
        self,
        messages: Messages,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> Dict[str, Any]:
        """Build the request, honouring whatever this model has rejected.

        Reasoning models (the GPT-5 family) refuse ``max_tokens`` and refuse
        any temperature but the default. Rather than keep a list of model
        names that will go stale, we send the ordinary shape once and let
        ``_learn_quirk`` record what came back.
        """
        quirks = _QUIRKS.setdefault(self.config.model, set())
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if "no_temperature" not in quirks:
            kwargs["temperature"] = temperature
        if "max_completion_tokens" in quirks:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        effort = self.config.reasoning_effort
        if effort and "no_reasoning_effort" not in quirks:
            kwargs["reasoning_effort"] = effort
        if json_mode and "no_response_format" not in quirks:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    # -- accounting -------------------------------------------------------

    def _record(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        pricing = MODEL_PRICING.get(self.config.model)
        if pricing is None:
            pricing = MODEL_PRICING["gpt-4o-mini"]
            logger.debug(
                "No pricing for model %s; costing at gpt-4o-mini rates",
                self.config.model,
            )
        cost = prompt * pricing["input"] + completion * pricing["output"]

        self.usage.add(prompt, completion, cost)
        # Roll up to the root so a run total is always available.
        node = self._parent
        while node is not None:
            node.usage.add(prompt, completion, cost)
            node = node._parent


#: Request-shape adaptations discovered at runtime, keyed by model name.
#: Learned once per process, so only the first call to a new model pays the
#: round trip.
_QUIRKS: Dict[str, set] = {}


def _learn_quirk(model: str, exc: Exception) -> bool:
    """Record a parameter rejection. True if this is worth retrying.

    Only a rejection we know how to answer counts. An unrecognised 400 falls
    through to the ordinary retry path so a real failure is not retried
    forever.
    """
    message = str(exc).lower()
    if "unsupported" not in message and "not supported" not in message:
        return False
    quirks = _QUIRKS.setdefault(model, set())
    learned = False
    for needle, quirk in (
        ("max_tokens", "max_completion_tokens"),
        ("temperature", "no_temperature"),
        ("reasoning_effort", "no_reasoning_effort"),
        ("response_format", "no_response_format"),
    ):
        if needle in message and quirk not in quirks:
            quirks.add(quirk)
            learned = True
    return learned


class StubLLMClient(LLMClient):
    """Scripted client for tests and dry runs. Makes no network calls.

    Either give it ``responses`` (consumed in order, last one repeating) or a
    ``responder`` callable that inspects the messages and decides what to say.
    The responder form is the useful one for testing the repair loop, where
    what should come back depends on which agent is asking.
    """

    def __init__(
        self,
        config: AutefConfig,
        responses: Optional[List[str]] = None,
        responder: Optional[Callable[[Messages], str]] = None,
        _shared: Optional["StubLLMClient"] = None,
    ):
        self.config = config
        self.usage = Usage()
        self._parent = _shared
        self._client = None
        self._responder = responder
        self._shared = _shared or self
        self._shared._responses = getattr(
            self._shared, "_responses", list(responses or [])
        )
        if _shared is None:
            self._responses = list(responses or [])
            self._index = 0
        self.calls: List[Messages] = []

    def _build_client(self):  # pragma: no cover - never reached
        return None

    def scoped(self) -> "StubLLMClient":
        return StubLLMClient(
            self.config, responder=self._responder, _shared=self._shared
        )

    def complete(self, messages: Messages, **kwargs) -> str:  # type: ignore[override]
        self._shared.calls.append(messages)

        if self._responder is not None:
            text = self._responder(messages)
        else:
            responses = self._shared._responses
            if not responses:
                text = "{}"
            else:
                index = min(self._shared._index, len(responses) - 1)
                self._shared._index += 1
                text = responses[index]

        prompt_tokens = len(str(messages)) // 4
        completion_tokens = len(text) // 4
        self.usage.add(prompt_tokens, completion_tokens, 0.0)
        node = self._parent
        while node is not None:
            node.usage.add(prompt_tokens, completion_tokens, 0.0)
            node = node._parent
        return text


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _is_ssl_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in ("ssl", "certificate verify failed", "cert_verify")
    )

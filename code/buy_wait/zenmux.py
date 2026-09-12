"""Minimal, dependency-free ZenMux OpenAI-compatible client.

The client is deliberately optional.  It is used only for evidence extraction
on deterministic misses, never for affordability or payment decisions.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .usage import UsageRecord, UsageTracker


DEFAULT_MODEL = "meta/muse-spark-1.3-contributor"
DEFAULT_BASE_URL = "https://zenmux.ai/api/v1"
DEFAULT_INPUT_USD_PER_MILLION = Decimal("0.10")
DEFAULT_OUTPUT_USD_PER_MILLION = Decimal("0.20")


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def load_env_value(name: str, *, dotenv_path: Path | None = None, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    root = Path(__file__).resolve().parents[2]
    return _dotenv_values(dotenv_path or root / ".env").get(name, default)


def _decimal_env(name: str, default: Decimal, *, dotenv_path: Path | None = None) -> Decimal:
    raw = load_env_value(name, dotenv_path=dotenv_path, default=str(default))
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a non-negative decimal") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a non-negative decimal")
    return value


@dataclass(frozen=True)
class ZenMuxSettings:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    max_model_calls: int = 16
    input_usd_per_million: Decimal = DEFAULT_INPUT_USD_PER_MILLION
    output_usd_per_million: Decimal = DEFAULT_OUTPUT_USD_PER_MILLION

    @classmethod
    def from_env(cls, *, dotenv_path: Path | None = None, max_model_calls: int | None = None) -> "ZenMuxSettings":
        raw_limit = max_model_calls
        if raw_limit is None:
            raw_limit = int(load_env_value("ZENMUX_MAX_MODEL_CALLS", dotenv_path=dotenv_path, default="16"))
        if raw_limit < 0:
            raise ValueError("max_model_calls must be non-negative")
        return cls(
            api_key=load_env_value("ZENMUX_API_KEY", dotenv_path=dotenv_path),
            model=load_env_value("ZENMUX_MODEL", dotenv_path=dotenv_path, default=DEFAULT_MODEL),
            base_url=load_env_value("ZENMUX_BASE_URL", dotenv_path=dotenv_path, default=DEFAULT_BASE_URL).rstrip("/"),
            max_model_calls=raw_limit,
            input_usd_per_million=_decimal_env("ZENMUX_INPUT_USD_PER_MILLION", DEFAULT_INPUT_USD_PER_MILLION, dotenv_path=dotenv_path),
            output_usd_per_million=_decimal_env("ZENMUX_OUTPUT_USD_PER_MILLION", DEFAULT_OUTPUT_USD_PER_MILLION, dotenv_path=dotenv_path),
        )


def _json_from_text(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min((index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0), default=-1)
        if start < 0:
            raise ValueError("model response did not contain JSON")
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end <= start:
            raise ValueError("model response contained incomplete JSON")
        return json.loads(cleaned[start:end + 1])


class ZenMuxClient:
    def __init__(self, settings: ZenMuxSettings, *, tracker: UsageTracker | None = None, timeout_seconds: int = 90):
        if not settings.api_key:
            raise ValueError("ZENMUX_API_KEY is not configured")
        self.settings = settings
        self.tracker = tracker or UsageTracker()
        self.timeout_seconds = timeout_seconds
        self.calls_made = 0

    @property
    def remaining_calls(self) -> int:
        return max(0, self.settings.max_model_calls - self.calls_made)

    def _complete(self, *, messages: list[dict[str, Any]], purpose: str, source_id: str, max_tokens: int = 350) -> Any:
        if self.remaining_calls <= 0:
            raise RuntimeError("ZenMux model-call budget exhausted")
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "reasoning_effort": "minimal",
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.settings.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        self.calls_made += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ZenMux request failed: {type(exc).__name__}") from exc
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        cost = (
            Decimal(input_tokens) * self.settings.input_usd_per_million
            + Decimal(output_tokens) * self.settings.output_usd_per_million
        ) / Decimal(1_000_000)
        self.tracker.record(UsageRecord(
            provider="zenmux.ai", model=self.settings.model, purpose=purpose,
            source_id=source_id, cache_hit=False, input_tokens=input_tokens,
            output_tokens=output_tokens, estimated_cost=cost,
        ))
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("ZenMux response did not contain a choice")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return _json_from_text(str(content))

    def extract_message(self, row: dict[str, str]) -> list[dict[str, Any]]:
        text = row.get("message_text", "")
        system = Path(__file__).resolve().parents[1] / "prompts" / "message_extraction.txt"
        prompt = system.read_text(encoding="utf-8")
        result = self._complete(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"message_id": row.get("message_id"), "related_event_id": row.get("related_event_id") or None, "message_text": text}, ensure_ascii=False)},
            ],
            purpose="message_evidence", source_id=row.get("message_id", ""),
        )
        if isinstance(result, dict):
            result = result.get("claims", result.get("facts", [result]))
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def extract_image(self, path: Path, row: dict[str, str]) -> dict[str, Any]:
        system = Path(__file__).resolve().parents[1] / "prompts" / "image_extraction.txt"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        result = self._complete(
            messages=[
                {"role": "system", "content": system.read_text(encoding="utf-8")},
                {"role": "user", "content": [
                    {"type": "text", "text": json.dumps({"image_id": row.get("image_id"), "related_event_id": row.get("related_event_id") or None}, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ]},
            ],
            purpose="image_evidence", source_id=row.get("image_id", ""),
            max_tokens=800,
        )
        if not isinstance(result, dict):
            raise ValueError("image extraction response was not an object")
        return result


__all__ = ["DEFAULT_MODEL", "ZenMuxClient", "ZenMuxSettings", "load_env_value"]

"""Phase 1 evidence scoping, deterministic extraction, caching, and validation.

This module intentionally produces facts, not financial decisions. Messages and
images are treated as untrusted evidence, and unresolved image amounts remain
explicitly unresolved so later ledger code can refuse unsafe calculations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import (
    CACHE_DIR,
    DATASET_DIR,
    EVIDENCE_OUTPUT,
    PROMPT_VERSION,
    SUPPORTED_CURRENCIES,
)
from .usage import UsageRecord, UsageTracker


CLAIM_TYPES = frozenset({
    "amount", "date", "confirm", "cancel", "amend", "delay", "status",
    "recurrence", "ignore",
})
STATUSES = frozenset({
    "confirmed", "pending", "failed", "cancelled", "scheduled", "settled",
    "unrealized", "not_credited", "ended", "unknown",
})
IMAGE_STATUSES = STATUSES | frozenset({"extracted", "unresolved", "paid", "unpaid", "received", "overdue", "due"})
PROMPT_OVERRIDE_RE = re.compile(
    r"(?:ignore|disregard|override|bypass|forget)\s+(?:the\s+)?(?:rules?|instructions?|policy|schema)|"
    r"(?:decide|approve|recommend|pay|transfer)\s+(?:this|the|it)",
    re.IGNORECASE,
)
EVENT_ID_RE = re.compile(r"\bevent_[A-Za-z0-9_-]+\b")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
MONTH_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"\b(?P<currency>EUR|IDR|INR|USD|ZAR)\s*"
    r"(?P<amount>[0-9][0-9,]*(?:\.[0-9]+)?)\b",
    re.IGNORECASE,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_hash(kind: str, row: dict[str, str], image_path: Path | None = None) -> str:
    if kind == "image" and image_path is not None and image_path.is_file():
        return sha256_bytes(image_path.read_bytes())
    return sha256_bytes(_canonical_json({"kind": kind, **row}))


def cache_key(kind: str, source_id: str, content_hash: str, prompt_version: str = PROMPT_VERSION) -> str:
    return f"{kind}:{source_id}:{content_hash}:{prompt_version}"


class EvidenceCache:
    """Small JSON cache with atomic replacement and no secret material."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                    self.entries = loaded["entries"]
            except (OSError, json.JSONDecodeError):
                # A corrupt cache is a miss, never a reason to trust bad facts.
                self.entries = {}

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.entries.get(key)
        return value if isinstance(value, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.entries[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cache_version": PROMPT_VERSION, "entries": self.entries}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def _parse_amount(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    return format(amount, "f")


def _parse_date(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        return None


def _date_from_text(text: str) -> str | None:
    match = DATE_RE.search(text)
    if match:
        return _parse_date(match.group(1))
    match = MONTH_DATE_RE.search(text)
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}", "%d %B %Y"
            ).date().isoformat()
        except ValueError:
            return None
    return None


def _amount_from_text(text: str) -> tuple[str | None, str | None]:
    match = AMOUNT_RE.search(text)
    if not match:
        return None, None
    currency = match.group("currency").upper()
    amount = _parse_amount(match.group("amount"))
    return amount, currency


def _base_fact(source_id: str, claim_type: str, target_event_id: str | None) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "claim_type": claim_type,
        "target_event_id": target_event_id,
        "amount": None,
        "currency": None,
        "effective_date": None,
        "status": None,
        "recurrence": None,
        "confidence": 0.0,
    }


def deterministic_message_facts(row: dict[str, str]) -> list[dict[str, Any]]:
    """Extract conservative, template-aware claims before any model call."""
    text = row.get("message_text", "")
    lower = text.casefold()
    source_id = row.get("message_id", "")
    target = row.get("related_event_id") or None
    amount, currency = _amount_from_text(text)
    effective_date = _date_from_text(text)
    facts: list[dict[str, Any]] = []

    def add(claim_type: str, *, status: str | None = None, confidence: float = 0.88) -> None:
        fact = _base_fact(source_id, claim_type, target)
        fact["amount"] = amount
        fact["currency"] = currency
        fact["effective_date"] = effective_date
        fact["status"] = status
        fact["confidence"] = confidence
        facts.append(fact)

    # These patterns describe financial state; they never select a payment.
    if any(term in lower for term in ("refund has been initiated", "refund ... not", "refund ... belum", "belum masuk ke rekening", "not reached your account")):
        add("status", status="not_credited")
    elif "refund" in lower and any(term in lower for term in ("processing", "pending", "belum")):
        add("status", status="pending")
    if any(term in lower for term in ("not credited", "has not been credited", "belum dikreditkan", "belum masuk")):
        add("status", status="not_credited")
    if any(term in lower for term in ("previous debit attempt failed", "debit attempt failed", "gagal")):
        add("status", status="failed")
    if any(term in lower for term in ("no units have been sold", "no cash proceeds", "belum dijual", "tidak ada transaksi tunai")):
        add("status", status="unrealized")
    if any(term in lower for term in ("still pending", "awaiting approval", "not been approved", "masih tertunda", "belum disetujui", "masih menunggu")):
        add("status", status="pending")
    if any(term in lower for term in ("confirmed", "approved", "dikonfirmasi", "disetujui", "sudah dikonfirmasi")):
        add("confirm", status="confirmed")
    if any(term in lower for term in ("employment has ended", "contract has ended", "income ... ended", "telah berakhir", "sudah berakhir")):
        add("cancel", status="ended")
    if any(term in lower for term in ("revised date", "replaces the payroll date", "new amount applies", "increases monthly rent", "meningkatkan biaya sewa", "jumlah baru berlaku")):
        add("amend")
    if any(term in lower for term in ("expected on", "confirmed credit date", "scheduled for", "dijadwalkan", "tanggal kredit")):
        add("date")
    if any(term in lower for term in ("regular salary", "monthly salary", "gaji bulanan", "gaji rutin", "recurring", "rutin")):
        add("recurrence", confidence=0.82)
    if amount is not None and not facts:
        add("amount", confidence=0.74)
    if not facts:
        add("ignore", status="unknown", confidence=0.45)
    return facts


def unresolved_image_fact(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "status": "unresolved",
        "amount": None,
        "currency": None,
        "date": None,
        "document_type": None,
        "confidence": 0.0,
        "reason": "no_cached_ocr_or_vision_extractor",
    }


def _validation_error(source_id: str, field: str, reason: str, value: Any = None) -> dict[str, Any]:
    return {"source_id": source_id, "field": field, "reason": reason, "value": value}


def validate_message_facts(
    facts: Iterable[dict[str, Any]],
    *,
    event_ids: set[str],
    prompt_override: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fact in facts:
        source_id = str(fact.get("source_id", ""))
        claim_type = fact.get("claim_type")
        if claim_type not in CLAIM_TYPES:
            errors.append(_validation_error(source_id, "claim_type", "unsupported_action", claim_type))
            continue
        target = fact.get("target_event_id")
        if target and target not in event_ids:
            errors.append(_validation_error(source_id, "target_event_id", "unknown_event_id", target))
            continue
        currency = fact.get("currency")
        if currency is not None and str(currency).upper() not in SUPPORTED_CURRENCIES:
            errors.append(_validation_error(source_id, "currency", "unsupported_currency", currency))
            continue
        amount = fact.get("amount")
        if amount is not None and _parse_amount(amount) is None:
            errors.append(_validation_error(source_id, "amount", "malformed_or_negative_amount", amount))
            continue
        effective_date = fact.get("effective_date")
        if effective_date is not None and _parse_date(effective_date) is None:
            errors.append(_validation_error(source_id, "effective_date", "malformed_date", effective_date))
            continue
        status = fact.get("status")
        if status is not None and status not in STATUSES:
            errors.append(_validation_error(source_id, "status", "unsupported_status", status))
            continue
        confidence = fact.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(_validation_error(source_id, "confidence", "invalid_confidence", confidence))
            continue
        if prompt_override:
            errors.append(_validation_error(source_id, "message_text", "embedded_rule_override_ignored"))
            continue
        clean = _base_fact(source_id, claim_type, target)
        clean.update({
            "amount": _parse_amount(amount),
            "currency": str(currency).upper() if currency is not None else None,
            "effective_date": _parse_date(effective_date),
            "status": status,
            "recurrence": fact.get("recurrence"),
            "confidence": float(confidence),
        })
        valid.append(clean)
    return valid, errors


def validate_image_fact(
    fact: dict[str, Any], *, source_id: str, related_event_id: str | None, event_ids: set[str]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if related_event_id and related_event_id not in event_ids:
        return None, [_validation_error(source_id, "related_event_id", "unknown_event_id", related_event_id)]
    if fact.get("status") == "unresolved":
        return {**unresolved_image_fact(source_id), "related_event_id": related_event_id}, []
    errors: list[dict[str, Any]] = []
    amount = _parse_amount(fact.get("amount"))
    if fact.get("amount") is not None and amount is None:
        errors.append(_validation_error(source_id, "amount", "malformed_or_negative_amount", fact.get("amount")))
    currency = fact.get("currency")
    if currency is not None and str(currency).upper() not in SUPPORTED_CURRENCIES:
        errors.append(_validation_error(source_id, "currency", "unsupported_currency", currency))
    extracted_date = _parse_date(fact.get("date"))
    if fact.get("date") is not None and extracted_date is None:
        errors.append(_validation_error(source_id, "date", "malformed_date", fact.get("date")))
    status = fact.get("status")
    if status is not None and status not in IMAGE_STATUSES:
        errors.append(_validation_error(source_id, "status", "unsupported_status", status))
    confidence = fact.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append(_validation_error(source_id, "confidence", "invalid_confidence", confidence))
    if errors:
        return None, errors
    return {
        "source_id": source_id,
        "related_event_id": related_event_id,
        "status": status or "extracted",
        "amount": amount,
        "currency": str(currency).upper() if currency is not None else None,
        "date": extracted_date,
        "document_type": fact.get("document_type"),
        "confidence": float(confidence),
    }, []


def scope_evidence(
    request: dict[str, str],
    *,
    messages: list[dict[str, str]],
    images: list[dict[str, str]],
    events: list[dict[str, str]],
) -> dict[str, Any]:
    """Return only user/request evidence and the complete linked event lifecycle."""
    user_id = request.get("user_id", "")
    request_id = request.get("request_id", "")
    scoped_messages = [
        row for row in messages
        if row.get("user_id") == user_id and (not row.get("request_id") or row.get("request_id") == request_id)
    ]
    scoped_images = [
        row for row in images
        if row.get("user_id") == user_id and row.get("request_id") == request_id
    ]
    event_by_id = {row.get("event_id"): row for row in events}
    target_ids = {
        row.get("related_event_id") for row in scoped_messages + scoped_images if row.get("related_event_id")
    }
    changed = True
    while changed:
        changed = False
        for event in events:
            event_id = event.get("event_id")
            linked = event.get("linked_event_id")
            if event_id in target_ids or linked in target_ids:
                for value in (event_id, linked):
                    if value and value not in target_ids and value in event_by_id:
                        target_ids.add(value)
                        changed = True
    scoped_events = [row for row in events if row.get("event_id") in target_ids]
    return {
        "user_id": user_id,
        "request_id": request_id,
        "message_ids": [row.get("message_id") for row in scoped_messages],
        "image_ids": [row.get("image_id") for row in scoped_images],
        "event_ids": [row.get("event_id") for row in scoped_events],
        "messages": scoped_messages,
        "images": scoped_images,
        "events": scoped_events,
    }


class EvidencePipeline:
    def __init__(
        self,
        dataset_dir: Path = DATASET_DIR,
        cache_path: Path | None = None,
        *,
        cache_only: bool = False,
        message_extractor: Callable[[dict[str, str]], list[dict[str, Any]]] | None = None,
        image_extractor: Callable[[Path, dict[str, str]], dict[str, Any] | None] | None = None,
        usage_tracker: UsageTracker | None = None,
        extractor_model: str = "deterministic",
    ):
        self.dataset_dir = Path(dataset_dir).resolve()
        self.cache = EvidenceCache(cache_path or CACHE_DIR / "evidence_cache.json")
        self.cache_only = cache_only
        self.message_extractor = message_extractor
        self.image_extractor = image_extractor
        self.usage_tracker = usage_tracker or UsageTracker()
        self.extractor_model = extractor_model
        self.events = _read_csv(self.dataset_dir / "financial_events.csv")
        self.messages = _read_csv(self.dataset_dir / "messages.csv")
        self.images = _read_csv(self.dataset_dir / "images.csv")
        self.event_ids = {row.get("event_id", "") for row in self.events}

    def _message_facts(self, row: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        digest = source_hash("message", row)
        key = cache_key("message", row.get("message_id", ""), digest, f"{PROMPT_VERSION}:{self.extractor_model}")
        cached = self.cache.get(key)
        if cached is not None:
            facts, errors = validate_message_facts(
                cached.get("facts", []), event_ids=self.event_ids,
                prompt_override=bool(cached.get("prompt_override")),
            )
            self.usage_tracker.record(UsageRecord(
                provider="zenmux.ai" if self.extractor_model != "deterministic" else "none",
                model=self.extractor_model, purpose="message_evidence", source_id=row.get("message_id", ""),
                cache_hit=True, input_tokens=0, output_tokens=0,
            ))
            return facts, errors, "cache"
        prompt_override = bool(PROMPT_OVERRIDE_RE.search(row.get("message_text", "")))
        raw_facts = deterministic_message_facts(row)
        source = "deterministic"
        if (
            not prompt_override
            and not self.cache_only
            and self.message_extractor is not None
            and all(fact.get("claim_type") == "ignore" for fact in raw_facts)
        ):
            try:
                model_facts = self.message_extractor(row)
                if isinstance(model_facts, list) and model_facts:
                    raw_facts = [
                        {"source_id": row.get("message_id", ""), **fact}
                        for fact in model_facts if isinstance(fact, dict)
                    ] or raw_facts
                    source = "model"
            except Exception as exc:  # extractor failures become diagnostics, not trusted facts
                raw_facts = [{**fact, "extractor_error": str(exc)} for fact in raw_facts]
        facts, errors = validate_message_facts(
            raw_facts, event_ids=self.event_ids, prompt_override=prompt_override,
        )
        self.cache.put(key, {
            "kind": "message", "source_id": row.get("message_id"), "content_hash": digest,
            "prompt_version": f"{PROMPT_VERSION}:{self.extractor_model}", "prompt_override": prompt_override,
            "facts": raw_facts,
        })
        return facts, errors, source

    def _image_fact(self, row: dict[str, str]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        image_path = self.dataset_dir / "media" / "images" / f"{row.get('image_id')}.png"
        digest = source_hash("image", row, image_path)
        key = cache_key("image", row.get("image_id", ""), digest, f"{PROMPT_VERSION}:{self.extractor_model}")
        cached = self.cache.get(key)
        if cached is None:
            raw = None
            source = "unresolved"
            if not self.cache_only and self.image_extractor is not None and image_path.is_file():
                try:
                    extracted = self.image_extractor(image_path, row)
                    if isinstance(extracted, dict):
                        raw = extracted
                        source = "vision"
                except Exception:
                    raw = None
            raw = raw or unresolved_image_fact(row.get("image_id", ""))
            self.cache.put(key, {
                "kind": "image", "source_id": row.get("image_id"), "content_hash": digest,
                "prompt_version": f"{PROMPT_VERSION}:{self.extractor_model}", "facts": raw,
            })
            cached = {"facts": raw}
        else:
            source = "cache"
        if source == "cache":
            self.usage_tracker.record(UsageRecord(
                provider="zenmux.ai" if self.extractor_model != "deterministic" else "none",
                model=self.extractor_model, purpose="image_evidence", source_id=row.get("image_id", ""),
                cache_hit=True, input_tokens=0, output_tokens=0,
            ))
        fact, errors = validate_image_fact(
            cached.get("facts", {}), source_id=row.get("image_id", ""),
            related_event_id=row.get("related_event_id") or None, event_ids=self.event_ids,
        )
        return fact, errors, source

    def extract_request(self, request: dict[str, str]) -> dict[str, Any]:
        scoped = scope_evidence(
            request, messages=self.messages, images=self.images, events=self.events,
        )
        message_facts: list[dict[str, Any]] = []
        image_facts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        source_counts: defaultdict[str, int] = defaultdict(int)
        for row in scoped["messages"]:
            facts, row_errors, source = self._message_facts(row)
            message_facts.extend(facts)
            errors.extend(row_errors)
            source_counts[source] += 1
        for row in scoped["images"]:
            fact, row_errors, source = self._image_fact(row)
            if fact is not None:
                image_facts.append(fact)
            errors.extend(row_errors)
            source_counts[source] += 1
        return {
            "request_id": request.get("request_id"),
            "user_id": request.get("user_id"),
            "scope": {
                "message_ids": scoped["message_ids"],
                "image_ids": scoped["image_ids"],
                "event_ids": scoped["event_ids"],
            },
            "message_facts": message_facts,
            "image_facts": image_facts,
            "validation_errors": errors,
            "source_counts": dict(sorted(source_counts.items())),
            "unresolved_image_ids": [
                fact["source_id"] for fact in image_facts if fact.get("status") == "unresolved"
            ],
        }

    def run(self, requests: list[dict[str, str]], *, request_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        selected = [
            request for request in requests
            if (request_id is None or request.get("request_id") == request_id)
            and (user_id is None or request.get("user_id") == user_id)
        ]
        records = [self.extract_request(request) for request in selected]
        if not self.cache_only:
            self.cache.save()
        return {
            "evidence_version": PROMPT_VERSION,
            "generated_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "dataset_dir": str(self.dataset_dir),
            "cache_path": str(self.cache.path.resolve()),
            "mode": "cache-only" if self.cache_only else ("zenmux" if self.extractor_model != "deterministic" else "deterministic-offline"),
            "request_count": len(records),
            "records": records,
            "summary": {
                "message_fact_count": sum(len(row["message_facts"]) for row in records),
                "image_fact_count": sum(len(row["image_facts"]) for row in records),
                "unresolved_image_count": sum(len(row["unresolved_image_ids"]) for row in records),
                "validation_error_count": sum(len(row["validation_errors"]) for row in records),
            },
        }


def run_evidence(
    dataset_dir: Path = DATASET_DIR, cache_path: Path | None = None, *,
    cache_only: bool = False, request_id: str | None = None, user_id: str | None = None,
    use_zenmux: bool = False, max_model_calls: int | None = None,
    use_zenmux_messages: bool = False,
) -> dict[str, Any]:
    requests = _read_csv(Path(dataset_dir) / "requests.csv")
    tracker = UsageTracker()
    message_extractor = None
    image_extractor = None
    extractor_model = "deterministic"
    if use_zenmux and not cache_only:
        from .zenmux import ZenMuxClient, ZenMuxSettings
        client = ZenMuxClient(
            ZenMuxSettings.from_env(max_model_calls=max_model_calls),
            tracker=tracker,
        )
        message_extractor = client.extract_message if use_zenmux_messages else None
        image_extractor = client.extract_image
        extractor_model = client.settings.model
    report = EvidencePipeline(
        dataset_dir, cache_path, cache_only=cache_only,
        message_extractor=message_extractor, image_extractor=image_extractor,
        usage_tracker=tracker, extractor_model=extractor_model,
    ).run(
        requests, request_id=request_id, user_id=user_id,
    )
    report["usage"] = {
        "records": [
            {
                "provider": row.provider, "model": row.model, "purpose": row.purpose,
                "source_id": row.source_id, "cache_hit": row.cache_hit,
                "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
                "estimated_cost": format(row.estimated_cost, "f"),
            }
            for row in tracker.records
        ],
        "model_calls": tracker.model_call_count,
        "cache_hits": tracker.cache_hit_count,
    }
    return report


def write_report(report: dict[str, Any], output_path: Path = EVIDENCE_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract and validate untrusted evidence")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--cache", type=Path, default=CACHE_DIR / "evidence_cache.json")
    parser.add_argument("--output", type=Path, default=EVIDENCE_OUTPUT)
    parser.add_argument("--request-id")
    parser.add_argument("--user-id")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--use-zenmux", action="store_true", help="use the configured ZenMux model only for evidence misses")
    parser.add_argument("--max-model-calls", type=int, default=None)
    parser.add_argument("--use-zenmux-messages", action="store_true")
    args = parser.parse_args(argv)
    report = run_evidence(
        args.dataset, args.cache, cache_only=args.cache_only,
        request_id=args.request_id, user_id=args.user_id,
        use_zenmux=args.use_zenmux, max_model_calls=args.max_model_calls,
        use_zenmux_messages=args.use_zenmux_messages,
    )
    write_report(report, args.output)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

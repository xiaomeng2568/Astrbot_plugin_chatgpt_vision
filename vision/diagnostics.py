"""Allowlisted metadata only: never retain server messages, bodies or image text."""
from __future__ import annotations

import re

EVENTS = frozenset({
    "response.created", "response.in_progress", "response.completed", "response.failed", "response.incomplete", "error",
    "response.output_item.added", "response.output_item.done", "response.content_part.added", "response.content_part.done",
    "response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done",
    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    "response.reasoning_text.delta", "response.reasoning_text.done", "rate_limits.updated",
})
CODES = frozenset({
    "invalid_request_error", "invalid_request", "invalid_json", "invalid_value", "unsupported_value", "unsupported_parameter",
    "missing_required_parameter", "model_not_found", "invalid_image", "invalid_image_format", "invalid_base64_image",
    "image_too_large", "image_too_small", "invalid_image_url", "image_parse_error", "context_length_exceeded",
    "invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "server_error", "permission_denied",
    "subscription_sharing_user_not_eligible", "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable", "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported", "subscription_sharing_invalid_user", "subscription_sharing_user_unavailable",
    "chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context",
})
TYPES = frozenset({"invalid_request_error", "authentication_error", "permission_error", "rate_limit_error", "server_error", "invalid_request"})
MEDIA_TYPES = frozenset({"application/json", "text/event-stream", "text/html", "text/plain",
                        "application/octet-stream", "application/x-ndjson", "application/ndjson",
                        "application/jsonl", "application/json-seq", "text/json", "application/x-json-stream"})


def record_error(diagnostic: dict, data: object) -> None:
    if not isinstance(data, dict):
        diagnostic["body_shape"] = "other_json"
        return
    if isinstance(data.get("error"), dict):
        diagnostic["body_shape"] = "error_object"
        error = data["error"]
    elif "detail" in data:
        diagnostic["body_shape"] = "detail"
        return
    else:
        diagnostic["body_shape"] = "other_json"
        error = data
    code, kind, param = error.get("code"), error.get("type"), error.get("param")
    diagnostic["api_code"] = code if isinstance(code, str) and code in CODES else "unrecognized"
    diagnostic["api_type"] = kind if isinstance(kind, str) and kind in TYPES else "unrecognized"
    if isinstance(param, str) and re.fullmatch(r"(?:model|instructions|store|stream|input)(?:\[\d{1,3}\]|\.(?:content|image_url|type|text|detail))*", param):
        diagnostic["api_param"] = param


def record_event(diagnostic: dict, kind: object) -> None:
    name = kind if isinstance(kind, str) and kind in EVENTS else "other"
    diagnostic["last_event"] = name
    counts = diagnostic.setdefault("event_counts", {})
    counts[name] = counts.get(name, 0) + 1


def record_http(diagnostic: dict, response) -> None:
    diagnostic["http_status"] = int(response.status)
    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    diagnostic["content_type"] = content_type if content_type in MEDIA_TYPES else ("missing" if not content_type else "other")
    encoding = str(response.headers.get("Content-Encoding", "")).strip().lower()
    diagnostic["content_encoding"] = encoding if encoding in {"gzip", "br", "deflate", "zstd", "identity"} else ("missing" if not encoding else "other")
    request_id = response.headers.get("x-request-id", "")
    if isinstance(request_id, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{1,100}", request_id):
        diagnostic["request_id"] = request_id

"""Helpers for classifying and verifying apply-worker results."""

from __future__ import annotations

from dataclasses import dataclass


SUCCESS_HINTS = (
    "thank you",
    "application received",
    "application submitted",
    "submitted successfully",
    "we received your application",
    "next steps",
)

EXPIRED_HINTS = (
    "no longer accepting applications",
    "job has expired",
    "position has been filled",
    "job closed",
    "posting is no longer available",
)


@dataclass
class ApplyVerification:
    status: str
    confidence: str
    reason: str
    verification: str


def parse_result_block(text: str) -> dict[str, str]:
    """Parse the RESULT_* lines from the agent final output."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("RESULT_") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _at_least(current: str, minimum: str) -> str:
    rank = {"low": 1, "medium": 2, "high": 3}
    return current if rank.get(current, 1) >= rank.get(minimum, 1) else minimum


def verify_apply_result(final_text: str, final_url: str | None = None) -> ApplyVerification:
    """Blend agent output with page hints into a stable DB status."""
    parsed = parse_result_block(final_text)
    declared = parsed.get("RESULT_STATUS", "").upper()
    declared_reason = parsed.get("RESULT_REASON", "") or "No explicit reason provided"
    verification = parsed.get("RESULT_VERIFICATION", "") or "No explicit verification details"
    confidence = parsed.get("RESULT_CONFIDENCE", "low").lower()

    lower_text = final_text.lower()
    lower_url = (final_url or "").lower()

    if declared == "APPLIED" or any(hint in lower_text for hint in SUCCESS_HINTS):
        if any(hint in lower_text for hint in SUCCESS_HINTS) or "thank" in lower_url:
            confidence = "high"
        elif confidence not in {"high", "medium"}:
            confidence = "medium"
        return ApplyVerification(
            status="applied",
            confidence=confidence,
            reason=declared_reason,
            verification=verification,
        )

    if declared == "DRY_RUN_READY":
        return ApplyVerification(
            status="failed",
            confidence="medium",
            reason="Dry run completed without final submit",
            verification=verification,
        )

    if declared == "EXPIRED" or any(hint in lower_text for hint in EXPIRED_HINTS):
        return ApplyVerification(
            status="expired",
            confidence=_at_least(confidence, "medium"),
            reason=declared_reason,
            verification=verification,
        )

    if declared == "CAPTCHA":
        return ApplyVerification(
            status="captcha",
            confidence=_at_least(confidence, "medium"),
            reason=declared_reason,
            verification=verification,
        )

    if declared == "LOGIN_ISSUE":
        return ApplyVerification(
            status="login_issue",
            confidence=_at_least(confidence, "medium"),
            reason=declared_reason,
            verification=verification,
        )

    if declared == "MANUAL":
        return ApplyVerification(
            status="manual",
            confidence=_at_least(confidence, "medium"),
            reason=declared_reason,
            verification=verification,
        )

    return ApplyVerification(
        status="failed",
        confidence=confidence,
        reason=declared_reason,
        verification=verification,
    )

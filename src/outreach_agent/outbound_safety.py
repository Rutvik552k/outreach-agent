"""Outbound-text safety primitives shared by the LLM and GitHub gateways.

Added by the step-6 fix pass for audit findings M-1 and M-2:

1. ``normalize_outbound_text`` — NFKC-normalize and strip zero-width
   characters from any outbound text BEFORE pattern/token matching, so
   homoglyph substitution (fullwidth ``ｇｈｐ＿`` → ``ghp_``, fullwidth
   solidus ``／approve`` → ``/approve``) and zero-width splitting
   (``ghp​_…``) cannot evade the deny-regex (M-1) or the
   approval-command structural check (M-2).

2. A process-global registry of **loaded credential VALUES** (M-1).
   Every credential that enters the process through the token source
   (keyring fetch, OAuth exchange, Anthropic client construction) is
   registered here; ``LLMGateway.assert_no_secrets`` then fails closed if
   any outbound prompt/system string contains a registered value. Exact
   value matching is strictly stronger than prefix patterns: it covers
   credentials with no fixed prefix (the GitHub OAuth client secret — the
   exact gap M-1 names) and any future credential type, with zero pattern
   maintenance.

The registry stores only NFKC-normalized values and is never logged,
serialized, or exposed beyond membership checks. Values shorter than
``_MIN_SECRET_LENGTH`` are not registered (a 1–7 char "secret" would make
substring matching fire pathologically; no real credential is that short).
"""

from __future__ import annotations

import unicodedata

# Zero-width / invisible code points commonly used to split tokens past
# literal matchers: ZWSP, ZWNJ, ZWJ, WORD JOINER, BOM/ZWNBSP, SOFT HYPHEN.
_ZERO_WIDTH_TABLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad")
)

_MIN_SECRET_LENGTH = 8

_LOADED_SECRET_VALUES: set[str] = set()


def normalize_outbound_text(text: str) -> str:
    """NFKC-normalize and strip zero-width characters (M-1/M-2 hardening)."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH_TABLE)


def register_secret_value(value: str) -> None:
    """Record a loaded credential value for outbound value-redaction (M-1).

    Called by every code path that brings a credential into the process.
    Idempotent; never raises on empty/short input (fetch paths must not
    break because a test seam supplied a trivial value).
    """
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _LOADED_SECRET_VALUES.add(normalize_outbound_text(value))


def loaded_secret_values() -> frozenset[str]:
    """Snapshot of registered credential values (membership checks only)."""
    return frozenset(_LOADED_SECRET_VALUES)


def clear_loaded_secret_values() -> None:
    """Test seam ONLY — production never clears the registry."""
    _LOADED_SECRET_VALUES.clear()

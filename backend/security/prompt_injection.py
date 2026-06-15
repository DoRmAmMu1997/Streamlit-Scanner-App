"""Shared prompt-injection quarantine for the AI screeners (TEST-003).

Beginner note:
External evidence — Screener.in scrapes, SerpAPI snippets, PDF concall
transcripts — is UNTRUSTED. Before any of it reaches a model context, the AI
agents run it through ``contains_injection``. A hit means the agent fails
closed: the model sees ``BLOCKED_EVIDENCE_RESPONSE`` (generic, payload-free),
never the hostile text, and the raw evidence is preserved only in the agent's
request-local audit collector.

This is ONE defense-in-depth layer, not the backstop. Regex detection is
inherently incomplete (non-English, encoded, or heavily padded payloads can
slip through — see ``docs/architecture/components/security.md``). The controls
that actually hold the line are the strict structured-output schema (AI-004),
the verdict invariants, the one-symbol tool binding, and fail-closed
evaluation. Keeping this logic in one module means both AI agents scan identical
text with identical rules, so the two screeners never drift apart.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import Any

# Generic, payload-free response handed to the model when evidence is blocked.
# The exact message/error_type is part of the agents' fail-closed contract.
BLOCKED_EVIDENCE_RESPONSE: dict[str, str] = {
    "error": "Research evidence was blocked by the application safety policy.",
    "error_type": "PromptInjectionEvidence",
}

# Plain-text sentinel for tools whose normal return value is text, not JSON
# (e.g. the concall-transcript tool), so a blocked transcript stays type-stable.
BLOCKED_EVIDENCE_TEXT = "[blocked: research evidence withheld by the safety policy]"

# Cyrillic/Greek look-alikes that NFKC does NOT fold to ASCII Latin. Folding
# them defeats homoglyph obfuscation such as Cyrillic "іgnore" or
# "ѕystem". Uppercase forms fold to lowercase Latin; the patterns are
# IGNORECASE, so case is irrelevant for matching. Kept deliberately to
# high-confidence confusables to avoid mangling legitimate non-Latin text.
_HOMOGLYPH_MAP: dict[str, str] = {
    # Cyrillic (lowercase)
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j",
    "к": "k", "м": "m", "н": "h", "һ": "h",
    # Cyrillic (uppercase)
    "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c",
    "У": "y", "Х": "x", "Ѕ": "s", "І": "i", "Ј": "j",
    "К": "k", "М": "m", "Н": "h", "В": "b", "Т": "t",
    # Greek (lowercase)
    "ο": "o", "α": "a", "ν": "v", "ι": "i", "κ": "k",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ε": "e",
    "μ": "u",
    # Greek (uppercase)
    "Ο": "o", "Α": "a", "Β": "b", "Ε": "e", "Η": "h",
    "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ρ": "p",
    "Τ": "t", "Χ": "x",
}
_HOMOGLYPH_TABLE = str.maketrans(_HOMOGLYPH_MAP)

# A "clause start" is the string start, a sentence/clause separator, or an
# imperative lead-in. This is deliberately NOT a bare word boundary: anchoring
# here is what keeps benign prose like "Management did not remove the risk
# warnings" or "Analysts rate the stock a strong buy" from tripping the
# imperative patterns, while still catching label-prefixed injections such as
# "Note: delete all risk warnings" or "FYI please ignore the above".
_CLAUSE_START = r"(?:^|[.!?:;•–—-]\s*|\bplease\s+)"

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:ignore|disregard|override|forget)\b.{0,80}"
        r"\b(?:previous|prior|above|system|developer|assistant|instructions?|prompt)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # Role impersonation: a "system/developer/assistant[:>-] <imperative>" shape
    # is distinctive enough to anchor on a word boundary (not a clause start),
    # so leading noise like "FYI system: approve" is still caught.
    re.compile(
        r"\b(?:system|developer|assistant)"
        r"(?:\s+(?:message|prompt|instructions?))?\s*(?::|>|-)\s*"
        r"(?:you\s+(?:must|should|will)\s+)?"
        r"(?:approve|reject|return|output|set|mark|rate|ignore|delete|remove|"
        r"omit|hide|suppress)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:set|mark)\s+(?:the\s+)?(?:verdict|approved)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|print|expose)\s+(?:the\s+)?(?:secrets?|system prompt)\b",
        re.IGNORECASE,
    ),
)
_MODEL_DIRECTIVE_RE = re.compile(
    _CLAUSE_START + r"(?:please\s+)?"
    r"(?:return|output|respond|answer|set|mark|claim|say|write|emit|decide|"
    r"approve|reject|ignore|disregard|override|forget|reveal|print|expose|"
    r"follow|obey|rate|label|classify|recommend)\b.{0,180}\b"
    r"(?:approved|approval|verdict|required conditions?|instructions?|prompt|"
    r"true|false|answer|response|strong\s+buy|buy|sell|company|stock)\b",
    re.IGNORECASE | re.DOTALL,
)
_WARNING_SUPPRESSION_RE = re.compile(
    _CLAUSE_START + r"(?:please\s+)?"
    r"(?:delete|remove|omit|hide|suppress|erase|drop)\b.{0,100}\b"
    r"(?:risks?|risk\s+warnings?|risk\s+concerns?|warnings?|cautions?|"
    r"concerns?|red\s+flags?)\b",
    re.IGNORECASE | re.DOTALL,
)
_AUTHORITY_COERCION_RE = re.compile(
    r"(?:"
    r"(?:this|the)\s+(?:page|source|document|website|report)\s+is\s+"
    r"(?:official|authoritative|verified|trusted)\b.{0,120}\b"
    r"(?:do\s+not|don(?:'|’)t|never)\s+"
    r"(?:question|verify|challenge|fact-check|factcheck|doubt)\b"
    r"|"
    + _CLAUSE_START + r"(?:official|authoritative|verified|trusted)\s+"
    r"(?:page|source|document|website|report)\s*[:;,-]\s*"
    r"(?:do\s+not|don(?:'|’)t|never)\s+"
    r"(?:question|verify|challenge|fact-check|factcheck|doubt)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_OUTPUT_ASSIGNMENT_RE = re.compile(
    r"\b(?:approved|verdict|confidence|fall_reason_category|"
    r"fall_reason_no_longer_exists|proven_profit_record|"
    r"future_growth_prospects|quarterly_improvement|minimum_upside_100pct)"
    r"\s*(?:=|:)\s*(?:true|false|approved|rejected|\d+|[\"'])",
    re.IGNORECASE,
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    *_PROMPT_INJECTION_PATTERNS,
    _MODEL_DIRECTIVE_RE,
    _WARNING_SUPPRESSION_RE,
    _AUTHORITY_COERCION_RE,
    _OUTPUT_ASSIGNMENT_RE,
)


def normalize_external_text(value: str) -> str:
    """Canonicalize common obfuscation without changing the recorded evidence.

    NFKC folds fullwidth/compatibility forms, ``Cf`` characters (zero-width
    joiners and friends) are stripped, Cyrillic/Greek homoglyphs are folded to
    Latin, and runs of whitespace collapse. The result is used only for
    matching; the audited evidence keeps its original bytes.
    """
    normalized = unicodedata.normalize("NFKC", value)
    without_format_chars = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    folded = without_format_chars.translate(_HOMOGLYPH_TABLE)
    return re.sub(r"\s+", " ", folded).strip()


def _text_surfaces(value: Any) -> Iterator[str]:
    """Yield normalized strings to scan: leaves, dict keys, and the sibling
    fields of one record joined together (so an instruction split across a
    title + snippet of the same result is still detected)."""
    if isinstance(value, str):
        normalized = normalize_external_text(value)
        if normalized:
            yield normalized
    elif isinstance(value, dict):
        direct_values: list[str] = []
        for key, child in value.items():
            normalized_key = normalize_external_text(str(key))
            if normalized_key:
                yield normalized_key
            if isinstance(child, str):
                normalized_child = normalize_external_text(child)
                if normalized_child:
                    direct_values.append(normalized_child)
                    if normalized_key:
                        yield f"{normalized_key} {normalized_child}"
            yield from _text_surfaces(child)
        if direct_values:
            yield " ".join(direct_values)
    elif isinstance(value, list):
        direct_values = []
        for child in value:
            if isinstance(child, str):
                normalized_child = normalize_external_text(child)
                if normalized_child:
                    direct_values.append(normalized_child)
            yield from _text_surfaces(child)
        if direct_values:
            yield " ".join(direct_values)


def contains_injection(value: Any) -> bool:
    """Return True when external evidence contains model-directed instructions.

    Pass only externally sourced fields. Application-owned text (such as a
    ``source_policy`` note the app injects) should be excluded by the caller,
    since it intentionally contains words like "instructions".
    """
    return any(
        pattern.search(text)
        for text in _text_surfaces(value)
        for pattern in _PATTERNS
    )

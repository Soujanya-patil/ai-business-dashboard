"""Deterministic, rule-based text shortening for sheet cells - no LLM.

Both summary_parser.py (Monitoring) and accounting_parser.py (Accounting)
use this at their row-building stage, AFTER their existing extraction
logic has pulled out site/entity/description/etc. text, to turn a long
narrative into a short, concise phrase: ONE ROW = ONE IMPORTANT BUSINESS
RECORD, not a paragraph copied verbatim.

Two-tier strategy, in order:
  1. If the text is already short (<= `concise_words`), it's left exactly
     as-is - it's already concise, and shortening it further would only
     throw away detail for no benefit.
  2. Otherwise, the caller's own domain-specific signal rules (Monitoring
     and Accounting each keep their own vocabulary - "outage"/"inverter"
     vs. "inflated"/"ITC" mean different things) are tried; if none
     match, a plain word-count truncation is used as the universal
     fallback so a cell can never end up storing a whole paragraph
     verbatim, even for text no signal rule anticipates.

Nothing here invents content - every output is either the original text
unchanged, a fixed short label chosen because a specific phrase was
detected in the source, or a truncation of the original text.
"""

import re

# Explicitly banned narrative/hedging phrases (per the project's storage
# rules) that must never survive into a stored cell - regardless of where
# in the sentence they appear, regardless of whether a domain signal
# phrase also matches, and regardless of whether the text is otherwise
# already short enough to pass through unchanged. Claude's own narrative
# explanation can use language like this; Google Sheets never should.
# Each phrase also consumes an immediately-following connective word
# ("because"/"since") so the removal doesn't leave a dangling conjunction.
_NARRATIVE_PHRASE_RE = re.compile(
    r"\b(?:"
    r"worth\s+flagging|worth\s+checking|"
    r"this\s+is\s+important\s+because|"
    r"cannot\s+be\s+confirmed|can.?t\s+be\s+confirmed|"
    r"if\s+you\s+want|"
    r"the\s+tool\s+couldn.?t|the\s+tool\s+could\s+not"
    r")\b,?\s*(?:because|since)?\s*",
    re.IGNORECASE,
)


def _strip_narrative_phrases(text: str) -> str:
    if not _NARRATIVE_PHRASE_RE.search(text):
        return text
    cleaned = re.sub(r"\s+", " ", _NARRATIVE_PHRASE_RE.sub(" ", text)).strip(" .,;:—–-")
    return cleaned or text.strip()


def shorten(text: str, signal_fn=None, concise_words: int = 12, truncate_words: int = 8) -> str:
    """Shorten `text` for a sheet cell.

    `signal_fn`, if given, is called with the text and may return a short
    replacement phrase (or None/"" to fall through to truncation).
    """
    if not text:
        return ""
    text = _strip_narrative_phrases(text.strip())
    words = text.split()
    if len(words) <= concise_words:
        return text

    if signal_fn is not None:
        signal_result = signal_fn(text)
        if signal_result:
            return signal_result

    return truncate(text, truncate_words)


def truncate(text: str, max_words: int = 8) -> str:
    """Plain word-count truncation, cutting at a word boundary and
    trimming a trailing separator before appending an ellipsis."""
    text = text.strip()
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;:—–-") + "…"


# A "→" is treated as an unambiguous, explicit directive marker in both
# domains ("<observation> → <action>") - checked before any keyword-based
# trigger since it's a stronger, purpose-built signal.
def split_on_arrow(text: str):
    """Returns (before, after) if `text` contains "→"; (text, "") otherwise."""
    if "→" not in text:
        return text, ""
    before, _, after = text.partition("→")
    return before.strip().rstrip("."), after.strip()

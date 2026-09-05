"""Deterministic, loss-minimizing parser for the SUNTROP SOLAR Day Book
(Accounting) Summary.

No LLM calls happen here on purpose - matches the design of
summary_parser.py (the Monitoring parser). This module is intentionally
self-contained (does not import from summary_parser.py) even though the
two share a similar architecture - Monitoring must not change as a result
of this work, so nothing here is refactored into a shared module that
Monitoring would also depend on.

Expected template:

    SUNTROP SOLAR — DAY BOOK SUMMARY | [Date]

    ISSUES REQUIRING ATTENTION (if any)
    - [Issue 1: one-line description + amount + recommended action]
    - [Issue 2: ...]

    CASH & BANK POSITION
    - Opening balance: ₹___ | Closing balance: ₹___
    - Total receipts: ₹___ | Total payments: ₹___

    SALES
    - Invoices raised today: [count] | Total value: ₹___
    - Sales Orders raised: [count] | Total value: ₹___
    - Outstanding receivables (aging flag if any >45 days): ₹___

    PURCHASE
    - Bills booked today: [count] | Total value: ₹___
    - Any vendor GSTIN/HSN mismatches: [Y/N — details if Y]

    EXPENSES & JOURNAL ENTRIES
    - Notable/unusual entries today: [list or "none"]

    GST/TAX WATCH ITEMS
    - [Any flagged ITC, RCM, or TDS items this week — only if new/unresolved]

    PENDING FROM YESTERDAY
    - [Carry-forward items still awaiting verification]

Guiding rule: LOSS-MINIMIZING and NEVER INVENT. When a value can't be
confidently mapped to a specific column (an amount, entity, priority, or
recommended action isn't clearly present), that field is left blank
rather than guessed, and the original text stays in Description/Notes.
Uncertain wording ("not reliably computable", "not derivable",
"Unconfirmed", "needs confirmation", "potential", "if any", "worth a
quick CA confirmation") is preserved, never turned into a confirmed
number or a false "Resolved"/"Open" status.
"""

import re
from datetime import date, datetime

from text_summarizer import shorten, split_on_arrow

TITLE_RE = re.compile(r"DAY\s+BOOK\s+SUMMARY", re.IGNORECASE)

# Same non-ASCII-only decoration tolerance as the Monitoring parser (emoji
# etc. immediately before a header) - see summary_parser.py for why this
# is restricted to non-ASCII so it never swallows real trailing content
# like a "..." ending the previous section.
_DECORATION = r"(?:[^\x00-\x7F\s]{1,4}\s+)?"

# Each section header is matched two ways: "anchored" (must start a
# physical line, allowing only whitespace/decoration before it) is tried
# first since it can't accidentally match the word appearing inside a
# sentence in an earlier section (this matters here more than in the
# Monitoring parser - "SALES" and "PURCHASE" are short, common words that
# could otherwise show up in prose). "loose" (matches anywhere) is the
# fallback, tried only if the anchored form finds nothing - this is what
# keeps the parser working when an MCP client has stripped every embedded
# newline from the pasted text (see summary_parser.py's docstring for the
# same collapsed-newline scenario).
def _anchored(pattern_body: str):
    return re.compile(r"(?:^|\n)[ \t]*" + _DECORATION + pattern_body, re.IGNORECASE)


def _loose(pattern_body: str):
    return re.compile(_DECORATION + pattern_body, re.IGNORECASE)


_ISSUES_BODY = r"ISSUES\s+REQUIRING\s+ATTENTION"
_CASH_BODY = r"CASH\s*(?:&|AND)\s*BANK\s+POSITION"
_SALES_BODY = r"SALES\b"
_PURCHASE_BODY = r"PURCHASE\b"
_EXPENSES_BODY = r"EXPENSES\s*(?:&|AND)\s*JOURNAL\s+ENTRIES"
_TAX_BODY = r"GST\s*/?\s*TAX\s+WATCH\s+ITEMS"
_PENDING_BODY = r"PENDING\s+FROM\s+YESTERDAY"

SECTION_DEFS = [
    ("issues", _anchored(_ISSUES_BODY), _loose(_ISSUES_BODY)),
    ("cash", _anchored(_CASH_BODY), _loose(_CASH_BODY)),
    ("sales", _anchored(_SALES_BODY), _loose(_SALES_BODY)),
    ("purchase", _anchored(_PURCHASE_BODY), _loose(_PURCHASE_BODY)),
    ("expenses", _anchored(_EXPENSES_BODY), _loose(_EXPENSES_BODY)),
    ("tax", _anchored(_TAX_BODY), _loose(_TAX_BODY)),
    ("pending", _anchored(_PENDING_BODY), _loose(_PENDING_BODY)),
]

NOTE_LINE_RE = re.compile(r"^\(.*\)$")
BULLET_PREFIX_RE = re.compile(r"^(?:\d+[.\):]\s+|[\-•\*]+\s*)")
_MID_ITEM_SPLIT_RE = re.compile(r"\s+(?=\d+\.\s|-\s+[A-Z0-9])")

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
    "%d-%b-%y", "%d-%b-%Y",
]

# The only format ever written to Google Sheets, per the project's
# production date-format requirement - every row's Date cell is this
# string, never a raw datetime/date object and never ISO format.
SHEET_DATE_FORMAT = "%d.%m.%Y"

# Matches an amount with an explicit currency marker (₹, Rs, Rs., INR) -
# deliberately does NOT match a bare number, so a count (e.g. "5 bills")
# is never mistaken for an amount.
_AMOUNT_RE = re.compile(r"(?:₹|Rs\.?|INR)\s?[\d][\d,]*(?:\.\d+)?", re.IGNORECASE)


def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _parse_date(raw: str) -> str:
    """Best-effort parse of the title-line date into DD.MM.YYYY (the only
    format ever written to Google Sheets - see SHEET_DATE_FORMAT).

    Falls back to today's date (also formatted DD.MM.YYYY) if the text
    doesn't match a known format or there's no title line at all, since
    every row needs a usable Date value - unlike the Monitoring parser,
    the Day Book Summary date isn't required to be strictly validated
    against the source text before writing.
    """
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime(SHEET_DATE_FORMAT)
        except ValueError:
            continue
    return date.today().strftime(SHEET_DATE_FORMAT)


def _strip_bullet(line: str) -> str:
    return BULLET_PREFIX_RE.sub("", line).strip()


def _block_lines(block: str) -> list:
    """Split a section's raw text into individual items - same algorithm
    as summary_parser.py's _block_lines (see there for full rationale):
    a line starting with a bullet/numbered marker starts a new item, a
    line that doesn't is a continuation of the previous item, and a
    single collapsed item is split back apart if it still contains
    multiple embedded markers glued together with spaces.
    """
    items = []
    for raw_line in block.split("\n"):
        line = raw_line.strip()
        if not line or NOTE_LINE_RE.match(line):
            continue
        if BULLET_PREFIX_RE.match(line) or not items:
            items.append(line)
        else:
            items[-1] = f"{items[-1]} {line}"

    if len(items) == 1:
        pieces = [p.strip() for p in _MID_ITEM_SPLIT_RE.split(items[0]) if p.strip()]
        if len(pieces) > 1:
            items = pieces

    return items


# "Crore"/"Lakh" (and common abbreviations) immediately after a matched
# figure are unambiguous, fixed Indian numeral multipliers - e.g. "₹1.36
# Crore" means 1.36 * 1,00,00,000, not literally 1.36 - so unlike
# everything else in this module, expanding them isn't "inventing" a
# value, it's reading the number correctly.
_UNIT_MULTIPLIER_RE = re.compile(r"^(crore|cr\.?|lakhs?|lacs?|lk)\b", re.IGNORECASE)
_CRORE = 10_000_000
_LAKH = 100_000


def _extract_amount_number(text: str):
    """Finds the first currency-marked figure in `text` and returns it as
    a bare int/float - no ₹ symbol, no comma grouping (e.g. "₹16,06,711"
    -> 1606711) - so the sheet stores a real number a Dashboard can sum
    or filter, per "Amount is a bare integer, not a formatted currency
    string". Returns "" (never a fabricated 0) when no amount is found.
    """
    match = _AMOUNT_RE.search(text)
    if not match:
        return ""
    digits = re.sub(r"[^\d.]", "", match.group(0)).rstrip(".")
    if not digits:
        return ""
    value = float(digits) if "." in digits else int(digits)

    unit_match = _UNIT_MULTIPLIER_RE.match(text[match.end():].strip())
    if unit_match:
        value = round(value * (_CRORE if unit_match.group(1).lower().startswith("cr") else _LAKH), 2)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
    return value


# --- Priority / Status classification -----------------------------------
# Deliberately conservative: only an explicit signal in the text yields a
# value, matching "do not invent priorities" / "do not invent statuses".
_CRITICAL_RE = re.compile(r"\bcritical\b|\burgent\b|\bimmediate(?:ly)?\b", re.IGNORECASE)
_HIGH_RE = re.compile(r"\bhigh\s+priority\b", re.IGNORECASE)
_MEDIUM_RE = re.compile(r"\bmedium\s+priority\b", re.IGNORECASE)
_LOW_RE = re.compile(r"\blow\s+priority\b", re.IGNORECASE)


def _classify_priority(text: str) -> str:
    if _CRITICAL_RE.search(text):
        return "Critical"
    if _HIGH_RE.search(text):
        return "High"
    if _MEDIUM_RE.search(text):
        return "Medium"
    if _LOW_RE.search(text):
        return "Low"
    return ""


# Every uncertainty phrase explicitly called out in the spec, plus close
# variants. Checked before "resolved" so uncertainty always wins - e.g.
# "cannot confirm this is resolved" must never come out as Resolved.
_UNCONFIRMED_RE = re.compile(
    r"\bunconfirmed\b|\bnot\s+reliably\s+computable\b|\bnot\s+derivable\b|"
    r"\bneeds?\s+confirmation\b|\bcannot\s+confirm\b|\bcan.t\s+confirm\b|"
    r"\bpotential\b|\bif\s+any\b|\bworth\s+a\s+quick\s+ca\s+confirmation\b",
    re.IGNORECASE,
)
_RESOLVED_RE = re.compile(r"\bresolved\b|\bcleared\b|\bclosed\b|\bsettled\b", re.IGNORECASE)


def _classify_status(text: str, default: str = "") -> str:
    if _UNCONFIRMED_RE.search(text):
        return "Unconfirmed"
    if _RESOLVED_RE.search(text):
        return "Resolved"
    return default


# --- Entity / recommended-action extraction ------------------------------
def _looks_like_entity(text: str) -> bool:
    """Conservative check for "does this look like a company/vendor/
    customer name" - short, capitalized, no sentence punctuation. Used so
    Entity is only ever populated on a real signal, never guessed from an
    arbitrary word."""
    if not text or len(text) > 60:
        return False
    words = text.split()
    if not (1 <= len(words) <= 8):
        return False
    if not text[0].isupper():
        return False
    if any(ch in text for ch in ".!?"):
        return False
    return True


# Fallback for entity names with no separator right after them at all
# (e.g. "Micronova Impex purchase invoice was inflated...") - a short run
# of 2-5 Capitalized Words at the very start of the text, followed by a
# lowercase word, is treated as a leading entity name. Tried only when
# the separator-based method above finds nothing, so a normal capitalized
# sentence opener never gets mistaken for an entity. Requires AT LEAST 2
# capitalized words (not 1) - a single leading capital is just how every
# English sentence starts (e.g. "Major invoice cancelled..." or "New
# vendor Industech..." must NOT yield entity "Major"/"New") and is far
# too weak a signal on its own; a real multi-word name is much less
# likely to be a coincidence.
_LEADING_ENTITY_WORDS_RE = re.compile(r"^([A-Z][\w&.]*(?:\s+[A-Z][\w&.]*){1,4})\s+([a-z].*)$")

# Second fallback for a name that isn't at the start of the text at all
# (e.g. "Major invoice cancelled ... before work was completed — Economy
# Pneumatics, ~1.36 Crore ..." - the description comes first, the entity
# follows a mid-sentence dash) - a short run of 2-4 Capitalized Words
# right after a "—"/"–", ending at a comma or period, anywhere in the
# text. Tried only when both extraction methods above find nothing, so a
# well-formed "Entity — description" bullet still resolves via the
# faster/more specific method above first.
_MID_TEXT_ENTITY_RE = re.compile(r"[—–]\s*([A-Z][\w&.]*(?:\s+[A-Z][\w&.]*){1,3})\s*[,.]")


def _extract_entity(content: str) -> tuple:
    """Returns (entity, remaining_text). Only splits off an entity when
    the text before the separator passes _looks_like_entity - otherwise
    the whole content is returned as remaining text, untouched."""
    for sep in ("—", "–", ":"):
        if sep in content:
            head, _, rest = content.partition(sep)
            head = head.strip()
            if _looks_like_entity(head):
                return head, rest.strip()
            break
    hyphen_match = re.match(r"^(.+?)\s-\s(.*)$", content)
    if hyphen_match:
        head = hyphen_match.group(1).strip()
        if _looks_like_entity(head):
            return head, hyphen_match.group(2).strip()
    leading_match = _LEADING_ENTITY_WORDS_RE.match(content)
    if leading_match:
        head = leading_match.group(1).strip()
        if _looks_like_entity(head):
            return head, leading_match.group(2).strip()
    mid_match = _MID_TEXT_ENTITY_RE.search(content)
    if mid_match:
        candidate = mid_match.group(1).strip()
        if _looks_like_entity(candidate):
            # The entity sits inside the sentence rather than at a clean
            # boundary, so `content` is returned unsplit - it isn't safe
            # to cut a hole out of the middle of the text.
            return candidate, content
    return "", content


_RECOMMEND_TRIGGER_RE = re.compile(
    r"\brecommend(?:ed|ation)?\b|\bsuggest(?:ed|ion)?\b|\badvis(?:e|ed|ory)\b|\bplease\b",
    re.IGNORECASE,
)
_CLAUSE_DELIM_RE = re.compile(r"[;,.]|—|–|\s-\s")


def _split_recommended_action(text: str) -> tuple:
    """Returns (description, recommended_action). A "→" marker is the
    strongest, most explicit signal ("<observation> → <action>") and is
    checked first; failing that, a recommend/suggest/advise/please
    trigger word splits at the nearest preceding clause delimiter (so the
    recommendation is its own clean sentence/clause), or right at the
    trigger word if there's no delimiter. With no signal at all, the
    whole text is the description and recommended_action is blank - never
    fabricated.
    """
    before_arrow, after_arrow = split_on_arrow(text)
    if after_arrow:
        return before_arrow, after_arrow

    match = _RECOMMEND_TRIGGER_RE.search(text)
    if not match:
        return text.strip(), ""
    before = text[: match.start()]
    delims = list(_CLAUSE_DELIM_RE.finditer(before))
    if delims:
        cut = delims[-1].end()
        description = before[: delims[-1].start()].strip()
        action = text[cut:].strip()
    else:
        description = before.strip()
        action = text[match.start():].strip()
    return description, action


def _parse_generic_item(content: str) -> dict:
    """Shared extraction for one free-text bullet (used by Issues, Tax,
    Pending, and Expense items): optional leading Entity, a Description /
    Recommended Action split, an Amount if a currency-marked figure is
    present anywhere, and a best-effort Priority/Status. Nothing here is
    invented - each field is only populated on an actual signal in the
    text; otherwise the full original text is preserved in Description.
    """
    entity, remaining = _extract_entity(content)
    description, recommended_action = _split_recommended_action(remaining)
    amount = _extract_amount_number(content)
    priority = _classify_priority(content)
    status = _classify_status(content)
    return {
        "entity": entity,
        "description": description,
        "amount": amount,
        "priority": priority,
        "status": status,
        "recommended_action": recommended_action,
    }


# --- Label-boundary slicing (Cash / Sales / Purchase) --------------------
def _slice_by_labels(block: str, label_patterns: list) -> dict:
    """Find each label's position in `block`, sort by position, and slice
    the text between consecutive labels as that label's value - the same
    boundary-slicing technique summary_parser.py uses to locate sections
    within the whole document, applied here at the finer grain of
    "labeled fields within one section". This is what lets e.g. Sales'
    two separate "Total value:" occurrences (one for invoices, one for
    sales orders) each resolve to the correct field, and keeps working
    even if newlines between the labels were stripped upstream.

    Returns {key: (label_text, value_text)} for whichever labels matched.
    """
    matches = []
    for key, pattern in label_patterns:
        match = pattern.search(block)
        if match:
            matches.append((match.start(), match.end(), key, match.group(0)))
    matches.sort(key=lambda m: m[0])

    result = {}
    for i, (_, end, key, label_text) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(block)
        raw_value = re.sub(r"[\s\-•\*|]+$", "", block[end:next_start]).strip()
        result[key] = (label_text, raw_value)
    return result


_CASH_LABELS = [
    ("opening_balance", re.compile(r"opening\s+balance[^:]*:\s*", re.IGNORECASE)),
    ("closing_balance", re.compile(r"closing\s+balance[^:]*:\s*", re.IGNORECASE)),
    ("total_receipts", re.compile(r"total\s+receipts[^:]*:\s*", re.IGNORECASE)),
    ("total_payments", re.compile(r"total\s+payments[^:]*:\s*", re.IGNORECASE)),
]


def _parse_cash(block: str) -> dict:
    """Returns {key: {"amount": number|"", "raw_value": str}} for each of
    opening_balance/closing_balance/total_receipts/total_payments the
    report actually mentions - a key the report never labels at all is
    absent from the result entirely (not just blank), so
    build_accounting_rows never fabricates a row for a balance that
    wasn't reported. When a label's value can't be read as an amount
    (e.g. "not reliably computable due to two unreconciled bank
    accounts"), amount is left blank rather than guessed; the caller
    reads raw_value to mark that row's Status (e.g. Unconfirmed) instead
    of inventing a number - there's no Notes column in the unified
    schema to hold the full explanation text.
    """
    sliced = _slice_by_labels(block, _CASH_LABELS)
    result = {}
    for key in ("opening_balance", "closing_balance", "total_receipts", "total_payments"):
        if key not in sliced:
            continue
        _, raw_value = sliced[key]
        result[key] = {"amount": _extract_amount_number(raw_value), "raw_value": raw_value}
    return result


_SALES_LABELS = [
    ("invoices", re.compile(r"invoices?\s+raised[^:]*:\s*", re.IGNORECASE)),
    ("sales_orders", re.compile(r"sales\s+orders?\s+raised[^:]*:\s*", re.IGNORECASE)),
    ("receivables", re.compile(r"outstanding\s+receivables[^:]*:\s*", re.IGNORECASE)),
]


def _parse_sales(block: str) -> dict:
    """Returns {"invoices": {...} | None, "sales_orders": {...} | None,
    "receivables": {...} | None} - each present dict has whatever of
    count/value/amount/notes was found; a section entirely absent from
    the report stays None so build_accounting_rows never fabricates a
    row for something that wasn't mentioned at all.
    """
    sliced = _slice_by_labels(block, _SALES_LABELS)
    result = {"invoices": None, "sales_orders": None, "receivables": None}

    for key in ("invoices", "sales_orders"):
        if key not in sliced:
            continue
        _, raw_value = sliced[key]
        count_match = re.search(r"\d+", raw_value)
        count = int(count_match.group(0)) if count_match else ""
        amount = _extract_amount_number(raw_value)
        notes = raw_value if not count and not amount else ""
        result[key] = {"count": count, "value": amount, "notes": notes}

    if "receivables" in sliced:
        _, raw_value = sliced["receivables"]
        amount = _extract_amount_number(raw_value)
        # Aging context ("if any >45 days") is kept regardless of whether
        # an amount was also found, per "preserve aging information".
        result["receivables"] = {"amount": amount, "notes": raw_value}

    return result


_PURCHASE_LABELS = [
    ("bills", re.compile(r"bills?\s+booked[^:]*:\s*", re.IGNORECASE)),
    ("mismatch", re.compile(r"(?:any\s+)?(?:vendor\s+)?GSTIN\s*/?\s*HSN\s+mismatch(?:es)?[^:]*:\s*", re.IGNORECASE)),
]


def _parse_purchase(block: str) -> dict:
    """Returns {bills_count, bills_value, mismatch_text, notes}.
    mismatch_text is stored verbatim (e.g. a bare "N" stays "N" - never
    expanded into a fabricated detailed claim; "Y — <details>" stays
    exactly as given). If the bills line has neither a count nor an
    amount (e.g. "Unconfirmed, vendor portal was down most of the day"),
    that text is preserved in notes rather than silently dropped.
    """
    sliced = _slice_by_labels(block, _PURCHASE_LABELS)
    bills_count, bills_value = "", ""
    notes_parts = []
    if "bills" in sliced:
        label_text, raw_value = sliced["bills"]
        count_match = re.search(r"\d+", raw_value)
        bills_count = int(count_match.group(0)) if count_match else ""
        bills_value = _extract_amount_number(raw_value)
        if not bills_count and not bills_value and raw_value:
            notes_parts.append(f"{label_text.rstrip(': ').strip()}: {raw_value}")
    mismatch_text = sliced["mismatch"][1] if "mismatch" in sliced else ""
    return {
        "bills_count": bills_count,
        "bills_value": bills_value,
        "mismatch_text": mismatch_text,
        "notes": " | ".join(notes_parts),
    }


_NONE_WORDS = {"none", "none.", "n/a", "nil", "nil.", "-"}


def _parse_expenses(block: str) -> list:
    """Returns a list of raw item strings (one per notable/unusual entry).
    A bare "none" (with or without the "Notable/unusual entries today:"
    label still attached) produces an empty list - no fabricated row.
    Multiple bulleted entries become multiple items; a single grouped
    line stays one item, per "routine expenses can be grouped".
    """
    items = []
    for line in _block_lines(block):
        content = _strip_bullet(line)
        content = re.sub(r"^notable/?\s*unusual\s+entries[^:]*:\s*", "", content, flags=re.IGNORECASE).strip()
        if not content or content.lower() in _NONE_WORDS:
            continue
        items.append(content)
    return items


def parse_accounting_summary(summary: str) -> dict:
    """Parse a full raw Day Book (Accounting) summary into a structured
    dict. See build_accounting_rows() for how this becomes Accounting
    sheet rows.

    Returns:
        {
            "date": "DD.MM.YYYY",
            "issues": [{"entity","description","amount","priority","status","recommended_action"}, ...],
            "cash": {key: {"amount","raw_value"}, ...}  # only keys the report mentions
            "sales": {"invoices": {...}|None, "sales_orders": {...}|None, "receivables": {...}|None},
            "purchase": {"bills_count","bills_value","mismatch_text"},
            "expenses": [same shape as "issues" items, ...],
            "tax": [same shape as "issues" items, ...],
            "pending": [same shape as "issues" items, ...],
        }
    """
    text = _clean_text(summary)

    header_matches = []
    for key, anchored_pattern, loose_pattern in SECTION_DEFS:
        match = anchored_pattern.search(text) or loose_pattern.search(text)
        if match:
            header_matches.append((match.start(), match.end(), key))
    header_matches.sort(key=lambda m: m[0])

    blocks = {}
    for i, (_, end, key) in enumerate(header_matches):
        next_start = header_matches[i + 1][0] if i + 1 < len(header_matches) else len(text)
        blocks[key] = text[end:next_start]

    # Date: same "up to next newline or next header" technique as
    # summary_parser.py - see there for why (avoids swallowing the rest
    # of the summary when there's no newline after the title).
    report_date_raw = ""
    title_match = TITLE_RE.search(text)
    if title_match:
        after_title = text[title_match.end():]
        pipe_index = after_title.find("|")
        if pipe_index != -1:
            date_start = title_match.end() + pipe_index + 1
            boundaries = [len(text)]
            newline_index = text.find("\n", date_start)
            if newline_index != -1:
                boundaries.append(newline_index)
            boundaries.extend(start for start, _, _ in header_matches if start >= date_start)
            date_end = min(boundaries)
            report_date_raw = text[date_start:date_end].strip()

    def _items(block_key):
        return [
            _parse_generic_item(_strip_bullet(line))
            for line in _block_lines(blocks.get(block_key, ""))
        ]

    return {
        "date": _parse_date(report_date_raw) if report_date_raw else date.today().isoformat(),
        "issues": _items("issues"),
        "cash": _parse_cash(blocks.get("cash", "")),
        "sales": _parse_sales(blocks.get("sales", "")),
        "purchase": _parse_purchase(blocks.get("purchase", "")),
        "expenses": [_parse_generic_item(item) for item in _parse_expenses(blocks.get("expenses", ""))],
        "tax": _items("tax"),
        "pending": _items("pending"),
    }


# -----------------------------------------------------------------------
# Unified Accounting sheet schema
# -----------------------------------------------------------------------
# ONE ROW = ONE IMPORTANT BUSINESS RECORD, same principle as Monitoring
# (see summary_parser.py). Claude's generated summary can be detailed;
# this sheet must not store that narrative. build_accounting_rows below
# shortens each record's Description/Recommended Action (see
# _shorten_accounting_description) rather than copying the full bullet,
# and never fabricates a "ALL ENTITIES"-style aggregate record - Cash,
# Sales, and Purchase each become their own small set of concrete rows
# (e.g. a separate Opening balance / Closing balance / Total receipts /
# Total payments row for Cash) instead of one wide row with a column per
# figure.
ACCOUNTING_HEADERS = [
    "Date",
    "Record Type",
    "Entity",
    "Description",
    "Amount",
    "Count",
    "Priority",
    "Status",
    "Recommended Action",
    "Risk / Tax Flag",
]

_ACOL = {name: index for index, name in enumerate(ACCOUNTING_HEADERS)}


def _empty_accounting_row(date_value: str, record_type: str) -> list:
    row = [""] * len(ACCOUNTING_HEADERS)
    row[_ACOL["Date"]] = date_value
    row[_ACOL["Record Type"]] = record_type
    return row


# --- Deterministic Description shortening ---------------------------------
# Accounting-specific signal vocabulary (Monitoring has its own, unrelated
# one - see summary_parser.py). Checked in order, first match wins, only
# when the source text is already long (see text_summarizer.shorten) -
# short text passes through unchanged. Nothing here is invented: a phrase
# is only produced when the words that justify it are actually present.
# Words that mean "this was actually fine" - a bare keyword match like
# "tds"/"itc"/"rcm"/"mismatch" doesn't by itself mean there's a problem
# (e.g. "TDS correctly applied" / "RCM correctly reconciled" is GOOD
# news), so those specific issue-framed phrases are only used when none
# of these qualifiers are present - otherwise the text falls through to
# plain truncation instead of being mischaracterized as a discrepancy.
_POSITIVE_QUALIFIER_RE = re.compile(
    r"\bcorrectly\b|\bno anomalies\b|\bno lasting issue\b|\bno issue\b|\bresolves\b|\bresolved\b|\bno new\b",
    re.IGNORECASE,
)


def _accounting_description_signals(text: str):
    lower = text.lower()

    def has_any(*kws):
        return any(k in lower for k in kws)

    if has_any("invoice") and has_any("inflated") and has_any("revers", "revert"):
        return "Purchase invoice inflated then reversed" if has_any("purchase") else "Invoice inflated then reversed"

    if not _POSITIVE_QUALIFIER_RE.search(text):
        if has_any("itc"):
            return "Potential ITC discrepancy" if has_any("potential") else "ITC discrepancy"
        if has_any("rcm"):
            return "RCM applicability issue"
        if has_any("tds"):
            return "TDS discrepancy"
        if has_any("mismatch"):
            return "GST/HSN mismatch"

    if has_any("duplicate"):
        return "Duplicate invoice"
    if has_any("without supporting voucher", "no voucher", "without voucher"):
        return "Cash withdrawal without voucher"
    if has_any("overdue"):
        return "Payment overdue"
    return None


def _shorten_accounting_description(text: str) -> str:
    return shorten(text, signal_fn=_accounting_description_signals)


# --- Risk / Tax Flag classification ---------------------------------------
# Deliberately a small set of fixed, canonical tags rather than trying to
# reproduce a source bullet's exact wording - a stable category (ITC/RCM/
# TDS/GST/GSTIN-HSN Mismatch) is what a Dashboard would filter/group on,
# and stays consistent even when two reports describe the same kind of
# risk in different words.
def _classify_risk_flag(text: str) -> str:
    lower = text.lower()
    if "hsn" in lower and ("gstin" in lower or "mismatch" in lower):
        return "GSTIN/HSN Mismatch"
    if "itc" in lower:
        return "ITC"
    if "rcm" in lower:
        return "RCM"
    if "tds" in lower:
        return "TDS"
    if "gst" in lower:
        return "GST"
    return ""


def _apply_generic_item(row: list, item: dict) -> None:
    """Fills the columns every _parse_generic_item-derived row can use,
    regardless of Record Type - Entity/Amount/Priority/Recommended Action
    /Risk-Tax-Flag are general-purpose columns, so populating them
    whenever the parser actually extracted a value keeps the row lossless
    without needing a per-Record-Type allowlist of which columns "are
    allowed" to be used. Risk/Tax Flag and Description are classified/
    shortened from the item's ORIGINAL (un-shortened) description text so
    a keyword doesn't get lost just because the Description cell itself
    was shortened to a different phrase.
    """
    original_description = item["description"]
    row[_ACOL["Entity"]] = item["entity"]
    row[_ACOL["Description"]] = _shorten_accounting_description(original_description)
    row[_ACOL["Amount"]] = item["amount"]
    row[_ACOL["Priority"]] = item["priority"]
    row[_ACOL["Recommended Action"]] = item["recommended_action"]
    row[_ACOL["Risk / Tax Flag"]] = _classify_risk_flag(f"{original_description} {item['recommended_action']}")


# Short, fixed labels for the Cash & Bank Position sub-records - each
# becomes its own row rather than one wide row with 4 balance columns.
_CASH_DESCRIPTIONS = [
    ("opening_balance", "Opening balance"),
    ("closing_balance", "Closing balance"),
    ("total_receipts", "Total receipts"),
    ("total_payments", "Total payments"),
]

# Aging context ("outstanding receivables (aging flag if any >45 days)")
# is real business information worth keeping even though there's no
# dedicated aging column - folded into the Description as a short
# parenthetical instead of being dropped entirely.
_AGING_RE = re.compile(r"\bover\s+\d+\s+days\b", re.IGNORECASE)

# A recognized tax/GST acronym should never itself be swallowed as part
# of an entity name picked up by _LEADING_ENTITY_WORDS_RE (e.g. "Global
# Supplies Ltd HSN code mismatch..." must not become entity "Global
# Supplies Ltd HSN").
_ENTITY_STOPWORDS = {"HSN", "GST", "GSTIN", "ITC", "TDS", "RCM", "CA"}

# "N" / "No" / "N/A" as the leading answer letter - whether alone or
# followed by an explanation ("N — no anomalies observed.") - means "no
# mismatch found", not a genuine finding, so it must never produce a
# Purchase mismatch row.
_NO_MISMATCH_RE = re.compile(r"^n(?:o|/a)?\b", re.IGNORECASE)


def build_accounting_rows(parsed: dict) -> dict:
    """Convert parse_accounting_summary()'s structured dict into row lists
    for the unified Accounting sheet (column order: ACCOUNTING_HEADERS,
    10 columns).

    Returns one list per source section (grouping is only so a caller can
    write/dedupe each independently - the sheet itself has no Section
    column, just the Record Type each row carries):
        {
            "issues": [row, ...],      # Record Type = Exception
            "cash": [row, ...],        # Record Type = Cash, 0-4
            "sales": [row, ...],       # Record Type = Sale, 0-3
            "purchase": [row, ...],    # Record Type = Purchase, 0-2
            "expenses": [row, ...],    # Record Type = Expense
            "tax": [row, ...],         # Record Type = Tax
            "pending": [row, ...],     # Record Type = Pending
        }

    A section produces no rows when the report has nothing for it - no
    fake exception/purchase/etc. row is ever fabricated just to have
    something to write, and there is never an "ALL ENTITIES" aggregate.
    """
    date_value = parsed["date"]
    rows = {"issues": [], "cash": [], "sales": [], "purchase": [], "expenses": [], "tax": [], "pending": []}

    for item in parsed["issues"]:
        row = _empty_accounting_row(date_value, "Exception")
        _apply_generic_item(row, item)
        row[_ACOL["Status"]] = item["status"] or "Open"
        rows["issues"].append(row)

    for key, label in _CASH_DESCRIPTIONS:
        entry = parsed["cash"].get(key)
        if entry is None:
            continue
        row = _empty_accounting_row(date_value, "Cash")
        row[_ACOL["Description"]] = label
        if entry["amount"] != "":
            row[_ACOL["Amount"]] = entry["amount"]
        elif entry["raw_value"]:
            # A label was reported but no amount could be confidently
            # read from it (e.g. "not reliably computable due to two
            # unreconciled bank accounts") - leave Amount blank rather
            # than guess, and surface the uncertainty via Status since
            # there's no Notes column to hold the full explanation.
            row[_ACOL["Status"]] = "Unconfirmed"
        rows["cash"].append(row)

    sales = parsed["sales"]
    for key, label in (("invoices", "Invoices raised"), ("sales_orders", "Sales orders raised")):
        entry = sales.get(key)
        if not entry:
            continue
        row = _empty_accounting_row(date_value, "Sale")
        row[_ACOL["Description"]] = label
        row[_ACOL["Amount"]] = entry["value"]
        row[_ACOL["Count"]] = entry["count"]
        if not entry["count"] and not entry["value"] and entry["notes"]:
            row[_ACOL["Status"]] = "Unconfirmed"
        rows["sales"].append(row)

    receivables = sales.get("receivables")
    if receivables:
        description = "Outstanding receivables"
        aging_match = _AGING_RE.search(receivables.get("notes", ""))
        if aging_match:
            description += f" ({aging_match.group(0)})"
        row = _empty_accounting_row(date_value, "Sale")
        row[_ACOL["Description"]] = description
        row[_ACOL["Amount"]] = receivables["amount"]
        rows["sales"].append(row)

    purchase = parsed["purchase"]
    if purchase["bills_count"] != "" or purchase["bills_value"] != "" or purchase.get("notes"):
        row = _empty_accounting_row(date_value, "Purchase")
        row[_ACOL["Description"]] = "Purchase bills booked"
        row[_ACOL["Amount"]] = purchase["bills_value"]
        row[_ACOL["Count"]] = purchase["bills_count"]
        if purchase["bills_count"] == "" and purchase["bills_value"] == "" and purchase.get("notes"):
            row[_ACOL["Status"]] = "Unconfirmed"
        rows["purchase"].append(row)

    mismatch_text = purchase.get("mismatch_text", "").strip()
    if mismatch_text and not _NO_MISMATCH_RE.match(mismatch_text):
        mismatch_body = re.sub(r"^Y\s*[-—–:]\s*", "", mismatch_text, flags=re.IGNORECASE).strip()
        entity, remaining = _extract_entity(mismatch_body)
        if entity:
            head_words = entity.split()
            while head_words and head_words[-1].upper().rstrip(".,") in _ENTITY_STOPWORDS:
                remaining = f"{head_words.pop()} {remaining}".strip()
            entity = " ".join(head_words)
        description_source = remaining if entity else mismatch_body
        row = _empty_accounting_row(date_value, "Purchase")
        row[_ACOL["Entity"]] = entity
        row[_ACOL["Description"]] = _shorten_accounting_description(description_source) or "GST/HSN mismatch"
        row[_ACOL["Status"]] = _classify_status(mismatch_text, default="Open")
        row[_ACOL["Risk / Tax Flag"]] = "GSTIN/HSN Mismatch"
        rows["purchase"].append(row)

    for item in parsed["expenses"]:
        row = _empty_accounting_row(date_value, "Expense")
        _apply_generic_item(row, item)
        rows["expenses"].append(row)

    for item in parsed["tax"]:
        row = _empty_accounting_row(date_value, "Tax")
        _apply_generic_item(row, item)
        row[_ACOL["Status"]] = item["status"] or "Open"
        rows["tax"].append(row)

    for item in parsed["pending"]:
        row = _empty_accounting_row(date_value, "Pending")
        _apply_generic_item(row, item)
        row[_ACOL["Status"]] = item["status"] or "Pending"
        rows["pending"].append(row)

    return rows

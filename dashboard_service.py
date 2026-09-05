"""Pure computation layer for the founder/CEO dashboard.

Takes the raw row data already read from the Monitoring and Accounting
tabs (via the existing sheets_service.get_tab_values - no new Sheets
access pattern) and aggregates it into the structured payload the
dashboard UI renders. Google Sheets stays the single source of truth:
nothing here writes back to Sheets, caches data beyond a single call, or
invents a row that isn't in the sheet - every number is recomputed fresh
from whatever rows are actually present each time build_dashboard() runs,
so the dashboard is always exactly as current as the sheet itself.

No LLM calls happen here on purpose, matching the rest of the project.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Rows that represent something still awaiting resolution, across both
# Monitoring's Status values and Accounting's - "Resolved" is the only
# value that means a Monitoring row is closed; Accounting has no
# "Resolved" concept in the unified schema, so any non-blank Status there
# counts as active review state.
_RESOLVED_STATUSES = {"resolved"}
_PRIORITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "": 4}

_RECENT_LIMIT = 10
_NEEDS_ATTENTION_LIMIT = 15

# "Recurring"/"repeated" literally means "happened more than once" - this
# is that plain definition, not an invented significance threshold. Every
# pattern below only fires on counts/values actually present in the data.
_RECURRING_MIN_COUNT = 2


def _to_records(rows: list[list[str]]) -> list[dict[str, str]]:
    """Turns a get_tab_values()-shaped [header, *data_rows] list into a
    list of {column_name: value} dicts, using whatever header row is
    actually in the sheet (not a hardcoded schema) - so this keeps
    working even if a column is ever added, without a code change here.
    An empty/header-only sheet returns an empty list, never a fabricated
    record.
    """
    if not rows:
        return []
    headers = rows[0]
    records = []
    for row in rows[1:]:
        if not any(cell for cell in row):
            continue
        records.append({headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))})
    return records


def _priority_rank(priority: str) -> int:
    return _PRIORITY_RANK.get(priority, 4)


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    """Chronological sort key for a "DD.MM.YYYY" Date cell.

    A plain string sort/max on DD.MM.YYYY is NOT chronological order (e.g.
    "01.01.2027" < "31.12.2026" lexicographically, which is backwards) -
    every date-based sort/max in this module must go through this instead
    of comparing the raw strings directly. A blank or malformed date sorts
    first (0, 0, 0) rather than raising, since a Sheets row is still valid
    business data even if its Date cell is somehow unreadable.
    """
    try:
        day, month, year = date_str.split(".")
        return (int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _is_open(status: str) -> bool:
    return status.strip().lower() not in _RESOLVED_STATUSES if status else True


def _parse_amount(value: Any) -> float | None:
    """Sheets always returns cell values as strings over the API, even
    for a column this project writes as a bare number - so every numeric
    read goes through this. Returns None (never a fabricated 0) for a
    blank or non-numeric cell (e.g. "Unconfirmed" left in an old Amount
    cell), so callers can skip it rather than silently counting it as 0.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_amount(value: float) -> float | int:
    return int(value) if value == int(value) else round(value, 2)


# --- Monitoring -------------------------------------------------------
# The Monitoring sheet has no Section/Type column - all four summary
# sections (Needs Attention, Actions Taken, What's Needed Next, Service
# Pattern Watch) write into the same 10 columns, and three of the four
# default their Status to "Open" (see summary_parser.build_monitoring_rows)
# since there's no better signal at write time. That means a naive
# "Status != Resolved" count blends real open issues together with
# already-performed actions and forward-looking requirements that were
# never themselves an "issue" - inflating Open Issues with rows that
# aren't open issues at all.
#
# _classify_monitoring_kind infers which section a row came from using
# ONLY fields build_monitoring_rows already writes deterministically per
# section - no new column, nothing invented:
#   - Status == "Monitoring" is Service Pattern Watch's unique default -
#     no other section ever writes that value.
#   - Action Taken is populated ONLY by the Actions Taken section.
#   - Issue and Next Action holding the IDENTICAL shortened text happens
#     ONLY in What's Needed Next (see build_monitoring_rows: `shortened`
#     is written into both columns verbatim) - Needs Attention rows split
#     the issue text and the action clause from different parts of the
#     original sentence, so they don't coincide this way in practice.
#   - Anything left is a genuine Needs Attention issue row.
_KIND_SERVICE_PATTERN_WATCH = "service_pattern_watch"
_KIND_ACTIONS_TAKEN = "actions_taken"
_KIND_WHATS_NEEDED_NEXT = "whats_needed_next"
_KIND_NEEDS_ATTENTION = "needs_attention"


def _classify_monitoring_kind(r: dict[str, str]) -> str:
    if r.get("Status") == "Monitoring":
        return _KIND_SERVICE_PATTERN_WATCH
    if r.get("Action Taken"):
        return _KIND_ACTIONS_TAKEN
    issue = r.get("Issue", "")
    if issue and issue == r.get("Next Action", ""):
        return _KIND_WHATS_NEEDED_NEXT
    return _KIND_NEEDS_ATTENTION


def _build_monitoring(records: list[dict[str, str]]) -> dict[str, Any]:
    open_records = [r for r in records if _is_open(r.get("Status", ""))]

    # Open Issues / Critical-High / Priority / Category / Sites Needing
    # Attention must only ever be computed from genuine issue rows - an
    # Actions Taken or What's Needed Next row is real information (see the
    # *_count fields below) but it is not itself an open issue.
    issue_records = [r for r in records if _classify_monitoring_kind(r) == _KIND_NEEDS_ATTENTION]
    open_issue_records = [r for r in issue_records if _is_open(r.get("Status", ""))]

    by_category = Counter(r["Category"] for r in open_issue_records if r.get("Category"))
    by_priority = Counter(r["Priority"] for r in open_issue_records if r.get("Priority"))
    critical_high = [r for r in open_issue_records if r.get("Priority") in ("Critical", "High")]

    sites_needing_attention = sorted(
        (
            {
                "date": r.get("Date", ""),
                "site": r.get("Site", ""),
                "issue": r.get("Issue", ""),
                "category": r.get("Category", ""),
                "priority": r.get("Priority", ""),
                "status": r.get("Status", ""),
                "days_open": r.get("Days Open", ""),
            }
            for r in open_issue_records
            if r.get("Site")
        ),
        key=lambda r: (_priority_rank(r["priority"]), _date_sort_key(r["date"])),
    )[:_NEEDS_ATTENTION_LIMIT]

    recent_actions = sorted(
        (
            {"date": r.get("Date", ""), "site": r.get("Site", ""), "action_taken": r.get("Action Taken", "")}
            for r in records
            if r.get("Action Taken")
        ),
        key=lambda r: _date_sort_key(r["date"]),
        reverse=True,
    )[:_RECENT_LIMIT]

    recent_next_actions = sorted(
        (
            {"date": r.get("Date", ""), "site": r.get("Site", ""), "next_action": r.get("Next Action", "")}
            for r in records
            if r.get("Next Action")
        ),
        key=lambda r: _date_sort_key(r["date"]),
        reverse=True,
    )[:_RECENT_LIMIT]

    # A trend is only meaningful with more than one distinct reporting
    # date in the sheet - a single-date snapshot has nothing to trend
    # against, so it's omitted entirely rather than shown as a flat line.
    # Scoped to genuine issue rows, same as total_open_issues above - a
    # trend labeled "open issues" must actually track open issues.
    open_issues_by_date = Counter(r["Date"] for r in open_issue_records if r.get("Date"))
    trend = (
        [
            {"date": d, "open_issues": c}
            for d, c in sorted(open_issues_by_date.items(), key=lambda item: _date_sort_key(item[0]))
        ]
        if len(open_issues_by_date) > 1
        else []
    )

    # The FULL row set (never limited/truncated, unlike the curated lists
    # above) - this is what the interactive UI filters/sorts/inspects
    # client-side. Still computed fresh from `records` on every call, and
    # still nothing but what's actually in the sheet - just reshaped to
    # plain lowercase keys matching MONITORING_HEADERS 1:1, so the client
    # never needs its own copy of the schema to render a row.
    all_records = [
        {
            "date": r.get("Date", ""),
            "site": r.get("Site", ""),
            "issue": r.get("Issue", ""),
            "category": r.get("Category", ""),
            "priority": r.get("Priority", ""),
            "status": r.get("Status", ""),
            "days_open": r.get("Days Open", ""),
            "action_taken": r.get("Action Taken", ""),
            "next_action": r.get("Next Action", ""),
            "vendor": r.get("Vendor", ""),
        }
        for r in records
    ]

    return {
        "total_rows": len(records),
        # Genuine issue rows only (see _classify_monitoring_kind above) -
        # this is what fixes "every Monitoring row counted as an open
        # issue" regardless of whether it was actually an action taken or
        # a forward-looking requirement.
        "total_open_issues": len(open_issue_records),
        "issue_records_count": len(issue_records),
        "resolved_count": len(records) - len(open_records),
        "critical_high_count": len(critical_high),
        "by_category": dict(by_category.most_common()),
        "by_priority": dict(by_priority.most_common()),
        "by_status": dict(Counter(r["Status"] for r in records if r.get("Status")).most_common()),
        "sites_needing_attention": sites_needing_attention,
        "recent_actions": recent_actions,
        "recent_next_actions": recent_next_actions,
        "open_issues_trend": trend,
        "records": all_records,
        # The other three sections, preserved as their own counts rather
        # than folded into (or dropped from) the issue metrics above.
        "actions_taken_count": sum(1 for r in records if _classify_monitoring_kind(r) == _KIND_ACTIONS_TAKEN),
        "whats_needed_next_count": sum(1 for r in records if _classify_monitoring_kind(r) == _KIND_WHATS_NEEDED_NEXT),
        "service_pattern_watch_count": sum(1 for r in records if _classify_monitoring_kind(r) == _KIND_SERVICE_PATTERN_WATCH),
    }


# --- Accounting ---------------------------------------------------------
_CASH_LABELS = ("Opening balance", "Closing balance", "Total receipts", "Total payments")


def _build_accounting(records: list[dict[str, str]]) -> dict[str, Any]:
    by_record_type = Counter(r["Record Type"] for r in records if r.get("Record Type"))

    amount_total_by_type: Counter[str] = Counter()
    for r in records:
        amount = _parse_amount(r.get("Amount"))
        if amount is not None and r.get("Record Type"):
            amount_total_by_type[r["Record Type"]] += amount
    amount_total_by_type = {k: _round_amount(v) for k, v in amount_total_by_type.items()}

    # sales_total is deliberately NOT amount_total_by_type["Sale"] - that
    # would silently blend "Outstanding receivables" (a balance still
    # owed, not sales activity) into a KPI meant to read as "value of
    # sales today". Only the two actual sales-activity sub-records count;
    # receivables get their own figure below instead of being hidden
    # inside "Sales Total".
    sales_total = _round_amount(sum(
        _parse_amount(r.get("Amount")) or 0
        for r in records
        if r.get("Record Type") == "Sale" and r.get("Description") in ("Invoices raised", "Sales orders raised")
    ))
    outstanding_receivables_total = _round_amount(sum(
        _parse_amount(r.get("Amount")) or 0
        for r in records
        if r.get("Record Type") == "Sale" and r.get("Description", "").startswith("Outstanding receivables")
    ))

    # Cash rows are upserted per (Date, Record Type, Description), so the
    # sheet holds one set of up-to-4 sub-records per date a Day Book
    # summary was processed for - the dashboard shows only the MOST
    # RECENT date's position, not a sum across every date ever processed.
    cash_rows = [r for r in records if r.get("Record Type") == "Cash"]
    latest_cash_date = max((r["Date"] for r in cash_rows if r.get("Date")), key=_date_sort_key, default="")
    cash_position = {
        label: next(
            (r.get("Amount", "") for r in cash_rows if r.get("Date") == latest_cash_date and r.get("Description") == label),
            "",
        )
        for label in _CASH_LABELS
    } if latest_cash_date else {}
    if latest_cash_date:
        cash_position["as_of"] = latest_cash_date

    tax_flags = [
        {
            "date": r.get("Date", ""),
            "entity": r.get("Entity", ""),
            "description": r.get("Description", ""),
            "flag": r.get("Risk / Tax Flag", ""),
            "status": r.get("Status", ""),
        }
        for r in records
        if r.get("Risk / Tax Flag")
    ]

    high_priority_exceptions = sorted(
        (
            {
                "date": r.get("Date", ""),
                "entity": r.get("Entity", ""),
                "description": r.get("Description", ""),
                "amount": r.get("Amount", ""),
                "priority": r.get("Priority", ""),
                "status": r.get("Status", ""),
                "recommended_action": r.get("Recommended Action", ""),
            }
            for r in records
            if r.get("Record Type") == "Exception" and (r.get("Priority") in ("Critical", "High") or _is_open(r.get("Status", "")))
        ),
        key=lambda r: (_priority_rank(r["priority"]), _date_sort_key(r["date"])),
    )[:_NEEDS_ATTENTION_LIMIT]

    # Pending From Yesterday rows, in full (not limited) - these are a
    # named metric in their own right ("Pending items"), not just another
    # Record Type count, so they get their own list the same way tax_flags
    # and high_priority_exceptions do.
    pending_items = sorted(
        (
            {
                "date": r.get("Date", ""),
                "entity": r.get("Entity", ""),
                "description": r.get("Description", ""),
                "status": r.get("Status", ""),
            }
            for r in records
            if r.get("Record Type") == "Pending"
        ),
        key=lambda r: _date_sort_key(r["date"]),
        reverse=True,
    )

    # The FULL row set (never limited/truncated) for client-side
    # filter/sort/inspect - see the matching comment in _build_monitoring.
    all_records = [
        {
            "date": r.get("Date", ""),
            "record_type": r.get("Record Type", ""),
            "entity": r.get("Entity", ""),
            "description": r.get("Description", ""),
            "amount": r.get("Amount", ""),
            "count": r.get("Count", ""),
            "priority": r.get("Priority", ""),
            "status": r.get("Status", ""),
            "recommended_action": r.get("Recommended Action", ""),
            "risk_tax_flag": r.get("Risk / Tax Flag", ""),
        }
        for r in records
    ]

    return {
        "total_rows": len(records),
        "by_record_type": dict(by_record_type.most_common()),
        "by_status": dict(Counter(r["Status"] for r in records if r.get("Status")).most_common()),
        "amount_total_by_record_type": amount_total_by_type,
        "sales_total": sales_total,
        "purchase_total": amount_total_by_type.get("Purchase", 0),
        "outstanding_receivables_total": outstanding_receivables_total,
        "cash_position": cash_position,
        "tax_flags": tax_flags,
        "high_priority_exceptions": high_priority_exceptions,
        "pending_items": pending_items,
        "records": all_records,
    }


# --- Needs Attention (cross-sheet) --------------------------------------
def _build_needs_attention(monitoring: list[dict[str, str]], accounting: list[dict[str, str]]) -> list[dict[str, Any]]:
    items = []
    for r in monitoring:
        if not _is_open(r.get("Status", "")):
            continue
        if r.get("Priority") not in ("Critical", "High") and r.get("Status") != "Unconfirmed":
            continue
        items.append({
            "source": "Monitoring",
            "date": r.get("Date", ""),
            "label": r.get("Site", "") or r.get("Issue", ""),
            "detail": r.get("Issue", ""),
            "priority": r.get("Priority", ""),
            "status": r.get("Status", ""),
            # Straight from the row's own Next Action cell - blank when the
            # sheet doesn't have one, never a made-up recommendation.
            "action": r.get("Next Action", ""),
        })
    for r in accounting:
        if r.get("Priority") not in ("Critical", "High") and r.get("Status") != "Unconfirmed":
            continue
        risk_flag = r.get("Risk / Tax Flag", "")
        # Recommended Action first; if the row has none but does carry a
        # Risk/Tax Flag, surface that flag itself as the prompt to act on -
        # still literally present in the row, never invented wording.
        action = r.get("Recommended Action", "") or (f"Review {risk_flag} flag" if risk_flag else "")
        items.append({
            "source": "Accounting",
            "date": r.get("Date", ""),
            "label": r.get("Entity", "") or r.get("Record Type", ""),
            "detail": r.get("Description", ""),
            "priority": r.get("Priority", ""),
            "status": r.get("Status", ""),
            "action": action,
        })
    items.sort(key=lambda r: (_priority_rank(r["priority"]), _date_sort_key(r["date"])))
    return items[:_NEEDS_ATTENTION_LIMIT]


# --- Recent Activity (cross-sheet) --------------------------------------
def _build_recent_activity(monitoring: list[dict[str, str]], accounting: list[dict[str, str]]) -> dict[str, Any]:
    recent_monitoring = sorted(
        (
            {
                "date": r.get("Date", ""),
                "site": r.get("Site", ""),
                "issue": r.get("Issue", ""),
                "status": r.get("Status", ""),
            }
            for r in monitoring
        ),
        key=lambda r: _date_sort_key(r["date"]),
        reverse=True,
    )[:_RECENT_LIMIT]

    recent_accounting = sorted(
        (
            {
                "date": r.get("Date", ""),
                "record_type": r.get("Record Type", ""),
                "entity": r.get("Entity", ""),
                "description": r.get("Description", ""),
                "amount": r.get("Amount", ""),
            }
            for r in accounting
        ),
        key=lambda r: _date_sort_key(r["date"]),
        reverse=True,
    )[:_RECENT_LIMIT]

    return {"monitoring": recent_monitoring, "accounting": recent_accounting}


# --- What Changed Today --------------------------------------------------
def _metric_amount(records: list[dict[str, str]], date_value: str, record_type: str, descriptions: tuple[str, ...]):
    """Sum of Amount for records matching (date, Record Type, Description
    in `descriptions`). Returns None (never a fabricated 0) when no such
    record exists for that date - "no record submitted" and "value is
    zero" are different facts, and only the sheet can tell them apart.
    """
    matching = [
        r for r in records
        if r.get("Date") == date_value and r.get("Record Type") == record_type and r.get("Description") in descriptions
    ]
    if not matching:
        return None
    return _round_amount(sum(_parse_amount(r.get("Amount")) or 0 for r in matching))


def _build_what_changed(monitoring: list[dict[str, str]], accounting: list[dict[str, str]]) -> dict[str, Any]:
    """"Today" is never the server clock - it's whatever the MOST RECENT
    date actually present in each sheet is (Monitoring and Accounting are
    tracked separately since one can be updated without the other). Every
    figure below is scoped to that date's own rows; a comparison figure
    (sales/purchase/payments change) additionally requires a second,
    earlier distinct date to exist - with only one date on record there is
    nothing to compare against, so that figure is simply absent rather
    than compared to a fabricated baseline of zero.
    """
    mon_dates = sorted({r["Date"] for r in monitoring if r.get("Date")}, key=_date_sort_key)
    acc_dates = sorted({r["Date"] for r in accounting if r.get("Date")}, key=_date_sort_key)

    monitoring_date = mon_dates[-1] if mon_dates else ""
    accounting_date = acc_dates[-1] if acc_dates else ""

    result: dict[str, Any] = {
        "monitoring_date": monitoring_date,
        "accounting_date": accounting_date,
    }

    if monitoring_date:
        today_mon = [r for r in monitoring if r.get("Date") == monitoring_date]
        # Same genuine-issue scoping as _build_monitoring's total_open_
        # issues - an Actions Taken or What's Needed Next row dated today
        # is real activity, but it is not itself a "new open issue".
        today_mon_issues = [r for r in today_mon if _classify_monitoring_kind(r) == _KIND_NEEDS_ATTENTION]
        result["new_open_issues"] = sum(1 for r in today_mon_issues if _is_open(r.get("Status", "")))
        # Resolved is deliberately NOT scoped to issue-kind rows only - an
        # Actions Taken row can itself report a resolution (e.g. "issue
        # resolved"), which is a real resolution event regardless of which
        # section it was written from.
        result["resolved_issues"] = sum(1 for r in today_mon if r.get("Status", "").strip().lower() == "resolved")
        # Only OPEN Critical/High ISSUE rows count here - a Critical issue
        # that was both reported and resolved the same day isn't something
        # still needing attention today.
        result["new_critical_high"] = sum(
            1 for r in today_mon_issues if r.get("Priority") in ("Critical", "High") and _is_open(r.get("Status", ""))
        )

    if accounting_date:
        today_acc = [r for r in accounting if r.get("Date") == accounting_date]
        result["new_accounting_exceptions"] = sum(1 for r in today_acc if r.get("Record Type") == "Exception")

    if len(acc_dates) >= 2:
        previous_date = acc_dates[-2]
        for key, record_type, descriptions in (
            ("sales", "Sale", ("Invoices raised", "Sales orders raised")),
            ("purchase", "Purchase", ("Purchase bills booked",)),
            ("payments", "Cash", ("Total payments",)),
        ):
            latest_value = _metric_amount(accounting, accounting_date, record_type, descriptions)
            previous_value = _metric_amount(accounting, previous_date, record_type, descriptions)
            if latest_value is not None and previous_value is not None:
                result[f"{key}_change"] = {
                    "latest": latest_value,
                    "previous": previous_value,
                    "previous_date": previous_date,
                    "delta": _round_amount(latest_value - previous_value),
                }

    return result


# --- Patterns & Risks ------------------------------------------------------
def _add_pattern(patterns: list[dict[str, Any]], category: str, description: str, **extra: Any) -> None:
    entry = {"category": category, "description": description}
    entry.update(extra)
    patterns.append(entry)


def _build_patterns_risks(
    monitoring: list[dict[str, str]],
    accounting: list[dict[str, str]],
    monitoring_section: dict[str, Any],
    accounting_section: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every entry here is a plain fact about the actual rows - a count
    that is literally >=2 ("recurring"), a MAX() over an actual column
    ("longest open", "largest transaction"), or a real increase between
    two actual computed trend points. Nothing here is a judgment call
    dressed up as data; there is no invented severity threshold anywhere
    in this function.
    """
    patterns: list[dict[str, Any]] = []

    # --- Monitoring: recurring groupings (count >= 2 is the plain
    # definition of "recurring"/"repeated", not a chosen significance bar).
    for site, count in Counter(r["Site"] for r in monitoring if r.get("Site")).most_common():
        if count < _RECURRING_MIN_COUNT:
            break
        _add_pattern(patterns, "Recurring Site Issue", f"{site} has {count} recorded issues", count=count)

    for category, count in Counter(r["Category"] for r in monitoring if r.get("Category")).most_common():
        if count < _RECURRING_MIN_COUNT:
            break
        _add_pattern(patterns, "Repeated Equipment Issue", f"{category} issues recorded {count} times", count=count)

    for vendor, count in Counter(r["Vendor"] for r in monitoring if r.get("Vendor")).most_common():
        if count < _RECURRING_MIN_COUNT:
            break
        _add_pattern(patterns, "Vendor Pattern", f"{vendor} involved in {count} recorded issues", count=count)

    # --- Monitoring: the longest currently-open issue(s) - a factual MAX,
    # not a fixed "more than N days" cutoff.
    open_with_days = [
        (r, _parse_amount(r.get("Days Open")))
        for r in monitoring
        if _is_open(r.get("Status", ""))
    ]
    open_with_days = [(r, d) for r, d in open_with_days if d is not None]
    if open_with_days:
        max_days = max(d for _, d in open_with_days)
        max_days_display = int(max_days) if max_days == int(max_days) else max_days
        for r, d in open_with_days:
            if d == max_days:
                label = r.get("Site") or r.get("Issue") or "(unlabeled record)"
                _add_pattern(
                    patterns, "Long-Open Issue",
                    f"{label} has been open {max_days_display} day(s) - the longest currently open",
                    days_open=max_days_display,
                )

    # --- Monitoring: rising open-issue volume, straight from the same
    # trend already computed for the chart - only the most recent step.
    trend = monitoring_section.get("open_issues_trend") or []
    if len(trend) >= 2 and trend[-1]["open_issues"] > trend[-2]["open_issues"]:
        _add_pattern(
            patterns, "Rising Open Issues",
            f"Open issues rose from {trend[-2]['open_issues']} to {trend[-1]['open_issues']} "
            f"between {trend[-2]['date']} and {trend[-1]['date']}",
        )

    # --- Accounting: recurring expense descriptions / repeated entities.
    expense_counts = Counter(
        r["Description"] for r in accounting if r.get("Record Type") == "Expense" and r.get("Description")
    )
    for description, count in expense_counts.most_common():
        if count < _RECURRING_MIN_COUNT:
            break
        _add_pattern(patterns, "Repeated Expense", f'"{description}" recorded {count} times', count=count)

    entity_counts = Counter(
        r["Entity"] for r in accounting if r.get("Record Type") in ("Exception", "Pending") and r.get("Entity")
    )
    for entity, count in entity_counts.most_common():
        if count < _RECURRING_MIN_COUNT:
            break
        _add_pattern(patterns, "Repeated Entity Issue", f"{entity} appears in {count} exception/pending records", count=count)

    # --- Accounting: tax/risk flags, reusing the already-computed list.
    flag_counts = Counter(t["flag"] for t in accounting_section.get("tax_flags", []) if t.get("flag"))
    for flag, count in flag_counts.most_common():
        _add_pattern(patterns, "Tax/Risk Flag", f"{count} record(s) flagged {flag}", count=count)

    # --- Accounting: the single largest recorded transaction - a factual
    # MAX over whatever amounts are actually present, not an arbitrary
    # "large transaction" cutoff. Restricted to actual transaction-type
    # records (Sale/Purchase/Expense/Exception) - a Cash balance/receipts
    # figure is a snapshot or aggregate, not itself "a transaction".
    _TRANSACTION_TYPES = ("Sale", "Purchase", "Expense", "Exception")
    amounts = [(r, _parse_amount(r.get("Amount"))) for r in accounting if r.get("Record Type") in _TRANSACTION_TYPES]
    amounts = [(r, a) for r, a in amounts if a is not None and a > 0]
    # "Largest" only means something relative to at least one other
    # transaction - with a single data point, calling it "the largest" is
    # trivially true and not an actual pattern.
    if len(amounts) >= _RECURRING_MIN_COUNT:
        max_amount = max(a for _, a in amounts)
        for r, a in amounts:
            if a == max_amount:
                label = r.get("Entity") or r.get("Description") or r.get("Record Type") or "(unlabeled record)"
                _add_pattern(
                    patterns, "Large Transaction",
                    f"{label} is the largest recorded transaction",
                    amount=_round_amount(max_amount),
                )

    # --- Accounting: pending backlog, reusing the already-computed list.
    pending_items = accounting_section.get("pending_items", [])
    if pending_items:
        _add_pattern(
            patterns, "Pending Items",
            f"{len(pending_items)} item(s) still pending from previous reports",
            count=len(pending_items),
        )

    return patterns


# --- Required Actions -------------------------------------------------
def _build_required_actions(monitoring: list[dict[str, str]], accounting: list[dict[str, str]]) -> list[dict[str, Any]]:
    """A concise, deduplicated action list - each entry's action text comes
    directly from that row's own Next Action / Recommended Action / Risk
    Tax Flag cell; the only non-verbatim text is the generic "Review - X
    priority" prompt, used only when a Critical/High record has none of
    those fields filled in (so it still surfaces, but never with an
    invented specific action). Closed/Resolved rows are excluded - nothing
    to act on there regardless of what fields they carry.
    """
    actions: list[dict[str, Any]] = []

    for r in monitoring:
        if not _is_open(r.get("Status", "")):
            continue
        next_action = r.get("Next Action", "")
        priority = r.get("Priority", "")
        if not next_action and priority not in ("Critical", "High"):
            continue
        actions.append({
            "source": "Monitoring",
            "date": r.get("Date", ""),
            "label": r.get("Site", "") or r.get("Issue", ""),
            "action": next_action or f"Review — {priority} priority",
            "priority": priority,
            "status": r.get("Status", ""),
            # Straight from the row's own Vendor cell - the only field in
            # the schema that names a responsible external party. Blank
            # when the sheet doesn't have one; never invented.
            "vendor": r.get("Vendor", ""),
        })

    for r in accounting:
        if not _is_open(r.get("Status", "")):
            continue
        recommended = r.get("Recommended Action", "")
        risk_flag = r.get("Risk / Tax Flag", "")
        priority = r.get("Priority", "")
        if not recommended and not risk_flag and priority not in ("Critical", "High"):
            continue
        action_text = recommended or (f"Review {risk_flag} flag" if risk_flag else f"Review — {priority} priority")
        actions.append({
            "source": "Accounting",
            "date": r.get("Date", ""),
            "label": r.get("Entity", "") or r.get("Record Type", ""),
            "action": action_text,
            "priority": priority,
            "status": r.get("Status", ""),
        })

    actions.sort(key=lambda a: (_priority_rank(a["priority"]), _date_sort_key(a["date"])))
    return actions[:_NEEDS_ATTENTION_LIMIT]


def build_dashboard(monitoring_rows: list[list[str]], accounting_rows: list[list[str]]) -> dict[str, Any]:
    """Build the full dashboard payload from raw Monitoring/Accounting
    sheet data (each including its header row, exactly as
    sheets_service.get_tab_values returns it).

    Returns:
        {
            "overview": {...},
            "monitoring": {...},
            "accounting": {...},
            "needs_attention": [...],
            "recent_activity": {"monitoring": [...], "accounting": [...]},
            "what_changed": {...},
            "patterns_risks": [...],
            "required_actions": [...],
        }

    An empty tab (no data rows) produces zeroed/empty sections, never a
    fabricated sample row.
    """
    monitoring = _to_records(monitoring_rows)
    accounting = _to_records(accounting_rows)

    monitoring_section = _build_monitoring(monitoring)
    accounting_section = _build_accounting(accounting)
    needs_attention = _build_needs_attention(monitoring, accounting)
    recent_activity = _build_recent_activity(monitoring, accounting)
    what_changed = _build_what_changed(monitoring, accounting)
    patterns_risks = _build_patterns_risks(monitoring, accounting, monitoring_section, accounting_section)
    required_actions = _build_required_actions(monitoring, accounting)

    # "Total payments" already exists per-cell inside cash_position (a raw
    # sheet string, for the most recent Cash date) - this is that same
    # figure, just parsed to a number and promoted to a top-level Executive
    # Overview KPI. None (not 0) when there's no Cash data to read it from.
    _payments_raw = _parse_amount((accounting_section.get("cash_position") or {}).get("Total payments"))
    payments_total = _round_amount(_payments_raw) if _payments_raw is not None else None

    overview = {
        "total_open_issues": monitoring_section["total_open_issues"],
        "critical_high_issues": (
            monitoring_section["critical_high_count"]
            + sum(1 for r in accounting_section["high_priority_exceptions"] if r["priority"] in ("Critical", "High"))
        ),
        # None (not 0) when Monitoring has no dated rows at all, i.e. there
        # is no "today" to have resolved anything on - same underlying
        # count _build_what_changed already computed, just also promoted
        # to the top-level KPI strip.
        "resolved_today": what_changed.get("resolved_issues"),
        # Whether Accounting has ANY rows at all - the only reliable way to
        # tell "genuine zero sales/purchases" apart from "no Day Book
        # Summary has been processed yet", since a sum over zero rows and
        # a sum of real zero-value rows both come out to the same 0.
        "has_accounting_data": accounting_section["total_rows"] > 0,
        "accounting_exceptions": accounting_section["by_record_type"].get("Exception", 0),
        "sales_total": accounting_section["sales_total"],
        "purchase_total": accounting_section["purchase_total"],
        "payments_total": payments_total,
        "outstanding_receivables_total": accounting_section["outstanding_receivables_total"],
        "cash_position": accounting_section["cash_position"],
        "needs_attention_count": len(needs_attention),
        "monitoring_row_count": monitoring_section["total_rows"],
        "accounting_row_count": accounting_section["total_rows"],
    }

    return {
        "overview": overview,
        "monitoring": monitoring_section,
        "accounting": accounting_section,
        "needs_attention": needs_attention,
        "recent_activity": recent_activity,
        "what_changed": what_changed,
        "patterns_risks": patterns_risks,
        "required_actions": required_actions,
    }

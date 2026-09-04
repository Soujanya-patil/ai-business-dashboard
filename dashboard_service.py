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
def _build_monitoring(records: list[dict[str, str]]) -> dict[str, Any]:
    open_records = [r for r in records if _is_open(r.get("Status", ""))]

    by_category = Counter(r["Category"] for r in open_records if r.get("Category"))
    by_priority = Counter(r["Priority"] for r in open_records if r.get("Priority"))
    critical_high = [r for r in open_records if r.get("Priority") in ("Critical", "High")]

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
            for r in open_records
            if r.get("Site")
        ),
        key=lambda r: (_priority_rank(r["priority"]), r["date"]),
    )[:_NEEDS_ATTENTION_LIMIT]

    recent_actions = sorted(
        (
            {"date": r.get("Date", ""), "site": r.get("Site", ""), "action_taken": r.get("Action Taken", "")}
            for r in records
            if r.get("Action Taken")
        ),
        key=lambda r: r["date"],
        reverse=True,
    )[:_RECENT_LIMIT]

    recent_next_actions = sorted(
        (
            {"date": r.get("Date", ""), "site": r.get("Site", ""), "next_action": r.get("Next Action", "")}
            for r in records
            if r.get("Next Action")
        ),
        key=lambda r: r["date"],
        reverse=True,
    )[:_RECENT_LIMIT]

    # A trend is only meaningful with more than one distinct reporting
    # date in the sheet - a single-date snapshot has nothing to trend
    # against, so it's omitted entirely rather than shown as a flat line.
    open_issues_by_date = Counter(r["Date"] for r in open_records if r.get("Date"))
    trend = (
        [{"date": d, "open_issues": c} for d, c in sorted(open_issues_by_date.items())]
        if len(open_issues_by_date) > 1
        else []
    )

    return {
        "total_rows": len(records),
        "total_open_issues": len(open_records),
        "critical_high_count": len(critical_high),
        "by_category": dict(by_category.most_common()),
        "by_priority": dict(by_priority.most_common()),
        "sites_needing_attention": sites_needing_attention,
        "recent_actions": recent_actions,
        "recent_next_actions": recent_next_actions,
        "open_issues_trend": trend,
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
    latest_cash_date = max((r["Date"] for r in cash_rows if r.get("Date")), default="")
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
        key=lambda r: (_priority_rank(r["priority"]), r["date"]),
    )[:_NEEDS_ATTENTION_LIMIT]

    return {
        "total_rows": len(records),
        "by_record_type": dict(by_record_type.most_common()),
        "amount_total_by_record_type": amount_total_by_type,
        "sales_total": sales_total,
        "purchase_total": amount_total_by_type.get("Purchase", 0),
        "outstanding_receivables_total": outstanding_receivables_total,
        "cash_position": cash_position,
        "tax_flags": tax_flags,
        "high_priority_exceptions": high_priority_exceptions,
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
        })
    for r in accounting:
        if r.get("Priority") not in ("Critical", "High") and r.get("Status") != "Unconfirmed":
            continue
        items.append({
            "source": "Accounting",
            "date": r.get("Date", ""),
            "label": r.get("Entity", "") or r.get("Record Type", ""),
            "detail": r.get("Description", ""),
            "priority": r.get("Priority", ""),
            "status": r.get("Status", ""),
        })
    items.sort(key=lambda r: (_priority_rank(r["priority"]), r["date"]))
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
        key=lambda r: r["date"],
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
        key=lambda r: r["date"],
        reverse=True,
    )[:_RECENT_LIMIT]

    return {"monitoring": recent_monitoring, "accounting": recent_accounting}


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

    overview = {
        "total_open_issues": monitoring_section["total_open_issues"],
        "critical_high_issues": (
            monitoring_section["critical_high_count"]
            + sum(1 for r in accounting_section["high_priority_exceptions"] if r["priority"] in ("Critical", "High"))
        ),
        "accounting_exceptions": accounting_section["by_record_type"].get("Exception", 0),
        "sales_total": accounting_section["sales_total"],
        "purchase_total": accounting_section["purchase_total"],
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
    }

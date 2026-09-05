"""Local tests for the AI Business Dashboard (get_business_dashboard tool
+ dashboard_service.py) - no Claude Pro required.

Run with:
    python test_dashboard.py

Covers three layers:
  A. dashboard_service.build_dashboard() - the actual calculation logic -
     against synthetic in-memory rows shaped exactly like
     sheets_service.get_tab_values() output (header row + data rows).
     Fully deterministic, no network access.
  B. MCP Apps wiring - get_business_dashboard is registered with the
     correct _meta.ui.resourceUri, and the ui://dashboard/app.html
     resource reads back as valid text/html;profile=mcp-app content.
  C. A live-Sheets check that reuses the exact same get_tab_values() calls
     get_business_dashboard makes internally, then feeds them through
     build_dashboard() - proves the calculation logic doesn't crash
     against whatever is actually in the sheet right now, without needing
     a full simulated MCP Apps client session (which would exercise the
     SDK's own protocol handling, not this project's code - the tool
     itself degrades gracefully for a non-Apps client via
     client_supports_apps(), which is a client_capabilities check the SDK
     owns, not something this project needs to re-verify).

Never writes to Sheets - this is a read-only feature, so there is nothing
to clean up afterward and no synthetic-date/run_id tagging is needed.

Dates: every Date cell is DD.MM.YYYY (e.g. "03.09.2026"), never ISO -
Test 20 specifically checks that date-based sorting/max (trend order,
"most recent" cash position, recent-activity order) is truly chronological
rather than a lexicographic string sort, which is NOT the same thing for
DD.MM.YYYY (e.g. "03.01.2027" < "28.12.2026" as plain strings, backwards).

Interactive dashboard (Tests 21-25): dashboard_app.html now renders as
three tabs (Overview/Monitoring/Accounting) with client-side filtering,
sortable columns, and click-to-inspect row detail, all driven purely from
the structuredContent this file's build_dashboard() already computes - no
new Sheets access pattern, no server-side filtering endpoint, no second
data store. Tests 21-23 check the new server-side fields those features
read (full, never-truncated "records" lists, "pending_items",
"resolved_count", "by_status"); Test 24 checks the HTML source for the new
structural/empty-state pieces. The actual interactive DOM behavior (tab
switching, filter/sort/click, both with an empty and a populated dataset)
was additionally verified with a one-off jsdom-based render harness run
outside this suite (jsdom is not a project dependency and is not required
to run these tests).

Row-kind correctness (Tests 38-45): the Monitoring sheet has no Section/
Type column - Needs Attention, Actions Taken, What's Needed Next, and
Service Pattern Watch all write into the same 10 columns, and three of
the four default their Status to "Open" (see summary_parser.
build_monitoring_rows). A naive "Status != Resolved" count therefore
blends real open issues together with already-performed actions and
forward-looking requirements. dashboard_service._classify_monitoring_kind
tells them apart using only fields already written deterministically per
section (Status == "Monitoring" for Service Pattern Watch, a populated
Action Taken for Actions Taken, Issue == Next Action for What's Needed
Next) - Tests 38-41 reproduce the real bug report's exact shape (39 total
rows, only 10 of them genuine issues) and check every angle of that
distinction. Tests 42-43 check the has_accounting_data distinction (no
rows vs. real computed zero) all the way through to both dashboard_app.
html and mcp_server._dashboard_summary_lines. Tests 44-45 check
_dashboard_summary_lines' date-honesty behavior directly - it's a pure
function extracted from get_business_dashboard specifically so it's
testable without a live MCP session (client_supports_apps needs a real
ctx that a test can't construct); get_business_dashboard's actual
behavior is unchanged, this is a pure refactor.

Owner-level dashboard (Tests 26-37): "today" is always the most recent
DATE ACTUALLY PRESENT in each sheet's own rows, never the server clock -
_build_what_changed derives Monitoring's and Accounting's "today" dates
independently (they can differ) and only computes a sales/purchase/
payments comparison when a second, earlier distinct Accounting date also
exists to compare against. Patterns & Risks and Required Actions are
built entirely from real counts/values already in the rows (a MAX() for
"longest open"/"largest transaction", a literal >=2 for "recurring") -
never an invented severity threshold. The owner-view tables in
dashboard_app.html show only 5-6 of the 10 raw columns; the full 10 remain
one click away via the existing detail panel, and the underlying Sheet
schema is completely unaffected (see Test 33, which checks the HTML source
for both the reduced table columns and the still-present full detail
fields).
"""

import asyncio

from dashboard_service import build_dashboard
from summary_parser import MONITORING_HEADERS
from accounting_parser import ACCOUNTING_HEADERS

MON_HEADER = MONITORING_HEADERS
ACC_HEADER = ACCOUNTING_HEADERS

# A small but representative synthetic Monitoring dataset: two dates, a
# resolved issue, an unconfirmed one, a couple of open Critical/High
# issues, an Action Taken, and a Next Action - enough to exercise every
# computed field in _build_monitoring without needing live Sheets access.
SYNTHETIC_MONITORING = [MON_HEADER] + [
    ["01.09.2026", "Site A", "Inverter outage", "Outage", "Critical", "Open", "3", "", "Priority escalation", ""],
    ["01.09.2026", "Site B", "Optimizer failure", "Optimizer", "High", "Open", "1", "", "", "Vendor X"],
    ["02.09.2026", "Site A", "Inverter outage", "Outage", "Critical", "Resolved", "4", "Replaced inverter", "", ""],
    ["02.09.2026", "Site C", "Not reporting", "Monitoring", "", "Unconfirmed", "", "", "", ""],
    ["02.09.2026", "Site D", "Minor GST mismatch", "Vendor", "Low", "Open", "", "", "Verify details", ""],
]

# A small representative synthetic Accounting dataset covering Cash (two
# dates, to prove "most recent date only"), Sale, Purchase, an Exception,
# and a Tax row with a Risk/Tax Flag.
SYNTHETIC_ACCOUNTING = [ACC_HEADER] + [
    ["01.09.2026", "Cash", "", "Opening balance", "100000", "", "", "", "", ""],
    ["01.09.2026", "Cash", "", "Closing balance", "90000", "", "", "", "", ""],
    ["02.09.2026", "Cash", "", "Opening balance", "90000", "", "", "", "", ""],
    ["02.09.2026", "Cash", "", "Closing balance", "120000", "", "", "", "", ""],
    ["01.09.2026", "Sale", "", "Invoices raised", "50000", "5", "", "", "", ""],
    ["01.09.2026", "Sale", "", "Outstanding receivables (over 60 days)", "15000", "", "", "", "", ""],
    ["01.09.2026", "Purchase", "", "Purchase bills booked", "20000", "2", "", "", "", ""],
    ["01.09.2026", "Exception", "ABC Traders", "GST mismatch", "12000", "", "Critical", "Open", "Verify with vendor", "GST"],
    ["02.09.2026", "Tax", "XYZ Ltd", "ITC discrepancy", "5000", "", "", "Unconfirmed", "Confirm with CA", "ITC"],
]


if __name__ == "__main__":
    print("=== Test 1: build_dashboard() returns the expected top-level shape ===")
    dashboard = build_dashboard(SYNTHETIC_MONITORING, SYNTHETIC_ACCOUNTING)
    assert set(dashboard.keys()) == {
        "overview", "monitoring", "accounting", "needs_attention", "recent_activity",
        "what_changed", "patterns_risks", "required_actions",
    }
    print("OK\n")

    print("=== Test 2: Monitoring open-issue counting excludes Resolved, includes Unconfirmed ===")
    m = dashboard["monitoring"]
    # 5 rows total, 1 Resolved -> 4 open (Critical Open, High Open, Unconfirmed, Low Open)
    assert m["total_open_issues"] == 4, m["total_open_issues"]
    assert m["total_rows"] == 5
    print("OK\n")

    print("=== Test 3: priority/category breakdowns only count OPEN rows ===")
    assert m["by_priority"] == {"Critical": 1, "High": 1, "Low": 1}, m["by_priority"]
    assert "Outage" in m["by_category"] and m["by_category"]["Outage"] == 1, (
        "The Resolved Site A row must not be double-counted into the Outage category"
    )
    print("OK\n")

    print("=== Test 4: sites needing attention sorted by priority rank, Resolved excluded ===")
    sites = m["sites_needing_attention"]
    site_names = [s["site"] for s in sites]
    assert "Site A" in site_names  # the OPEN Site A row (01.09.2026), not the Resolved one
    assert site_names[0] == "Site A" and sites[0]["priority"] == "Critical", (
        "Critical priority must sort first"
    )
    assert len([s for s in sites if s["status"] == "Resolved"]) == 0, "Resolved rows must never appear here"
    print("OK\n")

    print("=== Test 5: recent actions / next actions only include rows with real content ===")
    assert len(m["recent_actions"]) == 1 and m["recent_actions"][0]["action_taken"] == "Replaced inverter"
    assert len(m["recent_next_actions"]) == 2, m["recent_next_actions"]
    print("OK\n")

    print("=== Test 6: open-issues trend appears with 2+ distinct dates, in date order ===")
    trend = m["open_issues_trend"]
    assert [t["date"] for t in trend] == ["01.09.2026", "02.09.2026"], trend
    print("OK\n")

    print("=== Test 7: a single-date dataset produces NO trend (would be misleading) ===")
    single_date_dashboard = build_dashboard(
        [MON_HEADER, ["01.09.2026", "Site A", "Issue", "Outage", "Critical", "Open", "", "", "", ""]],
        [ACC_HEADER],
    )
    assert single_date_dashboard["monitoring"]["open_issues_trend"] == [], (
        "A single reporting date must not produce a fabricated 'trend'"
    )
    print("OK\n")

    print("=== Test 8: Accounting amounts are summed as real numbers, by Record Type ===")
    a = dashboard["accounting"]
    assert a["by_record_type"] == {"Cash": 4, "Sale": 2, "Purchase": 1, "Exception": 1, "Tax": 1}, a["by_record_type"]
    assert a["sales_total"] == 50000, (
        "sales_total must count only actual sales activity (Invoices/Sales orders raised), "
        f"not Outstanding receivables - got {a['sales_total']}"
    )
    assert a["outstanding_receivables_total"] == 15000, a["outstanding_receivables_total"]
    assert a["purchase_total"] == 20000
    print("OK: sales_total correctly excludes Outstanding Receivables (a balance, not sales activity).\n")

    print("=== Test 9: Cash position reflects only the MOST RECENT date, not a cross-date sum ===")
    cash = a["cash_position"]
    assert cash["as_of"] == "02.09.2026", cash
    assert cash["Opening balance"] == "90000" and cash["Closing balance"] == "120000", cash
    print("OK\n")

    print("=== Test 10: tax/GST flags and high-priority exceptions are surfaced correctly ===")
    # Both the GST-flagged Exception (ABC Traders) and the ITC-flagged Tax
    # row (XYZ Ltd) carry a Risk/Tax Flag - tax_flags surfaces any row
    # with one, regardless of Record Type.
    flags_by_entity = {f["entity"]: f["flag"] for f in a["tax_flags"]}
    assert flags_by_entity == {"ABC Traders": "GST", "XYZ Ltd": "ITC"}, flags_by_entity
    assert len(a["high_priority_exceptions"]) == 1
    assert a["high_priority_exceptions"][0]["entity"] == "ABC Traders"
    assert a["high_priority_exceptions"][0]["amount"] == "12000"
    print("OK\n")

    print("=== Test 11: Needs Attention merges both sheets, Critical/High/Unconfirmed only, sorted ===")
    na = dashboard["needs_attention"]
    sources = {item["source"] for item in na}
    assert sources == {"Monitoring", "Accounting"}
    assert na[0]["priority"] == "Critical", "Highest priority must sort first across both sheets"
    assert not any(item["priority"] == "Low" for item in na), "A bare Low-priority Open row must not appear"
    print(f"needs_attention: {len(na)} items across both sheets\n")

    print("=== Test 12: Overview KPIs match the section-level numbers (no double-fabrication) ===")
    o = dashboard["overview"]
    assert o["total_open_issues"] == m["total_open_issues"]
    assert o["accounting_exceptions"] == 1
    assert o["monitoring_row_count"] == 5 and o["accounting_row_count"] == 9
    assert o["outstanding_receivables_total"] == 15000
    print("OK\n")

    print("=== Test 13: Recent Activity returns both sheets, most-recent-first ===")
    ra = dashboard["recent_activity"]
    assert ra["monitoring"][0]["date"] == "02.09.2026"
    assert ra["accounting"][0]["date"] == "02.09.2026"
    print("OK\n")

    print("=== Test 14: an empty/header-only sheet produces zeroed sections, never fabricated data ===")
    empty_dashboard = build_dashboard([MON_HEADER], [ACC_HEADER])
    assert empty_dashboard["overview"]["total_open_issues"] == 0
    assert empty_dashboard["monitoring"]["sites_needing_attention"] == []
    assert empty_dashboard["accounting"]["cash_position"] == {}
    assert empty_dashboard["needs_attention"] == []
    print("OK\n")

    print("=== Test 15: a fully empty tab (no header at all) does not crash ===")
    also_empty = build_dashboard([], [])
    assert also_empty["overview"]["monitoring_row_count"] == 0
    print("OK\n")

    print("=== Test 16: get_business_dashboard is registered as an MCP Apps tool ===")
    import mcp_server as srv

    tools = asyncio.run(srv.mcp.list_tools())
    dashboard_tool = next((t for t in tools if t.name == "get_business_dashboard"), None)
    assert dashboard_tool is not None, "get_business_dashboard must be a registered MCP tool"
    assert dashboard_tool.meta is not None and dashboard_tool.meta.get("ui", {}).get("resourceUri") == "ui://dashboard/app.html", (
        f"Expected _meta.ui.resourceUri to bind the dashboard resource, got: {dashboard_tool.meta}"
    )
    # The 3 pre-existing tools must still be registered, untouched, alongside it.
    tool_names = {t.name for t in tools}
    assert {"test_connection", "process_monitoring_summary", "process_accounting_summary"} <= tool_names, tool_names
    print(f"OK: 4 tools registered ({sorted(tool_names)}), dashboard tool correctly UI-bound.\n")

    print("=== Test 17: the ui:// dashboard resource reads back as valid MCP-App HTML ===")
    resources = asyncio.run(srv.mcp.list_resources())
    dashboard_resource = next((r for r in resources if str(r.uri) == "ui://dashboard/app.html"), None)
    assert dashboard_resource is not None, "ui://dashboard/app.html must be registered"
    assert dashboard_resource.mime_type == "text/html;profile=mcp-app", dashboard_resource.mime_type
    read_result = asyncio.run(srv.mcp.read_resource("ui://dashboard/app.html"))
    html_text = read_result[0].content
    assert "<html>" in html_text and "ui/initialize" in html_text and "get_business_dashboard" in html_text, (
        "Dashboard HTML must reference the MCP Apps handshake and the correct tool name for its Refresh button"
    )
    print("OK: resource is valid HTML and references the correct protocol handshake + tool name.\n")

    print("=== Test 18: existing tools are still callable, unaffected by the Apps extension ===")
    result = asyncio.run(srv.mcp.call_tool("test_connection", {"message": "dashboard-phase-check"}))
    assert not result.is_error
    assert "dashboard-phase-check" in result.content[0].text
    print("OK: test_connection still works exactly as before.\n")

    print("=== Test 19: dashboard calculations against the ACTUAL current Google Sheets data ===")
    # Reuses the exact same service/SPREADSHEET_ID/tab names and
    # get_tab_values() call get_business_dashboard makes internally - this
    # is a read-only check (never writes anything), so it's safe to run
    # against the real sheet regardless of what's currently in it.
    from sheets_service import get_tab_values

    live_monitoring = get_tab_values(srv.service, srv.SPREADSHEET_ID, srv.MONITORING_TAB)
    live_accounting = get_tab_values(srv.service, srv.SPREADSHEET_ID, srv.ACCOUNTING_TAB)
    live_dashboard = build_dashboard(live_monitoring, live_accounting)
    assert live_dashboard["overview"]["monitoring_row_count"] == max(0, len(live_monitoring) - 1)
    assert live_dashboard["overview"]["accounting_row_count"] == max(0, len(live_accounting) - 1)
    print(
        f"Live sheet check OK: {live_dashboard['overview']['monitoring_row_count']} Monitoring row(s), "
        f"{live_dashboard['overview']['accounting_row_count']} Accounting row(s) - "
        "computed without error and without writing anything back.\n"
    )

    print("=== Test 20: date sorting is chronological (DD.MM.YYYY), not a lexicographic string sort ===")
    # "03.01.2027" < "28.12.2026" as plain strings (wrong order) but
    # 2027-01-03 is chronologically AFTER 2026-12-28 (right order) - this
    # is exactly the case a naive string sort/max gets backwards.
    year_boundary_monitoring = [MON_HEADER] + [
        ["28.12.2026", "Site A", "Issue A", "Outage", "Critical", "Open", "1", "", "", ""],
        ["03.01.2027", "Site B", "Issue B", "Outage", "Critical", "Open", "1", "", "", ""],
    ]
    year_boundary_accounting = [ACC_HEADER] + [
        ["28.12.2026", "Cash", "", "Closing balance", "10000", "", "", "", "", ""],
        ["03.01.2027", "Cash", "", "Closing balance", "99999", "", "", "", "", ""],
    ]
    yb_dashboard = build_dashboard(year_boundary_monitoring, year_boundary_accounting)
    yb_trend = yb_dashboard["monitoring"]["open_issues_trend"]
    assert [t["date"] for t in yb_trend] == ["28.12.2026", "03.01.2027"], (
        f"Trend must be in true chronological order, got: {yb_trend}"
    )
    yb_cash = yb_dashboard["accounting"]["cash_position"]
    assert yb_cash["as_of"] == "03.01.2027", (
        f"Most-recent-date cash position must pick the chronologically latest date, got: {yb_cash}"
    )
    assert yb_cash["Closing balance"] == "99999", yb_cash
    yb_recent = yb_dashboard["recent_activity"]["monitoring"]
    assert yb_recent[0]["date"] == "03.01.2027", (
        f"Recent Activity must list the chronologically latest date first, got: {yb_recent}"
    )
    print("OK: trend, cash 'as of' date, and Recent Activity all order by true chronology across a year boundary.\n")

    print("=== Test 21: monitoring/accounting 'records' carry every row, in the shape the interactive UI reads ===")
    assert len(m["records"]) == m["total_rows"] == 5, m["records"]
    assert len(a["records"]) == a["total_rows"] == 9, a["records"]
    rec0 = m["records"][0]
    assert set(rec0) == {"date", "site", "issue", "category", "priority", "status", "days_open", "action_taken", "next_action", "vendor"}, rec0
    arec0 = a["records"][0]
    assert set(arec0) == {"date", "record_type", "entity", "description", "amount", "count", "priority", "status", "recommended_action", "risk_tax_flag"}, arec0
    print("OK: 'records' present on both sections with plain lowercase keys covering every schema column.\n")

    print("=== Test 22: 'records' is NEVER truncated, unlike the curated needs-attention/exception lists (limit=15) ===")
    big_monitoring = [MON_HEADER] + [
        ["01.09.2026", "Site " + str(i), "Issue " + str(i), "Outage", "Critical", "Open", "1", "", "", ""]
        for i in range(20)
    ]
    big_accounting = [ACC_HEADER] + [
        ["01.09.2026", "Exception", "Entity " + str(i), "Issue " + str(i), str(1000 + i), "", "Critical", "Open", "", "GST"]
        for i in range(20)
    ]
    big_dashboard = build_dashboard(big_monitoring, big_accounting)
    assert len(big_dashboard["monitoring"]["records"]) == 20, len(big_dashboard["monitoring"]["records"])
    assert len(big_dashboard["monitoring"]["sites_needing_attention"]) == 15, "curated list must still be capped at 15"
    assert len(big_dashboard["accounting"]["records"]) == 20, len(big_dashboard["accounting"]["records"])
    assert len(big_dashboard["accounting"]["high_priority_exceptions"]) == 15, "curated list must still be capped at 15"
    print("OK: 'records' returned all 20 rows in both sections while the curated lists stayed capped at 15.\n")

    print("=== Test 23: resolved_count, by_status, and pending_items are computed correctly ===")
    assert m["resolved_count"] == 1, m["resolved_count"]  # the one Resolved row out of 5
    assert m["by_status"] == {"Open": 3, "Resolved": 1, "Unconfirmed": 1}, m["by_status"]
    pending_dashboard = build_dashboard(
        [MON_HEADER],
        [ACC_HEADER] + [
            ["01.09.2026", "Pending", "Vendor A", "Follow-up needed", "", "", "", "Pending", "", ""],
            ["02.09.2026", "Pending", "", "Bank reconciliation pending", "", "", "", "Pending", "", ""],
        ],
    )
    assert len(pending_dashboard["accounting"]["pending_items"]) == 2, pending_dashboard["accounting"]["pending_items"]
    assert pending_dashboard["accounting"]["pending_items"][0]["date"] == "02.09.2026", (
        "pending_items must be most-recent-first"
    )
    print("OK: resolved_count/by_status match the synthetic Monitoring set; pending_items lists every Pending row, newest first.\n")

    print("=== Test 24: dashboard_app.html contains the new interactive structure and empty-state copy ===")
    assert 'id="tabbar"' in html_text and 'data-tab="monitoring"' in html_text and 'data-tab="accounting"' in html_text, (
        "Expected the Overview/Monitoring/Accounting tab bar in the HTML"
    )
    assert "No monitoring data available yet." in html_text, "Expected the exact required Monitoring empty-state message"
    assert "No accounting data available yet." in html_text, "Expected the exact required Accounting empty-state message"
    for hook in ("data-filter=", "data-sort-col=", "data-row-idx=", "detail-grid"):
        assert hook in html_text, f"Expected filter/sort/detail-inspect hook {hook!r} in the HTML"
    print("OK: tab bar, both required empty-state strings, and the filter/sort/detail-inspect hooks are all present.\n")

    print("=== Test 25: an empty dashboard's 'records'/'pending_items' are empty lists, never omitted or fabricated ===")
    assert empty_dashboard["monitoring"]["records"] == []
    assert empty_dashboard["accounting"]["records"] == []
    assert empty_dashboard["accounting"]["pending_items"] == []
    print("OK\n")

    print("=== Test 26: Needs Attention items carry an 'action' field straight from Next/Recommended Action ===")
    mon_attn = next(item for item in na if item["source"] == "Monitoring")
    assert mon_attn["action"] == "Priority escalation", mon_attn  # Site A's Next Action cell, verbatim
    acc_attn = next(item for item in na if item["source"] == "Accounting")
    assert acc_attn["action"] == "Verify with vendor", acc_attn  # ABC Traders' Recommended Action cell, verbatim
    # A row with a Risk/Tax Flag but no Recommended Action falls back to
    # naming that flag - still literally present in the row, not invented.
    flag_only_dashboard = build_dashboard(
        [MON_HEADER],
        [ACC_HEADER, ["01.09.2026", "Exception", "Flagged Co", "Mismatch found", "9000", "", "High", "Open", "", "GST"]],
    )
    flag_attn = flag_only_dashboard["needs_attention"][0]
    assert flag_attn["action"] == "Review GST flag", flag_attn
    print("OK: needs-attention action text always comes from an actual cell (Next Action, Recommended Action, or Risk/Tax Flag).\n")

    print("=== Test 27: overview 'resolved_today' and 'payments_total' ===")
    assert o["resolved_today"] == 1, o["resolved_today"]  # the one row dated 02.09.2026 with Status=Resolved
    # SYNTHETIC_ACCOUNTING's latest Cash date (02.09.2026) has no "Total
    # payments" row at all - must be None, never a fabricated 0.
    assert o["payments_total"] is None, o["payments_total"]
    payments_dashboard = build_dashboard(
        [MON_HEADER],
        [ACC_HEADER, ["01.09.2026", "Cash", "", "Total payments", "45000", "", "", "", "", ""]],
    )
    assert payments_dashboard["overview"]["payments_total"] == 45000, payments_dashboard["overview"]["payments_total"]
    no_monitoring_dates_dashboard = build_dashboard([MON_HEADER], [ACC_HEADER])
    assert no_monitoring_dates_dashboard["overview"]["resolved_today"] is None, (
        "No dated Monitoring rows at all -> resolved_today must be None, not a fabricated 0"
    )
    print("OK: resolved_today/payments_total are real computed values, or None when the data can't support them.\n")

    print("=== Test 28: What Changed Today, computed against SYNTHETIC data ===")
    wc = dashboard["what_changed"]
    assert wc["monitoring_date"] == "02.09.2026" and wc["accounting_date"] == "02.09.2026", wc
    assert wc["new_open_issues"] == 2, wc  # Site C (Unconfirmed) + Site D (Open), both dated 02.09.2026
    assert wc["resolved_issues"] == 1, wc
    # Site A's Critical row dated 02.09.2026 is Resolved same-day - must
    # NOT count as a new Critical/High still needing attention.
    assert wc["new_critical_high"] == 0, wc
    assert wc["new_accounting_exceptions"] == 0, wc  # the one Exception is dated 01.09.2026, not "today"
    # The latest Accounting date (02.09.2026) has no Sale/Purchase/Total-
    # payments row at all, so none of these comparisons can be computed -
    # they must be absent, never compared against a fabricated 0.
    assert "sales_change" not in wc, wc
    assert "purchase_change" not in wc, wc
    assert "payments_change" not in wc, wc
    print("OK: every What Changed figure matches a hand count of the actual rows; missing comparisons stay absent.\n")

    print("=== Test 29: What Changed Today correctly computes a real sales/purchase/payments delta across two dates ===")
    delta_accounting = [ACC_HEADER] + [
        ["01.09.2026", "Sale", "", "Invoices raised", "50000", "3", "", "", "", ""],
        ["01.09.2026", "Purchase", "", "Purchase bills booked", "20000", "2", "", "", "", ""],
        ["01.09.2026", "Cash", "", "Total payments", "15000", "", "", "", "", ""],
        ["02.09.2026", "Sale", "", "Invoices raised", "80000", "4", "", "", "", ""],
        ["02.09.2026", "Purchase", "", "Purchase bills booked", "5000", "1", "", "", "", ""],
        ["02.09.2026", "Cash", "", "Total payments", "15000", "", "", "", "", ""],
    ]
    delta_dashboard = build_dashboard([MON_HEADER], delta_accounting)
    delta_wc = delta_dashboard["what_changed"]
    assert delta_wc["sales_change"] == {"latest": 80000, "previous": 50000, "previous_date": "01.09.2026", "delta": 30000}, delta_wc
    assert delta_wc["purchase_change"] == {"latest": 5000, "previous": 20000, "previous_date": "01.09.2026", "delta": -15000}, delta_wc
    assert delta_wc["payments_change"] == {"latest": 15000, "previous": 15000, "previous_date": "01.09.2026", "delta": 0}, delta_wc
    print("OK: sales/purchase/payments deltas are exact (increase, decrease, and no-change all computed correctly).\n")

    print("=== Test 30: with only ONE distinct date, today's figures still compute but NO comparison is fabricated ===")
    one_date_dashboard = build_dashboard(
        [MON_HEADER, ["01.09.2026", "Site A", "Issue", "Outage", "Critical", "Open", "1", "", "", ""]],
        [ACC_HEADER, ["01.09.2026", "Sale", "", "Invoices raised", "10000", "1", "", "", "", ""]],
    )
    one_wc = one_date_dashboard["what_changed"]
    assert one_wc["monitoring_date"] == "01.09.2026" and one_wc["new_open_issues"] == 1, one_wc
    assert "sales_change" not in one_wc, "A single date has nothing to compare against - must not invent a baseline"

    print("=== Test 31: with NO dated rows at all, What Changed degrades to an honest empty state ===")
    no_data_wc = build_dashboard([MON_HEADER], [ACC_HEADER])["what_changed"]
    assert no_data_wc == {"monitoring_date": "", "accounting_date": ""}, no_data_wc
    print("OK: single-date and zero-date cases never fabricate a comparison.\n")

    print("=== Test 32: Patterns & Risks surfaces only what the SYNTHETIC data actually supports ===")
    patterns = dashboard["patterns_risks"]
    by_category = {}
    for p in patterns:
        by_category.setdefault(p["category"], []).append(p)
    assert any("Site A has 2 recorded issues" in p["description"] for p in by_category.get("Recurring Site Issue", [])), patterns
    assert any("Outage issues recorded 2 times" in p["description"] for p in by_category.get("Repeated Equipment Issue", [])), patterns
    assert any("Site A" in p["description"] and p.get("days_open") == 3 for p in by_category.get("Long-Open Issue", [])), patterns
    flags_seen = {p["description"] for p in by_category.get("Tax/Risk Flag", [])}
    assert any("GST" in d for d in flags_seen) and any("ITC" in d for d in flags_seen), patterns
    large_txn = by_category.get("Large Transaction", [])
    assert large_txn and large_txn[0]["amount"] == 50000, large_txn  # the 50000 Sale, not a 120000 Cash balance row
    # The trend is flat (2 open issues both dates) for this fixture - must
    # NOT claim a rise that didn't happen.
    assert "Rising Open Issues" not in by_category, patterns
    print("OK: recurring site/category, longest-open, tax flags, and the largest actual transaction all match by hand.\n")

    print("=== Test 33: Patterns & Risks flags a real rising trend, and is empty when nothing qualifies ===")
    rising_monitoring = [MON_HEADER] + [
        ["01.09.2026", "Site A", "Issue", "Outage", "Critical", "Open", "1", "", "", ""],
        ["02.09.2026", "Site B", "Issue", "Outage", "Critical", "Open", "1", "", "", ""],
        ["02.09.2026", "Site C", "Issue", "Outage", "Critical", "Open", "1", "", "", ""],
    ]
    rising_patterns = build_dashboard(rising_monitoring, [ACC_HEADER])["patterns_risks"]
    assert any(p["category"] == "Rising Open Issues" for p in rising_patterns), rising_patterns

    quiet_dashboard = build_dashboard(
        [MON_HEADER, ["01.09.2026", "Site A", "Issue", "Outage", "Critical", "Open", "", "", "", ""]],
        [ACC_HEADER, ["01.09.2026", "Sale", "", "Invoices raised", "1000", "1", "", "", "", ""]],
    )
    assert quiet_dashboard["patterns_risks"] == [], (
        f"A single site/category/no-flags/no-repeats dataset must produce no patterns, got: {quiet_dashboard['patterns_risks']}"
    )
    print("OK: a real 2-step rise is caught; a dataset with nothing recurring/notable produces an empty list.\n")

    print("=== Test 34: Required Actions is built only from real Next/Recommended Action or Critical/High rows ===")
    ra_list = dashboard["required_actions"]
    by_label = {a["label"]: a for a in ra_list}
    assert by_label["Site A"]["action"] == "Priority escalation", by_label["Site A"]
    assert by_label["Site B"]["action"] == "Review — High priority", by_label["Site B"]  # High, no Next Action of its own
    assert by_label["Site D"]["action"] == "Verify details", by_label["Site D"]  # Low priority, but has a Next Action
    assert by_label["ABC Traders"]["action"] == "Verify with vendor", by_label["ABC Traders"]
    assert by_label["XYZ Ltd"]["action"] == "Confirm with CA", by_label["XYZ Ltd"]
    # The Resolved Site A row and every blank-everything Cash/Sale/Purchase
    # row must NOT appear - nothing to act on there.
    assert sum(1 for a in ra_list if a["label"] == "Site A") == 1, "The Resolved Site A row must not also appear"
    assert ra_list[0]["priority"] == "Critical", "Critical items must sort first"
    print(f"OK: required_actions has exactly the {len(ra_list)} rows with a real action signal, correctly sorted.\n")

    print("=== Test 35: dashboard_app.html shows the reduced owner-view columns, but the full 10 remain in the detail view ===")
    for owner_column in ("Next Action", "Recommended Action"):
        assert owner_column in html_text
    # Fields deliberately moved OUT of the main tables must still exist
    # somewhere in the page (the detail-panel column lists) - the raw
    # 10-column data is never dropped, just not shown up front.
    for detail_only_field in ("Days Open", "Action Taken", "Vendor", "Record Type", "Risk / Tax Flag"):
        assert detail_only_field in html_text, f"{detail_only_field!r} must still be available in the row-detail view"
    assert "MON_TABLE_COLUMNS" in html_text and "MON_DETAIL_COLUMNS" in html_text
    assert "ACC_TABLE_COLUMNS" in html_text and "ACC_DETAIL_COLUMNS" in html_text
    print("OK: owner-view tables and full detail views both present in the HTML; no raw column was dropped entirely.\n")

    print("=== Test 36: dashboard_app.html references all 5 new/renamed Overview sections ===")
    for heading in ("Needs My Attention", "What Changed Today", "Patterns", "Required Actions"):
        assert heading in html_text, f"Expected the {heading!r} section heading in the HTML"
    print("OK\n")

    print("=== Test 37: latest-date logic is per-sheet and never touches the server clock ===")
    import datetime as _datetime
    # A fixture dated far from any real "today" the test could ever run on
    # - if this ever depended on the server clock, monitoring_date would
    # not equal the fixture's own date.
    far_past_dashboard = build_dashboard(
        [MON_HEADER, ["15.03.2019", "Site A", "Issue", "Outage", "Critical", "Open", "1", "", "", ""]],
        [ACC_HEADER],
    )
    assert far_past_dashboard["what_changed"]["monitoring_date"] == "15.03.2019"
    assert far_past_dashboard["what_changed"]["monitoring_date"] != _datetime.date.today().strftime("%d.%m.%Y")
    print("OK: 'today' is read from the sheet's own dates, confirmed against a date nowhere near the real current date.\n")

    print("=== Test 38: mixed-section Monitoring reproduces the real bug report - 39 rows, only 10 genuine issues ===")
    # Shaped exactly like the live sheet that triggered this fix: real
    # Needs Attention issues, plus Actions Taken / What's Needed Next /
    # Service Pattern Watch rows that must NOT inflate Open Issues.
    mixed_monitoring = [MON_HEADER]
    for i in range(10):
        mixed_monitoring.append(["03.09.2026", f"Issue Site {i}", f"Outage {i}", "Outage", "", "Open", "", "", "", ""])
    for i in range(19):
        mixed_monitoring.append(["03.09.2026", f"Action Site {i}", f"Fix {i}", "Optimizer", "", "Open", "", f"replaced {i}", "", ""])
    for i in range(5):
        mixed_monitoring.append(["03.09.2026", "", f"Requirement {i}", "", "", "Open", "", "", f"Requirement {i}", ""])
    for i in range(5):
        mixed_monitoring.append(["03.09.2026", f"Pattern Site {i}", f"Pattern {i}", "Inverter", "", "Monitoring", "", "", "", ""])
    assert len(mixed_monitoring) - 1 == 39, len(mixed_monitoring) - 1

    mixed_dashboard = build_dashboard(mixed_monitoring, [ACC_HEADER])
    mixed_m = mixed_dashboard["monitoring"]
    assert mixed_m["total_rows"] == 39, mixed_m["total_rows"]
    assert mixed_m["total_open_issues"] == 10, (
        f"Expected only the 10 genuine Needs Attention rows to count as Open Issues, got {mixed_m['total_open_issues']}"
    )
    assert mixed_m["issue_records_count"] == 10, mixed_m["issue_records_count"]
    print("OK: 39 total rows, but Open Issues correctly reports 10 - not 39.\n")

    print("=== Test 39: Actions Taken rows are excluded from Open Issues/Sites Needing Attention ===")
    assert mixed_m["actions_taken_count"] == 19, mixed_m["actions_taken_count"]
    attention_sites = {s["site"] for s in mixed_m["sites_needing_attention"]}
    assert not any(site.startswith("Action Site") for site in attention_sites), (
        f"An Actions Taken row leaked into sites_needing_attention: {attention_sites}"
    )
    print("OK\n")

    print("=== Test 40: What's Needed Next rows are excluded from Open Issues/Sites Needing Attention ===")
    assert mixed_m["whats_needed_next_count"] == 5, mixed_m["whats_needed_next_count"]
    assert not any(s["issue"].startswith("Requirement") for s in mixed_m["sites_needing_attention"]), (
        "A What's Needed Next row leaked into sites_needing_attention"
    )
    print("OK\n")

    print("=== Test 41: Service Pattern Watch rows are excluded from Open Issues/Sites Needing Attention ===")
    assert mixed_m["service_pattern_watch_count"] == 5, mixed_m["service_pattern_watch_count"]
    assert not any(site.startswith("Pattern Site") for site in attention_sites), (
        f"A Service Pattern Watch row leaked into sites_needing_attention: {attention_sites}"
    )
    # Also check What Changed Today's "new_open_issues" (the same metric,
    # computed independently in _build_what_changed) isn't inflated either.
    assert mixed_dashboard["what_changed"]["new_open_issues"] == 10, mixed_dashboard["what_changed"]["new_open_issues"]
    print("OK: all three non-issue section kinds are correctly excluded everywhere Open Issues is computed.\n")

    print("=== Test 42: 'No Accounting data' is distinguished from a real computed ₹0, in build_dashboard() ===")
    no_data_dashboard = build_dashboard([MON_HEADER], [ACC_HEADER])
    assert no_data_dashboard["overview"]["has_accounting_data"] is False
    assert no_data_dashboard["overview"]["sales_total"] == 0  # still a real, honestly-computed 0
    real_zero_dashboard = build_dashboard(
        [MON_HEADER],
        [ACC_HEADER, ["03.09.2026", "Sale", "", "Invoices raised", "0", "0", "", "", "", ""]],
    )
    assert real_zero_dashboard["overview"]["has_accounting_data"] is True
    assert real_zero_dashboard["overview"]["sales_total"] == 0
    print("OK: both cases compute sales_total=0, but only has_accounting_data tells them apart.\n")

    print("=== Test 43: the no-data-vs-zero distinction reaches dashboard_app.html and the text summary ===")
    assert "has_accounting_data" in html_text, "dashboard_app.html must read the has_accounting_data flag"
    assert "No accounting data available for this date." in html_text
    no_acc_lines = srv._dashboard_summary_lines(no_data_dashboard, True, "03.09.2026")
    assert "No accounting data available for this date." in no_acc_lines, no_acc_lines
    assert not any("Sales Total: 0" in line or "₹0" in line for line in no_acc_lines), (
        f"Must never print a bare 0/₹0 for Sales when there is no Accounting data at all: {no_acc_lines}"
    )
    real_zero_lines = srv._dashboard_summary_lines(real_zero_dashboard, True, "03.09.2026")
    assert any("Sales Total: 0" in line for line in real_zero_lines), (
        f"A real computed 0 (from an actual Sale row) must still be shown as 0: {real_zero_lines}"
    )
    print("OK: dashboard_app.html and the MCP text response both distinguish no-data from a real zero.\n")

    print("=== Test 44: _dashboard_summary_lines exposes the actual report date, and flags an unavailable 'today' ===")
    current_dashboard = build_dashboard(
        [MON_HEADER, ["05.09.2026", "Site A", "Outage", "Outage", "", "Open", "", "", "", ""]],
        [ACC_HEADER],
    )
    current_lines = srv._dashboard_summary_lines(current_dashboard, True, "05.09.2026")
    assert "Monitoring report date: 05.09.2026." in current_lines, current_lines

    stale_dashboard = build_dashboard(
        [MON_HEADER, ["03.09.2026", "Site A", "Outage", "Outage", "", "Open", "", "", "", ""]],
        [ACC_HEADER],
    )
    stale_lines = srv._dashboard_summary_lines(stale_dashboard, True, "05.09.2026")
    assert "Today's report (05.09.2026) is not available. Latest available report: 03.09.2026." in stale_lines, stale_lines
    print("OK: the response states the real report date, and explicitly flags when it isn't today's.\n")

    print("=== Test 45: no developer/debug language in the normal owner-facing response ===")
    for banned in ("almost certainly", "doesn't look wired", "unverified", "test submissions", "test_connection", "unreliable"):
        assert banned not in " ".join(current_lines).lower(), f"Found debug language {banned!r} in the owner-facing response"
        assert banned not in " ".join(stale_lines).lower(), f"Found debug language {banned!r} in the owner-facing response"
    print("OK: the response text is factual and free of developer/debug language.\n")

    print("All dashboard tests passed.")

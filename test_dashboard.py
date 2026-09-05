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
    assert set(dashboard.keys()) == {"overview", "monitoring", "accounting", "needs_attention", "recent_activity"}
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

    print("All dashboard tests passed.")

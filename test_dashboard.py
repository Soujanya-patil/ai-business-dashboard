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
    ["2026-09-01", "Site A", "Inverter outage", "Outage", "Critical", "Open", "3", "", "Priority escalation", ""],
    ["2026-09-01", "Site B", "Optimizer failure", "Optimizer", "High", "Open", "1", "", "", "Vendor X"],
    ["2026-09-02", "Site A", "Inverter outage", "Outage", "Critical", "Resolved", "4", "Replaced inverter", "", ""],
    ["2026-09-02", "Site C", "Not reporting", "Monitoring", "", "Unconfirmed", "", "", "", ""],
    ["2026-09-02", "Site D", "Minor GST mismatch", "Vendor", "Low", "Open", "", "", "Verify details", ""],
]

# A small representative synthetic Accounting dataset covering Cash (two
# dates, to prove "most recent date only"), Sale, Purchase, an Exception,
# and a Tax row with a Risk/Tax Flag.
SYNTHETIC_ACCOUNTING = [ACC_HEADER] + [
    ["2026-09-01", "Cash", "", "Opening balance", "100000", "", "", "", "", ""],
    ["2026-09-01", "Cash", "", "Closing balance", "90000", "", "", "", "", ""],
    ["2026-09-02", "Cash", "", "Opening balance", "90000", "", "", "", "", ""],
    ["2026-09-02", "Cash", "", "Closing balance", "120000", "", "", "", "", ""],
    ["2026-09-01", "Sale", "", "Invoices raised", "50000", "5", "", "", "", ""],
    ["2026-09-01", "Sale", "", "Outstanding receivables (over 60 days)", "15000", "", "", "", "", ""],
    ["2026-09-01", "Purchase", "", "Purchase bills booked", "20000", "2", "", "", "", ""],
    ["2026-09-01", "Exception", "ABC Traders", "GST mismatch", "12000", "", "Critical", "Open", "Verify with vendor", "GST"],
    ["2026-09-02", "Tax", "XYZ Ltd", "ITC discrepancy", "5000", "", "", "Unconfirmed", "Confirm with CA", "ITC"],
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
    assert "Site A" in site_names  # the OPEN Site A row (2026-09-01), not the Resolved one
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
    assert [t["date"] for t in trend] == ["2026-09-01", "2026-09-02"], trend
    print("OK\n")

    print("=== Test 7: a single-date dataset produces NO trend (would be misleading) ===")
    single_date_dashboard = build_dashboard(
        [MON_HEADER, ["2026-09-01", "Site A", "Issue", "Outage", "Critical", "Open", "", "", "", ""]],
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
    assert cash["as_of"] == "2026-09-02", cash
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
    assert ra["monitoring"][0]["date"] == "2026-09-02"
    assert ra["accounting"][0]["date"] == "2026-09-02"
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

    print("All dashboard tests passed.")

"""Guards the single source of truth for which Google Spreadsheet this
project reads/writes: mcp_server.SPREADSHEET_ID. Every other module
(sheets_service.py, dashboard_service.py) takes spreadsheet_id as a
parameter rather than hardcoding one, so this constant is the only place
a spreadsheet migration can go wrong.

Run with:
    python test_spreadsheet_config.py

Fast and network-independent for the constant checks (Tests 1-3); Test 4
is a live connectivity check that requires the Google Sheets API to be
reachable for the service account's project - it reports clearly rather
than crashing uninformatively if that API is temporarily unavailable
(e.g. SERVICE_DISABLED), since that's an external/infra condition, not a
code defect.
"""

from mcp_server import SPREADSHEET_ID, MONITORING_TAB, ACCOUNTING_TAB, service
from sheets_service import get_tab_values

DASHBOARD_TAB = "Dashboard"

# The production spreadsheet as of the 2026-09-04 migration (see the
# project's own change history for the prior ids this replaced - the very
# first production sheet, then a briefly-misidentified intermediate one
# that turned out to be the wrong document entirely).
EXPECTED_SPREADSHEET_ID = "1EGlUndNNwiDm0RDwL5tLQeQKmuia5LCy8qKSMhhLQYM"
_RETIRED_SPREADSHEET_IDS = (
    "1bIifUY2LUi5C6is7ZJNzr-F_ov89_ViAdN-RhqoaBFQ",
    "1F75Id9ODLn3tV2p_cOI4G3DNc1Q9ron9Z_9j4dffndI",
)

if __name__ == "__main__":
    print("=== Test 1: mcp_server.SPREADSHEET_ID points at the current production spreadsheet ===")
    assert SPREADSHEET_ID == EXPECTED_SPREADSHEET_ID, (
        f"Expected {EXPECTED_SPREADSHEET_ID!r}, got {SPREADSHEET_ID!r}"
    )
    print(f"OK: SPREADSHEET_ID = {SPREADSHEET_ID!r}\n")

    print("=== Test 2: no retired spreadsheet id is referenced ===")
    assert SPREADSHEET_ID not in _RETIRED_SPREADSHEET_IDS, (
        f"SPREADSHEET_ID still points at a retired spreadsheet: {SPREADSHEET_ID!r}"
    )
    print("OK\n")

    print("=== Test 3: Monitoring/Accounting tab names unchanged by the migration ===")
    assert MONITORING_TAB == "Monitoring", MONITORING_TAB
    assert ACCOUNTING_TAB == "Accounting", ACCOUNTING_TAB
    print("OK\n")

    print("=== Test 4: live connectivity - service account can reach the current spreadsheet ===")
    live_access_ok = False
    try:
        metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    except Exception as e:  # noqa: BLE001 - deliberately broad: report clearly, don't obscure the cause
        print(f"SKIPPED (could not verify live access): {type(e).__name__}: {e}")
    else:
        assert metadata["spreadsheetId"] == SPREADSHEET_ID, "Sanity check: API echoed a different spreadsheetId back"
        title = metadata["properties"]["title"]
        tabs = sorted(s["properties"]["title"] for s in metadata["sheets"])
        print(f"OK: connected to spreadsheet {title!r} (id={metadata['spreadsheetId']}). Tabs present: {tabs}")
        for required in ("Monitoring", "Accounting", "Dashboard"):
            assert required in tabs, f"Expected tab {required!r} to exist in the spreadsheet, found: {tabs}"
        print("OK: all three required tabs (Monitoring, Accounting, Dashboard) are present.")
        live_access_ok = True

    print("\n=== Test 5: each required tab is actually READABLE (read-only, no writes) ===")
    if not live_access_ok:
        print("SKIPPED (Test 4 did not establish live access).")
    else:
        for tab in (MONITORING_TAB, ACCOUNTING_TAB, DASHBOARD_TAB):
            rows = get_tab_values(service, SPREADSHEET_ID, tab)
            header = rows[0] if rows else None
            print(f"  '{tab}': read OK - {len(rows)} row(s) total (header: {header!r})")
        print("OK: Monitoring, Accounting, and Dashboard tabs all read successfully.")

    print("\nAll spreadsheet configuration checks completed.")

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.apps import Apps, client_supports_apps
from mcp_types import CallToolResult, TextContent
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from starlette.requests import Request
from starlette.responses import JSONResponse

from summary_parser import parse_monitoring_summary, build_monitoring_rows, MONITORING_HEADERS
from accounting_parser import parse_accounting_summary, build_accounting_rows, ACCOUNTING_HEADERS
from sheets_service import append_unique_rows, upsert_row_by_key, get_tab_values
from dashboard_service import build_dashboard
from auth_middleware import BearerTokenMiddleware

# -----------------------------
# MCP Apps (interactive dashboard UI)
# -----------------------------
# The `io.modelcontextprotocol/ui` extension: additive and opt-in, per
# SEP-2133 - it only contributes a new tool (get_business_dashboard,
# defined below) and a `ui://` HTML resource. It never touches how
# test_connection/process_monitoring_summary/process_accounting_summary
# are registered or called, so nothing here can regress them. A client
# that hasn't negotiated Apps support (checked via client_supports_apps())
# still gets the same dashboard tool - it just receives the computed
# summary as plain text instead of the interactive iframe.
apps = Apps()
_DASHBOARD_HTML = (Path(__file__).parent / "dashboard_app.html").read_text(encoding="utf-8")
apps.add_html_resource(
    "ui://dashboard/app.html",
    _DASHBOARD_HTML,
    name="dashboard",
    title="AI Business Dashboard",
    description="Founder/CEO dashboard: KPIs, open issues, and financials computed live from the Monitoring and Accounting Google Sheets.",
)

# -----------------------------
# Google Sheets configuration
# -----------------------------
SPREADSHEET_ID = "1EGlUndNNwiDm0RDwL5tLQeQKmuia5LCy8qKSMhhLQYM"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

# -----------------------------
# Tab used by process_monitoring_summary
# -----------------------------
# Needs Attention, Actions Taken, What's Needed Next, and Service Pattern
# Watch write into this ONE tab as plain business-facing rows - no
# Section column (see summary_parser.MONITORING_HEADERS - a concise,
# 10-column schema - for the exact column order and build_monitoring_
# rows() for how a parsed summary becomes rows). ONE ROW = ONE REAL OPERATIONAL RECORD: there is no
# synthetic "ALL SITES"/aggregate row here - the day's New Issues/Issues
# Resolved/Total Open Issues figures are reported in the tool's response
# text instead of being written to the sheet (see process_monitoring_
# summary below). Previously each section had its own tab; those tabs
# (Daily Summary, Needs Attention, etc.) still exist in the spreadsheet
# but are no longer written to.
MONITORING_TAB = "Monitoring"

# -----------------------------
# Tab used by process_accounting_summary
# -----------------------------
# Renamed from "Finance" (which held only an unused placeholder header,
# no real data - see accounting_parser.py for the migration performed).
# All accounting record types (Exception, Cash, Sale, Purchase, Expense,
# Tax, Pending) write into this ONE tab as plain rows distinguished by
# their Record Type column - see accounting_parser.ACCOUNTING_HEADERS for
# the exact column order and build_accounting_rows() for how a parsed
# summary becomes rows.
ACCOUNTING_TAB = "Accounting"

# Composite key for the Cash and Purchase rows' upsert: (Date, Record
# Type, Description). Cash now produces up to 4 short rows per date
# (Opening balance / Closing balance / Total receipts / Total payments)
# and Purchase up to 2 (Purchase bills booked / a GSTIN-HSN mismatch, if
# any) rather than one wide row each - Description (a short fixed label
# per sub-record, e.g. "Opening balance") is what distinguishes them, so
# it must be part of the key for reprocessing the same date to update
# each sub-record in place rather than accumulate duplicates.
_ACCT_DATE_COL = ACCOUNTING_HEADERS.index("Date")
_ACCT_RECORD_TYPE_COL = ACCOUNTING_HEADERS.index("Record Type")
_ACCT_DESCRIPTION_COL = ACCOUNTING_HEADERS.index("Description")


# -----------------------------
# Connect to Google Sheets
# -----------------------------
# Path to the service account key file. Defaults to "credentials.json" in
# the project directory, exactly as before, so local/stdio use is
# unchanged. On a host like Render there is no committed credentials.json
# (it's gitignored on purpose) - set GOOGLE_APPLICATION_CREDENTIALS to the
# path of a securely-provided key file instead (see README: "Providing
# Google credentials on Render"). This is the standard Google Cloud env
# var name for this exact purpose.
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

credentials = Credentials.from_service_account_file(
    GOOGLE_CREDENTIALS_PATH,
    scopes=SCOPES
)

service = build(
    "sheets",
    "v4",
    credentials=credentials
)


# ---------------------------
# Tool: Show the AI Business Dashboard
# ---------------------------
# Defined here, BEFORE the MCPServer(extensions=[apps]) call below - the
# Apps extension reads apps.tools()/apps.resources() synchronously at
# MCPServer construction time (not lazily), so every @apps.tool()
# decorator must have already run by the time `mcp` is constructed. This
# is also why `mcp = MCPServer(...)` itself had to move down here from
# the top of the file, after `service`/SPREADSHEET_ID/MONITORING_TAB/
# ACCOUNTING_TAB exist for this tool's body to use.

@apps.tool(resource_uri="ui://dashboard/app.html", visibility=["model", "app"])
def get_business_dashboard(ctx: Context) -> CallToolResult:
    """Compute and show the founder/CEO-level AI Business Dashboard:
    overview KPIs (open issues, critical/high counts, sales/purchase/cash
    totals), a Monitoring section (priority/category breakdowns, sites
    needing attention, recent actions, next actions, an open-issues trend
    when more than one date is present), an Accounting section (record
    types, amounts, cash position, tax/GST flags, high-priority
    exceptions), a cross-sheet Needs Attention list, and Recent Activity.

    Everything is calculated FRESH from the current Monitoring and
    Accounting Google Sheets rows on every call (via the same
    sheets_service.get_tab_values used by the processing tools) - Google
    Sheets remains the single source of truth, nothing is cached or
    written back, and no synthetic aggregate row (e.g. "ALL SITES") is
    ever created. Call this any time to see the current state, and
    especially right after process_monitoring_summary or
    process_accounting_summary to show the admin the just-updated
    dashboard.

    On an MCP Apps-capable client this renders as an interactive
    dashboard inline in the chat, with its own Refresh button that re-
    calls this tool for live data; on any other client it returns the
    same computed totals as a concise text summary instead.
    """
    monitoring_rows = get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB)
    accounting_rows = get_tab_values(service, SPREADSHEET_ID, ACCOUNTING_TAB)
    dashboard = build_dashboard(monitoring_rows, accounting_rows)

    overview = dashboard["overview"]
    summary_lines = [
        "AI Business Dashboard (live from Google Sheets):",
        f"Open Issues: {overview['total_open_issues']} (Critical/High: {overview['critical_high_issues']})",
        f"Needs Attention: {overview['needs_attention_count']}",
        f"Accounting Exceptions: {overview['accounting_exceptions']}",
        f"Sales Total: {overview['sales_total']} | Purchase Total: {overview['purchase_total']}",
    ]
    if overview.get("outstanding_receivables_total"):
        summary_lines.append(f"Outstanding Receivables: {overview['outstanding_receivables_total']}")
    cash_position = overview.get("cash_position") or {}
    if cash_position.get("as_of"):
        closing = cash_position.get("Closing balance") or "n/a"
        summary_lines.append(f"Cash Position (as of {cash_position['as_of']}): Closing balance {closing}")
    if not client_supports_apps(ctx):
        summary_lines.append(
            "(This client does not support the MCP Apps interactive UI - showing computed totals only.)"
        )

    return CallToolResult(
        content=[TextContent(type="text", text="\n".join(summary_lines))],
        structured_content=dashboard,
    )


# -----------------------------
# MCP Server
# -----------------------------
# Constructed here (not at the top of the file) so the Apps extension's
# apps.tools()/apps.resources() are already fully populated - see the
# comment above get_business_dashboard.
mcp = MCPServer("AI Business Dashboard", extensions=[apps])


# -----------------------------
# Tool 1: Test connection
# -----------------------------
@mcp.tool()
def test_connection(message: str) -> str:
    """Test whether the MCP server is working."""
    return f"Connection successful: {message}"


# -----------------------------
# Health check (HTTP transport only)
# -----------------------------
# Plain HTTP endpoint outside the MCP protocol itself, for hosting-platform
# uptime/liveness checks once this runs as a remote server. Has no effect
# on stdio/MCP Inspector usage - custom_route only applies when the server
# is actually running over HTTP.
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Basic liveness endpoint for hosting platforms / uptime checks."""
    return JSONResponse({"status": "ok", "service": "ai-business-dashboard-mcp"})


# ---------------------------
# Tool 2: Process a full Plant Monitoring Summary
# ---------------------------

@mcp.tool()
def process_monitoring_summary(summary: str) -> str:
    """Parse a complete SUNTROP SOLAR Plant Monitoring Summary and write
    every real site-level record - needs attention, actions taken, what's
    needed next, and service pattern watch items - into the unified
    Monitoring sheet as concise, plain business-facing rows (one tab, one
    10-column schema, no Section column). ONE ROW = ONE IMPORTANT
    BUSINESS RECORD: long narrative text is shortened before it's stored
    (see summary_parser._shorten_issue_text/_shorten_action_text) - Sheets
    never gets the whole paragraph. The day's New Issues / Issues
    Resolved / Total Open Issues totals are NOT written as a row (no
    "ALL SITES" or other synthetic aggregate site) - they're reported
    back in this tool's response text instead. This is the main tool for
    the Admin -> Claude -> Sheets workflow: paste the whole raw summary
    text as `summary`.

    Parsing is deterministic (no LLM call here) and tied to the fixed
    template. Re-running the same summary will not create duplicate rows.

    After this returns, call get_business_dashboard to show the admin the
    updated dashboard reflecting what was just written.
    """

    # Deterministic parsing of the raw text into a structured dict, then
    # reshaped into per-section row lists sharing MONITORING_HEADERS'
    # column order - see summary_parser.py for both steps. Note
    # build_monitoring_rows() produces no row at all for the day's
    # aggregate figures (parsed["new_issues"]/["resolved_issues"]/
    # ["total_open_issues"]/["issues_today_notes"]) - those are read
    # directly off `parsed` below instead, for the response text only.
    parsed = parse_monitoring_summary(summary)
    rows = build_monitoring_rows(parsed)
    report_date = parsed["date"]

    # --- Needs Attention: one row per overdue/escalated item, if any ---
    needs_attention_count = append_unique_rows(
        service, SPREADSHEET_ID, MONITORING_TAB, MONITORING_HEADERS,
        rows["needs_attention"], key_indexes=list(range(len(MONITORING_HEADERS))),
    )

    # --- Actions Taken ---
    actions_taken_count = append_unique_rows(
        service, SPREADSHEET_ID, MONITORING_TAB, MONITORING_HEADERS,
        rows["actions_taken"], key_indexes=list(range(len(MONITORING_HEADERS))),
    )

    # --- What's Needed Next ---
    needed_next_count = append_unique_rows(
        service, SPREADSHEET_ID, MONITORING_TAB, MONITORING_HEADERS,
        rows["whats_needed_next"], key_indexes=list(range(len(MONITORING_HEADERS))),
    )

    # --- Service Pattern Watch: only written when the section had real content ---
    pattern_count = append_unique_rows(
        service, SPREADSHEET_ID, MONITORING_TAB, MONITORING_HEADERS,
        rows["service_pattern_watch"], key_indexes=list(range(len(MONITORING_HEADERS))),
    )

    # Daily aggregate figures are surfaced here, in the response text, so
    # nothing from ISSUES TODAY is silently lost - just never persisted
    # as a fake site row. A future Dashboard tab is expected to compute
    # the equivalent KPIs from the individual rows above via formulas.
    metrics_line = (
        f"Daily metrics (reported here, not stored as a row): "
        f"New Issues={parsed['new_issues']}, "
        f"Issues Resolved={parsed['resolved_issues']}, "
        f"Total Open Issues={parsed['total_open_issues']}"
    )
    issues_today_notes = parsed.get("issues_today_notes", "")
    if issues_today_notes:
        metrics_line += f"\nDaily metrics notes: {issues_today_notes}"

    return (
        "Monitoring summary processed successfully.\n"
        f"Date: {report_date}\n"
        f"{metrics_line}\n"
        f"Needs Attention: {needs_attention_count} row(s)\n"
        f"Actions Taken: {actions_taken_count} row(s)\n"
        f"What's Needed Next: {needed_next_count} row(s)\n"
        f"Service Pattern Watch: {pattern_count} row(s)"
    )


# ---------------------------
# Tool 3: Process a full Day Book (Accounting) Summary
# ---------------------------

@mcp.tool()
def process_accounting_summary(summary: str) -> str:
    """Parse a complete SUNTROP SOLAR Day Book Summary and write every
    section - exceptions requiring attention, cash & bank position,
    sales, purchase, expenses/journal entries, GST/tax watch items, and
    pending-from-yesterday items - into the unified Accounting sheet as
    concise rows (one tab, one 10-column schema, distinguished by a
    Record Type column: Exception, Cash, Sale, Purchase, Expense, Tax,
    Pending). ONE ROW = ONE IMPORTANT BUSINESS RECORD: long narrative
    text is shortened before it's stored (see accounting_parser.
    _shorten_accounting_description) and Cash/Sales/Purchase each become
    several small concrete rows (e.g. a separate Opening balance /
    Closing balance row) instead of one wide row - Sheets never gets the
    whole paragraph. Paste the whole raw Day Book summary text as
    `summary`.

    Parsing is deterministic (no LLM call here) and tied to the fixed
    template. Re-running the same summary will not create duplicate rows.

    After this returns, call get_business_dashboard to show the admin the
    updated dashboard reflecting what was just written.
    """

    parsed = parse_accounting_summary(summary)
    rows = build_accounting_rows(parsed)
    report_date = parsed["date"]

    # --- Cash & Purchase: one row per (Date, Record Type, Description) sub-record; re-runs update in place ---
    for cash_row in rows["cash"]:
        upsert_row_by_key(
            service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
            cash_row, key_indexes=[_ACCT_DATE_COL, _ACCT_RECORD_TYPE_COL, _ACCT_DESCRIPTION_COL],
        )
    for purchase_row in rows["purchase"]:
        upsert_row_by_key(
            service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
            purchase_row, key_indexes=[_ACCT_DATE_COL, _ACCT_RECORD_TYPE_COL, _ACCT_DESCRIPTION_COL],
        )

    # --- Exceptions, Sales, Expenses, Tax watch, Pending: append-if-not-duplicate ---
    issues_count = append_unique_rows(
        service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
        rows["issues"], key_indexes=list(range(len(ACCOUNTING_HEADERS))),
    )
    sales_count = append_unique_rows(
        service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
        rows["sales"], key_indexes=list(range(len(ACCOUNTING_HEADERS))),
    )
    expenses_count = append_unique_rows(
        service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
        rows["expenses"], key_indexes=list(range(len(ACCOUNTING_HEADERS))),
    )
    tax_count = append_unique_rows(
        service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
        rows["tax"], key_indexes=list(range(len(ACCOUNTING_HEADERS))),
    )
    pending_count = append_unique_rows(
        service, SPREADSHEET_ID, ACCOUNTING_TAB, ACCOUNTING_HEADERS,
        rows["pending"], key_indexes=list(range(len(ACCOUNTING_HEADERS))),
    )

    return (
        "Accounting summary processed successfully.\n"
        f"Date: {report_date}\n"
        "Sections processed: Issues Requiring Attention, Cash & Bank Position, "
        "Sales, Purchase, Expenses & Journal Entries, GST/Tax Watch Items, "
        "Pending From Yesterday\n"
        f"Cash & Bank Position: {'updated (' + str(len(rows['cash'])) + ' row(s))' if rows['cash'] else '0 row(s)'}\n"
        f"Purchase: {'updated (' + str(len(rows['purchase'])) + ' row(s))' if rows['purchase'] else '0 row(s)'}\n"
        f"Issues Requiring Attention: {issues_count} row(s)\n"
        f"Sales: {sales_count} row(s)\n"
        f"Expenses & Journal Entries: {expenses_count} row(s)\n"
        f"GST/Tax Watch Items: {tax_count} row(s)\n"
        f"Pending From Yesterday: {pending_count} row(s)"
    )


# -----------------------------
# Start MCP server
# -----------------------------
# Transport is chosen at runtime via MCP_TRANSPORT so this one entry point
# covers both phases without touching any tool code:
#
#   MCP_TRANSPORT unset / "stdio"   -> local dev, MCP Inspector (unchanged, default)
#   MCP_TRANSPORT=streamable-http   -> remote HTTP server for a Claude Pro
#                                       custom connector (Phase 2). Not run
#                                       anywhere yet - this just makes it
#                                       possible to opt in later.
#
# host/port for HTTP mode also come from the environment (never hard-coded)
# so the same code works locally and on a future hosting platform:
#   - PORT is read first since most hosting platforms (Render, Railway, etc.)
#     inject it automatically; MCP_PORT is a manual override; 8000 is the
#     final fallback.
#   - MCP_HOST defaults to 0.0.0.0 (not 127.0.0.1) so the server is
#     reachable from outside its container once deployed. This only takes
#     effect in HTTP mode - stdio mode never binds a network port.
#
# HTTP mode is also where bearer-token auth is enforced (see
# auth_middleware.py): MCP_AUTH_TOKEN is required in this branch and
# checked on every request except GET /health. stdio mode has no HTTP
# layer at all, so it is completely unaffected by any of this.
if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "stdio":
        mcp.run()
    elif transport == "streamable-http":
        import uvicorn

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))

        auth_token = os.getenv("MCP_AUTH_TOKEN")
        if not auth_token:
            # Fail fast rather than ever silently serving the MCP endpoint
            # without authentication once this is reachable on a public URL.
            raise ValueError(
                "MCP_AUTH_TOKEN must be set when running with "
                "MCP_TRANSPORT=streamable-http - the remote MCP endpoint "
                "would otherwise be reachable by anyone with the URL."
            )

        # Build the same Starlette app mcp.run() would have used internally
        # (same routes: POST /mcp, GET /health), then wrap it with the
        # bearer-token check before handing it to uvicorn ourselves - this
        # is the only way to add middleware, since mcp.run() builds and
        # serves the app in one step without exposing a hook for it.
        app = mcp.streamable_http_app(streamable_http_path="/mcp", host=host)
        app = BearerTokenMiddleware(app, token=auth_token)

        uvicorn.run(app, host=host, port=port, log_level=mcp.settings.log_level.lower())
    else:
        raise ValueError(f"Unknown MCP_TRANSPORT: {transport!r}. Use 'stdio' or 'streamable-http'.")

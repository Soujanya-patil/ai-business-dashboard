import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.apps import Apps
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
# that hasn't negotiated Apps support still gets the same dashboard tool
# and the same fixed text response - it just doesn't render the
# interactive iframe alongside it.
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

def _fmt_rupees(value) -> str:
    if value is None:
        return "n/a"
    return f"₹{value:,}"


def _needs_attention_bullets(items: list) -> list:
    if not items:
        return ["Nothing needs attention right now."]
    bullets = []
    for item in items:
        label = item.get("label") or item.get("source") or "(unlabeled)"
        detail = item.get("detail") or ""
        line = f"{label} — {detail}" if detail else label
        if item.get("priority"):
            line += f" (Priority: {item['priority']})"
        if item.get("action"):
            line += f" — Action: {item['action']}"
        bullets.append(line)
    return bullets


def _what_changed_bullets(wc: dict) -> list:
    bullets = []
    if wc.get("new_open_issues"):
        bullets.append(f"{wc['new_open_issues']} new open issue(s) as of {wc['monitoring_date']}")
    if wc.get("resolved_issues"):
        bullets.append(f"{wc['resolved_issues']} issue(s) resolved as of {wc['monitoring_date']}")
    if wc.get("new_critical_high"):
        bullets.append(f"{wc['new_critical_high']} new Critical/High issue(s) as of {wc['monitoring_date']}")
    if wc.get("new_accounting_exceptions"):
        bullets.append(f"{wc['new_accounting_exceptions']} new accounting exception(s) as of {wc['accounting_date']}")
    for key, label in (("sales_change", "Sales"), ("purchase_change", "Purchases"), ("payments_change", "Payments")):
        change = wc.get(key)
        if not change:
            continue
        delta = change["delta"]
        if delta > 0:
            bullets.append(f"{label} up {_fmt_rupees(delta)} vs {change['previous_date']}")
        elif delta < 0:
            bullets.append(f"{label} down {_fmt_rupees(abs(delta))} vs {change['previous_date']}")
        else:
            bullets.append(f"{label} unchanged vs {change['previous_date']}")
    if not bullets:
        if not wc.get("monitoring_date") and not wc.get("accounting_date"):
            bullets.append("No reports on record yet to compare.")
        else:
            bullets.append("No significant changes since the last report.")
    return bullets


def _patterns_risks_bullets(patterns: list) -> list:
    if not patterns:
        return ["No recurring patterns or risks identified from the current data."]
    return [p["description"] for p in patterns]


def _required_actions_bullets(actions: list) -> list:
    if not actions:
        return ["No outstanding actions right now."]
    bullets = []
    for a in actions:
        label = a.get("label") or a.get("source") or "(unlabeled)"
        line = f"{label}: {a.get('action', '')}"
        if a.get("priority"):
            line += f" (Priority: {a['priority']})"
        bullets.append(line)
    return bullets


def _dashboard_summary_lines(dashboard: dict) -> list:
    """Builds get_business_dashboard's plain-text response as a pure
    function of already-computed data (no Sheets access, no ctx, no
    server clock) - kept separate from the tool function itself purely so
    this text-composition logic can be unit tested directly.

    This is WORKFLOW 2 (owner dashboard) output ONLY - it must always
    render EXACTLY the fixed structure: Executive Overview / Needs My
    Attention / What Changed / Patterns & Risks / Required Actions,
    followed by the "[Interactive Dashboard]" marker. No introductory
    paragraph, no concluding paragraph, no narrative commentary ("Fresh
    pull...", "Done...", "worth flagging...", "if you want...", etc.) -
    every line must be one of the fixed headings or a concise data bullet.

    The Executive Overview heading always states the actual latest report
    date on record ("as of DD.MM.YYYY") rather than the server's current
    date - so it can never imply today's data is being shown when the
    latest real report is from an earlier day. Built entirely from the
    already-computed dashboard sections (needs_attention, what_changed,
    patterns_risks, required_actions) - never the raw 10-column row data,
    which stays one click away in the interactive dashboard's detail view.
    """
    overview = dashboard["overview"]
    what_changed = dashboard["what_changed"]
    monitoring_date = what_changed.get("monitoring_date") or ""
    accounting_date = what_changed.get("accounting_date") or ""
    latest_date = monitoring_date or accounting_date

    lines = []
    lines.append(f"Executive Overview (as of {latest_date})" if latest_date else "Executive Overview (no reports on record yet)")
    lines.append(f"- Open Issues: {overview['total_open_issues']}")
    lines.append(f"- Critical/High: {overview['critical_high_issues']}")
    if overview.get("has_accounting_data"):
        lines.append(f"- Accounting Exceptions: {overview['accounting_exceptions']}")
        lines.append(f"- Sales: {_fmt_rupees(overview['sales_total'])}")
        lines.append(f"- Purchases: {_fmt_rupees(overview['purchase_total'])}")
        lines.append(f"- Payments: {_fmt_rupees(overview.get('payments_total'))}")
    else:
        # Never a bare "₹0" here - a sum over zero rows and a sum of real
        # zero-value rows both come out to 0, and only this explicit line
        # tells them apart.
        lines.append("- Accounting: no accounting data available for this date.")

    lines.append("")
    lines.append("Needs My Attention")
    lines.extend(f"- {b}" for b in _needs_attention_bullets(dashboard["needs_attention"]))

    lines.append("")
    lines.append("What Changed")
    lines.extend(f"- {b}" for b in _what_changed_bullets(what_changed))

    lines.append("")
    lines.append("Patterns & Risks")
    lines.extend(f"- {b}" for b in _patterns_risks_bullets(dashboard["patterns_risks"]))

    lines.append("")
    lines.append("Required Actions")
    lines.extend(f"- {b}" for b in _required_actions_bullets(dashboard["required_actions"]))

    lines.append("")
    lines.append("[Interactive Dashboard]")

    return lines


@apps.tool(resource_uri="ui://dashboard/app.html", visibility=["model", "app"])
def get_business_dashboard(ctx: Context) -> CallToolResult:
    """WORKFLOW 2 (OWNER DASHBOARD) - the ONLY tool responsible for the
    owner-facing dashboard. Strictly separate from WORKFLOW 1 (
    process_monitoring_summary / process_accounting_summary): this tool
    never writes to Google Sheets, and the processing tools never generate
    dashboard/executive commentary. Do not merge the two - after a
    process_monitoring_summary or process_accounting_summary call returns
    its short confirmation, only call this tool separately, as its own
    explicit step, if the owner's dashboard is actually being requested.

    READ-ONLY: computes the dashboard FRESH from the current Monitoring
    and Accounting Google Sheets rows on every call (via the same
    sheets_service.get_tab_values used by the processing tools) and never
    calls any Sheets-write function - Google Sheets remains the single
    source of truth, nothing is cached or written back.

    The textual response ALWAYS follows EXACTLY this fixed structure, with
    NO introductory paragraph and NO concluding paragraph - no "Fresh
    pull...", "Done...", "worth flagging...", "if you want...", "the
    dashboard above...", or similar narrative:

        Executive Overview (as of DD.MM.YYYY)
        - Open Issues: X
        - Critical/High: X
        - Accounting Exceptions: X
        - Sales: ₹X
        - Purchases: ₹X
        - Payments: ₹X

        Needs My Attention
        - concise item

        What Changed
        - concise item

        Patterns & Risks
        - concise item

        Required Actions
        - concise item

        [Interactive Dashboard]

    The Executive Overview heading always states the actual latest report
    date on record (never the server's clock), so it can never imply
    today's data is shown when the latest real report is from an earlier
    day. It never prints a bare "0" for Sales/Purchases/Payments when
    Accounting simply has no rows for that date - see "no accounting data
    available for this date" vs. a real computed 0. None of the five
    sections ever expose raw 10-column row data in this text - that detail
    stays one click away in the interactive dashboard.

    On an MCP Apps-capable client the interactive dashboard also renders
    inline in the chat, with its own Refresh button that re-calls this
    tool for live data; on any other client only the text above is shown.
    """
    monitoring_rows = get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB)
    accounting_rows = get_tab_values(service, SPREADSHEET_ID, ACCOUNTING_TAB)
    dashboard = build_dashboard(monitoring_rows, accounting_rows)

    summary_lines = _dashboard_summary_lines(dashboard)

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

    The report date is ALWAYS extracted from the summary's own title line
    (e.g. "SUNTROP SOLAR — PLANT MONITORING SUMMARY | 03-Sep-26") and
    stored as DD.MM.YYYY - never the server's current date. If no
    confident date can be extracted, this returns an error and writes
    NOTHING to Google Sheets - see summary_parser.parse_monitoring_summary.

    WORKFLOW 1 (EMPLOYEE REPORT PROCESSING) - strictly separate from
    WORKFLOW 2 (get_business_dashboard, the owner dashboard). This tool
    ONLY extracts concise structured records and saves them to Google
    Sheets. On success its ENTIRE response is exactly:

        Report processed successfully.

        - Date: DD.MM.YYYY
        - Records saved: N

    Nothing else - no dashboard summary, no executive commentary, no
    "worth flagging"/"worth checking", no missing-data explanations, no
    recommendations, no discussion of previous reports or date gaps, no
    claim that a dashboard was generated, and no raw report text. This
    tool never writes its own response text into Google Sheets. Call
    get_business_dashboard separately, as its own explicit step, if the
    owner's dashboard is actually being requested - do not merge the two.
    """

    # Deterministic parsing of the raw text into a structured dict, then
    # reshaped into per-section row lists sharing MONITORING_HEADERS'
    # column order - see summary_parser.py for both steps.
    parsed = parse_monitoring_summary(summary)

    # The report date must always come from the summary itself - if
    # parse_monitoring_summary couldn't confidently extract one, stop here
    # and write NOTHING (no rows, in any section) rather than guessing
    # today's date for a report that might be from a different day.
    if parsed["date"] is None:
        return (
            "Could not process this Monitoring summary: " + parsed["date_error"] + "\n"
            "Please resubmit with a valid, dated Plant Monitoring Summary "
            '(e.g. "SUNTROP SOLAR — PLANT MONITORING SUMMARY | 03-Sep-26"). '
            "No rows were written to Google Sheets."
        )

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

    # The day's ISSUES TODAY aggregate figures (New Issues/Issues
    # Resolved/Total Open Issues) are intentionally not surfaced here -
    # this tool's response is ONLY the fixed processing confirmation
    # (see docstring); executive-level totals belong to get_business_
    # dashboard, computed independently from the rows actually in Sheets.
    records_saved = needs_attention_count + actions_taken_count + needed_next_count + pattern_count

    return (
        "Report processed successfully.\n\n"
        f"- Date: {report_date}\n"
        f"- Records saved: {records_saved}"
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

    WORKFLOW 1 (EMPLOYEE REPORT PROCESSING) - strictly separate from
    WORKFLOW 2 (get_business_dashboard, the owner dashboard). This tool
    ONLY extracts concise structured records and saves them to Google
    Sheets. On success its ENTIRE response is exactly:

        Report processed successfully.

        - Date: DD.MM.YYYY
        - Records saved: N

    Nothing else - no dashboard summary, no executive commentary, no
    "worth flagging"/"worth checking", no missing-data explanations, no
    recommendations, no discussion of previous reports or date gaps, no
    claim that a dashboard was generated, and no raw report text. This
    tool never writes its own response text into Google Sheets. Call
    get_business_dashboard separately, as its own explicit step, if the
    owner's dashboard is actually being requested - do not merge the two.
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

    # This tool's response is ONLY the fixed processing confirmation (see
    # docstring) - per-section breakdowns belong to get_business_dashboard,
    # computed independently from the rows actually in Sheets.
    records_saved = (
        len(rows["cash"]) + len(rows["purchase"])
        + issues_count + sales_count + expenses_count + tax_count + pending_count
    )

    return (
        "Report processed successfully.\n\n"
        f"- Date: {report_date}\n"
        f"- Records saved: {records_saved}"
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

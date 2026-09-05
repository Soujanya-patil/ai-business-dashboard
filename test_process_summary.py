"""Local test for process_monitoring_summary() - no Claude Pro required.

Run with:
    python test_process_summary.py

This exercises the full flow (raw summary text -> parser -> row-building ->
Google Sheets) using the credentials.json / SPREADSHEET_ID already
configured in mcp_server.py. The Sheets-writing/idempotency checks write
real rows into the single unified "Monitoring" tab (10-column, concise
business schema, no Section column - see summary_parser.MONITORING_HEADERS).
No Anthropic API call is involved.

ONE ROW = ONE IMPORTANT BUSINESS RECORD: long narrative Issue/Action Taken/
Next Action text is shortened before being stored (see summary_parser.
_shorten_issue_text/_shorten_action_text) rather than kept verbatim - Test 5b
below reproduces the exact worked example from the schema spec end to end.

Covers two input shapes:
  A. The clean template (REPORTED_BUG_SUMMARY, NO_ISSUES_SUMMARY).
  B. A real-world messy report (MESSY_SUMMARY) where values aren't clean:
     "Unconfirmed" metrics, multi-site bullets (some with per-site values
     mapped by position), numbered What's Needed Next items, Service
     Pattern Watch bullets with inline/leading site references, and
     free-text qualifiers that must survive rather than being dropped.

Determinism note: the Sheets-writing/idempotency checks generate fresh,
uniquely-tagged content on every run (see _build_unique_summary and
_build_unique_messy_summary) instead of reusing fixed site names and a
fixed near-term date, and use a synthetic year (2099) so they can never be
mistaken for, or overwrite, a real admin-submitted report. They are
SKIPPED BY DEFAULT (see RUN_LIVE_SHEETS_TESTS below) so a normal run of
this file never writes anything to the configured spreadsheet - set
RUN_LIVE_SHEETS_TESTS=1 to opt in and exercise them against it.

Dates: every row's Date cell is written as DD.MM.YYYY (e.g. "03.09.2026"),
never ISO format - see summary_parser.SHEET_DATE_FORMAT. The report date
always comes from the summary's own title line; process_monitoring_summary
refuses to write anything and returns an error if no confident date can be
extracted (see Tests 18-21).
"""

import os
import uuid

from summary_parser import parse_monitoring_summary, build_monitoring_rows, MONITORING_HEADERS
from mcp_server import process_monitoring_summary, service, SPREADSHEET_ID, MONITORING_TAB
from sheets_service import get_tab_values

# Sheets-writing/idempotency tests (16-17) are opt-in only - they write
# real (if synthetically-tagged) rows to whatever spreadsheet mcp_server.py
# is currently configured for. Default OFF so running this file never
# populates the production spreadsheet with test data.
RUN_LIVE_SHEETS_TESTS = os.getenv("RUN_LIVE_SHEETS_TESTS") == "1"

# Exact repro of a real parsing bug: this summary was returning 0 rows for
# every section. Root cause was the old line-by-line parser swallowing the
# whole message into the title line whenever section headers weren't
# reliably on their own physical line (e.g. an MCP client that collapses
# embedded newlines in a single-line text argument). Kept here verbatim as
# a parser-only regression test - it never touches Google Sheets, so it's
# already fully deterministic.
REPORTED_BUG_SUMMARY = """
SUNTROP SOLAR — PLANT MONITORING SUMMARY | 02 Sep 2026

ISSUES TODAY
- New issues detected: 3
- Issues resolved today: 2
- Total issues currently open: 7

⚠️ NEEDS ATTENTION
- Bengaluru — Inverter failure — open 8 days — awaiting vendor replacement

ACTIONS TAKEN TODAY
- Mysuru — Panel fault — replacement scheduled

WHAT'S NEEDED NEXT
- Approval needed for expedited shipping

SERVICE PATTERN WATCH
- Recurring inverter failures at Bengaluru site
"""

# parse_monitoring_summary()'s structured dict is unchanged by the schema
# change below - only build_monitoring_rows()'s OUTPUT row shape changed
# (14 columns, no Section, new classified fields). This checks the parser
# layer is still exactly right.
EXPECTED_PARSED_BUG_SUMMARY = {
    "date": "02.09.2026",
    "date_error": None,
    "new_issues": 3,
    "resolved_issues": 2,
    "total_open_issues": 7,
    "issues_today_notes": "",
    "needs_attention": [
        {
            "site": "Bengaluru",
            "description": "Inverter failure — awaiting vendor replacement",
            "days_open": 8,
        }
    ],
    "actions_taken": [
        {"site": "Mysuru", "description": "Panel fault", "action": "replacement scheduled"}
    ],
    "whats_needed_next": [{"site": "", "requirement": "Approval needed for expedited shipping"}],
    "service_pattern_watch": [
        {
            "pattern": "Recurring inverter failures at Bengaluru site",
            "site": "",
            "vendor": "",
            "notes": "",
        }
    ],
}

# Expected unified-schema rows for REPORTED_BUG_SUMMARY, in MONITORING_HEADERS
# column order: Date, Site, Issue, Category, Priority, Status, Days Open,
# Action Taken, Next Action, Vendor. No Section column, no New Issues/
# Issues Resolved/Total Open Issues/Notes columns, and no aggregate row -
# see ONE_ROW_ONE_RECORD tests below for why there's no "daily_summary" key.
# Every bullet here is already short (<= 12 words), so nothing gets
# shortened further - see Test 5b for a bullet long enough to trigger
# shortening.
EXPECTED_BUG_SUMMARY_ROWS = {
    "needs_attention": [
        ["02.09.2026", "Bengaluru", "Inverter failure — awaiting vendor replacement", "Inverter", "", "Open", 8, "", "", ""]
    ],
    "actions_taken": [
        ["02.09.2026", "Mysuru", "Panel fault", "Service", "", "Open", "", "replacement scheduled", "", ""]
    ],
    "whats_needed_next": [
        ["02.09.2026", "", "Approval needed for expedited shipping", "", "", "Open", "", "", "Approval needed for expedited shipping", ""]
    ],
    "service_pattern_watch": [
        ["02.09.2026", "", "Recurring inverter failures at Bengaluru site", "Inverter", "", "Monitoring", "", "", "", ""]
    ],
}

# The exact worked example from the schema spec: a long narrative bullet
# that MUST be shortened rather than stored verbatim, while Category/
# Priority/Status/Next Action are still correctly derived from the full
# text. This is the canonical "ONE ROW = ONE IMPORTANT BUSINESS RECORD"
# proof for Monitoring.
LONG_NARRATIVE_SUMMARY = """
SUNTROP SOLAR — PLANT MONITORING SUMMARY | 03 Sep 2026

NEEDS ATTENTION
- 079 Oaza Global Krishnagiri — full outage today (all 4 inverters down, 280 min collectively) plus 31 optimizers at 100% down and 59 at 50% down out of 615, with remark inverter no 5 is not working. Recommend priority escalation to resolve before end of day.
"""

EXPECTED_LONG_NARRATIVE_ROW = [
    "03.09.2026", "079 Oaza Global Krishnagiri", "Full inverter outage + Optimizer failures",
    "Outage", "Critical", "Open", "", "", "Priority escalation", "",
]

# A summary with nothing to escalate - none of these sections should
# produce fabricated rows. Also parser-only (no Sheets write).
NO_ISSUES_SUMMARY = """
SUNTROP SOLAR — PLANT MONITORING SUMMARY | September 1, 2026

ISSUES TODAY
- New issues detected: 0
- Issues resolved today: 0
- Total issues currently open: 3

⚠️ NEEDS ATTENTION
- No overdue or escalated issues today.

ACTIONS TAKEN TODAY

WHAT'S NEEDED NEXT

SERVICE PATTERN WATCH
(Only when relevant.)
"""

# The real-world report that previously "did not parse/work correctly":
# unclean/free-text metrics, multi-site bullets (some with per-site values
# in matching order), numbered What's Needed Next items, Service Pattern
# Watch bullets with a leading bare site name (no dash) and an inline
# multi-site mention, and qualifiers that must not be dropped. Uses the
# "DD-Mon-YY" title-date style (e.g. "03-Sep-26") - a real title line is
# required now: a missing one no longer falls back to today's date, it's
# a hard error (see Tests 18-21) - so unlike earlier versions of this
# fixture, this one can't omit it and still expect rows to be produced.
MESSY_SUMMARY = """SUNTROP SOLAR — PLANT MONITORING SUMMARY | 03-Sep-26

ISSUES TODAY

- New issues detected: Unconfirmed — can't distinguish new vs. recurring without tracker
- Issues resolved today: Unconfirmed — need tracker to verify any issue moved to "Done at Site"
- Total issues currently open: 18 sites reporting today (5 inverter-only, 12 optimizer-only, 1 with both — see below)

NEEDS ATTENTION

- 079 Oaza Global Krishnagiri — full outage today (all 4 inverters down, 280 min collectively) plus 31 optimizers at 100% down and 59 at 50% down out of 615, with remark "inverter no 5 is not working."
- 034 IIM Bangalore-2023, 017 Prabhu Kanakpura Road, 075 nVent Rajadhani Paper Bidadi, 066 Matrinox Riddhi Siddhi Metal Jigani — not appearing in today's report at all.
- 027 Ranganath babu Mahalakshmi layout, 055 MediTech, 072 Harish Pillai — repeated inverter tripping today (10x/350min, 14x/135min, 7x/205min respectively).

ACTIONS TAKEN TODAY

- 011 R P Metal Sections Pvt Ltd Bidadi — 3 optimizers received; noted "tomorrow going for replacement"
- 084 Bentley India Pvt Ltd — 1 optimizer received; noted "tomorrow going for replacement"
- 088 Mechano Unit 1 — assigned to Rakesh for on-site check
- 008, 009, 012, 022, 024, 060 — remarks show "case filed/approved/received," but these read as status labels, not explicitly dated to today.
- 042 Jain Mission Trust — "one received, one case filed"
- 056 Shree LakshmiNarasimha Agro — one optimizer received, one still needs on-site check

WHAT'S NEEDED NEXT

1. Upload the Master Issue Tracker so I can reconcile today's 18 sites...
2. Confirm which remarks above reflect actions taken today...
3. Status check needed on 034, 017, 075, 066...
4. Site visit confirmation needed for 011 and 084 tomorrow...

SERVICE PATTERN WATCH

- 079 Oaza Global Krishnagiri showing simultaneous full inverter outage...
- Three separate sites (027, 055, 072) logged inverter tripping today...
"""


def _build_unique_summary(run_id: str, report_date: str) -> str:
    """Clean-template summary, uniquely tagged - see module docstring."""
    return f"""
SUNTROP SOLAR — PLANT MONITORING SUMMARY | {report_date}

ISSUES TODAY
- New issues detected: 3
- Issues resolved today: 2
- Total issues currently open: 7

⚠️ NEEDS ATTENTION
- {run_id}-Bengaluru — Inverter failure — open 8 days — awaiting vendor replacement

ACTIONS TAKEN TODAY
- {run_id}-Mysuru — Panel fault — replacement scheduled

WHAT'S NEEDED NEXT
- {run_id}-Mysuru — Follow-up call needed with vendor

SERVICE PATTERN WATCH
- Recurring inverter failures at Bengaluru site [{run_id}]
"""


def _build_unique_messy_summary(run_id: str, report_date: str) -> str:
    """Messy-style summary, uniquely tagged, covering the same tricky
    shapes as MESSY_SUMMARY (Unconfirmed metric, a multi-site bullet with
    per-site values mapped by position, a numbered What's Needed Next
    list, and a Service Pattern Watch bullet with an inline multi-site
    mention) so the live Sheets round-trip test also exercises that code
    path, deterministically.
    """
    return f"""
SUNTROP SOLAR — PLANT MONITORING SUMMARY | {report_date}

ISSUES TODAY
- New issues detected: Unconfirmed — needs tracker [{run_id}]
- Issues resolved today: 1
- Total issues currently open: 5

NEEDS ATTENTION
- 101 {run_id} Site A, 102 {run_id} Site B — repeated tripping today (3x/50min, 5x/70min respectively).

ACTIONS TAKEN TODAY
- 201 {run_id} Site C — case filed, not yet confirmed, follow up tomorrow

WHAT'S NEEDED NEXT
1. Confirm status for {run_id} sites tomorrow...
2. Escalate {run_id} priority items...

SERVICE PATTERN WATCH
- Two sites (301, 302) logged a chronic recurring pattern for tag {run_id}, priority escalation recommended
"""


if __name__ == "__main__":
    print("=== Test 1: exact 10-column schema, no Section column ===")
    assert MONITORING_HEADERS == [
        "Date", "Site", "Issue", "Category", "Priority", "Status", "Days Open",
        "Action Taken", "Next Action", "Vendor",
    ], f"Unexpected schema: {MONITORING_HEADERS}"
    assert len(MONITORING_HEADERS) == 10
    assert "Section" not in MONITORING_HEADERS
    for removed in ("New Issues", "Issues Resolved", "Total Open Issues", "Notes"):
        assert removed not in MONITORING_HEADERS, f"{removed!r} should have been removed from the schema"
    print("OK\n")

    print("=== Test 2: sections with no real data produce no rows ===")
    parsed = parse_monitoring_summary(NO_ISSUES_SUMMARY)
    print(parsed)
    assert parsed["date"] == "01.09.2026"
    assert parsed["needs_attention"] == [], "Expected no Needs Attention rows"
    assert parsed["actions_taken"] == [], "Expected no Actions Taken rows"
    assert parsed["whats_needed_next"] == [], "Expected no What's Needed Next rows"
    assert parsed["service_pattern_watch"] == [], "Expected no Service Pattern Watch rows"
    print("OK: sections with no real data produced no rows.\n")

    print("=== Test 3: clean summary parses exactly as expected ===")
    bug_parsed = parse_monitoring_summary(REPORTED_BUG_SUMMARY)
    print(bug_parsed)
    assert bug_parsed == EXPECTED_PARSED_BUG_SUMMARY, (
        f"Parser output does not match expected structure.\nGot: {bug_parsed}"
    )
    print("OK: parser produces exactly the expected structure.\n")

    print("=== Test 4: MCP Inspector-style collapsed/whitespace-normalized input still parses correctly ===")
    collapsed = REPORTED_BUG_SUMMARY.replace("\n", " ")
    collapsed_parsed = parse_monitoring_summary(collapsed)
    assert collapsed_parsed == EXPECTED_PARSED_BUG_SUMMARY, (
        f"Collapsed input did not parse the same as normal input.\nGot: {collapsed_parsed}"
    )
    print("OK: collapsed single-line input parses identically to normal multi-line input.\n")

    print("=== Test 5: build_monitoring_rows() maps the clean summary to the correct 10-column rows ===")
    bug_rows = build_monitoring_rows(bug_parsed)
    assert "daily_summary" not in bug_rows, "build_monitoring_rows() must not produce an aggregate row at all"
    assert set(bug_rows.keys()) == {"needs_attention", "actions_taken", "whats_needed_next", "service_pattern_watch"}
    for section, expected_rows in EXPECTED_BUG_SUMMARY_ROWS.items():
        actual_rows = bug_rows[section]
        assert actual_rows == expected_rows, f"{section}: expected {expected_rows}, got {actual_rows}"
        for row in actual_rows:
            assert len(row) == 10, f"{section} row has wrong column count: {row}"
    print("OK: all four sections produced the correct 10-column Monitoring row(s); no aggregate section at all.\n")

    print("=== Test 5b: long narrative text is SHORTENED into a concise structured row (schema spec worked example) ===")
    long_parsed = parse_monitoring_summary(LONG_NARRATIVE_SUMMARY)
    long_rows = build_monitoring_rows(long_parsed)
    assert len(long_rows["needs_attention"]) == 1, long_rows["needs_attention"]
    actual_long_row = long_rows["needs_attention"][0]
    assert actual_long_row == EXPECTED_LONG_NARRATIVE_ROW, (
        f"Expected {EXPECTED_LONG_NARRATIVE_ROW}, got {actual_long_row}"
    )
    original_bullet_word_count = len(long_parsed["needs_attention"][0]["description"].split())
    shortened_issue_word_count = len(actual_long_row[MONITORING_HEADERS.index("Issue")].split())
    assert original_bullet_word_count > 30, "Fixture bullet should be long enough to actually exercise shortening"
    assert shortened_issue_word_count <= 6, (
        f"Issue cell must be a short phrase, not the {original_bullet_word_count}-word original bullet: {actual_long_row}"
    )
    print(f"Original bullet: {original_bullet_word_count} words -> stored Issue: {shortened_issue_word_count} words")
    print("OK: the full paragraph was never stored - Sheets got a concise structured row instead.\n")

    print("=== Test 6: daily metrics are NOT lost - still available on the parsed dict, just not as a row ===")
    assert bug_parsed["new_issues"] == 3
    assert bug_parsed["resolved_issues"] == 2
    assert bug_parsed["total_open_issues"] == 7
    print("OK: New Issues / Issues Resolved / Total Open Issues remain fully parsed and accessible.\n")

    print("=== Test 7: a summary without a Service Pattern Watch section still works ===")
    no_spw = MESSY_SUMMARY.split("SERVICE PATTERN WATCH")[0]
    no_spw_parsed = parse_monitoring_summary(no_spw)
    assert no_spw_parsed["service_pattern_watch"] == []
    assert len(no_spw_parsed["actions_taken"]) > 0
    no_spw_rows = build_monitoring_rows(no_spw_parsed)
    assert no_spw_rows["service_pattern_watch"] == []
    print("OK: summary without Service Pattern Watch parses and builds rows correctly.\n")

    print("=== Test 8: real-world messy summary - all five sections detected, nothing crashes ===")
    messy_parsed = parse_monitoring_summary(MESSY_SUMMARY)
    messy_rows = build_monitoring_rows(messy_parsed)
    for key in ("needs_attention", "actions_taken", "whats_needed_next", "service_pattern_watch"):
        assert len(messy_parsed[key]) > 0, f"Expected at least one {key} row"
    print(f"needs_attention: {len(messy_parsed['needs_attention'])} rows")
    print(f"actions_taken: {len(messy_parsed['actions_taken'])} rows")
    print(f"whats_needed_next: {len(messy_parsed['whats_needed_next'])} rows")
    print(f"service_pattern_watch rows built: {len(messy_rows['service_pattern_watch'])}")
    print("OK: all five sections detected.\n")

    print("=== Test 9: 'ALL SITES' (or any equivalent aggregate label) is never created, in any build ===")
    _AGGREGATE_LABELS = {"ALL SITES", "ALL", "TOTAL"}
    site_idx = MONITORING_HEADERS.index("Site")
    for label, row_dict in (("clean summary", bug_rows), ("messy summary", messy_rows), ("no-SPW summary", no_spw_rows)):
        assert "daily_summary" not in row_dict, f"{label}: build_monitoring_rows() must not return an aggregate section"
        all_produced_rows = [row for section_rows in row_dict.values() for row in section_rows]
        assert all_produced_rows, f"{label}: expected at least one real row to check"
        for row in all_produced_rows:
            site_value = row[site_idx].strip().upper()
            assert site_value not in _AGGREGATE_LABELS, (
                f"{label}: found aggregate Site label {row[site_idx]!r} in row {row} - "
                "every row must be a real operational record"
            )
    print("OK: scanned every row produced by both the clean and messy summaries - no aggregate")
    print("    Site label ('ALL SITES', 'ALL', 'TOTAL') anywhere, and no 'daily_summary' section at all.\n")

    print("=== Test 10: Unconfirmed metrics are preserved, not converted to 0 ===")
    assert isinstance(messy_parsed["new_issues"], str) and "Unconfirmed" in messy_parsed["new_issues"], (
        f"Expected new_issues to preserve 'Unconfirmed' text, got: {messy_parsed['new_issues']!r}"
    )
    assert isinstance(messy_parsed["resolved_issues"], str) and "Unconfirmed" in messy_parsed["resolved_issues"], (
        f"Expected resolved_issues to preserve 'Unconfirmed' text, got: {messy_parsed['resolved_issues']!r}"
    )
    assert messy_parsed["total_open_issues"] == 18, (
        f"Expected total_open_issues to parse the explicit number 18, got: {messy_parsed['total_open_issues']!r}"
    )
    assert "daily_summary" not in messy_rows, "No aggregate row/section for the messy summary either"
    print("new_issues:", messy_parsed["new_issues"])
    print("resolved_issues:", messy_parsed["resolved_issues"])
    print("total_open_issues:", messy_parsed["total_open_issues"])
    print("OK: 'Unconfirmed' preserved verbatim; explicit numeric value still parsed when present.\n")

    print("=== Test 11: multiple-site bullets are split into rows, not silently lost ===")
    na_sites = [item["site"] for item in messy_parsed["needs_attention"]]
    for code in ("034 IIM Bangalore-2023", "017 Prabhu Kanakpura Road",
                 "075 nVent Rajadhani Paper Bidadi", "066 Matrinox Riddhi Siddhi Metal Jigani"):
        assert code in na_sites, f"Expected site {code!r} to have its own Needs Attention row"
    not_appearing_rows = [item for item in messy_parsed["needs_attention"] if "not appearing" in item["description"]]
    assert len(not_appearing_rows) == 4, f"Expected 4 rows for the 'not appearing' bullet, got {len(not_appearing_rows)}"

    tripping_by_site = {
        item["site"]: item["description"]
        for item in messy_parsed["needs_attention"]
        if "tripping" in item["description"]
    }
    assert tripping_by_site["027 Ranganath babu Mahalakshmi layout"].endswith("(10x/350min)")
    assert tripping_by_site["055 MediTech"].endswith("(14x/135min)")
    assert tripping_by_site["072 Harish Pillai"].endswith("(7x/205min)")

    at_sites = [item["site"] for item in messy_parsed["actions_taken"]]
    for code in ("008", "009", "012", "022", "024", "060"):
        assert code in at_sites, f"Expected site code {code!r} to have its own Actions Taken row"
    assert len(messy_parsed["needs_attention"]) == 8, len(messy_parsed["needs_attention"])
    print("OK: multi-site bullets correctly split into per-site rows, with per-site values mapped by position.\n")

    print("=== Test 12: numbered next-action items are preserved individually ===")
    requirements = [item["requirement"] for item in messy_parsed["whats_needed_next"]]
    assert len(requirements) == 4, f"Expected 4 numbered items, got {len(requirements)}: {requirements}"
    assert any("Master Issue Tracker" in r for r in requirements)
    assert any(r.rstrip().endswith("tomorrow...") for r in requirements), (
        "Expected the last numbered item's trailing '...' to survive intact"
    )
    for row in messy_rows["whats_needed_next"]:
        assert row[MONITORING_HEADERS.index("Next Action")], "Every What's Needed Next row must populate Next Action"
    print("OK: all 4 numbered items preserved as individual rows, full text intact.\n")

    print("=== Test 13: Service Pattern Watch information becomes normal Monitoring rows ===")
    spw_rows = messy_rows["service_pattern_watch"]
    spw_sites = [row[MONITORING_HEADERS.index("Site")] for row in spw_rows]
    assert "079 Oaza Global Krishnagiri" in spw_sites, (
        f"Expected the leading bare site name to be extracted even with no dash separator, got sites: {spw_sites}"
    )
    for row in spw_rows:
        assert row[MONITORING_HEADERS.index("Status")] == "Monitoring", (
            f"Expected Service Pattern Watch rows to default to Status=Monitoring, got: {row}"
        )
    inline_site_rows = [row for row in spw_rows if "logged inverter tripping" in row[MONITORING_HEADERS.index("Issue")]]
    assert {row[MONITORING_HEADERS.index("Site")] for row in inline_site_rows} == {"027", "055", "072"}, (
        "Expected the inline '(027, 055, 072)' mention to split into 3 separate rows, one per site"
    )
    outage_row = next(r for r in spw_rows if r[MONITORING_HEADERS.index("Site")] == "079 Oaza Global Krishnagiri")
    assert outage_row[MONITORING_HEADERS.index("Category")] == "Outage"
    assert outage_row[MONITORING_HEADERS.index("Priority")] == "Critical"
    print("OK: Service Pattern Watch produced normal rows (no separate Section/sheet), with sites correctly")
    print("    identified from both a bare leading name and an inline multi-site mention.\n")

    print("=== Test 14: Actions Taken qualifiers are preserved, not stripped ===")
    actions_text = " | ".join(item["action"] for item in messy_parsed["actions_taken"])
    for qualifier in ("tomorrow", "not explicitly dated to today"):
        assert qualifier in actions_text, f"Expected qualifier {qualifier!r} to survive somewhere in Actions Taken"
    print("OK: qualifiers like 'tomorrow' and 'not explicitly dated to today' survived.\n")

    print("=== Test 15: no useful information disappears (spot-check distinctive phrases across all rows) ===")
    full_dump = str(messy_parsed) + str(messy_rows)
    for phrase in (
        "inverter no 5 is not working",
        "case filed/approved/received",
        "one received, one case filed",
        "Confirm which remarks above reflect actions taken today",
    ):
        assert phrase in full_dump, f"Expected {phrase!r} to appear somewhere in the parsed output"
    print("OK: spot-checked distinctive phrases all survived somewhere in the output.\n")

    if not RUN_LIVE_SHEETS_TESTS:
        print("=== Tests 16-17: Google Sheets integration + idempotency checks SKIPPED ===")
        print("Set RUN_LIVE_SHEETS_TESTS=1 to run these against the configured production spreadsheet")
        print("(they write real, synthetically-tagged 2099-dated rows and are opt-in on purpose).\n")
    else:
        print("=== Test 16: Google Sheets integration + idempotency check (clean-template style) ===")
        run_uuid = uuid.uuid4()
        run_id = run_uuid.hex[:10]
        synthetic_month = (run_uuid.int % 12) + 1
        synthetic_day = (run_uuid.int % 28) + 1
        # ISO stays a valid INPUT format for the title line; the row
        # actually written to Sheets must be DD.MM.YYYY - these are two
        # different strings for the same calendar date.
        synthetic_date_input = f"2099-{synthetic_month:02d}-{synthetic_day:02d}"
        synthetic_date_output = f"{synthetic_day:02d}.{synthetic_month:02d}.2099"
        summary_text = _build_unique_summary(run_id, synthetic_date_input)
        print(f"Generated run_id={run_id!r}, synthetic date={synthetic_date_input!r} -> stored as {synthetic_date_output!r}")

        result_1 = process_monitoring_summary(summary_text)
        print(result_1)
        assert result_1 == "Report processed successfully.\n\n- Date: " + synthetic_date_output + "\n- Records saved: 4", result_1
        assert "ALL SITES" not in result_1, "Response text must never mention an aggregate site"

        all_rows = get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB)
        header = all_rows[0]
        assert header == MONITORING_HEADERS, f"Live sheet header does not match: {header}"
        tagged_rows = [row for row in all_rows if any(run_id in str(cell) for cell in row)]
        site_idx = MONITORING_HEADERS.index("Site")
        for row in tagged_rows:
            assert row[0] == synthetic_date_output, f"Row has wrong date (expected DD.MM.YYYY): {row}"
            assert row[site_idx].strip().upper() not in {"ALL SITES", "ALL", "TOTAL"}, (
                f"Found an aggregate Site value actually written to the live sheet: {row}"
            )

        result_2 = process_monitoring_summary(summary_text)
        print(result_2)
        assert result_2 == "Report processed successfully.\n\n- Date: " + synthetic_date_output + "\n- Records saved: 0", result_2

        tagged_rows_after = [row for row in get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB) if any(run_id in str(cell) for cell in row)]
        assert len(tagged_rows_after) == len(tagged_rows), (
            f"Row count for run_id={run_id!r} changed after a duplicate write: {len(tagged_rows)} -> {len(tagged_rows_after)}"
        )
        print(f"OK: first write created {len(tagged_rows)} new rows; duplicate write created 0 more. Live header verified.\n")

        print("=== Test 17: Google Sheets integration + idempotency check (messy-style, multi-row bullets) ===")
        messy_run_uuid = uuid.uuid4()
        messy_run_id = messy_run_uuid.hex[:10]
        messy_month = (messy_run_uuid.int % 12) + 1
        messy_day = (messy_run_uuid.int % 28) + 1
        messy_synthetic_date_input = f"2099-{messy_month:02d}-{messy_day:02d}"
        messy_synthetic_date_output = f"{messy_day:02d}.{messy_month:02d}.2099"
        messy_summary_text = _build_unique_messy_summary(messy_run_id, messy_synthetic_date_input)
        print(f"Generated run_id={messy_run_id!r}, synthetic date={messy_synthetic_date_input!r} -> stored as {messy_synthetic_date_output!r}")

        messy_result_1 = process_monitoring_summary(messy_summary_text)
        print(messy_result_1)
        # The 2-site tripping bullet produces 2 Needs Attention rows; the
        # 2-site inline Service Pattern Watch mention produces 2 more rows:
        # 2 (Needs Attention) + 1 (Actions Taken) + 2 (What's Needed Next) +
        # 2 (Service Pattern Watch) = 7 records saved. (The Unconfirmed New
        # Issues figure is preserved at the parser level - see Test 10 -
        # but is no longer surfaced in this tool's response text by design:
        # the response is ONLY the fixed processing confirmation.)
        assert messy_result_1 == (
            "Report processed successfully.\n\n- Date: " + messy_synthetic_date_output + "\n- Records saved: 7"
        ), messy_result_1
        assert "ALL SITES" not in messy_result_1

        messy_tagged_before = [
            row for row in get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB)
            if any(messy_run_id in str(cell) for cell in row)
        ]
        messy_site_idx = MONITORING_HEADERS.index("Site")
        for row in messy_tagged_before:
            assert row[0] == messy_synthetic_date_output, f"Row has wrong date (expected DD.MM.YYYY): {row}"
            assert row[messy_site_idx].strip().upper() not in {"ALL SITES", "ALL", "TOTAL"}, (
                f"Found an aggregate Site value actually written to the live sheet: {row}"
            )

        messy_result_2 = process_monitoring_summary(messy_summary_text)
        print(messy_result_2)
        assert messy_result_2 == (
            "Report processed successfully.\n\n- Date: " + messy_synthetic_date_output + "\n- Records saved: 0"
        ), messy_result_2

        messy_tagged_after = [
            row for row in get_tab_values(service, SPREADSHEET_ID, MONITORING_TAB)
            if any(messy_run_id in str(cell) for cell in row)
        ]
        assert len(messy_tagged_after) == len(messy_tagged_before), (
            f"Row count changed after a duplicate write of the messy-style summary: "
            f"{len(messy_tagged_before)} -> {len(messy_tagged_after)}"
        )
        print(f"OK: first write created {len(messy_tagged_before)} new rows (including split multi-site rows); "
              "duplicate write created 0 more.\n")

    print("=== Test 18: DD.MM.YYYY date format conversions ===")
    date_format_template = """SUNTROP SOLAR — PLANT MONITORING SUMMARY | {title_date}

NEEDS ATTENTION
- Test Site — minor issue — open 1 day — no action needed yet
"""
    d1 = parse_monitoring_summary(date_format_template.format(title_date="03-Sep-26"))
    assert d1["date"] == "03.09.2026", d1["date"]
    d2 = parse_monitoring_summary(date_format_template.format(title_date="4-Sep-26"))
    assert d2["date"] == "04.09.2026", d2["date"]
    print("OK: '03-Sep-26' -> '03.09.2026', '4-Sep-26' -> '04.09.2026'.\n")

    print("=== Test 19: missing/unparseable report date - parser flags it, NEVER falls back to today ===")
    no_date_summary = """SUNTROP SOLAR — PLANT MONITORING SUMMARY

NEEDS ATTENTION
- Test Site — minor issue — open 1 day — no action needed yet
"""
    unparseable_date_summary = """SUNTROP SOLAR — PLANT MONITORING SUMMARY | not-a-real-date

NEEDS ATTENTION
- Test Site — minor issue — open 1 day — no action needed yet
"""
    no_date_parsed = parse_monitoring_summary(no_date_summary)
    assert no_date_parsed["date"] is None, no_date_parsed["date"]
    assert no_date_parsed["date_error"], "Expected a date_error message when no date is present at all"

    bad_date_parsed = parse_monitoring_summary(unparseable_date_summary)
    assert bad_date_parsed["date"] is None, bad_date_parsed["date"]
    assert bad_date_parsed["date_error"], "Expected a date_error message for an unparseable date"
    print("date_error (no date):", no_date_parsed["date_error"])
    print("date_error (bad date):", bad_date_parsed["date_error"])
    print("OK: both come back as date=None with a clear date_error - never a silent fallback to today.\n")

    print("=== Test 20: process_monitoring_summary refuses to write and returns a clear error (zero Sheets writes) ===")
    # Safe to call for real (even against the production spreadsheet):
    # mcp_server.process_monitoring_summary returns its error string
    # immediately once parsed["date"] is None, BEFORE build_monitoring_rows
    # or any append_unique_rows/Sheets call - this path never reaches the
    # network, so no rows can possibly be written by this test.
    result_no_date = process_monitoring_summary(no_date_summary)
    print(result_no_date)
    assert "Could not process this Monitoring summary" in result_no_date, result_no_date
    assert "No rows were written" in result_no_date, result_no_date

    result_bad_date = process_monitoring_summary(unparseable_date_summary)
    print(result_bad_date)
    assert "No rows were written" in result_bad_date, result_bad_date
    print("OK: a clear error is returned and Google Sheets is never reached when no confident date is found.\n")

    print("=== Test 21: every row generated from one summary carries THAT summary's date, not the server's current date ===")
    from datetime import date as _date, timedelta as _timedelta
    different_date = _date.today() - _timedelta(days=100)
    different_date_title = different_date.strftime("%d-%b-%y")
    expected_sheet_date = different_date.strftime("%d.%m.%Y")
    assert expected_sheet_date != _date.today().strftime("%d.%m.%Y"), "Fixture must use a date other than today"
    multi_row_summary = f"""SUNTROP SOLAR — PLANT MONITORING SUMMARY | {different_date_title}

NEEDS ATTENTION
- 079 Oaza Global Krishnagiri — full inverter outage — open 2 days — priority escalation required
- 027 Ranganath Mahalakshmi Layout — repeated inverter tripping — open 1 day — service inspection needed

ACTIONS TAKEN TODAY
- 011 R P Metal Sections — replacement received — site visit scheduled
"""
    multi_parsed = parse_monitoring_summary(multi_row_summary)
    assert multi_parsed["date"] == expected_sheet_date, (
        f"Expected the row date to come from the summary ({expected_sheet_date}), got {multi_parsed['date']}"
    )
    multi_rows = build_monitoring_rows(multi_parsed)
    all_generated_rows = [row for section_rows in multi_rows.values() for row in section_rows]
    assert len(all_generated_rows) == 3, all_generated_rows
    date_col = MONITORING_HEADERS.index("Date")
    for row in all_generated_rows:
        assert row[date_col] == expected_sheet_date, f"Row did not carry the summary's own date: {row}"
    print(f"OK: all {len(all_generated_rows)} rows generated from this summary carry its date ({expected_sheet_date}), "
          f"not today's server date ({_date.today().strftime('%d.%m.%Y')}).\n")

    print("=== Test 22: reprocessing the same summary is deterministic (the precondition sheets_service's exact-match ===")
    print("             dedup relies on to guarantee no duplicate rows are ever written) ===")
    # sheets_service.append_unique_rows/upsert_row_by_key detect a
    # duplicate by comparing rows for EXACT equality - that guarantee only
    # holds if parsing + row-building is itself a pure, deterministic
    # function of the summary text. This proves that precondition locally,
    # without touching Google Sheets (the live round-trip is covered
    # separately by the opt-in RUN_LIVE_SHEETS_TESTS=1 Tests 16-17 above).
    first_pass = build_monitoring_rows(parse_monitoring_summary(MESSY_SUMMARY))
    second_pass = build_monitoring_rows(parse_monitoring_summary(MESSY_SUMMARY))
    assert first_pass == second_pass, "Re-parsing/re-building the identical summary must yield byte-identical rows"
    print("OK: parsing and building the same summary twice produces byte-identical rows.\n")

    print("=== Test 23: process_monitoring_summary (WORKFLOW 1) never merges into the owner dashboard (WORKFLOW 2) ===")
    import ast
    import inspect

    process_source = inspect.getsource(process_monitoring_summary)
    # Structural proof: this tool's code path never calls the dashboard-
    # building functions - Workflow 1 (employee report processing) and
    # Workflow 2 (get_business_dashboard, the owner dashboard) must stay
    # fully separate. process_monitoring_summary can never itself compute
    # or claim to have generated a dashboard.
    for forbidden_call in ("build_dashboard", "_dashboard_summary_lines", "get_business_dashboard("):
        assert forbidden_call not in process_source, (
            f"process_monitoring_summary must never reference {forbidden_call!r} - "
            "Workflow 1 and Workflow 2 must stay separate"
        )
    # The EXECUTABLE code (docstring excluded, since the docstring itself
    # documents these as BANNED phrases - e.g. 'no "worth flagging"...' -
    # which would otherwise be a false positive) must never actually
    # produce dashboard/narrative/hedging language in a real response.
    func_node = ast.parse(process_source).body[0]
    body_nodes = func_node.body[1:] if isinstance(func_node.body[0], ast.Expr) else func_node.body
    code_only = "\n".join(ast.get_source_segment(process_source, n) for n in body_nodes).lower()
    for banned_phrase in (
        "worth flagging", "worth checking", "if you want", "dashboard above",
        "master issue tracker", "this matches", "nothing suspicious",
        "fresh pull", "executive overview", "needs my attention",
    ):
        assert banned_phrase not in code_only, (
            f"Found banned dashboard/narrative phrase {banned_phrase!r} in process_monitoring_summary's executable code"
        )
    assert '"report processed successfully.' in code_only, (
        "Success response must use the fixed 'Report processed successfully.' confirmation template"
    )
    print("OK: process_monitoring_summary's code path never calls dashboard-building functions, "
          "and its response template carries no narrative language.\n")

    print("=== Test 24: process_monitoring_summary's SUCCESS response is exactly the fixed confirmation format ===")
    # append_unique_rows is stubbed out for the duration of this call, so
    # this exercises the real function end-to-end (parsing, row-building,
    # response assembly) WITHOUT making any live Sheets/network call -
    # unlike Tests 16-17, this one always runs (no RUN_LIVE_SHEETS_TESTS
    # gate needed) since it never touches the production spreadsheet.
    import mcp_server

    original_append_unique_rows = mcp_server.append_unique_rows

    def _fake_append_unique_rows(service, spreadsheet_id, tab_name, headers, rows, key_indexes):
        return len(rows)

    mcp_server.append_unique_rows = _fake_append_unique_rows
    try:
        stub_summary = """SUNTROP SOLAR — PLANT MONITORING SUMMARY | 03-Sep-26

NEEDS ATTENTION
- Test Site — minor issue — open 1 day — no action needed yet
"""
        stub_result = process_monitoring_summary(stub_summary)
    finally:
        mcp_server.append_unique_rows = original_append_unique_rows

    print(stub_result)
    assert stub_result == "Report processed successfully.\n\n- Date: 03.09.2026\n- Records saved: 1", stub_result
    print("OK: the success response is EXACTLY 'Report processed successfully.' + '- Date: ...' + '- Records saved: N' - nothing else.\n")

    print("All process_monitoring_summary() tests passed.")

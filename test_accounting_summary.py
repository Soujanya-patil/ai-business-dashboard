"""Local test for process_accounting_summary() - no Claude Pro required.

Run with:
    python test_accounting_summary.py

Exercises the full flow (raw Day Book summary text -> parser -> row-
building -> Google Sheets) using the credentials.json / SPREADSHEET_ID
already configured in mcp_server.py. The Sheets-writing/idempotency check
writes real rows into the "Accounting" tab (10-column, concise business
schema, Record Type column - see accounting_parser.ACCOUNTING_HEADERS). No
Anthropic API call is involved.

ONE ROW = ONE IMPORTANT BUSINESS RECORD: long narrative Description text is
shortened before being stored (see accounting_parser.
_shorten_accounting_description) and Cash/Sales/Purchase each become several
small concrete rows instead of one wide row with a column per figure - Test
13 below reproduces the exact worked examples from the schema spec end to
end (including a bare-integer Amount, e.g. 1606711 not "₹16,06,711").

Determinism note: the Sheets-writing/idempotency check generates fresh,
uniquely-tagged content on every run (see _build_unique_summary) and uses
a synthetic year (2099) so it can never be mistaken for, or overwrite, a
real admin-submitted report - same approach as test_process_summary.py.
It is SKIPPED BY DEFAULT (see RUN_LIVE_SHEETS_TESTS below) so a normal run
of this file never writes anything to the configured spreadsheet - set
RUN_LIVE_SHEETS_TESTS=1 to opt in.

Dates: every row's Date cell is written as DD.MM.YYYY (e.g. "03.09.2026"),
never ISO format - see accounting_parser.SHEET_DATE_FORMAT. Unlike the
Monitoring parser, a missing/unparseable Day Book date still falls back to
today's date (also DD.MM.YYYY) rather than erroring - that stricter
"never fall back, error instead" rule is Monitoring-specific.
"""

import os
import uuid

from accounting_parser import parse_accounting_summary, build_accounting_rows, ACCOUNTING_HEADERS
from mcp_server import process_accounting_summary, service, SPREADSHEET_ID, ACCOUNTING_TAB
from sheets_service import get_tab_values

# Sheets-writing/idempotency test (14) is opt-in only - see the matching
# note in test_process_summary.py.
RUN_LIVE_SHEETS_TESTS = os.getenv("RUN_LIVE_SHEETS_TESTS") == "1"

# A clean, fully-populated Day Book summary covering every section with
# more than one item where the template allows it (2 exceptions, 2 tax
# watch items, 2 pending items) - proves multiple rows per section work.
CLEAN_SUMMARY = """SUNTROP SOLAR — DAY BOOK SUMMARY | 03 Sep 2026

ISSUES REQUIRING ATTENTION (if any)
- ABC Traders — GST mismatch of ₹12,000 found on vendor invoice; recommend verifying with vendor before payment
- Vendor XYZ Pvt Ltd — Payment of ₹45,000 overdue by 10 days; critical, recommend immediate follow-up

CASH & BANK POSITION
- Opening balance: ₹1,50,000 | Closing balance: ₹1,80,000
- Total receipts: ₹80,000 | Total payments: ₹50,000

SALES
- Invoices raised today: 5 | Total value: ₹2,50,000
- Sales Orders raised: 2 | Total value: ₹90,000
- Outstanding receivables (aging flag if any >45 days): ₹3,20,000, one customer over 45 days

PURCHASE
- Bills booked today: 3 | Total value: ₹1,10,000
- Any vendor GSTIN/HSN mismatches: N

EXPENSES & JOURNAL ENTRIES
- Notable/unusual entries today: Office rent ₹40,000 paid to Landlord Properties

GST/TAX WATCH ITEMS
- ITC discrepancy of ₹8,000 flagged on vendor invoice; recommend CA review this week
- RCM applicability unconfirmed for consulting services received

PENDING FROM YESTERDAY
- Vendor ABC Traders — GST reconciliation still awaiting verification; recommend follow-up call
- Bank reconciliation for account ending 4521 still pending
"""

# Uncertain wording, missing values, "none"/no-mismatch cases - the
# explicit uncertainty phrases from the spec must survive (as a Status,
# since there's no Notes column in the unified schema), numeric fields
# must stay blank (never a fabricated 0) when the report itself says a
# figure isn't computable.
UNCERTAIN_SUMMARY = """SUNTROP SOLAR — DAY BOOK SUMMARY | 04 Sep 2026

ISSUES REQUIRING ATTENTION (if any)
- Large cash withdrawal of ₹75,000 recorded without supporting voucher; needs confirmation from accounts team before closing books

CASH & BANK POSITION
- Opening balance: not reliably computable due to two unreconciled bank accounts | Closing balance: ₹2,10,000
- Total receipts: ₹95,000 | Total payments: not derivable without yesterday's ledger

SALES
- Invoices raised today: 8 | Total value: ₹4,10,000
- Outstanding receivables (aging flag if any >45 days): ₹1,05,000, two customers over 60 days

PURCHASE
- Bills booked today: Unconfirmed, vendor portal was down most of the day
- Any vendor GSTIN/HSN mismatches: Y — Global Supplies Ltd HSN code mismatch on invoice #4521, needs confirmation

EXPENSES & JOURNAL ENTRIES
- Notable/unusual entries today: none

GST/TAX WATCH ITEMS
(Only if new/unresolved.)

PENDING FROM YESTERDAY
- Confirm bank reconciliation for account ending 4521
- Vendor Global Supplies Ltd — HSN mismatch still pending vendor response
- TDS deduction on professional fees not yet verified against 26AS, potential issue
"""

# The exact worked example from the schema spec: a long narrative
# Exception bullet with an entity buried mid-sentence (no leading
# separator), an amount in Rs (not ₹), and a "→" recommended-action
# marker - must come out as a concise, fully-populated 10-column row,
# never the original paragraph.
LONG_NARRATIVE_SUMMARY = """SUNTROP SOLAR — DAY BOOK SUMMARY | 05 Sep 2026

ISSUES REQUIRING ATTENTION (if any)
- Micronova Impex invoice was inflated and then subsequently reversed within the same settlement window, which is a critical concern raising a potential ITC overclaim risk of approximately Rs 16,06,711 that should be flagged before month-end filing. → Confirm with CA
"""

EXPECTED_LONG_NARRATIVE_ROW = [
    "05.09.2026", "Exception", "Micronova Impex", "Invoice inflated then reversed", 1606711,
    "", "Critical", "Unconfirmed", "Confirm with CA", "ITC",
]


def _build_unique_summary(run_id: str, report_date: str) -> str:
    """Clean-shaped summary, uniquely tagged - see module docstring.
    Covers exceptions, cash, both sales sub-types, purchase, an expense,
    a tax item, and a pending item in one pass.
    """
    return f"""
SUNTROP SOLAR — DAY BOOK SUMMARY | {report_date}

ISSUES REQUIRING ATTENTION (if any)
- {run_id} Traders — Discrepancy of ₹5,000 found; recommend review

CASH & BANK POSITION
- Opening balance: ₹10,000 | Closing balance: ₹20,000
- Total receipts: ₹15,000 | Total payments: ₹5,000

SALES
- Invoices raised today: 1 | Total value: ₹5,000
- Sales Orders raised: 1 | Total value: ₹3,000

PURCHASE
- Bills booked today: 1 | Total value: ₹2,000
- Any vendor GSTIN/HSN mismatches: N

EXPENSES & JOURNAL ENTRIES
- Notable/unusual entries today: {run_id} expense entry ₹1,000

GST/TAX WATCH ITEMS
- {run_id} ITC discrepancy of ₹500 flagged

PENDING FROM YESTERDAY
- {run_id} follow-up still pending
"""


if __name__ == "__main__":
    print("=== Test 1: exact 10-column Accounting schema ===")
    assert ACCOUNTING_HEADERS == [
        "Date", "Record Type", "Entity", "Description", "Amount", "Count",
        "Priority", "Status", "Recommended Action", "Risk / Tax Flag",
    ], f"Unexpected schema: {ACCOUNTING_HEADERS}"
    assert len(ACCOUNTING_HEADERS) == 10
    assert "Section" not in ACCOUNTING_HEADERS
    for removed in (
        "Opening Balance", "Closing Balance", "Total Receipts", "Total Payments",
        "Sales Value", "Outstanding Receivables", "Purchase Value",
        "GSTIN/HSN Mismatch", "GST/Tax Watch", "Pending From Yesterday", "Notes",
    ):
        assert removed not in ACCOUNTING_HEADERS, f"{removed!r} should have been removed from the schema"
    print("OK\n")

    print("=== Test 2: correct tab name 'Accounting' ===")
    assert ACCOUNTING_TAB == "Accounting"
    print("OK\n")

    print("=== Test 3: clean summary - all sections parse, multiple rows per section, all rows 10 columns ===")
    parsed = parse_accounting_summary(CLEAN_SUMMARY)
    rows = build_accounting_rows(parsed)
    assert parsed["date"] == "03.09.2026"
    assert len(rows["issues"]) == 2, f"Expected 2 exceptions, got {len(rows['issues'])}"
    assert len(rows["cash"]) == 4, f"Expected 4 Cash rows (opening/closing/receipts/payments), got {len(rows['cash'])}"
    assert len(rows["sales"]) == 3, f"Expected 3 Sale rows (invoices + orders + receivables), got {len(rows['sales'])}"
    assert len(rows["purchase"]) == 1, "N mismatch must not produce a second Purchase row"
    assert len(rows["expenses"]) == 1
    assert len(rows["tax"]) == 2, f"Expected 2 tax watch rows, got {len(rows['tax'])}"
    assert len(rows["pending"]) == 2, f"Expected 2 pending rows, got {len(rows['pending'])}"
    for section_rows in rows.values():
        for row in section_rows:
            assert len(row) == 10, f"Row has wrong column count: {row}"
    print("OK: all 7 sections produced correct row counts, all rows are 10 columns; no wide multi-balance row.\n")

    print("=== Test 4: multiple exceptions mapped correctly (Entity, Amount as bare integer, Priority, Status, Risk/Tax Flag) ===")
    issue_rows = rows["issues"]
    assert all(row[ACCOUNTING_HEADERS.index("Record Type")] == "Exception" for row in issue_rows)
    entities = {row[ACCOUNTING_HEADERS.index("Entity")] for row in issue_rows}
    assert "ABC Traders" in entities
    assert "Vendor XYZ Pvt Ltd" in entities
    amounts = {row[ACCOUNTING_HEADERS.index("Amount")] for row in issue_rows}
    assert 12000 in amounts and 45000 in amounts, f"Amounts must be bare integers, got {amounts}"
    critical_row = next(r for r in issue_rows if r[ACCOUNTING_HEADERS.index("Entity")] == "Vendor XYZ Pvt Ltd")
    assert critical_row[ACCOUNTING_HEADERS.index("Priority")] == "Critical"
    assert critical_row[ACCOUNTING_HEADERS.index("Status")] == "Open"
    gst_row = next(r for r in issue_rows if r[ACCOUNTING_HEADERS.index("Entity")] == "ABC Traders")
    assert gst_row[ACCOUNTING_HEADERS.index("Risk / Tax Flag")] == "GST"
    print("OK\n")

    print("=== Test 5: Cash position becomes 4 separate concise rows, not one wide row ===")
    cash_by_desc = {r[ACCOUNTING_HEADERS.index("Description")]: r for r in rows["cash"]}
    assert set(cash_by_desc) == {"Opening balance", "Closing balance", "Total receipts", "Total payments"}
    assert cash_by_desc["Opening balance"][ACCOUNTING_HEADERS.index("Amount")] == 150000
    assert cash_by_desc["Closing balance"][ACCOUNTING_HEADERS.index("Amount")] == 180000
    assert cash_by_desc["Total receipts"][ACCOUNTING_HEADERS.index("Amount")] == 80000
    assert cash_by_desc["Total payments"][ACCOUNTING_HEADERS.index("Amount")] == 50000
    for row in rows["cash"]:
        assert row[ACCOUNTING_HEADERS.index("Record Type")] == "Cash"
    print("OK\n")

    print("=== Test 6: sales with invoices AND sales orders as separate rows, receivables its own row ===")
    sales_rows = rows["sales"]
    assert all(r[ACCOUNTING_HEADERS.index("Record Type")] == "Sale" for r in sales_rows)
    counts_amounts = {(r[ACCOUNTING_HEADERS.index("Count")], r[ACCOUNTING_HEADERS.index("Amount")]) for r in sales_rows if r[ACCOUNTING_HEADERS.index("Count")] != ""}
    assert (5, 250000) in counts_amounts, counts_amounts
    assert (2, 90000) in counts_amounts, counts_amounts
    receivables_row = next(r for r in sales_rows if "Outstanding receivables" in r[ACCOUNTING_HEADERS.index("Description")])
    assert receivables_row[ACCOUNTING_HEADERS.index("Amount")] == 320000
    assert "45 days" in receivables_row[ACCOUNTING_HEADERS.index("Description")], (
        "Aging risk info must be preserved even without a dedicated aging column"
    )
    print("OK\n")

    print("=== Test 7: purchase information mapped correctly; bare 'N' produces no mismatch row ===")
    purchase_row = rows["purchase"][0]
    assert purchase_row[ACCOUNTING_HEADERS.index("Record Type")] == "Purchase"
    assert purchase_row[ACCOUNTING_HEADERS.index("Count")] == 3
    assert purchase_row[ACCOUNTING_HEADERS.index("Amount")] == 110000
    assert len(rows["purchase"]) == 1, "A bare 'N' mismatch answer must not create a second Purchase row"
    print("OK\n")

    print("=== Test 8: expenses/journal entries mapped correctly, Amount as bare integer ===")
    expense_row = rows["expenses"][0]
    assert expense_row[ACCOUNTING_HEADERS.index("Record Type")] == "Expense"
    assert "Office rent" in expense_row[ACCOUNTING_HEADERS.index("Description")]
    assert expense_row[ACCOUNTING_HEADERS.index("Amount")] == 40000
    print("OK\n")

    print("=== Test 9: GST/Tax watch items mapped correctly, including Unconfirmed status and Risk/Tax Flag ===")
    tax_rows = rows["tax"]
    assert all(r[ACCOUNTING_HEADERS.index("Record Type")] == "Tax" for r in tax_rows)
    itc_row = next(r for r in tax_rows if "ITC" in r[ACCOUNTING_HEADERS.index("Description")])
    assert itc_row[ACCOUNTING_HEADERS.index("Amount")] == 8000
    assert "recommend CA review" in itc_row[ACCOUNTING_HEADERS.index("Recommended Action")]
    assert itc_row[ACCOUNTING_HEADERS.index("Risk / Tax Flag")] == "ITC"
    rcm_row = next(r for r in tax_rows if "RCM" in r[ACCOUNTING_HEADERS.index("Description")])
    assert rcm_row[ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed"
    assert rcm_row[ACCOUNTING_HEADERS.index("Risk / Tax Flag")] == "RCM"
    print("OK\n")

    print("=== Test 10: pending-from-yesterday items mapped correctly, default Status=Pending ===")
    pending_rows = rows["pending"]
    assert all(r[ACCOUNTING_HEADERS.index("Record Type")] == "Pending" for r in pending_rows)
    assert any(r[ACCOUNTING_HEADERS.index("Entity")] == "Vendor ABC Traders" for r in pending_rows)
    assert all(r[ACCOUNTING_HEADERS.index("Description")] for r in pending_rows)
    assert all(r[ACCOUNTING_HEADERS.index("Status")] == "Pending" for r in pending_rows)
    print("OK\n")

    print("=== Test 11: uncertain wording surfaces as Status=Unconfirmed, missing values stay blank (never fabricated) ===")
    u_parsed = parse_accounting_summary(UNCERTAIN_SUMMARY)
    u_rows = build_accounting_rows(u_parsed)

    u_cash_by_desc = {r[ACCOUNTING_HEADERS.index("Description")]: r for r in u_rows["cash"]}
    assert u_cash_by_desc["Opening balance"][ACCOUNTING_HEADERS.index("Amount")] == "", (
        "Opening balance Amount must stay blank, not a fabricated 0"
    )
    assert u_cash_by_desc["Opening balance"][ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed"
    assert u_cash_by_desc["Total payments"][ACCOUNTING_HEADERS.index("Amount")] == ""
    assert u_cash_by_desc["Total payments"][ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed"
    assert u_cash_by_desc["Closing balance"][ACCOUNTING_HEADERS.index("Amount")] == 210000, (
        "Explicit figures must still be parsed"
    )

    u_purchase_bills = next(r for r in u_rows["purchase"] if r[ACCOUNTING_HEADERS.index("Description")] == "Purchase bills booked")
    assert u_purchase_bills[ACCOUNTING_HEADERS.index("Count")] == "", "Bill count must stay blank when reported as Unconfirmed"
    assert u_purchase_bills[ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed"
    u_mismatch_row = next(r for r in u_rows["purchase"] if r[ACCOUNTING_HEADERS.index("Risk / Tax Flag")] == "GSTIN/HSN Mismatch")
    assert u_mismatch_row[ACCOUNTING_HEADERS.index("Entity")] == "Global Supplies Ltd", (
        f"Entity must not swallow the 'HSN' acronym: {u_mismatch_row}"
    )

    assert u_rows["expenses"] == [], "A bare 'none' must not create a fabricated Expense row"
    assert u_rows["tax"] == [], "An empty/placeholder Tax section must not create a fabricated row"

    u_issue = u_rows["issues"][0]
    assert u_issue[ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed", (
        "'needs confirmation' must map to Status=Unconfirmed, never a false Open/Resolved"
    )
    assert u_issue[ACCOUNTING_HEADERS.index("Description")] == "Cash withdrawal without voucher"

    tds_row = next(r for r in u_rows["pending"] if "TDS" in r[ACCOUNTING_HEADERS.index("Description")])
    assert tds_row[ACCOUNTING_HEADERS.index("Status")] == "Unconfirmed", "'potential' must map to Status=Unconfirmed"
    assert tds_row[ACCOUNTING_HEADERS.index("Risk / Tax Flag")] == "TDS"
    print("OK: 'not reliably computable' / 'not derivable' / 'Unconfirmed' / 'needs confirmation' / 'potential'")
    print("    all preserved as Status=Unconfirmed without inventing numbers or false statuses.\n")

    print("=== Test 12: 'N — no anomalies observed'-style negative answers still produce no mismatch row ===")
    n_summary = CLEAN_SUMMARY.replace(
        "- Any vendor GSTIN/HSN mismatches: N",
        "- Any vendor GSTIN/HSN mismatches: N — no anomalies observed.",
    )
    n_parsed = parse_accounting_summary(n_summary)
    n_rows = build_accounting_rows(n_parsed)
    assert len(n_rows["purchase"]) == 1, (
        f"'N — no anomalies observed.' must still count as no genuine mismatch, got {len(n_rows['purchase'])} Purchase row(s)"
    )
    print("OK\n")

    print("=== Test 13: long narrative text is SHORTENED into a concise, fully-populated row (schema spec worked example) ===")
    long_parsed = parse_accounting_summary(LONG_NARRATIVE_SUMMARY)
    long_rows = build_accounting_rows(long_parsed)
    assert len(long_rows["issues"]) == 1, long_rows["issues"]
    actual_long_row = long_rows["issues"][0]
    assert actual_long_row == EXPECTED_LONG_NARRATIVE_ROW, (
        f"Expected {EXPECTED_LONG_NARRATIVE_ROW}, got {actual_long_row}"
    )
    original_word_count = len(long_parsed["issues"][0]["description"].split())
    shortened_word_count = len(actual_long_row[ACCOUNTING_HEADERS.index("Description")].split())
    assert original_word_count > 25, "Fixture bullet should be long enough to actually exercise shortening"
    assert shortened_word_count <= 6, (
        f"Description cell must be a short phrase, not the {original_word_count}-word original bullet: {actual_long_row}"
    )
    assert isinstance(actual_long_row[ACCOUNTING_HEADERS.index("Amount")], int), (
        "Amount must be a bare integer, not a formatted currency string"
    )
    print(f"Original bullet: {original_word_count} words -> stored Description: {shortened_word_count} words")
    print(f"Amount: {actual_long_row[ACCOUNTING_HEADERS.index('Amount')]} (bare integer, not '₹16,06,711')")
    print("OK: the full paragraph was never stored - Sheets got a concise, fully-populated row instead.\n")

    if not RUN_LIVE_SHEETS_TESTS:
        print("=== Test 14: Google Sheets integration + idempotency check SKIPPED ===")
        print("Set RUN_LIVE_SHEETS_TESTS=1 to run this against the configured production spreadsheet")
        print("(it writes real, synthetically-tagged 2099-dated rows and is opt-in on purpose).\n")
    else:
        print("=== Test 14: Google Sheets integration + idempotency check ===")
        run_uuid = uuid.uuid4()
        run_id = run_uuid.hex[:10]
        synthetic_month = (run_uuid.int % 12) + 1
        synthetic_day = (run_uuid.int % 28) + 1
        # ISO stays a valid INPUT format for the title line; the row
        # actually written to Sheets must be DD.MM.YYYY.
        synthetic_date_input = f"2099-{synthetic_month:02d}-{synthetic_day:02d}"
        synthetic_date_output = f"{synthetic_day:02d}.{synthetic_month:02d}.2099"
        summary_text = _build_unique_summary(run_id, synthetic_date_input)
        print(f"Generated run_id={run_id!r}, synthetic date={synthetic_date_input!r} -> stored as {synthetic_date_output!r}")

        result_1 = process_accounting_summary(summary_text)
        print(result_1)
        assert "Cash & Bank Position: updated" in result_1
        assert "Purchase: updated" in result_1
        assert "Issues Requiring Attention: 1 row(s)" in result_1
        assert "Sales: 2 row(s)" in result_1
        assert "Expenses & Journal Entries: 1 row(s)" in result_1
        assert "GST/Tax Watch Items: 1 row(s)" in result_1
        assert "Pending From Yesterday: 1 row(s)" in result_1

        all_rows = get_tab_values(service, SPREADSHEET_ID, ACCOUNTING_TAB)
        header = all_rows[0]
        assert header == ACCOUNTING_HEADERS, f"Live sheet header does not match: {header}"
        # Cash/Sales/Purchase lines in the generator don't embed run_id (their
        # template shape leaves no room for a free-text tag without breaking
        # amount extraction), so identify this run's rows by its unique
        # synthetic date instead - equally reliable since a fresh date is
        # generated every run specifically to avoid cross-run collisions.
        date_rows = [row for row in all_rows if row[0] == synthetic_date_output]
        # 1 issue + 4 cash + 2 sales + 1 purchase + 1 expense + 1 tax + 1 pending = 11
        assert len(date_rows) == 11, f"Expected 11 rows for date {synthetic_date_output!r} on first write, got {len(date_rows)}"
        tagged_rows = [row for row in date_rows if any(run_id in str(cell) for cell in row)]
        assert len(tagged_rows) == 4, f"Expected 4 run_id-tagged rows (issue/expense/tax/pending), got {len(tagged_rows)}"

        result_2 = process_accounting_summary(summary_text)
        print(result_2)
        assert "Issues Requiring Attention: 0 row(s)" in result_2
        assert "Sales: 0 row(s)" in result_2
        assert "Expenses & Journal Entries: 0 row(s)" in result_2
        assert "GST/Tax Watch Items: 0 row(s)" in result_2
        assert "Pending From Yesterday: 0 row(s)" in result_2
        # Cash/Purchase are upserts, so they still report "updated" (in place), not a count.
        assert "Cash & Bank Position: updated" in result_2
        assert "Purchase: updated" in result_2

        date_rows_after = [row for row in get_tab_values(service, SPREADSHEET_ID, ACCOUNTING_TAB) if row[0] == synthetic_date_output]
        assert len(date_rows_after) == len(date_rows), (
            f"Row count for date {synthetic_date_output!r} changed after a duplicate write: {len(date_rows)} -> {len(date_rows_after)}"
        )
        print(f"OK: first write created {len(date_rows)} rows total (incl. 4 Cash + 1 Purchase upserts); "
              "duplicate write created 0 additional rows, and Cash/Purchase sub-records were updated in place, not duplicated.\n")

    print("=== Test 15: DD.MM.YYYY date format conversion (Accounting) ===")
    date_format_summary = """SUNTROP SOLAR — DAY BOOK SUMMARY | 03-Sep-26

EXPENSES & JOURNAL ENTRIES
- Minor stationery purchase ₹500
"""
    d1 = parse_accounting_summary(date_format_summary)
    assert d1["date"] == "03.09.2026", d1["date"]
    d2 = parse_accounting_summary(date_format_summary.replace("03-Sep-26", "4-Sep-26"))
    assert d2["date"] == "04.09.2026", d2["date"]
    print("OK: '03-Sep-26' -> '03.09.2026', '4-Sep-26' -> '04.09.2026'.\n")

    print("All process_accounting_summary() tests passed.")

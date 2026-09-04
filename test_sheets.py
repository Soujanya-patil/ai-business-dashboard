from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# Google Sheet details
SPREADSHEET_ID = "1EGlUndNNwiDm0RDwL5tLQeQKmuia5LCy8qKSMhhLQYM"
SHEET_NAME = "Monitoring"

# Google Sheets permission
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

# Load the service account credentials
credentials = Credentials.from_service_account_file(
    "credentials.json",
    scopes=SCOPES
)

# Create Google Sheets API connection
service = build(
    "sheets",
    "v4",
    credentials=credentials
)

# Test row - matches the unified Monitoring sheet schema (see
# summary_parser.MONITORING_HEADERS):
# Date | Site | Issue | Category | Priority | Status | Days Open |
# Action Taken | Next Action | Vendor | New Issues | Issues Resolved |
# Total Open Issues | Notes
# A clearly synthetic date (year 2099) and Site keep this from ever being
# mistaken for a real report.
test_row = [
    "2099-01-01",
    "TEST SITE",
    "Testing Google Sheets connection",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    0,
    0,
    0,
    "Added by test_sheets.py"
]

# Add row to Monitoring sheet. valueInputOption="RAW" (not USER_ENTERED)
# so the date string is stored as literal text - USER_ENTERED lets Sheets
# auto-parse date-looking strings into date-typed cells, which can then
# read back as a raw date-serial number instead of this exact string
# (see sheets_service.py's module docstring for the full explanation).
result = service.spreadsheets().values().append(
    spreadsheetId=SPREADSHEET_ID,
    range=f"{SHEET_NAME}!A:N",
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",
    body={
        "values": [test_row]
    }
).execute()

print("SUCCESS!")
print("Test row added to Monitoring sheet.")

# Challenges

## Replacing a Spreadsheet Workflow

The system needed to preserve the operational familiarity of spreadsheets while improving structure, security and traceability. The challenge was not only technical; the interface had to be simple enough for a busy sales team.

## Handling Duplicate Customers

Customer identifiers may arrive formatted differently in Excel. The system normalizes identifiers before checking duplicates, preventing repeated records caused by formatting differences.

## Importing Real-World Excel Files

Excel files often contain inconsistent headers, multiple sheets and partial values. The import layer was designed to normalize headers, choose the most compatible sheet and ignore invalid rows safely.

## Field-Level Permissions

The application needed more than page-level permissions. Operators, supervisors and admins can see similar screens but edit different fields. This required backend-enforced editable field sets.

## Keeping Status and Position Configurable

Operational labels change over time. Hardcoding them would force code deployments for simple business changes. The solution was to persist configurable options in the database.

## Auditability Without Slowing Users Down

The audit trail needed to capture changes without making the UI complex. The solution records changes automatically in the backend and exposes only relevant summaries to users and admins.

## Safe Google Onboarding

Google OAuth makes login easier, but automatic account creation could expose the CRM. The approval queue balances convenience and security.

## Operating With SQLite

SQLite is simple, but concurrent writes must be considered. The production approach includes controlled worker configuration, busy timeouts, backups and optional WAL mode.

## Public Case Study Without Exposing IP

The project is valuable as a public technical case study, but the original code and data cannot be published. This repository documents architecture, decisions and outcomes without revealing proprietary implementation.

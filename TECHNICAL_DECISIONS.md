# Technical Decisions

## Flask Instead of Django

Flask was selected because the application needed a focused internal workflow rather than a broad platform. The team needed direct control over routing, authentication behavior, permission checks and form handling.

Django would also be a valid option, especially for projects that need a built-in admin panel, migrations and a more opinionated structure. In this case, Flask provided a smaller surface area and faster iteration.

## SQLite for the Initial Production Phase

SQLite was selected to reduce infrastructure overhead. For a small internal CRM, a file-based relational database can be reliable when paired with:

- controlled deployment;
- consistent backups;
- limited worker count;
- write timeout configuration;
- WAL mode when needed.

The architecture remains migration-ready because persistence is abstracted through SQLAlchemy.

## SQLAlchemy ORM

SQLAlchemy was used to keep the database layer explicit and maintainable. It also makes a future move to PostgreSQL less disruptive.

## Server-Side Rendering

Server-side rendering was chosen because the product is mostly forms, filters, tables and admin screens. A separate SPA would add complexity without clear business value at the initial scale.

## Google OAuth With Approval Flow

Google OAuth improves login experience but does not automatically grant access. New Google users are routed into an approval queue where an admin assigns role, team and customer linkage.

This keeps onboarding convenient without weakening access control.

## Granular Permissions

The project started with three role concepts, but operational needs required more flexibility. Granular permissions allow admins to create custom profiles without code changes.

Examples:

- allow creating customers without allowing imports;
- allow editing contact notes without editing status;
- allow managing users only for administrative profiles.

## Audit Trail

Audit logging was implemented as a first-class feature because the system handles sensitive operational data. The audit design records:

- actor;
- action;
- target record;
- changed field;
- previous value;
- new value;
- timestamp;
- import batch when relevant.

## Soft Deletion

Customer records are soft-deleted to preserve history. This supports operational accountability and avoids breaking audit references.

## Configurable Operational Lists

Status and position values are database-driven. Admins can add, update, deactivate or delete options without changing code.

## Backup Strategy

SQLite backups should be created through the database backup mechanism instead of copying the database file while active. This reduces the risk of inconsistent backups.


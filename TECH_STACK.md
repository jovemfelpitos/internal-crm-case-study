# Technology Stack

## Python

Python was selected because it offers a strong ecosystem for web development, data processing and automation. It also integrates naturally with Pandas, which was essential for Excel import workflows.

## Flask

Flask was used as the web framework because the project required:

- fast delivery;
- simple routing;
- flexible application structure;
- low framework overhead;
- easy integration with SQLAlchemy, Authlib and Jinja2.

## SQLAlchemy

SQLAlchemy provides structured database access, model definitions and expressive queries. It also keeps the project better prepared for a future migration from SQLite to PostgreSQL.

## SQLite

SQLite was chosen for the initial production phase because:

- the system is an internal tool;
- expected traffic is moderate;
- deployment cost should stay low;
- maintenance should be simple;
- the database can be backed up consistently.

## Pandas and OpenPyXL

Pandas reads Excel files and helps normalize tabular data. OpenPyXL is used as the engine for `.xlsx` files.

## Jinja2

Jinja2 renders server-side HTML templates. This matches the application's form-based workflow and avoids the complexity of maintaining a separate frontend SPA.

## Bootstrap

Bootstrap provides responsive layout, form controls and table styling. Custom CSS extends it with theme support, sidebar navigation and CRM-specific visual components.

## Authlib and Google OAuth

Authlib integrates the application with Google OAuth/OpenID Connect. This improves login experience and enables a controlled approval flow for new users.

## Gunicorn

Gunicorn is used as the production WSGI server. It allows the Flask application to run behind a reverse proxy with multiple workers/threads.

## Linux and systemd

The production deployment is designed for a Linux VPS, using systemd to manage application and backup services.

## Git

Git is used for version control and release discipline. This public repository intentionally contains documentation only.


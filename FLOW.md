# System Flow

## Login Flow

```mermaid
sequenceDiagram
    participant User
    participant Browser
    participant Flask
    participant Google
    participant Database

    User->>Browser: Opens login page
    Browser->>Flask: GET /login
    Flask-->>Browser: Login page
    User->>Browser: Chooses login method

    alt Local login
        Browser->>Flask: POST credentials
        Flask->>Database: Find active user
        Database-->>Flask: User record
        Flask-->>Browser: Session created
    else Google login
        Browser->>Google: OAuth redirect
        Google-->>Flask: Callback with profile
        Flask->>Database: Find user or access request
        Flask-->>Browser: Login or pending approval message
    end
```

## Dashboard Flow

1. The user accesses the dashboard.
2. The backend identifies the user's role and permissions.
3. A scoped database query is built:
   - admins can see all active customers;
   - supervisors can see their team;
   - operators can see their own assigned customers.
4. Search, status, position, date and consultant filters are applied.
5. Sorting and pagination are applied.
6. The template renders only relevant columns and actions.

## Excel Import Flow

```mermaid
flowchart TD
    A["Admin uploads .xlsx"] --> B["Validate extension and size"]
    B --> C["Pandas reads workbook"]
    C --> D["Select compatible sheet"]
    D --> E["Normalize headers"]
    E --> F["Normalize customer identifier"]
    F --> G{"Duplicate in file?"}
    G -->|Yes| H["Merge safe fields and count duplicate"]
    G -->|No| I["Build customer payload"]
    H --> I
    I --> J{"Already exists in database?"}
    J -->|Yes| K["Count as duplicate"]
    J -->|No| L["Create customer"]
    L --> M["Create audit event"]
    K --> N["Update import batch"]
    M --> N
```

## Audit Flow

```mermaid
flowchart LR
    A["User action"] --> B["Backend validates permissions"]
    B --> C["Detect changed fields"]
    C --> D["Persist business change"]
    D --> E["Write audit log"]
    E --> F["Admin audit page"]
    E --> G["Customer profile history"]
```

Audit events include:

- import batches;
- customer creation;
- field updates;
- customer deletion;
- user changes;
- Google access approvals;
- configuration changes.

## Permission Flow

```mermaid
flowchart TD
    A["Authenticated user"] --> B["Load base role"]
    B --> C["Load granular permissions"]
    C --> D{"Can view all?"}
    D -->|Yes| E["All active customers"]
    D -->|No| F{"Supervisor?"}
    F -->|Yes| G["Customers from user team"]
    F -->|No| H["Customers assigned to user"]
    C --> I["Build editable field set"]
    I --> J["Ignore unauthorized submitted fields"]
```

## Customer Update Flow

1. User opens a customer profile.
2. The template disables fields that the user cannot edit.
3. On submit, the backend recalculates allowed fields.
4. Unauthorized fields are ignored.
5. Valid option fields are checked against active configuration values.
6. Changes are persisted.
7. Each changed field is audited.

## Administration Flow

Admins can:

- approve or reject Google access requests;
- create users manually;
- assign teams and role profiles;
- configure status and position options;
- create teams;
- create role profiles;
- review imports and audit events.


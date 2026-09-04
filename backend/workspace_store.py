from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any


LOCAL_USER_ID = "local-user"
LOCAL_TENANT_ID = "local-tenant"
LOCAL_WORKSPACE_ID = "local-workspace"
WORKSPACE_HEADER = "X-InLumen-Workspace-Id"
_schema_lock = threading.Lock()
_schema_ready = False


class WorkspaceStoreError(RuntimeError):
    pass


class WorkspaceAccessDenied(WorkspaceStoreError):
    pass


class AuthModeTransitionError(WorkspaceStoreError):
    """A durable deployment was started in a different identity mode."""

    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    subject: str
    issuer: str
    workspace_id: str
    tenant_id: str
    workspace_role: str
    display_name: str = ""

    @property
    def is_local(self) -> bool:
        return self.user_id == LOCAL_USER_ID


def local_principal() -> Principal:
    return Principal(
        user_id=LOCAL_USER_ID,
        subject=LOCAL_USER_ID,
        issuer="local",
        workspace_id=LOCAL_WORKSPACE_ID,
        tenant_id=LOCAL_TENANT_ID,
        workspace_role="owner",
        display_name="Local user",
    )


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _connect():
    database_url = _database_url()
    if not database_url:
        raise WorkspaceStoreError(
            "DATABASE_URL is required when Keycloak authentication is enabled."
        )
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise WorkspaceStoreError("The PostgreSQL driver is not installed.") from exc
    try:
        return psycopg.connect(database_url)
    except Exception as exc:
        raise WorkspaceStoreError("The workspace database is unavailable.") from exc


def ensure_schema() -> None:
    """Create the small identity/workspace schema idempotently.

    Production deployments run the same statements in a one-shot migration
    container before starting the application. Keeping this guard makes first
    login fail safely if that deployment step was missed.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                id UUID PRIMARY KEY,
                keycloak_issuer TEXT NOT NULL,
                keycloak_subject TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (keycloak_issuer, keycloak_subject)
            );
            CREATE TABLE IF NOT EXISTS tenants (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL,
                created_by UUID NOT NULL REFERENCES app_users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS tenant_memberships (
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('owner','editor','runner','viewer')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                id UUID PRIMARY KEY,
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_by UUID NOT NULL REFERENCES app_users(id),
                revision BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('owner','editor','runner','viewer')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS workspace_memberships_user_idx
                ON workspace_memberships(user_id, workspace_id);
            CREATE TABLE IF NOT EXISTS application_runtime_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
                """
            )
        _schema_ready = True


def validate_auth_mode_continuity(auth_enabled: bool) -> None:
    """Reject accidental reuse of durable data under another auth mode.

    ``AUTH_ENABLED=false`` is a single shared local identity, whereas true is
    a set of private identities.  Mixing them against one database is both
    confusing and unsafe, so an operator must explicitly acknowledge a planned
    transition with ``INLUMEN_ALLOW_AUTH_MODE_SWITCH=true``.
    """
    if not _database_url():
        return
    ensure_schema()
    requested = "keycloak" if auth_enabled else "local"
    allow_switch = os.getenv("INLUMEN_ALLOW_AUTH_MODE_SWITCH", "").strip().lower() == "true"
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT setting_value FROM application_runtime_settings "
            "WHERE setting_key='auth_mode' FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO application_runtime_settings (setting_key, setting_value) "
                "VALUES ('auth_mode', %s)",
                (requested,),
            )
            return
        previous = str(row[0])
        if previous == requested:
            return
        if not allow_switch:
            raise AuthModeTransitionError(
                "AUTH_ENABLED would change this deployment from "
                f"'{previous}' to '{requested}'. Use a separate database/volumes for "
                "local testing, or perform a backed-up migration with "
                "INLUMEN_ALLOW_AUTH_MODE_SWITCH=true for this one deployment."
            )
        cursor.execute(
            "UPDATE application_runtime_settings SET setting_value=%s, updated_at=now() "
            "WHERE setting_key='auth_mode'",
            (requested,),
        )


def resolve_principal(
    claims: dict[str, Any], requested_workspace_id: str | None = None
) -> Principal:
    issuer = str(
        claims.get("iss")
        or os.getenv("KEYCLOAK_ISSUER")
        or (
            "development" if os.getenv("APP_ENV", "development") != "production" else ""
        )
    ).strip()
    subject = str(claims.get("sub") or "").strip()
    if not issuer or not subject:
        raise WorkspaceStoreError(
            "The access token is missing issuer or subject claims."
        )
    display_name = str(
        claims.get("name") or claims.get("preferred_username") or subject
    ).strip()

    if not _database_url():
        if os.getenv("APP_ENV", "development").strip().lower() == "production":
            raise WorkspaceStoreError("DATABASE_URL is required in production.")
        user_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{issuer}:{subject}:user"))
        tenant_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{issuer}:{subject}:tenant"))
        workspace_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{issuer}:{subject}:workspace")
        )
        if requested_workspace_id and requested_workspace_id != workspace_id:
            raise WorkspaceAccessDenied("Workspace was not found.")
        return Principal(
            user_id=user_id,
            subject=subject,
            issuer=issuer,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            workspace_role="owner",
            display_name=display_name,
        )

    ensure_schema()
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO app_users (
                id, keycloak_issuer, keycloak_subject, display_name
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (keycloak_issuer, keycloak_subject) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                updated_at = now()
            RETURNING id
            """,
            (str(uuid.uuid4()), issuer, subject, display_name),
        )
        user_id = str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (user_id,),
        )

        cursor.execute(
            """
            SELECT w.id, w.tenant_id, wm.role
            FROM workspace_memberships wm
            JOIN workspaces w ON w.id = wm.workspace_id
            WHERE wm.user_id = %s
            ORDER BY w.created_at, w.id
            LIMIT 1
            """,
            (user_id,),
        )
        default_row = cursor.fetchone()
        if default_row is None:
            tenant_id = str(uuid.uuid4())
            workspace_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO tenants (id, name, created_by) VALUES (%s, %s, %s)",
                (tenant_id, f"{display_name}'s tenant", user_id),
            )
            cursor.execute(
                """
                INSERT INTO tenant_memberships (tenant_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (tenant_id, user_id),
            )
            cursor.execute(
                """
                INSERT INTO workspaces (id, tenant_id, name, created_by)
                VALUES (%s, %s, 'Personal workspace', %s)
                """,
                (workspace_id, tenant_id, user_id),
            )
            cursor.execute(
                """
                INSERT INTO workspace_memberships (workspace_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (workspace_id, user_id),
            )
            default_row = (workspace_id, tenant_id, "owner")

        selected_workspace_id = str(requested_workspace_id or default_row[0]).strip()
        try:
            selected_workspace_uuid = str(uuid.UUID(selected_workspace_id))
        except ValueError as exc:
            raise WorkspaceAccessDenied("Workspace was not found.") from exc
        cursor.execute(
            """
            SELECT w.id, w.tenant_id, wm.role
            FROM workspace_memberships wm
            JOIN workspaces w ON w.id = wm.workspace_id
            WHERE wm.user_id = %s AND wm.workspace_id = %s
            """,
            (user_id, selected_workspace_uuid),
        )
        selected = cursor.fetchone()
        if selected is None:
            raise WorkspaceAccessDenied("Workspace was not found.")

    return Principal(
        user_id=user_id,
        subject=subject,
        issuer=issuer,
        workspace_id=str(selected[0]),
        tenant_id=str(selected[1]),
        workspace_role=str(selected[2]),
        display_name=display_name,
    )


def list_workspaces(user_id: str) -> list[dict[str, Any]]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT w.id, w.tenant_id, w.name, w.revision, wm.role,
                   w.created_at, w.updated_at
            FROM workspace_memberships wm
            JOIN workspaces w ON w.id = wm.workspace_id
            WHERE wm.user_id = %s
            ORDER BY w.created_at, w.id
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": str(row[0]),
            "tenant_id": str(row[1]),
            "name": str(row[2]),
            "revision": int(row[3]),
            "role": str(row[4]),
            "created_at": row[5].isoformat(),
            "updated_at": row[6].isoformat(),
        }
        for row in rows
    ]


def create_workspace(user_id: str, name: str) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("Workspace name must contain between 1 and 120 characters.")
    workspace_id = str(uuid.uuid4())
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tenant_id
            FROM tenant_memberships
            WHERE user_id = %s AND role = 'owner'
            ORDER BY created_at, tenant_id
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorkspaceAccessDenied("No tenant is available for this user.")
        tenant_id = str(row[0])
        cursor.execute(
            """
            INSERT INTO workspaces (id, tenant_id, name, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING created_at, updated_at
            """,
            (workspace_id, tenant_id, clean_name, user_id),
        )
        created_at, updated_at = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO workspace_memberships (workspace_id, user_id, role)
            VALUES (%s, %s, 'owner')
            """,
            (workspace_id, user_id),
        )
    return {
        "id": workspace_id,
        "tenant_id": tenant_id,
        "name": clean_name,
        "revision": 0,
        "role": "owner",
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }

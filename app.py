import csv
import json
import os
import re
import secrets
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
from hmac import compare_digest
from io import StringIO
from urllib.parse import urlencode

import pandas as pd
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    has_app_context,
    g,
    has_request_context,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import bindparam, event, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from models import (
    ATUACAO_FIELDS,
    PERMISSION_FIELDS,
    AuditLog,
    Company,
    ChecklistStageConfiguration,
    ConfigurationMigration,
    Customer,
    FieldOption,
    Franchise,
    GoogleAccessRequest,
    ImportBatch,
    Lead,
    LeadWorkflowChecklistCheck,
    LeadWorkflowScheduledTask,
    LeadActivity,
    LeadWorkflow,
    ReversalAssignmentBatch,
    ReversalDistributionMode,
    ReversalAttempt,
    ReversalNotification,
    ReversalPriority,
    ReversalResult,
    ReversalSettings,
    ReversalStatus,
    ReversalTeamConfig,
    ReversalTask,
    Role,
    RoleProfile,
    SupportNotification,
    Team,
    TaskConfigurationScope,
    TaskConfigurationRevision,
    User,
    Workflow,
    WorkflowChecklistItem,
    WorkflowStage,
    WorkflowTransition,
    db,
)
from task_configuration import (
    add_configured_task, checklist_item_view, company_scope, configuration_inventory,
    matrix_configuration_required, preserve_existing_tasks, record_company_configuration,
    task_snapshot, contact_agenda_revision, effective_revision,
)
from task_configuration_editor import (DraftConflict, deactivate_all_draft_tasks, editor_scope,
                                       panel_state, save_task, start_draft, editable_draft, write_draft)
from task_configuration_publishing import (apply_replan, can_publish, publication_errors,
                                          publish_draft, replan_state)
from task_schedule import (business_deadline, contact_calendar, parse_day, preview_replan,
                           schedule_configuration, simulate_schedule)


try:
    from authlib.integrations.flask_client import OAuth
except ImportError:
    OAuth = None


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DEFAULT_DB_PATH = os.path.join(INSTANCE_DIR, "crm.sqlite3")
LEGACY_DB_PATH = os.path.join(BASE_DIR, "crm.sqlite3")
DEV_SECRET_PATH = os.path.join(INSTANCE_DIR, ".secret_key")
ALLOWED_EXTENSIONS = {".xlsx"}
CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MODAL_FORM_STATE_SESSION_KEY = "_modal_form_state"
SENSITIVE_MODAL_FORM_FIELDS = {
    "_csrf_token",
    "csrf_token",
    "password",
    "confirm_password",
    "current_password",
    "new_password",
    "password_confirm",
}
LOGIN_ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPTS = {}
PAGE_SIZE_OPTIONS = (15, 50, 100)
DASHBOARD_ORDER_OPTIONS = (
    {"value": "ultima_atualizacao.asc", "label": "Sem atualização primeiro", "sort": "ultima_atualizacao", "direction": "asc"},
    {"value": "ultima_atualizacao.desc", "label": "Atualização mais recente", "sort": "ultima_atualizacao", "direction": "desc"},
    {"value": "data_venda.desc", "label": "Venda mais recente", "sort": "data_venda", "direction": "desc"},
    {"value": "data_venda.asc", "label": "Venda mais antiga", "sort": "data_venda", "direction": "asc"},
    {"value": "concessao.desc", "label": "Concessão mais recente", "sort": "concessao", "direction": "desc"},
    {"value": "concessao.asc", "label": "Concessão mais antiga", "sort": "concessao", "direction": "asc"},
    {"value": "nome_cliente.asc", "label": "Cliente A-Z", "sort": "nome_cliente", "direction": "asc"},
    {"value": "nome_cliente.desc", "label": "Cliente Z-A", "sort": "nome_cliente", "direction": "desc"},
)
DASHBOARD_ORDER_MAP = {option["value"]: option for option in DASHBOARD_ORDER_OPTIONS}
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 10000
DEFAULT_COMPANY_SLUG = os.environ.get("DEFAULT_COMPANY_SLUG", "demo")
DEFAULT_COMPANY_NAME = os.environ.get("DEFAULT_COMPANY_NAME", "CRM Demo")
DEFAULT_FRANCHISE_NAME = os.environ.get("DEFAULT_FRANCHISE_NAME", "Matriz")
FILTER_EMPTY_VALUE = "__empty__"
NEW_LEAD_OPERATIONAL_SLA_HOURS = 4
DEFAULT_STAGE_OPERATIONAL_SLA_HOURS = 24
FAZER_INSS_OPERATIONAL_SLA_HOURS = 48
UNLOCK_EXECUTOR_SELLER = "seller"
UNLOCK_EXECUTOR_SUPPORT = "support"
UNLOCK_EXECUTOR_CHOICES = {UNLOCK_EXECUTOR_SELLER, UNLOCK_EXECUTOR_SUPPORT}
SUPPORT_EXECUTOR_SELLER = "seller"
SUPPORT_EXECUTOR_SUPPORT = "support"
SUPPORT_EXECUTOR_CHOICES = {SUPPORT_EXECUTOR_SELLER, SUPPORT_EXECUTOR_SUPPORT}
SUPPORT_DECISION_STAGE_SLUGS = {"anuencia", "pos-venda"}
WORKFLOW_KANBAN_MOVES_ENABLED = "WORKFLOW_KANBAN_MOVES_ENABLED"
WORKFLOW_REQUIRED_FIELD_CHOICES = (
    ("data_venda", "Data venda"),
    ("concessao", "Concessao"),
    ("contato", "Contato"),
    ("status", "Status"),
)
UNLOCK_TOTAL_DAYS = 90
UNLOCK_TRACKING_DAYS = 90
UNLOCK_WORKFLOW_SLUG = "30-dias"
UNLOCK_ENTRY_STAGE_SLUG = "90-dias"
UNLOCK_AUTO_STAGE_SLUGS = {"90-dias", "60-dias", "30-dias", "20-dias", "10-dias"}
UNLOCK_STAGE_WINDOWS = (
    (0, "dia-do-desbloqueio", "Dia do Desbloqueio"),
    (10, "10-dias", "10 dias"),
    (20, "20-dias", "20 dias"),
    (30, "30-dias", "30 dias"),
    (60, "60-dias", "Relacionamento: 60 a 31 dias"),
    (90, "90-dias", "Preparacao inicial: 90 a 61 dias"),
)
UNLOCK_CONTACT_TASK_RULES = {
    "90-dias": {"contacts_per_week": 1, "window_ceiling_days": 90, "window_floor_days": 61},
    "60-dias": {"contacts_per_week": 1, "window_ceiling_days": 60, "window_floor_days": 31},
    "30-dias": {"contacts_per_week": 2, "window_ceiling_days": 30, "window_floor_days": 21},
    "20-dias": {"contacts_per_week": 3, "window_ceiling_days": 20, "window_floor_days": 11},
    "10-dias": {"contacts_per_week": None, "window_ceiling_days": 10, "window_floor_days": 1},
}
FIDELITY_INSS_TASKS = (
    ("inss:fazer", "Fazer INSS", "fazer-inss"),
    ("inss:confirmar", "Confirmar INSS", "confirmacao-inss-1"),
    ("inss:retornar", "Retorno INSS", "retorno-inss-1"),
)
STATUS_OPTION_ALIASES = {
    "novo": {
        "keys": ("novo",),
        "fallback": "Novo",
    },
    "andamento": {
        "keys": ("em andamento", "andamento"),
        "fallback": "Em andamento",
    },
    "concluido": {
        "keys": ("concluido",),
        "fallback": "Concluido",
    },
    "recusado": {
        "keys": ("recusado", "perdido", "perda"),
        "fallback": "Recusado",
    },
}
TEMPORARY_WORKFLOW_BY_PRINCIPAL_STAGE = {
    "fidelizacao": {
        "workflow_slug": UNLOCK_WORKFLOW_SLUG,
        "entry_stage_slug": UNLOCK_ENTRY_STAGE_SLUG,
        "return_stage_slug": "dia-do-desbloqueio",
    },
    "30-dias-desbloqueio": {
        "workflow_slug": UNLOCK_WORKFLOW_SLUG,
        "entry_stage_slug": UNLOCK_ENTRY_STAGE_SLUG,
        "return_stage_slug": "dia-do-desbloqueio",
    },
}
CONTRACT_ACTIVITY_FIELDS = ("status",)
LAST_ACTIVITY_FIELDS = ATUACAO_FIELDS
OPERATIONAL_AUDIT_ACTIONS = (
    "activity_create",
    "workflow_checklist_update",
    "workflow_move",
    "support_complete",
    "support_return",
    "support_return_resolution",
    "scheduled_task_complete",
    "reversal_request",
    "reversal_cancel",
)
OPERATIONAL_TRANSITION_ACTIONS = (
    "move",
    "complete_temporary",
    "return_from_temporary",
    "support_complete",
    "support_return",
    "support_return_resolution",
    "seller_execution_complete",
    "scheduled_task_complete",
)
BASE_REQUIRED_COLUMNS = [
    "Equipe",
    "Consultor",
    "Nome Cliente",
    "CPF",
    "Data Venda",
    "Concessão",
    "Contato",
]
DEFAULT_OPTIONS = {
    "status": ["Novo", "Em andamento", "Concluido", "Recusado"],
    "motivo_perda": ["Sem retorno", "Cliente desistiu", "Dados divergentes", "Nao autorizado", "Outro"],
}
SELLER_EXECUTION_TASKS = {
    ("principal", "dia-do-desbloqueio"): {
        "key": "seller_unlock_execution",
        "label": "Fazer desbloqueio do beneficio",
    },
    ("principal", "anuencia"): {
        "key": "seller_anuencia_execution",
        "label": "Fazer anuencia do contrato",
    },
    ("principal", "pos-venda"): {
        "key": "seller_post_sale_execution",
        "label": "Realizar pos venda com cliente",
    },
}
REVERSAL_STATUS_LABELS = {
    ReversalStatus.QUEUED: "Reversao em fila",
    ReversalStatus.ASSIGNED: "Reversao em andamento",
    ReversalStatus.IN_PROGRESS: "Reversao em andamento",
    ReversalStatus.OVERDUE: "Reversao em atraso",
    ReversalStatus.COMPLETED: "Reversao devolvida",
    ReversalStatus.FAILED: "Reversao devolvida",
    ReversalStatus.RETURNED: "Reversao devolvida",
    ReversalStatus.CANCELED: "Reversao cancelada",
}
REVERSAL_STATUS_COLORS = {
    ReversalStatus.QUEUED: "#64748b",
    ReversalStatus.ASSIGNED: "#2563eb",
    ReversalStatus.IN_PROGRESS: "#0f766e",
    ReversalStatus.OVERDUE: "#dc2626",
    ReversalStatus.COMPLETED: "#f59e0b",
    ReversalStatus.FAILED: "#f59e0b",
    ReversalStatus.RETURNED: "#f59e0b",
    ReversalStatus.CANCELED: "#94a3b8",
}
REVERSAL_PUBLIC_STATUS_GROUPS = {
    ReversalStatus.QUEUED: (ReversalStatus.QUEUED,),
    ReversalStatus.IN_PROGRESS: (ReversalStatus.ASSIGNED, ReversalStatus.IN_PROGRESS),
    ReversalStatus.OVERDUE: (ReversalStatus.OVERDUE,),
    ReversalStatus.RETURNED: (ReversalStatus.RETURNED, ReversalStatus.COMPLETED, ReversalStatus.FAILED),
    ReversalStatus.CANCELED: (ReversalStatus.CANCELED,),
}
REVERSAL_PUBLIC_STATUS_ORDER = (
    ReversalStatus.OVERDUE,
    ReversalStatus.IN_PROGRESS,
    ReversalStatus.QUEUED,
    ReversalStatus.RETURNED,
    ReversalStatus.CANCELED,
)
REVERSAL_PRIORITY_LABELS = {
    ReversalPriority.STANDARD: "Leve",
    ReversalPriority.ATTENTION: "Moderada",
    ReversalPriority.URGENT: "Urgente",
}
REVERSAL_DISTRIBUTION_LABELS = {
    ReversalDistributionMode.AUTOMATIC: "Sorteio automatico",
    ReversalDistributionMode.MANUAL: "Sorteio pelo supervisor",
}
REVERSAL_RESULT_LABELS = {
    ReversalResult.SEM_CONTATO: "Sem contato (nao atendeu / nao respondeu WhatsApp)",
    ReversalResult.FIDELIZADO: "Cliente fidelizado",
    ReversalResult.NAO_ATENDEU: "Nao atendeu",
    ReversalResult.PEDIU_RETORNO: "Pediu retorno",
    ReversalResult.BLOQUEOU: "Bloqueou contato",
    ReversalResult.CLIENTE_INSEGURO: "Cliente inseguro",
    ReversalResult.NAO_QUIS_CONTRATAR: "Nao quis contratar",
    ReversalResult.NEGOCIANDO: "Negociando",
    ReversalResult.VENDA_FECHADA: "Venda fechada",
    ReversalResult.NEGOCIANDO_TERCEIRO: "Negociando com terceiro",
    ReversalResult.CLIENTE_VENDIDO_PERDIDO: "Cliente vendido/perdido",
    ReversalResult.OUTRO: "Outro",
}
REVERSAL_RESULT_POSITIVE = "positive"
REVERSAL_RESULT_NEGATIVE = "negative"
REVERSAL_RESULT_ALERT = "alert"
REVERSAL_RESULT_CRITICAL = "critical"
REVERSAL_RESULT_SENTIMENT_LABELS = {
    REVERSAL_RESULT_POSITIVE: "Positiva",
    REVERSAL_RESULT_NEGATIVE: "Negativa",
    REVERSAL_RESULT_ALERT: "Alerta",
    REVERSAL_RESULT_CRITICAL: "Critica",
}
REVERSAL_RESULT_SENTIMENTS = {
    ReversalResult.FIDELIZADO: REVERSAL_RESULT_POSITIVE,
    ReversalResult.NAO_ATENDEU: REVERSAL_RESULT_NEGATIVE,
    ReversalResult.PEDIU_RETORNO: REVERSAL_RESULT_NEGATIVE,
    ReversalResult.BLOQUEOU: REVERSAL_RESULT_NEGATIVE,
    ReversalResult.CLIENTE_INSEGURO: REVERSAL_RESULT_NEGATIVE,
    ReversalResult.NEGOCIANDO_TERCEIRO: REVERSAL_RESULT_CRITICAL,
    ReversalResult.CLIENTE_VENDIDO_PERDIDO: REVERSAL_RESULT_CRITICAL,
    ReversalResult.OUTRO: REVERSAL_RESULT_NEGATIVE,
}
REVERSAL_TABULATION_LOST_CONTACT = "perdi_contato_cliente"
REVERSAL_TABULATION_MODERATE_CONTACT = "contato_moderado"
REVERSAL_TABULATION_TEST_FIDELITY = "contato_em_dia_testar_fidelidade"
REVERSAL_TABULATIONS = {
    REVERSAL_TABULATION_LOST_CONTACT: {
        "label": "Perdi o contato",
        "priority": ReversalPriority.URGENT,
        "sla_hours": 10,
    },
    REVERSAL_TABULATION_TEST_FIDELITY: {
        "label": "Testar fidelidade",
        "priority": ReversalPriority.STANDARD,
        "sla_hours": 48,
    },
}
REVERSAL_TABULATION_ALIASES = {
    REVERSAL_TABULATION_MODERATE_CONTACT: REVERSAL_TABULATION_TEST_FIDELITY,
}
REVERSAL_RESULT_SENTIMENTS_BY_TABULATION = {
    REVERSAL_TABULATION_LOST_CONTACT: {
        ReversalResult.SEM_CONTATO: REVERSAL_RESULT_CRITICAL,
        ReversalResult.BLOQUEOU: REVERSAL_RESULT_CRITICAL,
        ReversalResult.NAO_QUIS_CONTRATAR: REVERSAL_RESULT_CRITICAL,
        ReversalResult.NEGOCIANDO: REVERSAL_RESULT_POSITIVE,
        ReversalResult.VENDA_FECHADA: REVERSAL_RESULT_POSITIVE,
        ReversalResult.NEGOCIANDO_TERCEIRO: REVERSAL_RESULT_CRITICAL,
        ReversalResult.OUTRO: REVERSAL_RESULT_NEGATIVE,
    },
    REVERSAL_TABULATION_TEST_FIDELITY: {
        ReversalResult.SEM_CONTATO: REVERSAL_RESULT_POSITIVE,
        ReversalResult.BLOQUEOU: REVERSAL_RESULT_POSITIVE,
        ReversalResult.NAO_QUIS_CONTRATAR: REVERSAL_RESULT_ALERT,
        ReversalResult.NEGOCIANDO: REVERSAL_RESULT_NEGATIVE,
        ReversalResult.VENDA_FECHADA: REVERSAL_RESULT_CRITICAL,
        ReversalResult.NEGOCIANDO_TERCEIRO: REVERSAL_RESULT_CRITICAL,
        ReversalResult.OUTRO: REVERSAL_RESULT_NEGATIVE,
    },
}
REVERSAL_NOTIFICATION_RECEIVED = "reversal_received"
REVERSAL_NOTIFICATION_OVERDUE = "reversal_overdue"
REVERSAL_NOTIFICATION_RESULT = "reversal_result"
REVERSAL_NOTIFICATION_CRITICAL_RESULT = "reversal_critical_result"
REVERSAL_NOTIFICATION_CLIENT_SOLD_LOST = "reversal_client_sold_lost"
SUPPORT_NOTIFICATION_ASSIGNED = "support_assigned"
SUPPORT_NOTIFICATION_OVERDUE = "support_overdue"
SUPPORT_NOTIFICATION_RESENT = "support_resent"
REVERSAL_PRIORITY_SORT = {
    ReversalPriority.URGENT: 0,
    ReversalPriority.ATTENTION: 1,
    ReversalPriority.STANDARD: 2,
}
REVERSAL_QUEUE_COLUMNS = (
    {"key": "days_30", "label": "30 dias", "description": "Reversoes mais distantes do desbloqueio."},
    {"key": "days_20", "label": "20 dias", "description": "Reversoes em fase de atencao."},
    {"key": "days_10", "label": "10 dias", "description": "Reversoes perto do desbloqueio."},
)
DEFAULT_OPTION_COLORS = {
    "status": {
        "novo": "#8b5cf6",
        "concluido": "#16a34a",
        "andamento": "#2563eb",
        "em andamento": "#2563eb",
        "recusado": "#dc2626",
    },
    "motivo_perda": {
        "sem retorno": "#64748b",
        "cliente desistiu": "#dc2626",
        "dados divergentes": "#f59e0b",
        "nao autorizado": "#7c3aed",
        "outro": "#0f766e",
    },
}
DEFAULT_WORKFLOWS = [
    {
        "slug": "principal",
        "name": "Fluxo Principal",
        "description": "Jornada macro do cliente, da venda ao pos-venda.",
        "sort_order": 1,
        "stages": [
            {
                "slug": "venda",
                "name": "Venda",
                "sort_order": 1,
                "requires_note": True,
                "requires_checklist": True,
                "required_fields": ["data_venda", "concessao"],
                "checklist": ["Dados da venda conferidos"],
            },
            {
                "slug": "fidelizacao",
                "name": "Fidelizacao",
                "sort_order": 2,
                "requires_note": True,
                "requires_checklist": True,
                "checklist": ["Fidelizacao concluida ou justificada"],
            },
            {
                "slug": "30-dias-desbloqueio",
                "name": "30 dias Desbloqueio",
                "sort_order": 3,
                "alert_days_before": 3,
                "requires_note": True,
                "requires_checklist": True,
                "required_fields": ["concessao"],
                "checklist": ["Cliente pronto para acompanhamento dos 30 dias"],
            },
            {
                "slug": "dia-do-desbloqueio",
                "name": "Dia do Desbloqueio",
                "sort_order": 4,
                "alert_days_before": 1,
                "requires_note": True,
                "requires_checklist": True,
                "required_fields": ["concessao"],
                "checklist": ["Desbloqueio conferido"],
                "allow_auto_move": True,
            },
            {
                "slug": "anuencia",
                "name": "Anuencia",
                "sort_order": 5,
                "requires_note": True,
                "requires_checklist": True,
                "requires_support": True,
                "support_sla_hours": 24,
                "checklist": ["Anuencia registrada"],
            },
            {
                "slug": "pos-venda",
                "name": "Pos Venda",
                "sort_order": 6,
                "requires_support": True,
                "support_sla_hours": 24,
                "requires_checklist": True,
                "checklist": ["Pos-venda concluido"],
            },
        ],
    },
    {
        "slug": "fidelizacao",
        "name": "Fluxo Fidelizacao",
        "description": "Acompanhamento das confirmacoes e retornos do INSS.",
        "sort_order": 2,
        "stages": [
            {"slug": "fazer-inss", "name": "Fazer INSS", "sort_order": 1, "first_action_hours": FAZER_INSS_OPERATIONAL_SLA_HOURS, "requires_note": True, "requires_checklist": True, "checklist": ["INSS iniciado"]},
            {"slug": "confirmacao-inss-1", "name": "Confirmacao INSS 1", "sort_order": 2, "requires_note": True, "requires_checklist": True, "checklist": ["Confirmacao registrada"]},
            {"slug": "retorno-inss-1", "name": "Retorno INSS 1", "sort_order": 3, "requires_note": True, "requires_checklist": True, "checklist": ["Retorno registrado"]},
            {"slug": "confirmacao-inss-2", "name": "Confirmacao INSS 2", "sort_order": 4, "requires_note": True, "requires_checklist": True, "checklist": ["Confirmacao registrada"]},
            {"slug": "retorno-inss-2", "name": "Retorno INSS 2", "sort_order": 5, "requires_note": True, "requires_checklist": True, "checklist": ["Retorno registrado"]},
            {"slug": "confirmacao-inss-3", "name": "Confirmacao INSS 3", "sort_order": 6, "requires_note": True, "requires_checklist": True, "checklist": ["Confirmacao registrada"]},
            {"slug": "retorno-inss-3", "name": "Retorno INSS 3", "sort_order": 7, "requires_note": True, "requires_checklist": True, "checklist": ["Retorno final registrado"]},
        ],
    },
    {
        "slug": "30-dias",
        "name": "Fidelizacao",
        "description": "Acompanhamento do cliente da concessao ate o desbloqueio.",
        "sort_order": 3,
        "stages": [
            {"slug": "90-dias", "name": "Preparacao inicial · 90 a 61 dias", "sort_order": -1},
            {"slug": "60-dias", "name": "Relacionamento · 60 a 31 dias", "sort_order": 0},
            {"slug": "30-dias", "name": "30 dias", "sort_order": 1, "max_days": 10, "requires_note": True, "requires_checklist": True, "required_fields": ["concessao"], "checklist": ["Cliente acompanhado no periodo de 30 dias"]},
            {"slug": "20-dias", "name": "20 dias", "sort_order": 2, "max_days": 10, "requires_note": True, "requires_checklist": True, "required_fields": ["concessao"], "checklist": ["Cliente acompanhado no periodo de 20 dias"]},
            {"slug": "10-dias", "name": "10 dias", "sort_order": 3, "max_days": 9, "alert_days_before": 3, "requires_note": True, "requires_checklist": True, "required_fields": ["concessao"], "checklist": ["Cliente preparado para o desbloqueio"]},
        ],
    },
]
DEFAULT_LEAD_WORKFLOW_SLUG = "principal"
DEFAULT_LEAD_WORKFLOW_STAGE_SLUG = "venda"
POST_SALE_STAGE_SLUG = "pos-venda"
OPERATOR_SEEDS = [
    ("Equipe Demo A", "Operador Demo 01"),
    ("Equipe Demo A", "Operador Demo 02"),
    ("Equipe Demo B", "Operador Demo 03"),
]

CANONICAL_COLUMNS = {
    "equipe": "Equipe",
    "consultor": "Consultor",
    "nome cliente": "Nome Cliente",
    "cpf": "CPF",
    "data venda": "Data Venda",
    "concessao": "Concessão",
    "contato": "Contato",
    "status": "Status",
    "acao": "Ação",
    "action": "Ação",
    "motivo": "Motivo",
    "reason": "Motivo",
}
for index in range(1, 13):
    CANONICAL_COLUMNS[f"atuacao {index}"] = f"Atuação {index}"


oauth = OAuth() if OAuth else None


def create_app():
    ensure_default_database_location()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = resolve_secret_key()
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{DEFAULT_DB_PATH}",
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLITE_BUSY_TIMEOUT_MS"] = sqlite_busy_timeout_ms()
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {
                "timeout": app.config["SQLITE_BUSY_TIMEOUT_MS"] / 1000,
            }
        }
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
    app.config["ALLOWED_HOSTS"] = {
        host.strip().lower()
        for host in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost,::1").split(",")
        if host.strip()
    }
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = env_truthy("SESSION_COOKIE_SECURE")
    app.permanent_session_lifetime = timedelta(hours=int(os.environ.get("SESSION_HOURS", "8")))
    app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID")
    app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")
    app.config["PUBLIC_BASE_URL"] = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    app.config["GOOGLE_SIGNUP_ALLOWED_DOMAINS"] = {
        domain.strip().lower().lstrip("@")
        for domain in os.environ.get(
            "GOOGLE_SIGNUP_ALLOWED_DOMAINS",
            "example.invalid",
        ).split(",")
        if domain.strip()
    }
    app.config["ADMIN_EMAILS"] = {
        email.strip().lower()
        for email in os.environ.get("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }

    db.init_app(app)
    with app.app_context():
        configure_sqlite_engine(app)
    configure_google_oauth(app)

    register_auth_hooks(app)
    register_security_hooks(app)
    register_cli(app)
    register_routes(app)

    return app


def env_truthy(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def sqlite_busy_timeout_ms():
    raw_value = os.environ.get("SQLITE_BUSY_TIMEOUT_MS", DEFAULT_SQLITE_BUSY_TIMEOUT_MS)
    try:
        return min(max(int(raw_value), 1000), 60000)
    except (TypeError, ValueError):
        return DEFAULT_SQLITE_BUSY_TIMEOUT_MS


def configure_sqlite_engine(app):
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        return

    timeout_ms = app.config["SQLITE_BUSY_TIMEOUT_MS"]

    @event.listens_for(db.engine, "connect")
    def configure_connection(dbapi_connection, _connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
        finally:
            cursor.close()


def is_production_env():
    return (
        env_truthy("APP_PRODUCTION")
        or os.environ.get("APP_ENV", "").lower() == "production"
        or os.environ.get("FLASK_ENV", "").lower() == "production"
    )


def resolve_secret_key():
    secret_key = os.environ.get("SECRET_KEY")
    if secret_key:
        return secret_key

    if is_production_env():
        raise RuntimeError("Configure SECRET_KEY antes de iniciar o CRM em producao.")

    os.makedirs(INSTANCE_DIR, exist_ok=True)
    if os.path.exists(DEV_SECRET_PATH):
        with open(DEV_SECRET_PATH, "r", encoding="utf-8") as file:
            stored_secret = file.read().strip()
            if stored_secret:
                return stored_secret

    generated_secret = secrets.token_urlsafe(48)
    with open(DEV_SECRET_PATH, "w", encoding="utf-8") as file:
        file.write(generated_secret)
    return generated_secret


def ensure_default_database_location():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    if not os.path.exists(DEFAULT_DB_PATH) and os.path.exists(LEGACY_DB_PATH):
        shutil.copy2(LEGACY_DB_PATH, DEFAULT_DB_PATH)


def configure_google_oauth(app):
    if not oauth:
        app.config["GOOGLE_AUTH_AVAILABLE"] = False
        return

    oauth.init_app(app)

    if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
        app.config["GOOGLE_AUTH_AVAILABLE"] = False
        return

    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        client_kwargs={"scope": "openid email profile"},
    )
    app.config["GOOGLE_AUTH_AVAILABLE"] = True


def user_must_change_password(user):
    return bool(user and user.password_hash and user.must_change_password)


def store_modal_form_state(modal_id, form=None):
    form = form or request.form
    values = {}
    for field in form.keys():
        if field in SENSITIVE_MODAL_FORM_FIELDS or field == "next":
            continue
        submitted_values = [
            clean_text(value) or ""
            for value in form.getlist(field)
        ]
        values[field] = submitted_values if len(submitted_values) > 1 else (
            submitted_values[0] if submitted_values else ""
        )

    session[MODAL_FORM_STATE_SESSION_KEY] = {
        "modal_id": modal_id,
        "values": values,
    }
    session.modified = True


def modal_state_matches(state, modal_id):
    return bool(state and state.get("modal_id") == modal_id)


def modal_state_value(state, modal_id, field, default=""):
    if not modal_state_matches(state, modal_id):
        return default
    value = state.get("values", {}).get(field, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def modal_state_values(state, modal_id, field):
    if not modal_state_matches(state, modal_id):
        return []
    value = state.get("values", {}).get(field, [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def modal_state_checked(state, modal_id, field, default=False):
    if not modal_state_matches(state, modal_id):
        return default
    value = state.get("values", {}).get(field)
    if isinstance(value, list):
        return any(str(item).lower() in {"on", "1", "true", "yes"} for item in value)
    return str(value).lower() in {"on", "1", "true", "yes"}


def modal_state_selected(state, modal_id, field, candidate, default=False):
    if not modal_state_matches(state, modal_id):
        return default
    if field not in state.get("values", {}):
        return default
    return str(candidate) in modal_state_values(state, modal_id, field)


def register_auth_hooks(app):
    @app.before_request
    def load_current_user():
        user_id = session.get("user_id")
        g.user = db.session.get(User, user_id) if user_id else None
        if g.user and not g.user.active:
            session.clear()
            g.user = None
        if (
            g.user
            and user_must_change_password(g.user)
            and request.endpoint not in {"account_password", "logout", "static"}
        ):
            if request.method == "GET":
                flash("Troque sua senha local para continuar usando o CRM.", "warning")
            return redirect(url_for("account_password"))


def register_security_hooks(app):
    @app.context_processor
    def inject_security_helpers():
        modal_form_state = session.pop(MODAL_FORM_STATE_SESSION_KEY, None) or {}
        return {
            "csrf_token": csrf_token,
            "modal_form_state": modal_form_state,
            "modal_should_open": lambda modal_id: modal_state_matches(modal_form_state, modal_id),
            "modal_value": lambda modal_id, field, default="": modal_state_value(
                modal_form_state,
                modal_id,
                field,
                default,
            ),
            "modal_checked": lambda modal_id, field, default=False: modal_state_checked(
                modal_form_state,
                modal_id,
                field,
                default,
            ),
            "modal_selected": lambda modal_id, field, candidate, default=False: modal_state_selected(
                modal_form_state,
                modal_id,
                field,
                candidate,
                default,
            ),
            "reversal_notification_count": unread_reversal_notification_count(getattr(g, "user", None)),
            "support_notification_count": unread_support_notification_count(getattr(g, "user", None)),
        }

    @app.before_request
    def validate_host_header():
        allowed_hosts = app.config.get("ALLOWED_HOSTS") or set()
        if not allowed_hosts:
            return

        host = normalize_request_host(request.host)
        if host not in allowed_hosts:
            abort(400)

    @app.before_request
    def csrf_protect():
        if request.method not in CSRF_METHODS:
            return

        expected_token = session.get("_csrf_token")
        submitted_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected_token or not submitted_token or not compare_digest(expected_token, submitted_token):
            abort(400)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "base-uri 'self'; "
                "frame-ancestors 'none'; "
                "object-src 'none'; "
                "form-action 'self'; "
                "img-src 'self' data:; "
                "font-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'"
            ),
        )
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store, max-age=0")
            response.headers.setdefault("Pragma", "no-cache")
        return response


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def normalize_request_host(host):
    host = (host or "").lower().strip()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    return host.rsplit(":", 1)[0]


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def user_has_permission(user, *permissions):
    return bool(user and any(getattr(user, permission, False) for permission in permissions))


def permission_required(*permissions):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login"))
            if permissions and not user_has_permission(g.user, *permissions):
                flash("Seu usuário não tem permissão para acessar essa ação.", "danger")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


BULK_ACTION_PERMISSIONS = {
    "export": "can_bulk_export_leads",
    "inactivate": "can_bulk_inactivate_leads",
    "reactivate": "can_bulk_reactivate_leads",
    "set_status": "can_bulk_edit_status",
    "transfer": "can_bulk_transfer_leads",
    "delete": "can_bulk_delete_leads",
}
BULK_ACTION_PERMISSION_FIELDS = tuple(dict.fromkeys(BULK_ACTION_PERMISSIONS.values()))


def bulk_permission_for_action(action):
    return BULK_ACTION_PERMISSIONS.get(action)


def user_can_perform_bulk_action(user, action):
    permission = bulk_permission_for_action(action)
    return bool(permission and getattr(user, permission, False))


def user_has_any_bulk_action(user):
    return user_has_permission(user, *BULK_ACTION_PERMISSION_FIELDS)


def user_can_view_inactive_leads(user):
    return bool(
        user_has_permission(user, "can_view_all", "can_reactivate_leads", "can_bulk_reactivate_leads")
    )


PERMISSION_LABELS = {
    "can_view_all": "Ver todas as carteiras",
    "can_view_all_franchises": "Ver todas as franquias",
    "can_upload": "Importar planilhas",
    "can_create_leads": "Criar cliente",
    "can_export_leads": "Exportar clientes",
    "can_edit_core": "Editar dados cadastrais",
    "can_edit_contact": "Editar contato",
    "can_edit_status": "Editar status manual",
    "can_edit_posicao": "Editar campo legado de posição",
    "can_edit_atuacoes": "Registrar atuacoes",
    "can_transfer_leads": "Transferir cliente",
    "can_delete_leads": "Excluir cliente",
    "can_deactivate_leads": "Inativar cliente",
    "can_reactivate_leads": "Reativar cliente",
    "can_conclude_contract": "Concluir contrato",
    "can_reject_contract": "Recusar contrato",
    "can_undo_contract_status": "Desfazer status final",
    "can_bulk_manage_leads": "Painel em massa legado",
    "can_bulk_export_leads": "Exportar em massa",
    "can_bulk_transfer_leads": "Transferir em massa",
    "can_bulk_edit_status": "Alterar status em massa",
    "can_bulk_edit_posicao": "Alterar campo legado de posição em massa",
    "can_bulk_inactivate_leads": "Inativar em massa",
    "can_bulk_reactivate_leads": "Reativar em massa",
    "can_bulk_delete_leads": "Excluir em massa",
    "can_move_workflows": "Avancar etapas",
    "can_move_workflows_backwards": "Voltar etapas",
    "can_act_as_support": "Atuar como apoio",
    "can_request_support": "Solicitar apoio",
    "can_request_reversal": "Enviar para reversao",
    "can_work_reversal": "Atuar reversoes",
    "can_manage_reversals": "Gerenciar reversoes",
    "can_view_audit": "Ver auditoria",
    "can_manage_settings": "Configurar listas",
    "can_manage_workflows": "Configurar fluxos",
    "can_manage_roles": "Configurar cargos",
    "can_manage_teams": "Configurar equipes",
    "can_manage_users": "Gerenciar usuarios",
}


PERMISSION_GROUPS = (
    {
        "slug": "access",
        "title": "Acesso e carteira",
        "description": "Define alcance de visualizacao, importacao e cadastro de clientes.",
        "fields": (
            "can_view_all",
            "can_view_all_franchises",
            "can_upload",
            "can_create_leads",
            "can_export_leads",
        ),
    },
    {
        "slug": "lead",
        "title": "Ficha do cliente",
        "description": "Controla alteracoes diretas nos dados e na propriedade do cliente.",
        "fields": (
            "can_edit_core",
            "can_edit_contact",
            "can_edit_status",
            "can_edit_atuacoes",
            "can_transfer_leads",
            "can_deactivate_leads",
            "can_reactivate_leads",
            "can_delete_leads",
        ),
    },
    {
        "slug": "contract",
        "title": "Resultado do contrato",
        "description": "Acoes finais que mudam o estado comercial do contrato.",
        "fields": ("can_conclude_contract", "can_reject_contract", "can_undo_contract_status"),
    },
    {
        "slug": "bulk",
        "title": "Acoes em massa",
        "description": "Permissoes para atuar sobre varios clientes selecionados ou por importacao.",
        "fields": (
            "can_bulk_manage_leads",
            "can_bulk_export_leads",
            "can_bulk_transfer_leads",
            "can_bulk_edit_status",
            "can_bulk_inactivate_leads",
            "can_bulk_reactivate_leads",
            "can_bulk_delete_leads",
        ),
    },
    {
        "slug": "workflow",
        "title": "Fluxos, apoio e reversao",
        "description": "Controla movimentacao em etapas, Apoio Comercial e rotina de Reversao.",
        "fields": (
            "can_move_workflows",
            "can_move_workflows_backwards",
            "can_act_as_support",
            "can_request_support",
            "can_request_reversal",
            "can_work_reversal",
            "can_manage_reversals",
        ),
    },
    {
        "slug": "admin",
        "title": "Administracao",
        "description": "Acessos sensiveis de configuracao, auditoria e usuarios.",
        "fields": (
            "can_view_audit",
            "can_manage_settings",
            "can_manage_workflows",
            "can_manage_teams",
            "can_manage_roles",
            "can_manage_users",
        ),
    },
)


def permission_label(field):
    return PERMISSION_LABELS.get(field, field)


def assert_actor_can_assign_base_role(actor, base_role):
    if base_role == Role.ADMIN and not getattr(actor, "is_admin", False):
        raise ValueError("Apenas um admin pode conceder o tipo base ADMIN.")


def assert_actor_can_assign_permissions(actor, subject):
    assert_actor_can_assign_base_role(actor, getattr(subject, "role", None) or getattr(subject, "base_role", None))
    if bool(getattr(subject, "can_view_all_franchises", False)) and not user_can_view_all_franchises(actor):
        raise ValueError("Seu usuario nao pode conceder acesso a todas as franquias.")
    if getattr(actor, "is_admin", False):
        return

    disallowed = [
        permission_label(field)
        for field in PERMISSION_FIELDS
        if bool(getattr(subject, field, False)) and not bool(getattr(actor, field, False))
    ]
    if disallowed:
        preview = ", ".join(disallowed[:5])
        if len(disallowed) > 5:
            preview = f"{preview} e mais {len(disallowed) - 5}"
        raise ValueError(f"Seu usuario nao pode conceder permissoes acima do proprio acesso: {preview}.")


def is_admin_control_snapshot(snapshot):
    return bool(
        snapshot
        and snapshot.get("active")
        and snapshot.get("role") == Role.ADMIN
        and snapshot.get("can_manage_users")
        and snapshot.get("can_view_all")
        and snapshot.get("can_view_all_franchises")
    )


def is_admin_control_user(user):
    return bool(
        user
        and user.active
        and user.role == Role.ADMIN
        and user.can_manage_users
        and user.can_view_all
        and getattr(user, "can_view_all_franchises", False)
    )


def active_admin_control_count(exclude_user_id=None):
    query = User.query.filter(
        User.active.is_(True),
        User.role == Role.ADMIN,
        User.can_manage_users.is_(True),
        User.can_view_all.is_(True),
        User.can_view_all_franchises.is_(True),
    )
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def assert_not_removing_last_admin(before_snapshot, user):
    if (
        is_admin_control_snapshot(before_snapshot)
        and not is_admin_control_user(user)
        and active_admin_control_count(exclude_user_id=user.id) == 0
    ):
        raise ValueError("Mantenha pelo menos um ADMIN ativo com acesso total antes de alterar este usuario.")


def assert_system_has_admin_control():
    if active_admin_control_count() == 0:
        raise ValueError("A operacao deixaria o CRM sem nenhum ADMIN ativo com acesso total.")


def ensure_default_company_structure():
    slug = normalize_slug(DEFAULT_COMPANY_SLUG) or "demo"
    company = Company.query.filter(func.lower(Company.slug) == slug.lower()).first()
    if not company:
        company = Company(
            name=DEFAULT_COMPANY_NAME,
            slug=slug,
            active=True,
        )
        db.session.add(company)
        db.session.flush()
    else:
        company.name = company.name or DEFAULT_COMPANY_NAME
        company.active = True

    franchise = Franchise.query.filter(
        Franchise.company_id == company.id,
        func.lower(Franchise.name) == DEFAULT_FRANCHISE_NAME.lower(),
    ).first()
    if not franchise:
        franchise = Franchise(
            company_id=company.id,
            name=DEFAULT_FRANCHISE_NAME,
            active=True,
        )
        db.session.add(franchise)
        db.session.flush()
    else:
        franchise.active = True

    return company, franchise


def default_company():
    company, _ = ensure_default_company_structure()
    return company


def default_franchise():
    _, franchise = ensure_default_company_structure()
    return franchise


def default_company_for_user(user):
    if user and user.company:
        return user.company
    company, _ = ensure_default_company_structure()
    return company


def default_franchise_for_user(user):
    if user and user.franchise:
        return user.franchise
    _, franchise = ensure_default_company_structure()
    return franchise


def company_id_for_user(user):
    if user and user.company_id:
        return user.company_id
    return default_company().id


def franchise_id_for_user(user):
    if user and user.franchise_id:
        return user.franchise_id
    return default_franchise().id


def target_franchise_id_from_form(form, user):
    if user_can_view_all_franchises(user):
        return validate_franchise_id_for_form(
            form.get("franchise_id"),
            user,
            fallback_id=franchise_id_for_user(user),
        )
    return franchise_id_for_user(user)


def configured_admin_emails():
    if has_app_context():
        return current_app.config.get("ADMIN_EMAILS", set())
    return {
        email.strip().lower()
        for email in os.environ.get("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }


def user_can_view_all_franchises(user):
    return bool(user and getattr(user, "can_view_all_franchises", False))


def scoped_franchise_filter(column, user, include_unscoped=False):
    franchise_id = getattr(user, "franchise_id", None)
    if not franchise_id:
        return None
    return column == franchise_id


def scoped_company_query(query, model, user=None, include_unscoped=False):
    company_column = getattr(model, "company_id", None)
    franchise_column = getattr(model, "franchise_id", None)

    company_id = company_id_for_user(user) if user else default_company().id
    if include_unscoped:
        if company_column is not None:
            query = query.filter(or_(company_column == company_id, company_column.is_(None)))
    elif company_column is not None:
        query = query.filter(company_column == company_id)

    if user and not user_can_view_all_franchises(user):
        if model is Franchise:
            franchise_filter = Franchise.id == user.franchise_id if user.franchise_id else None
        elif franchise_column is not None:
            franchise_filter = scoped_franchise_filter(franchise_column, user, include_unscoped)
        else:
            franchise_filter = None
        if franchise_filter is not None:
            query = query.filter(franchise_filter)

    return query


def apply_joined_lead_scope(query, user=None, include_unscoped=True):
    company_id = company_id_for_user(user) if user else default_company().id
    if include_unscoped:
        query = query.filter(or_(Lead.company_id == company_id, Lead.company_id.is_(None)))
    else:
        query = query.filter(Lead.company_id == company_id)

    if user and not user_can_view_all_franchises(user) and user.franchise_id:
        query = query.filter(Lead.franchise_id == user.franchise_id)
    return query


def in_same_company_scope(user, record):
    if not user or not record:
        return False
    record_company_id = getattr(record, "company_id", None)
    if record_company_id and record_company_id != company_id_for_user(user):
        return False
    if user_can_view_all_franchises(user):
        return True
    if isinstance(record, Franchise):
        return bool(user.franchise_id and record.id == user.franchise_id)
    if hasattr(record, "franchise_id"):
        return bool(user.franchise_id and record.franchise_id == user.franchise_id)
    return True


def user_matches_lead_franchise_scope(user, lead):
    if not user or not lead:
        return False
    if lead.company_id and user.company_id != lead.company_id:
        return False
    if lead.franchise_id and user.franchise_id != lead.franchise_id:
        return False
    return True


def scope_user_query_to_lead(query, lead):
    if not lead:
        return query.filter(False)
    if lead.company_id:
        query = query.filter(User.company_id == lead.company_id)
    if lead.franchise_id:
        query = query.filter(User.franchise_id == lead.franchise_id)
    return query


def request_user_or_none():
    if not has_request_context():
        return None
    return getattr(g, "user", None)


def register_cli(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Cria/atualiza o banco e usuarios iniciais para desenvolvimento."""
        db.create_all()
        migrate_sqlite_schema()
        ensure_default_company_structure()
        seed_default_options()
        retire_legacy_reversal_position()
        seed_default_teams()
        seed_default_reversal_configs()
        seed_default_role_profiles()
        if User.query.count() == 0:
            seed_default_users(app)
        seed_default_workflows()
        print("Banco inicializado/atualizado em instance/crm.sqlite3")

    @app.cli.command("sqlite-status")
    def sqlite_status_command():
        """Exibe as configuracoes atuais do SQLite."""
        status = sqlite_status()
        if not status:
            print("O banco configurado nao usa SQLite.")
            return
        for name, value in status.items():
            print(f"{name}={value}")

    @app.cli.command("sqlite-enable-wal")
    def sqlite_enable_wal_command():
        """Ativa WAL no SQLite. Execute durante a publicacao, com o CRM parado."""
        status = enable_sqlite_wal()
        if not status:
            print("O banco configurado nao usa SQLite.")
            return
        print("SQLite preparado para concorrencia de leitura e escrita.")
        for name, value in status.items():
            print(f"{name}={value}")


def sqlite_status():
    if not db.engine.url.drivername.startswith("sqlite"):
        return None

    with db.engine.connect() as connection:
        return {
            "journal_mode": connection.execute(text("PRAGMA journal_mode")).scalar(),
            "busy_timeout_ms": connection.execute(text("PRAGMA busy_timeout")).scalar(),
            "foreign_keys": connection.execute(text("PRAGMA foreign_keys")).scalar(),
        }


def enable_sqlite_wal():
    if not db.engine.url.drivername.startswith("sqlite"):
        return None

    with db.engine.connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("PRAGMA wal_autocheckpoint=1000"))

    return sqlite_status()


def migrate_sqlite_schema():
    if not db.engine.url.drivername.startswith("sqlite"):
        return

    table_names = set(inspect(db.engine).get_table_names())
    if "users" not in table_names or "leads" not in table_names:
        return

    for model in (ConfigurationMigration, TaskConfigurationScope, TaskConfigurationRevision, ChecklistStageConfiguration):
        model.__table__.create(db.engine, checkfirst=True)
    ensure_customers_table()
    table_names = set(inspect(db.engine).get_table_names())

    added_user_columns = ensure_columns(
        "users",
        {
            "email": "VARCHAR(255)",
            "google_sub": "VARCHAR(255)",
            "lead_name": "VARCHAR(120)",
            "role_profile_id": "INTEGER",
            "company_id": "INTEGER",
            "franchise_id": "INTEGER",
            "last_login_at": "DATETIME",
            "last_login_method": "VARCHAR(30)",
            "last_login_ip": "VARCHAR(64)",
            "password_changed_at": "DATETIME",
            "must_change_password": "BOOLEAN NOT NULL DEFAULT 0",
            "can_view_all": "BOOLEAN NOT NULL DEFAULT 0",
            "can_view_all_franchises": "BOOLEAN NOT NULL DEFAULT 0",
            "can_upload": "BOOLEAN NOT NULL DEFAULT 0",
            "can_create_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_users": "BOOLEAN NOT NULL DEFAULT 0",
            "can_edit_core": "BOOLEAN NOT NULL DEFAULT 0",
            "can_edit_contact": "BOOLEAN NOT NULL DEFAULT 1",
            "can_edit_status": "BOOLEAN NOT NULL DEFAULT 0",
            "can_edit_posicao": "BOOLEAN NOT NULL DEFAULT 0",
            "can_edit_atuacoes": "BOOLEAN NOT NULL DEFAULT 1",
            "can_bulk_manage_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_export_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_transfer_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_edit_status": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_edit_posicao": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_inactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_reactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_bulk_delete_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_export_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_transfer_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_move_workflows": "BOOLEAN NOT NULL DEFAULT 1",
            "can_move_workflows_backwards": "BOOLEAN NOT NULL DEFAULT 0",
            "can_delete_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_deactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_reactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
            "can_conclude_contract": "BOOLEAN NOT NULL DEFAULT 0",
            "can_reject_contract": "BOOLEAN NOT NULL DEFAULT 0",
            "can_undo_contract_status": "BOOLEAN NOT NULL DEFAULT 0",
            "can_act_as_support": "BOOLEAN NOT NULL DEFAULT 0",
            "can_request_support": "BOOLEAN NOT NULL DEFAULT 0",
            "can_request_reversal": "BOOLEAN NOT NULL DEFAULT 0",
            "can_work_reversal": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_reversals": "BOOLEAN NOT NULL DEFAULT 0",
            "can_view_audit": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_settings": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_workflows": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_roles": "BOOLEAN NOT NULL DEFAULT 0",
            "can_manage_teams": "BOOLEAN NOT NULL DEFAULT 0",
            "updated_at": "DATETIME",
        },
    )
    added_role_profile_columns = set()
    if "role_profiles" in table_names:
        added_role_profile_columns = ensure_columns(
            "role_profiles",
        {
            "company_id": "INTEGER",
            "can_view_all": "BOOLEAN NOT NULL DEFAULT 0",
                "can_view_all_franchises": "BOOLEAN NOT NULL DEFAULT 0",
                "can_upload": "BOOLEAN NOT NULL DEFAULT 0",
                "can_create_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_users": "BOOLEAN NOT NULL DEFAULT 0",
                "can_edit_core": "BOOLEAN NOT NULL DEFAULT 0",
                "can_edit_contact": "BOOLEAN NOT NULL DEFAULT 1",
                "can_edit_status": "BOOLEAN NOT NULL DEFAULT 0",
                "can_edit_posicao": "BOOLEAN NOT NULL DEFAULT 0",
                "can_edit_atuacoes": "BOOLEAN NOT NULL DEFAULT 1",
                "can_bulk_manage_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_export_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_transfer_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_edit_status": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_edit_posicao": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_inactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_reactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_bulk_delete_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_export_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_transfer_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_move_workflows": "BOOLEAN NOT NULL DEFAULT 1",
                "can_move_workflows_backwards": "BOOLEAN NOT NULL DEFAULT 0",
                "can_delete_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_deactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_reactivate_leads": "BOOLEAN NOT NULL DEFAULT 0",
                "can_conclude_contract": "BOOLEAN NOT NULL DEFAULT 0",
                "can_reject_contract": "BOOLEAN NOT NULL DEFAULT 0",
                "can_undo_contract_status": "BOOLEAN NOT NULL DEFAULT 0",
                "can_act_as_support": "BOOLEAN NOT NULL DEFAULT 0",
                "can_request_support": "BOOLEAN NOT NULL DEFAULT 0",
                "can_request_reversal": "BOOLEAN NOT NULL DEFAULT 0",
                "can_work_reversal": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_reversals": "BOOLEAN NOT NULL DEFAULT 0",
                "can_view_audit": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_settings": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_workflows": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_roles": "BOOLEAN NOT NULL DEFAULT 0",
                "can_manage_teams": "BOOLEAN NOT NULL DEFAULT 0",
            },
        )
    ensure_columns(
        "leads",
        {
            "customer_id": "INTEGER",
            "company_id": "INTEGER",
            "franchise_id": "INTEGER",
            "posicao": "VARCHAR(120)",
            "contato_manual": "BOOLEAN NOT NULL DEFAULT 0",
            "posicao_manual": "BOOLEAN NOT NULL DEFAULT 0",
            "deleted_at": "DATETIME",
            "deleted_by_id": "INTEGER",
            "deleted_reason": "TEXT",
            **{field: "TEXT" for field in ATUACAO_FIELDS},
            **{f"{field}_manual": "BOOLEAN NOT NULL DEFAULT 0" for field in ATUACAO_FIELDS},
        },
    )
    ensure_columns(
        "field_options",
        {
            "company_id": "INTEGER",
            "color_hex": "VARCHAR(20)",
        },
    )
    ensure_columns(
        "teams",
        {
            "company_id": "INTEGER",
            "franchise_id": "INTEGER",
        },
    )
    if "google_access_requests" in table_names:
        ensure_columns(
            "google_access_requests",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
            },
        )
    if "import_batches" in table_names:
        ensure_columns(
            "import_batches",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
            },
        )
    if "audit_logs" in table_names:
        ensure_columns(
            "audit_logs",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
            },
        )

    db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"))
    db.session.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users(google_sub)")
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_lead_name ON users(lead_name)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_role_profile_id ON users(role_profile_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_company_id ON users(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_franchise_id ON users(franchise_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_last_login_at ON users(last_login_at)"))
    if "atuacao" in get_columns("leads"):
        db.session.execute(
            text("UPDATE leads SET atuacao_1 = atuacao WHERE atuacao_1 IS NULL AND atuacao IS NOT NULL")
        )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_deleted_at ON leads(deleted_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_customer_id ON leads(customer_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_company_id ON leads(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_franchise_id ON leads(franchise_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_company_id ON teams(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_franchise_id ON teams(franchise_id)"))
    if "role_profiles" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_role_profiles_company_id ON role_profiles(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_field_options_company_id ON field_options(company_id)"))
    if "google_access_requests" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_google_access_requests_company_id ON google_access_requests(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_google_access_requests_franchise_id ON google_access_requests(franchise_id)"))
    if "import_batches" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_import_batches_company_id ON import_batches(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_import_batches_franchise_id ON import_batches(franchise_id)"))
    if "audit_logs" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_company_id ON audit_logs(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_franchise_id ON audit_logs(franchise_id)"))
    table_names = set(inspect(db.engine).get_table_names())
    if "workflows" in table_names:
        ensure_columns(
            "workflows",
            {
                "company_id": "INTEGER",
            },
        )
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_workflows_slug ON workflows(slug)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflows_active ON workflows(active)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflows_company_id ON workflows(company_id)"))
    if "workflow_stages" in table_names:
        ensure_columns(
            "workflow_stages",
            {
                "required_fields": "TEXT",
                "first_action_hours": "INTEGER",
                "requires_support": "BOOLEAN NOT NULL DEFAULT 0",
                "support_sla_hours": "INTEGER",
            },
        )
        db.session.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_workflow_stages_workflow_slug ON workflow_stages(workflow_id, slug)")
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_stages_workflow_id ON workflow_stages(workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_stages_active ON workflow_stages(active)"))
    if "lead_workflows" in table_names:
        ensure_columns(
            "lead_workflows",
            {
                "support_requested_at": "DATETIME",
                "support_assigned_at": "DATETIME",
                "support_completed_at": "DATETIME",
                "support_returned_at": "DATETIME",
                "support_return_reason": "TEXT",
                "support_return_resolution_requested_at": "DATETIME",
                "support_return_resolution_completed_at": "DATETIME",
                "support_return_resolution_completed_by_id": "INTEGER",
                "support_return_resolution_note": "TEXT",
                "fidelity_started_on": "DATE",
                "support_executor": "VARCHAR(20)",
                "support_executor_decided_at": "DATETIME",
                "support_executor_decided_by_id": "INTEGER",
                "unlock_executor": "VARCHAR(20)",
                "unlock_executor_decided_at": "DATETIME",
                "unlock_executor_decided_by_id": "INTEGER",
            },
        )
        db.session.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_lead_workflows_lead_workflow ON lead_workflows(lead_id, workflow_id)")
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_current_stage_id ON lead_workflows(current_stage_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_status ON lead_workflows(status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_due_at ON lead_workflows(due_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_owner_user_id ON lead_workflows(owner_user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_assigned_user_id ON lead_workflows(assigned_user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_support_return_resolution_completed_by_id ON lead_workflows(support_return_resolution_completed_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_support_executor_decided_by_id ON lead_workflows(support_executor_decided_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflows_unlock_executor_decided_by_id ON lead_workflows(unlock_executor_decided_by_id)"))
        db.session.execute(
            text(
                """
                UPDATE lead_workflows
                SET support_executor = :support
                WHERE support_executor IS NULL
                  AND (
                    assigned_user_id IS NOT NULL
                    OR support_requested_at IS NOT NULL
                    OR support_assigned_at IS NOT NULL
                  )
                """
            ),
            {"support": SUPPORT_EXECUTOR_SUPPORT},
        )
    if "workflow_transitions" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transitions_lead_workflow_id ON workflow_transitions(lead_workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transitions_lead_id ON workflow_transitions(lead_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transitions_workflow_id ON workflow_transitions(workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transitions_created_at ON workflow_transitions(created_at)"))
    if "workflow_checklist_items" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_checklist_items_stage_id ON workflow_checklist_items(stage_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_checklist_items_required ON workflow_checklist_items(required)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_checklist_items_active ON workflow_checklist_items(active)"))
    if "lead_workflow_checklist_checks" in table_names:
        ensure_columns("lead_workflow_checklist_checks", {
            "configuration_revision_id": "INTEGER REFERENCES task_configuration_revisions(id)",
            "configuration_snapshot": "TEXT",
        })
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_lead_workflow_checklist_checks_unique ON lead_workflow_checklist_checks(lead_workflow_id, checklist_item_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_checklist_checks_lead_workflow_id ON lead_workflow_checklist_checks(lead_workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_checklist_checks_checklist_item_id ON lead_workflow_checklist_checks(checklist_item_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_checklist_checks_checked ON lead_workflow_checklist_checks(checked)"))
    if "lead_workflow_scheduled_tasks" in table_names:
        ensure_columns("lead_workflow_scheduled_tasks", {
            "configuration_revision_id": "INTEGER REFERENCES task_configuration_revisions(id)",
            "configuration_snapshot": "TEXT",
        })
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_unique ON lead_workflow_scheduled_tasks(lead_workflow_id, task_key)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_lead_workflow_id ON lead_workflow_scheduled_tasks(lead_workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_lead_id ON lead_workflow_scheduled_tasks(lead_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_workflow_id ON lead_workflow_scheduled_tasks(workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_stage_id ON lead_workflow_scheduled_tasks(stage_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_scheduled_for ON lead_workflow_scheduled_tasks(scheduled_for)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_completed_at ON lead_workflow_scheduled_tasks(completed_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_completed_by_id ON lead_workflow_scheduled_tasks(completed_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_workflow_scheduled_tasks_active ON lead_workflow_scheduled_tasks(active)"))
    if "lead_activities" in table_names:
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_lead_id ON lead_activities(lead_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_user_id ON lead_activities(user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_lead_workflow_id ON lead_activities(lead_workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_workflow_id ON lead_activities(workflow_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_stage_id ON lead_activities(stage_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_source_field ON lead_activities(source_field)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_created_at ON lead_activities(created_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_lead_activities_deleted_at ON lead_activities(deleted_at)"))
    ensure_reversal_schema(table_names)
    normalize_old_roles()
    backfill_bulk_manage_permission()
    backfill_transfer_permission()
    backfill_workflow_move_permission()
    backfill_workflow_backward_move_permission()
    backfill_admin_configuration_permissions()
    backfill_user_access_metadata()
    backfill_company_scope()
    backfill_customers_from_leads()
    migrate_lead_cpf_unique_scope()
    migrate_team_unique_scope()
    backfill_granular_permission_defaults(added_user_columns, added_role_profile_columns)
    backfill_global_franchise_admin_permission()
    seed_default_reversal_configs()
    db.session.commit()


def ensure_customers_table():
    db.session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER NOT NULL PRIMARY KEY,
                company_id INTEGER NOT NULL,
                cpf VARCHAR(20) NOT NULL,
                nome VARCHAR(180),
                contato VARCHAR(255),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies (id)
            )
            """
        )
    )
    ensure_columns(
        "customers",
        {
            "company_id": "INTEGER",
            "cpf": "VARCHAR(20)",
            "nome": "VARCHAR(180)",
            "contato": "VARCHAR(255)",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_customers_company_id ON customers(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_customers_cpf ON customers(cpf)"))
    db.session.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_company_cpf ON customers(company_id, cpf)")
    )


def ensure_reversal_schema(table_names=None):
    table_names = set(table_names or inspect(db.engine).get_table_names())

    if "reversal_settings" in table_names:
        ensure_columns(
            "reversal_settings",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
                "enabled": "BOOLEAN NOT NULL DEFAULT 1",
                "default_distribution_mode": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                "min_note_chars": "INTEGER NOT NULL DEFAULT 40",
                "sla_30_days_hours": "INTEGER NOT NULL DEFAULT 48",
                "sla_20_days_hours": "INTEGER NOT NULL DEFAULT 24",
                "sla_10_days_hours": "INTEGER NOT NULL DEFAULT 10",
                "standard_days_threshold": "INTEGER NOT NULL DEFAULT 30",
                "attention_days_threshold": "INTEGER NOT NULL DEFAULT 20",
                "urgent_days_threshold": "INTEGER NOT NULL DEFAULT 10",
                "show_reversor_to_owner": "BOOLEAN NOT NULL DEFAULT 1",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_settings_company_id ON reversal_settings(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_settings_franchise_id ON reversal_settings(franchise_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_settings_enabled ON reversal_settings(enabled)"))
        db.session.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_reversal_settings_scope
                ON reversal_settings(company_id, franchise_id)
                """
            )
        )

    if "reversal_team_configs" in table_names:
        ensure_columns(
            "reversal_team_configs",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
                "team_id": "INTEGER",
                "team_name": "VARCHAR(120)",
                "enabled": "BOOLEAN NOT NULL DEFAULT 1",
                "distribution_mode": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_team_configs_company_id ON reversal_team_configs(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_team_configs_franchise_id ON reversal_team_configs(franchise_id)"))
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_reversal_team_configs_team_id ON reversal_team_configs(team_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_team_configs_team_name ON reversal_team_configs(team_name)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_team_configs_enabled ON reversal_team_configs(enabled)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_team_configs_distribution_mode ON reversal_team_configs(distribution_mode)"))
        db.session.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_reversal_team_config_scope_team
                ON reversal_team_configs(company_id, franchise_id, team_name)
                """
            )
        )

    if "reversal_assignment_batches" in table_names:
        ensure_columns(
            "reversal_assignment_batches",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
                "team_id": "INTEGER",
                "team_name": "VARCHAR(120)",
                "mode": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                "status": "VARCHAR(30) NOT NULL DEFAULT 'draft'",
                "requested_by_id": "INTEGER",
                "assigned_by_id": "INTEGER",
                "total_tasks": "INTEGER NOT NULL DEFAULT 0",
                "total_assigned": "INTEGER NOT NULL DEFAULT 0",
                "details": "TEXT",
                "started_at": "DATETIME",
                "finished_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_company_id ON reversal_assignment_batches(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_franchise_id ON reversal_assignment_batches(franchise_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_team_id ON reversal_assignment_batches(team_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_team_name ON reversal_assignment_batches(team_name)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_mode ON reversal_assignment_batches(mode)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_status ON reversal_assignment_batches(status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_requested_by_id ON reversal_assignment_batches(requested_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_assigned_by_id ON reversal_assignment_batches(assigned_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_started_at ON reversal_assignment_batches(started_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_finished_at ON reversal_assignment_batches(finished_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_assignment_batches_created_at ON reversal_assignment_batches(created_at)"))

    if "reversal_tasks" in table_names:
        ensure_columns(
            "reversal_tasks",
            {
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
                "lead_id": "INTEGER",
                "team_id": "INTEGER",
                "team_name": "VARCHAR(120)",
                "owner_user_id": "INTEGER",
                "owner_name": "VARCHAR(120)",
                "requested_by_id": "INTEGER",
                "assignment_batch_id": "INTEGER",
                "assigned_user_id": "INTEGER",
                "assigned_by_id": "INTEGER",
                "status": "VARCHAR(30) NOT NULL DEFAULT 'queued'",
                "priority_level": "VARCHAR(20) NOT NULL DEFAULT 'standard'",
                "distribution_mode": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                "tabulation": "VARCHAR(80)",
                "days_to_unlock": "INTEGER",
                "sla_hours": "INTEGER",
                "due_at": "DATETIME",
                "final_result": "VARCHAR(60)",
                "final_note": "TEXT",
                "owner_notified_at": "DATETIME",
                "supervisor_notified_at": "DATETIME",
                "queued_at": "DATETIME",
                "assigned_at": "DATETIME",
                "first_attempt_at": "DATETIME",
                "last_attempt_at": "DATETIME",
                "completed_at": "DATETIME",
                "canceled_at": "DATETIME",
                "canceled_by_id": "INTEGER",
                "cancel_reason": "TEXT",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_company_id ON reversal_tasks(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_franchise_id ON reversal_tasks(franchise_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_lead_id ON reversal_tasks(lead_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_team_id ON reversal_tasks(team_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_team_name ON reversal_tasks(team_name)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_owner_user_id ON reversal_tasks(owner_user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_owner_name ON reversal_tasks(owner_name)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_requested_by_id ON reversal_tasks(requested_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_assignment_batch_id ON reversal_tasks(assignment_batch_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_assigned_user_id ON reversal_tasks(assigned_user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_assigned_by_id ON reversal_tasks(assigned_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_status ON reversal_tasks(status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_priority_level ON reversal_tasks(priority_level)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_distribution_mode ON reversal_tasks(distribution_mode)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_tabulation ON reversal_tasks(tabulation)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_days_to_unlock ON reversal_tasks(days_to_unlock)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_due_at ON reversal_tasks(due_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_final_result ON reversal_tasks(final_result)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_owner_notified_at ON reversal_tasks(owner_notified_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_supervisor_notified_at ON reversal_tasks(supervisor_notified_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_queued_at ON reversal_tasks(queued_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_assigned_at ON reversal_tasks(assigned_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_first_attempt_at ON reversal_tasks(first_attempt_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_last_attempt_at ON reversal_tasks(last_attempt_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_completed_at ON reversal_tasks(completed_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_canceled_at ON reversal_tasks(canceled_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_canceled_by_id ON reversal_tasks(canceled_by_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_created_at ON reversal_tasks(created_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_scope_status ON reversal_tasks(company_id, franchise_id, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_team_status ON reversal_tasks(company_id, franchise_id, team_name, status)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_tasks_assigned_status ON reversal_tasks(assigned_user_id, status)"))

    if "reversal_attempts" in table_names:
        ensure_columns(
            "reversal_attempts",
            {
                "reversal_task_id": "INTEGER",
                "company_id": "INTEGER",
                "franchise_id": "INTEGER",
                "lead_id": "INTEGER",
                "user_id": "INTEGER",
                "result": "VARCHAR(60)",
                "note": "TEXT",
                "note_char_count": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        )
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_reversal_task_id ON reversal_attempts(reversal_task_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_company_id ON reversal_attempts(company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_franchise_id ON reversal_attempts(franchise_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_lead_id ON reversal_attempts(lead_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_user_id ON reversal_attempts(user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_result ON reversal_attempts(result)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_created_at ON reversal_attempts(created_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_task_created ON reversal_attempts(reversal_task_id, created_at)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_attempts_scope_result ON reversal_attempts(company_id, franchise_id, result)"))

    db.session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS reversal_notifications (
                id INTEGER NOT NULL PRIMARY KEY,
                company_id INTEGER,
                franchise_id INTEGER,
                lead_id INTEGER NOT NULL,
                reversal_task_id INTEGER NOT NULL,
                recipient_user_id INTEGER NOT NULL,
                notification_type VARCHAR(60) NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                title VARCHAR(160) NOT NULL,
                message TEXT NOT NULL,
                event_key VARCHAR(220) NOT NULL,
                read_at DATETIME,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies (id),
                FOREIGN KEY(franchise_id) REFERENCES franchises (id),
                FOREIGN KEY(lead_id) REFERENCES leads (id),
                FOREIGN KEY(reversal_task_id) REFERENCES reversal_tasks (id),
                FOREIGN KEY(recipient_user_id) REFERENCES users (id)
            )
            """
        )
    )
    ensure_columns(
        "reversal_notifications",
        {
            "company_id": "INTEGER",
            "franchise_id": "INTEGER",
            "lead_id": "INTEGER",
            "reversal_task_id": "INTEGER",
            "recipient_user_id": "INTEGER",
            "notification_type": "VARCHAR(60)",
            "severity": "VARCHAR(20) NOT NULL DEFAULT 'info'",
            "title": "VARCHAR(160)",
            "message": "TEXT",
            "event_key": "VARCHAR(220)",
            "read_at": "DATETIME",
            "created_at": "DATETIME",
        },
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_company_id ON reversal_notifications(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_franchise_id ON reversal_notifications(franchise_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_lead_id ON reversal_notifications(lead_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_reversal_task_id ON reversal_notifications(reversal_task_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_recipient_user_id ON reversal_notifications(recipient_user_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_notification_type ON reversal_notifications(notification_type)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_severity ON reversal_notifications(severity)"))
    db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_reversal_notifications_event_key ON reversal_notifications(event_key)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_read_at ON reversal_notifications(read_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_created_at ON reversal_notifications(created_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_recipient_read ON reversal_notifications(recipient_user_id, read_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reversal_notifications_scope_created ON reversal_notifications(company_id, franchise_id, created_at)"))

    db.session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS support_notifications (
                id INTEGER NOT NULL PRIMARY KEY,
                company_id INTEGER,
                franchise_id INTEGER,
                lead_id INTEGER NOT NULL,
                lead_workflow_id INTEGER NOT NULL,
                recipient_user_id INTEGER NOT NULL,
                notification_type VARCHAR(60) NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                title VARCHAR(160) NOT NULL,
                message TEXT NOT NULL,
                event_key VARCHAR(220) NOT NULL,
                read_at DATETIME,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies (id),
                FOREIGN KEY(franchise_id) REFERENCES franchises (id),
                FOREIGN KEY(lead_id) REFERENCES leads (id),
                FOREIGN KEY(lead_workflow_id) REFERENCES lead_workflows (id),
                FOREIGN KEY(recipient_user_id) REFERENCES users (id)
            )
            """
        )
    )
    ensure_columns(
        "support_notifications",
        {
            "company_id": "INTEGER",
            "franchise_id": "INTEGER",
            "lead_id": "INTEGER",
            "lead_workflow_id": "INTEGER",
            "recipient_user_id": "INTEGER",
            "notification_type": "VARCHAR(60)",
            "severity": "VARCHAR(20) NOT NULL DEFAULT 'info'",
            "title": "VARCHAR(160)",
            "message": "TEXT",
            "event_key": "VARCHAR(220)",
            "read_at": "DATETIME",
            "created_at": "DATETIME",
        },
    )
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_company_id ON support_notifications(company_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_franchise_id ON support_notifications(franchise_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_lead_id ON support_notifications(lead_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_lead_workflow_id ON support_notifications(lead_workflow_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_recipient_user_id ON support_notifications(recipient_user_id)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_notification_type ON support_notifications(notification_type)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_severity ON support_notifications(severity)"))
    db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_support_notifications_event_key ON support_notifications(event_key)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_read_at ON support_notifications(read_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_created_at ON support_notifications(created_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_recipient_read ON support_notifications(recipient_user_id, read_at)"))
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_support_notifications_scope_created ON support_notifications(company_id, franchise_id, created_at)"))


def ensure_columns(table_name, definitions):
    columns = get_columns(table_name)
    added = set()
    for column, definition in definitions.items():
        if column not in columns:
            db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"))
            added.add(column)
    return added


def sqlite_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def get_columns(table_name):
    rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).mappings()
    return {row["name"] for row in rows}


def lead_contract_duplicate_rows(limit=10):
    return db.session.execute(
        text(
            """
            SELECT company_id, franchise_id, cpf, COUNT(*) AS total
            FROM leads
            WHERE cpf IS NOT NULL
              AND cpf != ''
            GROUP BY company_id, franchise_id, cpf
            HAVING COUNT(*) > 1
            ORDER BY total DESC, cpf ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()


def migrate_lead_cpf_unique_scope():
    if "leads" not in inspect(db.engine).get_table_names():
        return
    required_columns = {"company_id", "franchise_id", "cpf"}
    if not required_columns.issubset(get_columns("leads")):
        return

    duplicates = lead_contract_duplicate_rows()
    if duplicates:
        preview = "; ".join(
            f"empresa={row['company_id']} franquia={row['franchise_id']} cpf={row['cpf']} total={row['total']}"
            for row in duplicates[:5]
        )
        raise RuntimeError(
            "Nao foi possivel criar a unicidade por franquia: existem CPFs duplicados dentro da mesma franquia. "
            f"Exemplos: {preview}"
        )

    indexes = list(db.session.execute(text("PRAGMA index_list('leads')")).mappings())
    for index in indexes:
        index_name = index["name"]
        if not index["unique"]:
            continue
        column_rows = list(
            db.session.execute(text(f"PRAGMA index_info({sqlite_identifier(index_name)})")).mappings()
        )
        columns = [row["name"] for row in column_rows]
        if columns == ["cpf"]:
            if str(index_name).startswith("sqlite_autoindex"):
                raise RuntimeError(
                    "O banco tem uma constraint unica global de CPF embutida na tabela leads. "
                    "Sera necessario reconstruir a tabela antes de liberar CPFs por franquia."
                )
            db.session.execute(text(f"DROP INDEX IF EXISTS {sqlite_identifier(index_name)}"))

    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_leads_cpf ON leads(cpf)"))
    db.session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_company_franchise_cpf
            ON leads(company_id, franchise_id, cpf)
            """
        )
    )


def team_duplicate_rows(limit=10):
    return db.session.execute(
        text(
            """
            SELECT company_id, franchise_id, name, COUNT(*) AS total
            FROM teams
            WHERE name IS NOT NULL
              AND name != ''
            GROUP BY company_id, franchise_id, name
            HAVING COUNT(*) > 1
            ORDER BY total DESC, name ASC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()


def migrate_team_unique_scope():
    if "teams" not in inspect(db.engine).get_table_names():
        return
    required_columns = {"company_id", "franchise_id", "name"}
    if not required_columns.issubset(get_columns("teams")):
        return

    duplicates = team_duplicate_rows()
    if duplicates:
        preview = "; ".join(
            f"empresa={row['company_id']} franquia={row['franchise_id']} equipe={row['name']} total={row['total']}"
            for row in duplicates[:5]
        )
        raise RuntimeError(
            "Nao foi possivel criar a unicidade de equipes por franquia: existem equipes duplicadas "
            f"dentro da mesma franquia. Exemplos: {preview}"
        )

    indexes = list(db.session.execute(text("PRAGMA index_list('teams')")).mappings())
    for index in indexes:
        index_name = index["name"]
        if not index["unique"]:
            continue
        column_rows = list(
            db.session.execute(text(f"PRAGMA index_info({sqlite_identifier(index_name)})")).mappings()
        )
        columns = [row["name"] for row in column_rows]
        if columns == ["name"]:
            if str(index_name).startswith("sqlite_autoindex"):
                raise RuntimeError(
                    "O banco tem uma constraint unica global de equipe embutida na tabela teams. "
                    "Sera necessario reconstruir a tabela antes de liberar nomes de equipe por franquia."
                )
            db.session.execute(text(f"DROP INDEX IF EXISTS {sqlite_identifier(index_name)}"))

    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_name ON teams(name)"))
    db.session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_teams_company_franchise_name
            ON teams(company_id, franchise_id, name)
            """
        )
    )


def normalize_old_roles():
    db.session.execute(
        text("UPDATE users SET role = :new_role WHERE role = :old_role"),
        {"new_role": Role.OPERADOR, "old_role": "CONSULTOR"},
    )
    for user in User.query.all():
        if not any(getattr(user, field, False) for field in PERMISSION_FIELDS):
            user.apply_role_defaults()


def backfill_bulk_manage_permission():
    if "can_bulk_manage_leads" in get_columns("users"):
        db.session.execute(
            text("UPDATE users SET can_bulk_manage_leads = 1 WHERE role = :role"),
            {"role": Role.ADMIN},
        )
    if "role_profiles" in inspect(db.engine).get_table_names() and "can_bulk_manage_leads" in get_columns("role_profiles"):
        db.session.execute(
            text("UPDATE role_profiles SET can_bulk_manage_leads = 1 WHERE base_role = :role"),
            {"role": Role.ADMIN},
        )


def backfill_transfer_permission():
    if "can_transfer_leads" in get_columns("users"):
        db.session.execute(
            text("UPDATE users SET can_transfer_leads = 1 WHERE role = :admin OR role = :supervisor"),
            {"admin": Role.ADMIN, "supervisor": Role.SUPERVISOR},
        )
    if "role_profiles" in inspect(db.engine).get_table_names() and "can_transfer_leads" in get_columns("role_profiles"):
        db.session.execute(
            text("UPDATE role_profiles SET can_transfer_leads = 1 WHERE base_role = :admin OR base_role = :supervisor"),
            {"admin": Role.ADMIN, "supervisor": Role.SUPERVISOR},
        )


def backfill_workflow_move_permission():
    if "can_move_workflows" in get_columns("users"):
        db.session.execute(
            text("UPDATE users SET can_move_workflows = 1 WHERE can_move_workflows IS NULL")
        )
    if "role_profiles" in inspect(db.engine).get_table_names() and "can_move_workflows" in get_columns("role_profiles"):
        db.session.execute(
            text("UPDATE role_profiles SET can_move_workflows = 1 WHERE can_move_workflows IS NULL")
        )


def backfill_workflow_backward_move_permission():
    if "can_move_workflows_backwards" in get_columns("users"):
        db.session.execute(
            text(
                """
                UPDATE users
                SET can_move_workflows_backwards = 1
                WHERE role = :admin OR can_manage_users = 1
                """
            ),
            {"admin": Role.ADMIN},
        )
        db.session.execute(
            text(
                """
                UPDATE users
                SET can_move_workflows_backwards = 0
                WHERE can_move_workflows_backwards IS NULL
                """
            )
        )
    if (
        "role_profiles" in inspect(db.engine).get_table_names()
        and "can_move_workflows_backwards" in get_columns("role_profiles")
    ):
        db.session.execute(
            text(
                """
                UPDATE role_profiles
                SET can_move_workflows_backwards = 1
                WHERE base_role = :admin OR can_manage_users = 1
                """
            ),
            {"admin": Role.ADMIN},
        )
        db.session.execute(
            text(
                """
                UPDATE role_profiles
                SET can_move_workflows_backwards = 0
                WHERE can_move_workflows_backwards IS NULL
                """
            )
        )


def backfill_admin_configuration_permissions():
    user_columns = get_columns("users")
    admin_fields = [
        field
        for field in (
            "can_view_audit",
            "can_manage_settings",
            "can_manage_workflows",
            "can_manage_roles",
            "can_manage_teams",
        )
        if field in user_columns
    ]
    if admin_fields:
        assignments = ", ".join(f"{field} = 1" for field in admin_fields)
        db.session.execute(
            text(
                f"""
                UPDATE users
                SET {assignments}
                WHERE role = :admin
                   OR (can_manage_users = 1 AND can_view_all = 1)
                """
            ),
            {"admin": Role.ADMIN},
        )

    if "role_profiles" not in inspect(db.engine).get_table_names():
        return

    profile_columns = get_columns("role_profiles")
    profile_fields = [
        field
        for field in (
            "can_view_audit",
            "can_manage_settings",
            "can_manage_workflows",
            "can_manage_roles",
            "can_manage_teams",
        )
        if field in profile_columns
    ]
    if profile_fields:
        assignments = ", ".join(f"{field} = 1" for field in profile_fields)
        db.session.execute(
            text(
                f"""
                UPDATE role_profiles
                SET {assignments}
                WHERE base_role = :admin
                   OR (can_manage_users = 1 AND can_view_all = 1)
                """
            ),
            {"admin": Role.ADMIN},
        )


def backfill_user_access_metadata():
    user_columns = get_columns("users")
    if not {"password_changed_at", "password_hash"}.issubset(user_columns):
        return

    db.session.execute(
        text(
            """
            UPDATE users
            SET password_changed_at = COALESCE(updated_at, created_at)
            WHERE password_changed_at IS NULL
              AND password_hash IS NOT NULL
              AND password_hash != ''
            """
        )
    )


def backfill_company_scope():
    table_names = set(inspect(db.engine).get_table_names())
    if "companies" not in table_names or "franchises" not in table_names:
        return

    company, franchise = ensure_default_company_structure()
    db.session.flush()
    company_id = company.id
    franchise_id = franchise.id

    scoped_tables = {
        "users": ("company_id", "franchise_id"),
        "leads": ("company_id", "franchise_id"),
        "teams": ("company_id", "franchise_id"),
        "role_profiles": ("company_id", None),
        "field_options": ("company_id", None),
        "workflows": ("company_id", None),
        "google_access_requests": ("company_id", "franchise_id"),
        "import_batches": ("company_id", "franchise_id"),
        "audit_logs": ("company_id", "franchise_id"),
        "reversal_settings": ("company_id", None),
        "reversal_team_configs": ("company_id", "franchise_id"),
        "reversal_assignment_batches": ("company_id", "franchise_id"),
        "reversal_tasks": ("company_id", "franchise_id"),
        "reversal_attempts": ("company_id", "franchise_id"),
        "reversal_notifications": ("company_id", "franchise_id"),
        "support_notifications": ("company_id", "franchise_id"),
    }
    for table_name, (company_field, franchise_field) in scoped_tables.items():
        if table_name not in table_names:
            continue
        columns = get_columns(table_name)
        assignments = []
        params = {}
        if company_field and company_field in columns:
            assignments.append(f"{company_field} = COALESCE({company_field}, :company_id)")
            params["company_id"] = company_id
        if franchise_field and franchise_field in columns:
            assignments.append(f"{franchise_field} = COALESCE({franchise_field}, :franchise_id)")
            params["franchise_id"] = franchise_id
        if assignments:
            db.session.execute(
                text(f"UPDATE {table_name} SET {', '.join(assignments)}"),
                params,
            )


def backfill_customers_from_leads():
    table_names = set(inspect(db.engine).get_table_names())
    if "customers" not in table_names or "leads" not in table_names:
        return
    if "customer_id" not in get_columns("leads"):
        return

    now = datetime.utcnow()
    lead_rows = db.session.execute(
        text(
            """
            SELECT company_id, cpf, MIN(nome_cliente) AS nome_cliente, MIN(contato) AS contato
            FROM leads
            WHERE cpf IS NOT NULL
              AND cpf != ''
              AND company_id IS NOT NULL
            GROUP BY company_id, cpf
            """
        )
    ).mappings()

    for row in lead_rows:
        customer = Customer.query.filter(
            Customer.company_id == row["company_id"],
            Customer.cpf == row["cpf"],
        ).first()
        if not customer:
            customer = Customer(
                company_id=row["company_id"],
                cpf=row["cpf"],
                nome=clean_text(row["nome_cliente"]),
                contato=clean_text(row["contato"]),
                created_at=now,
                updated_at=now,
            )
            db.session.add(customer)
        else:
            customer.nome = customer.nome or clean_text(row["nome_cliente"])
            customer.contato = customer.contato or clean_text(row["contato"])

    db.session.flush()
    db.session.execute(
        text(
            """
            UPDATE leads
            SET customer_id = (
                SELECT customers.id
                FROM customers
                WHERE customers.company_id = leads.company_id
                  AND customers.cpf = leads.cpf
                LIMIT 1
            )
            WHERE customer_id IS NULL
              AND cpf IS NOT NULL
              AND cpf != ''
              AND company_id IS NOT NULL
            """
        )
    )


def backfill_global_franchise_admin_permission():
    if "can_view_all_franchises" not in get_columns("users"):
        return

    admin_emails = configured_admin_emails()
    if admin_emails:
        db.session.execute(
            text(
                """
                UPDATE users
                SET can_view_all_franchises = 1
                WHERE email IS NOT NULL
                  AND lower(email) IN :admin_emails
                """
            ).bindparams(bindparam("admin_emails", expanding=True)),
            {"admin_emails": tuple(admin_emails)},
        )

    has_global_admin = User.query.filter(
        User.active.is_(True),
        User.role == Role.ADMIN,
        User.can_manage_users.is_(True),
        User.can_view_all.is_(True),
        User.can_view_all_franchises.is_(True),
    ).first()
    if has_global_admin:
        return

    fallback_admin = (
        User.query.filter(
            User.active.is_(True),
            User.role == Role.ADMIN,
            User.can_manage_users.is_(True),
            User.can_view_all.is_(True),
        )
        .order_by(User.id.asc())
        .first()
    )
    if fallback_admin:
        fallback_admin.can_view_all_franchises = True


def backfill_granular_permission_defaults(added_user_columns=None, added_role_profile_columns=None):
    added_user_columns = set(added_user_columns or ()) & set(PERMISSION_FIELDS)
    added_role_profile_columns = set(added_role_profile_columns or ()) & set(PERMISSION_FIELDS)

    if added_user_columns:
        for user in User.query.all():
            if getattr(user, "can_act_as_support", False):
                permissions = support_commercial_permissions()
            else:
                permissions = default_permissions_for_role(user.role)
            for field in added_user_columns:
                setattr(user, field, bool(permissions.get(field, False)))

    if (
        added_role_profile_columns
        and "role_profiles" in inspect(db.engine).get_table_names()
    ):
        for profile in RoleProfile.query.all():
            if profile.name.lower() == "apoio comercial" or getattr(profile, "can_act_as_support", False):
                permissions = support_commercial_permissions()
            elif profile.name.lower() == "gestor":
                permissions = manager_permissions()
            else:
                permissions = default_permissions_for_role(profile.base_role)
            for field in added_role_profile_columns:
                setattr(profile, field, bool(permissions.get(field, False)))


def seed_default_options():
    company_id = default_company().id
    for field_name, values in DEFAULT_OPTIONS.items():
        existing_sort_orders = [
            row[0]
            for row in db.session.query(FieldOption.sort_order)
            .filter_by(field_name=field_name)
            .all()
            if row[0] is not None
        ]
        next_sort_order = (max(existing_sort_orders) + 1) if existing_sort_orders else 1
        for sort_order, value in enumerate(values, start=1):
            option = FieldOption.query.filter_by(field_name=field_name, value=value).first()
            if not option:
                new_sort_order = sort_order if not existing_sort_orders else next_sort_order
                if existing_sort_orders and field_name == "status" and normalize_option_key(value) == "novo":
                    new_sort_order = min(existing_sort_orders) - 1
                db.session.add(
                    FieldOption(
                        company_id=company_id,
                        field_name=field_name,
                        value=value,
                        color_hex=default_option_color(field_name, value),
                        sort_order=new_sort_order,
                    )
                )
                if new_sort_order == next_sort_order:
                    next_sort_order += 1
            else:
                option.company_id = option.company_id or company_id
                option.color_hex = option.color_hex or default_option_color(field_name, value)
                option.active = True
    normalize_status_option_aliases()
    for option in FieldOption.query.all():
        option.company_id = option.company_id or company_id
        option.color_hex = option.color_hex or default_option_color(option.field_name, option.value)
    db.session.commit()


def retire_legacy_reversal_position():
    changed = False
    for option in FieldOption.query.filter(FieldOption.field_name == "posicao").all():
        if normalize_option_key(option.value) == "reversao":
            db.session.delete(option)
            changed = True

    for lead in Lead.query.filter(Lead.posicao.isnot(None)).all():
        if normalize_option_key(lead.posicao) != "reversao":
            continue
        old_value = serialize_audit_value(lead.posicao)
        lead.posicao = None
        lead.posicao_manual = False
        changed = True
        log_audit(
            lead,
            None,
            "update",
            field_name="posicao",
            old_value=old_value,
            new_value=None,
            details={"source": "retire_legacy_reversal_position", "automatic": True},
        )

    if changed:
        db.session.commit()


def normalize_status_option_aliases():
    status_options = FieldOption.query.filter_by(field_name="status").all()
    em_andamento = next((option for option in status_options if normalize_option_key(option.value) == "em andamento"), None)
    andamento = next((option for option in status_options if normalize_option_key(option.value) == "andamento"), None)

    if andamento and not em_andamento:
        andamento.value = "Em andamento"
        andamento.color_hex = andamento.color_hex or default_option_color("status", "Em andamento")
        andamento.active = True
        return

    if not andamento or not em_andamento or andamento.id == em_andamento.id:
        return

    Lead.query.filter(Lead.status == andamento.value).update(
        {"status": em_andamento.value},
        synchronize_session=False,
    )
    andamento.active = False
    andamento.sort_order = max((option.sort_order or 0 for option in status_options), default=0) + 1


def default_option_color(field_name, value):
    key = normalize_option_key(value)
    return DEFAULT_OPTION_COLORS.get(field_name, {}).get(key, "#64748b")


def normalize_option_key(value):
    if not value:
        return ""
    without_accents = unicodedata.normalize("NFKD", str(value))
    ascii_text = "".join(char for char in without_accents if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_text.strip().lower())


def seed_default_workflows():
    company_id = default_company().id
    seed_key = f"workflow_defaults_v1:{company_id}"
    already_seeded = db.session.get(ConfigurationMigration, seed_key) is not None
    for workflow_data in (() if already_seeded else DEFAULT_WORKFLOWS):
        workflow = Workflow.query.filter_by(slug=workflow_data["slug"]).first()
        if not workflow:
            workflow = Workflow(
                company_id=company_id,
                slug=workflow_data["slug"],
                name=workflow_data["name"],
                description=workflow_data.get("description"),
                sort_order=workflow_data.get("sort_order", 0),
                active=True,
                auto_move_enabled=bool(workflow_data.get("auto_move_enabled", False)),
            )
            db.session.add(workflow)
            db.session.flush()
        else:
            workflow.company_id = workflow.company_id or company_id

        for stage_data in workflow_data["stages"]:
            stage = WorkflowStage.query.filter_by(
                workflow_id=workflow.id,
                slug=stage_data["slug"],
            ).first()
            if not stage:
                stage = WorkflowStage(
                    workflow_id=workflow.id,
                    slug=stage_data["slug"],
                    name=stage_data["name"],
                    description=stage_data.get("description"),
                    sort_order=stage_data.get("sort_order", 0),
                    active=True,
                    max_days=stage_data.get("max_days"),
                    alert_days_before=stage_data.get("alert_days_before"),
                    first_action_hours=stage_data.get("first_action_hours"),
                    requires_support=bool(stage_data.get("requires_support", False)),
                    support_sla_hours=stage_data.get("support_sla_hours"),
                    requires_note=bool(stage_data.get("requires_note", False)),
                    requires_checklist=bool(stage_data.get("requires_checklist", False)),
                    required_fields=serialize_required_fields(stage_data.get("required_fields")),
                    allow_auto_move=bool(stage_data.get("allow_auto_move", False)),
                )
                db.session.add(stage)
                db.session.flush()
                seed_stage_checklist_items(stage, stage_data.get("checklist", []))

    configure_unified_fidelity()
    if not already_seeded:
        db.session.add(ConfigurationMigration(key=seed_key))
    record_current_task_configuration()
    preserve_existing_tasks()
    db.session.commit()
    ensure_all_default_lead_workflows()
    migrate_unified_fidelity()
    ensure_initial_workflow_transitions()
    ensure_legacy_lead_activities()
    backfill_in_progress_status_for_activity_leads()
    backfill_new_status_for_untouched_leads()
    sync_lead_workflow_activity_timestamps()
    sync_sale_entry_workflows()
    ensure_unlock_tracking_workflows()
    ensure_temporary_workflows_for_leads()
    sync_lead_workflow_due_dates()
    sync_unlock_workflow_stages()
    ensure_support_assignments()
    for lead_workflow in LeadWorkflow.query.filter(LeadWorkflow.status.notin_(TERMINAL_WORKFLOW_STATUSES)).all():
        if lead_workflow.workflow.slug != UNLOCK_WORKFLOW_SLUG:
            checklist_items_for_workflow(lead_workflow)
    preserve_existing_tasks()
    db.session.commit()


def task_admin_panel_state(actor):
    state = panel_state(actor, SELLER_EXECUTION_TASKS)
    revision = db.session.get(TaskConfigurationRevision, state["revision_id"])
    base = db.session.get(TaskConfigurationRevision, revision.base_revision_id) if state["is_draft"] else revision
    state["can_publish"] = can_publish(actor)
    state["publication_errors"] = publication_errors(json.loads(revision.content), json.loads(base.content), SELLER_EXECUTION_TASKS) if base and state["is_draft"] else []
    return state


def record_current_task_configuration(actor=None):
    company_id = actor.company_id if actor else default_company().id
    scope = company_scope(company_id)
    matrix_id = scope.matrix_franchise_id if scope else default_franchise().id
    inventory = configuration_inventory(company_id, UNLOCK_CONTACT_TASK_RULES, FIDELITY_INSS_TASKS, UNLOCK_TOTAL_DAYS)
    return record_company_configuration(company_id, matrix_id, inventory, actor=actor)


def configure_unified_fidelity():
    key = "unified_fidelity_configuration_v1"
    if db.session.get(ConfigurationMigration, key):
        return
    legacy = Workflow.query.filter_by(slug="fidelizacao").first()
    # Adopt already-migrated installations without overwriting their admin settings.
    workflow = Workflow.query.filter_by(slug=UNLOCK_WORKFLOW_SLUG).first()
    _, retired = workflow_stage_by_slug(DEFAULT_LEAD_WORKFLOW_SLUG, "30-dias-desbloqueio")
    already_unified = bool(workflow and legacy and not legacy.active and retired and not retired.active
                           and WorkflowStage.query.filter_by(workflow_id=workflow.id, slug="90-dias").first())
    db.session.add(ConfigurationMigration(key=key))
    if already_unified:
        return
    if legacy:
        legacy.active = False
    workflow = Workflow.query.filter_by(slug=UNLOCK_WORKFLOW_SLUG).first()
    if workflow:
        workflow.name = "Fidelizacao"
        workflow.description = "Tarefas e relacionamento ate o desbloqueio."
        workflow.sort_order = 2
        workflow.active = True
        names = {
            "90-dias": "Preparacao inicial · 90 a 61 dias",
            "60-dias": "Relacionamento · 60 a 31 dias",
            "30-dias": "Acompanhamento · 30 a 21 dias",
            "20-dias": "Acompanhamento · 20 a 11 dias",
            "10-dias": "Preparacao final · 10 a 1 dias",
        }
        for stage in WorkflowStage.query.filter_by(workflow_id=workflow.id).all():
            if stage.slug in names:
                stage.name = names[stage.slug]
                stage.max_days = None
                stage.first_action_hours = None
                stage.requires_note = False
                stage.requires_checklist = False
                stage.allow_auto_move = False
    _, retired = workflow_stage_by_slug(DEFAULT_LEAD_WORKFLOW_SLUG, "30-dias-desbloqueio")
    if retired:
        retired.active = False


def migrate_unified_fidelity():
    # Preserve original workflow records and history; only the operational pointer changes.
    _, retired = workflow_stage_by_slug(DEFAULT_LEAD_WORKFLOW_SLUG, "30-dias-desbloqueio")
    _, target = workflow_stage_by_slug(DEFAULT_LEAD_WORKFLOW_SLUG, "fidelizacao")
    if not retired or not target:
        return
    now = datetime.now()
    for principal in LeadWorkflow.query.filter_by(current_stage_id=retired.id).all():
        db.session.add(WorkflowTransition(
            lead_workflow_id=principal.id, lead_id=principal.lead_id,
            workflow_id=principal.workflow_id, from_stage_id=retired.id,
            to_stage_id=target.id, action="fidelity_migration",
            note="Acompanhamento reunido na Fidelizacao; registros anteriores preservados.",
            details=json.dumps({"source": "unified_fidelity_v1"}), created_at=now,
        ))
        principal.current_stage = target
        principal.current_stage_id = target.id
    db.session.flush()
    for principal in LeadWorkflow.query.filter_by(current_stage_id=target.id).all():
        if principal.status in TERMINAL_WORKFLOW_STATUSES or principal.lead.deleted_at:
            continue
        tracking, _ = ensure_temporary_workflow_for_principal(principal)
        if not tracking:
            continue
        if tracking.status == "concluido" and (unlock_days_left(principal.lead) or 0) > 0:
            tracking.status = workflow_status_for_lead(principal.lead)
            tracking.completed_at = None
        sync_unlock_workflow_stage(tracking)
        ensure_scheduled_contact_tasks_for_workflow(tracking)
        legacy = lead_workflow_by_slug(principal.lead, "fidelizacao")
        if legacy and legacy.status not in TERMINAL_WORKFLOW_STATUSES:
            migrate_legacy_fidelity_pending_tasks(legacy, tracking)
            legacy.status = "inativo"
    db.session.commit()


def migrate_legacy_fidelity_pending_tasks(legacy, tracking):
    checks = checklist_checks_by_item_id(legacy)
    for item in checklist_items_for_stage(legacy.current_stage):
        if checks.get(item.id) and checks[item.id].checked:
            continue
        # The standard INSS sequence is restored by ensure_fidelity_inss_tasks.
        if legacy.current_stage.slug in {row[2] for row in FIDELITY_INSS_TASKS} and item.sort_order == 1:
            continue
        key = f"legacy-checklist:{legacy.id}:{item.id}"
        if LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id, task_key=key).first():
            continue
        due = legacy.due_at or legacy.entered_stage_at or datetime.now()
        add_configured_task(LeadWorkflowScheduledTask(
            lead_workflow_id=tracking.id, lead_id=tracking.lead_id,
            workflow_id=tracking.workflow_id, stage_id=tracking.current_stage_id,
            task_key=key, label=item.label, scheduled_for=due.date(), active=True,
        ))


def workflow_stage_rules_are_unconfigured(stage):
    if stage.requires_note or stage.requires_checklist or clean_text(stage.required_fields):
        return False
    return not WorkflowChecklistItem.query.filter_by(stage_id=stage.id).first()


def serialize_required_fields(fields):
    fields = [field for field in fields or [] if field]
    return json.dumps(fields, ensure_ascii=False) if fields else None


def seed_stage_checklist_items(stage, checklist_labels):
    if not checklist_labels:
        return
    if WorkflowChecklistItem.query.filter_by(stage_id=stage.id).first():
        return

    for sort_order, label in enumerate(checklist_labels, start=1):
        db.session.add(
            WorkflowChecklistItem(
                stage_id=stage.id,
                label=label,
                sort_order=sort_order,
                required=True,
                active=True,
            )
        )


def ensure_all_default_lead_workflows():
    workflow, stage = default_lead_workflow_stage()
    if not workflow or not stage:
        return 0

    existing_lead_ids = {
        row[0]
        for row in db.session.query(LeadWorkflow.lead_id)
        .filter(LeadWorkflow.workflow_id == workflow.id)
        .all()
    }
    created = 0
    for lead in Lead.query.order_by(Lead.id.asc()).all():
        if lead.id in existing_lead_ids:
            continue
        db.session.add(build_default_lead_workflow(lead, workflow, stage))
        created += 1

    if created:
        db.session.commit()
    return created


def ensure_lead_default_workflows(lead):
    workflow, stage = default_lead_workflow_stage()
    if not workflow or not stage:
        return None

    existing = LeadWorkflow.query.filter_by(
        lead_id=lead.id,
        workflow_id=workflow.id,
    ).first()
    if existing:
        return existing

    lead_workflow = build_default_lead_workflow(lead, workflow, stage)
    db.session.add(lead_workflow)
    db.session.flush()
    ensure_initial_workflow_transition(lead_workflow)
    return lead_workflow


def ensure_unlock_tracking_workflows():
    workflow = Workflow.query.filter_by(slug=UNLOCK_WORKFLOW_SLUG).first()
    if not workflow:
        return 0
    entry_stage = WorkflowStage.query.filter_by(
        workflow_id=workflow.id,
        slug=UNLOCK_ENTRY_STAGE_SLUG,
    ).first()
    if not entry_stage:
        return 0

    existing_lead_ids = {
        row[0]
        for row in db.session.query(LeadWorkflow.lead_id)
        .filter(LeadWorkflow.workflow_id == workflow.id)
        .all()
    }
    created = 0
    for lead in Lead.query.filter(Lead.deleted_at.is_(None), Lead.concessao.isnot(None)).order_by(Lead.id.asc()).all():
        if lead.id in existing_lead_ids:
            continue
        if not lead_is_unlock_workflow_eligible(lead):
            continue
        days_left = unlock_days_left(lead)
        if days_left is None or days_left > UNLOCK_TRACKING_DAYS:
            continue

        lead_workflow = LeadWorkflow(
            lead_id=lead.id,
            workflow_id=workflow.id,
            current_stage_id=entry_stage.id,
            status=workflow_status_for_lead(lead),
            entered_stage_at=datetime.now(),
            due_at=workflow_due_at_for_stage(lead, entry_stage),
            owner_user_id=workflow_owner_id_for_lead(lead),
            last_action_at=latest_activity_datetime_for_lead(lead),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.session.add(lead_workflow)
        db.session.flush()
        ensure_initial_workflow_transition(lead_workflow, source="unlock_tracking_seed")
        created += 1

    if created:
        db.session.commit()
    return created


def ensure_unlock_tracking_workflow_for_lead(lead):
    if not lead or lead.deleted_at or not lead.concessao:
        return None
    if not lead_is_unlock_workflow_eligible(lead):
        return None
    days_left = unlock_days_left(lead)
    if days_left is None or days_left > UNLOCK_TRACKING_DAYS:
        return None

    workflow = Workflow.query.filter_by(slug=UNLOCK_WORKFLOW_SLUG).first()
    if not workflow:
        return None
    existing = LeadWorkflow.query.filter_by(lead_id=lead.id, workflow_id=workflow.id).first()
    if existing:
        return existing

    stage = WorkflowStage.query.filter_by(workflow_id=workflow.id, slug=UNLOCK_ENTRY_STAGE_SLUG).first()
    if not stage:
        return None

    lead_workflow = LeadWorkflow(
        lead_id=lead.id,
        workflow_id=workflow.id,
        current_stage_id=stage.id,
        status=workflow_status_for_lead(lead),
        entered_stage_at=datetime.now(),
        due_at=workflow_due_at_for_stage(lead, stage),
        owner_user_id=workflow_owner_id_for_lead(lead),
        last_action_at=latest_activity_datetime_for_lead(lead),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.session.add(lead_workflow)
    db.session.flush()
    ensure_initial_workflow_transition(lead_workflow, source="unlock_tracking_seed")
    return lead_workflow


def workflow_stage_by_slug(workflow_slug, stage_slug):
    workflow = Workflow.query.filter_by(slug=workflow_slug).first()
    if not workflow:
        return None, None
    stage = WorkflowStage.query.filter_by(workflow_id=workflow.id, slug=stage_slug).first()
    return workflow, stage


def lead_workflow_by_slug(lead, workflow_slug):
    if not lead:
        return None
    return (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.owner_user),
            joinedload(LeadWorkflow.assigned_user),
        )
        .join(Workflow, LeadWorkflow.workflow_id == Workflow.id)
        .filter(
            LeadWorkflow.lead_id == lead.id,
            Workflow.slug == workflow_slug,
        )
        .first()
    )


def lead_workflow_by_slug_from_list(lead_workflows, workflow_slug):
    for lead_workflow in lead_workflows or []:
        if lead_workflow.workflow and lead_workflow.workflow.slug == workflow_slug:
            return lead_workflow
    return None


def lead_is_unlock_workflow_eligible(lead, principal_lead_workflow=None, lead_workflows=None):
    if not lead or lead.deleted_at:
        return False

    principal = principal_lead_workflow
    if not principal:
        if lead_workflows is not None:
            principal = lead_workflow_by_slug_from_list(lead_workflows, DEFAULT_LEAD_WORKFLOW_SLUG)
        else:
            principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if (not principal or not principal.current_stage
            or principal.status in TERMINAL_WORKFLOW_STATUSES
            or principal.current_stage.slug not in {"fidelizacao", "30-dias-desbloqueio"}):
        return False
    return True


def ensure_lead_workflow_for_slug(lead, workflow_slug, entry_stage_slug, source="workflow_seed", reactivate=False):
    if not lead or lead.deleted_at:
        return None, False

    workflow, entry_stage = workflow_stage_by_slug(workflow_slug, entry_stage_slug)
    if not workflow or not entry_stage:
        return None, False

    existing = LeadWorkflow.query.filter_by(lead_id=lead.id, workflow_id=workflow.id).first()
    if existing:
        changed = False
        if reactivate and existing.status == "concluido":
            now = datetime.now()
            existing.current_stage_id = entry_stage.id
            existing.status = workflow_status_for_lead(lead)
            existing.entered_stage_at = now
            existing.due_at = workflow_due_at_for_stage(lead, entry_stage)
            existing.owner_user_id = workflow_owner_id_for_lead(lead)
            existing.completed_at = None
            existing.updated_at = now
            existing.current_stage = entry_stage
            if reset_support_state_for_stage(existing, preserve_recorded_work=True):
                assign_support_to_workflow(existing)
            db.session.add(
                WorkflowTransition(
                    lead_workflow_id=existing.id,
                    lead_id=existing.lead_id,
                    workflow_id=existing.workflow_id,
                    from_stage_id=None,
                    to_stage_id=entry_stage.id,
                    user_id=None,
                    action="reopen_temporary",
                    note="Fluxo temporario reaberto pelo fluxo principal.",
                    details=json.dumps({"source": source}, ensure_ascii=False),
                    created_at=now,
                )
            )
            changed = True
        return existing, changed

    now = datetime.now()
    lead_workflow = LeadWorkflow(
        lead_id=lead.id,
        workflow_id=workflow.id,
        current_stage_id=entry_stage.id,
        current_stage=entry_stage,
        status=workflow_status_for_lead(lead),
        entered_stage_at=now,
        due_at=workflow_due_at_for_stage(lead, entry_stage),
        owner_user_id=workflow_owner_id_for_lead(lead),
        last_action_at=latest_activity_datetime_for_lead(lead),
        created_at=now,
        updated_at=now,
    )
    db.session.add(lead_workflow)
    db.session.flush()
    ensure_initial_workflow_transition(lead_workflow, source=source)
    assign_support_to_workflow(lead_workflow)
    if workflow_slug == UNLOCK_WORKFLOW_SLUG:
        ensure_scheduled_contact_tasks_for_workflow(lead_workflow)
    return lead_workflow, True


def temporary_workflow_config_for_principal_stage(stage_slug):
    return TEMPORARY_WORKFLOW_BY_PRINCIPAL_STAGE.get(stage_slug or "")


def temporary_workflow_config_for_child_workflow(workflow_slug):
    for principal_stage_slug, config in TEMPORARY_WORKFLOW_BY_PRINCIPAL_STAGE.items():
        if config["workflow_slug"] == workflow_slug:
            return principal_stage_slug, config
    return None, None


def ensure_temporary_workflow_for_principal(principal_lead_workflow, reactivate=False):
    if (
        not principal_lead_workflow
        or not principal_lead_workflow.workflow
        or principal_lead_workflow.workflow.slug != DEFAULT_LEAD_WORKFLOW_SLUG
        or not principal_lead_workflow.current_stage
    ):
        return None, False

    config = temporary_workflow_config_for_principal_stage(principal_lead_workflow.current_stage.slug)
    if not config:
        return None, False
    if config["workflow_slug"] == UNLOCK_WORKFLOW_SLUG and not lead_is_unlock_workflow_eligible(
        principal_lead_workflow.lead,
        principal_lead_workflow=principal_lead_workflow,
    ):
        return None, False

    expected = expected_unlock_stage(unlock_days_left(principal_lead_workflow.lead))
    entry_slug = expected["slug"] if expected and expected["slug"] in UNLOCK_AUTO_STAGE_SLUGS else config["entry_stage_slug"]
    return ensure_lead_workflow_for_slug(
        principal_lead_workflow.lead,
        config["workflow_slug"],
        entry_slug,
        source=f"temporary_{config['workflow_slug']}_seed",
        reactivate=reactivate,
    )


def ensure_temporary_workflows_for_leads():
    principal_workflow = Workflow.query.filter_by(slug=DEFAULT_LEAD_WORKFLOW_SLUG).first()
    if not principal_workflow:
        return 0

    stage_slugs = tuple(TEMPORARY_WORKFLOW_BY_PRINCIPAL_STAGE.keys())
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .join(WorkflowStage, LeadWorkflow.current_stage_id == WorkflowStage.id)
        .filter(
            LeadWorkflow.workflow_id == principal_workflow.id,
            WorkflowStage.slug.in_(stage_slugs),
            LeadWorkflow.status != "concluido",
        )
        .all()
    )

    changed = 0
    for lead_workflow in lead_workflows:
        _, did_change = ensure_temporary_workflow_for_principal(lead_workflow)
        if did_change:
            changed += 1

    if changed:
        db.session.commit()
    return changed


def is_principal_unlock_stage(lead_workflow, stage_slugs=None):
    if (
        not lead_workflow
        or not lead_workflow.workflow
        or not lead_workflow.current_stage
        or lead_workflow.workflow.slug != DEFAULT_LEAD_WORKFLOW_SLUG
    ):
        return False
    if stage_slugs is None:
        stage_slugs = {"fidelizacao", "30-dias-desbloqueio", "dia-do-desbloqueio"}
    return lead_workflow.current_stage.slug in set(stage_slugs)


def lead_workflow_requires_support(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return False
    if support_decision_enabled_for_workflow(lead_workflow):
        return lead_workflow.support_executor == SUPPORT_EXECUTOR_SUPPORT
    if lead_workflow.current_stage.requires_support:
        return True
    return bool(
        lead_workflow.unlock_executor == UNLOCK_EXECUTOR_SUPPORT
        and is_principal_unlock_stage(lead_workflow, {"dia-do-desbloqueio"})
    )


def support_decision_enabled_for_workflow(lead_workflow):
    return bool(
        lead_workflow
        and lead_workflow.workflow
        and lead_workflow.current_stage
        and (
            lead_workflow.current_stage.requires_support
            or (
                lead_workflow.workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG
                and lead_workflow.current_stage.slug in SUPPORT_DECISION_STAGE_SLUGS
            )
        )
        and lead_workflow.status not in TERMINAL_WORKFLOW_STATUSES
    )


def support_executor_label(value, stage_name=None):
    stage = clean_text(stage_name) or "etapa"
    labels = {
        SUPPORT_EXECUTOR_SELLER: f"Vendedor fara {stage}",
        SUPPORT_EXECUTOR_SUPPORT: f"Apoio fara {stage}",
    }
    return labels.get(value or "", "Ainda nao sinalizado")


def support_decision_prompt(lead_workflow):
    stage_name = lead_workflow.current_stage.name if lead_workflow and lead_workflow.current_stage else "esta etapa"
    return f"Sinalize quem fara {stage_name}."


def lead_workflow_owner_is_user(user, lead_workflow):
    if not user or not lead_workflow or not lead_workflow.lead:
        return False
    lead_consultor = clean_text(getattr(lead_workflow.lead, "consultor", None))
    return bool(
        lead_consultor
        and normalize_option_key(lead_consultor) == normalize_option_key(lead_assignment_name(user))
    )


def lead_owner_display_name(lead_workflow):
    if not lead_workflow:
        return "Vendedor"
    lead = lead_workflow.lead
    return (
        clean_text(getattr(lead, "consultor", None))
        or clean_text(getattr(lead_workflow.owner_user, "name", None))
        or "Vendedor"
    )


def support_display_name(lead_workflow):
    return clean_text(getattr(getattr(lead_workflow, "assigned_user", None), "name", None)) or "Apoio"


def stage_action_name(lead_workflow, default="esta etapa"):
    if is_principal_unlock_stage(lead_workflow):
        return "desbloqueio"
    if lead_workflow and lead_workflow.current_stage:
        return clean_text(lead_workflow.current_stage.name) or default
    return default


def support_action_verb(action_name):
    action_key = normalize_option_key(action_name)
    if action_key == "desbloqueio":
        return "fazer o desbloqueio"
    if action_key == "anuencia":
        return "fazer Anuencia"
    if action_key in {"pos venda", "pos-venda"}:
        return "fazer Pos Venda"
    return f"fazer {action_name}"


def executor_display_name(lead_workflow, executor, current_user=None):
    if executor in {SUPPORT_EXECUTOR_SELLER, UNLOCK_EXECUTOR_SELLER}:
        return "voce" if lead_workflow_owner_is_user(current_user, lead_workflow) else lead_owner_display_name(lead_workflow)
    if executor in {SUPPORT_EXECUTOR_SUPPORT, UNLOCK_EXECUTOR_SUPPORT}:
        support_name = support_display_name(lead_workflow)
        return "o apoio" if support_name == "Apoio" else support_name
    return ""


def responsible_executor_phrase(lead_workflow, executor, action_name, current_user=None, empty_prefix=False):
    verb = support_action_verb(action_name)
    name = executor_display_name(lead_workflow, executor, current_user=current_user)
    if name:
        return f"{name} vai {verb}" if empty_prefix else f"{name} fara {action_name}"
    return "Ainda sem escolha"


def executor_option_label(lead_workflow, executor, action_name, current_user=None, current=None):
    name = executor_display_name(lead_workflow, executor, current_user=current_user)
    verb = support_action_verb(action_name)
    phrase = f"{name} vai {verb}" if name else "Ainda sem escolha"
    if current:
        return f"Alterar para {name} {verb}" if name else "Alterar responsavel"
    return phrase[0].upper() + phrase[1:] if phrase else phrase


def unlock_executor_display_label(lead_workflow, current_user=None):
    if not lead_workflow or not lead_workflow.unlock_executor:
        return "Ainda sem escolha"
    return responsible_executor_phrase(
        lead_workflow,
        lead_workflow.unlock_executor,
        "desbloqueio",
        current_user=current_user,
    )


def support_executor_display_label(lead_workflow, current_user=None):
    if not lead_workflow or not lead_workflow.support_executor:
        return "Ainda sem escolha"
    action_name = stage_action_name(lead_workflow)
    return responsible_executor_phrase(
        lead_workflow,
        lead_workflow.support_executor,
        action_name,
        current_user=current_user,
    )


def unlock_executor_select_options(lead_workflow, current_user=None):
    if not lead_workflow:
        return []
    options = []
    current = lead_workflow.unlock_executor
    if current != UNLOCK_EXECUTOR_SELLER:
        label = executor_option_label(
            lead_workflow,
            UNLOCK_EXECUTOR_SELLER,
            "desbloqueio",
            current_user=current_user,
            current=current,
        )
        options.append({"value": UNLOCK_EXECUTOR_SELLER, "label": label})
    if current != UNLOCK_EXECUTOR_SUPPORT:
        label = executor_option_label(
            lead_workflow,
            UNLOCK_EXECUTOR_SUPPORT,
            "desbloqueio",
            current_user=current_user,
            current=current,
        )
        options.append({"value": UNLOCK_EXECUTOR_SUPPORT, "label": label})
    return options


def support_executor_select_options(lead_workflow, current_user=None):
    if not lead_workflow:
        return []
    options = []
    current = lead_workflow.support_executor
    action_name = stage_action_name(lead_workflow)
    if current != SUPPORT_EXECUTOR_SELLER:
        label = executor_option_label(
            lead_workflow,
            SUPPORT_EXECUTOR_SELLER,
            action_name,
            current_user=current_user,
            current=current,
        )
        options.append({"value": SUPPORT_EXECUTOR_SELLER, "label": label})
    if current != SUPPORT_EXECUTOR_SUPPORT:
        label = executor_option_label(
            lead_workflow,
            SUPPORT_EXECUTOR_SUPPORT,
            action_name,
            current_user=current_user,
            current=current,
        )
        options.append({"value": SUPPORT_EXECUTOR_SUPPORT, "label": label})
    return options


def can_set_support_executor(user, lead_workflow):
    return bool(
        can_move_lead_workflow(user, lead_workflow)
        and support_decision_enabled_for_workflow(lead_workflow)
        and not lead_workflow.support_completed_at
    )


def set_support_executor_for_workflow(lead_workflow, user, executor):
    executor = clean_text(executor)
    if executor not in SUPPORT_EXECUTOR_CHOICES:
        raise ValueError("Escolha quem fara esta etapa.")
    if not can_set_support_executor(user, lead_workflow):
        raise ValueError("Seu usuario nao pode sinalizar o responsavel desta etapa.")

    now = datetime.now()
    old_value = lead_workflow.support_executor
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "etapa"

    if old_value == executor:
        if executor == SUPPORT_EXECUTOR_SUPPORT:
            return assign_support_to_workflow(lead_workflow, requested_by=user, force=not lead_workflow.assigned_user_id)
        return lead_workflow.assigned_user

    lead_workflow.support_executor = executor
    lead_workflow.support_executor_decided_at = now
    lead_workflow.support_executor_decided_by_id = user.id if user else None
    lead_workflow.updated_at = now

    if executor == SUPPORT_EXECUTOR_SELLER:
        reset_support_state_for_stage(lead_workflow)
        lead_workflow.support_executor = executor
        lead_workflow.support_executor_decided_at = now
        lead_workflow.support_executor_decided_by_id = user.id if user else None

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="support_executor_update",
            note=support_executor_label(executor, stage_name),
            details=json.dumps(
                {
                    "source": "lead_detail_support_executor",
                    "old_value": old_value,
                    "new_value": executor,
                    "stage": stage_name,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_executor_update",
        field_name="support_executor",
        old_value=support_executor_label(old_value, stage_name),
        new_value=support_executor_label(executor, stage_name),
        details={
            "lead_workflow_id": lead_workflow.id,
            "executor": executor,
            "stage": stage_name,
        },
    )

    support_user = None
    if executor == SUPPORT_EXECUTOR_SUPPORT:
        support_user = assign_support_to_workflow(lead_workflow, requested_by=user, force=True)
    return support_user


def normalized_support_executor_choice(value):
    value = clean_text(value)
    if value in SUPPORT_EXECUTOR_CHOICES:
        return value
    if value == UNLOCK_EXECUTOR_SELLER:
        return SUPPORT_EXECUTOR_SELLER
    if value == UNLOCK_EXECUTOR_SUPPORT:
        return SUPPORT_EXECUTOR_SUPPORT
    return None


def executor_preference_for_stage_entry(lead_workflow):
    if not lead_workflow:
        return None
    return normalized_support_executor_choice(lead_workflow.support_executor) or normalized_support_executor_choice(
        lead_workflow.unlock_executor
    )


def inherit_support_executor_for_stage(lead_workflow, previous_executor=None, user=None, source="stage_entry"):
    if (
        not support_decision_enabled_for_workflow(lead_workflow)
        or lead_workflow.support_executor in SUPPORT_EXECUTOR_CHOICES
    ):
        return False

    inherited = normalized_support_executor_choice(previous_executor) or normalized_support_executor_choice(
        lead_workflow.unlock_executor
    )
    if inherited not in SUPPORT_EXECUTOR_CHOICES:
        return False

    now = datetime.now()
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "etapa"
    lead_workflow.support_executor = inherited
    lead_workflow.support_executor_decided_at = now
    lead_workflow.support_executor_decided_by_id = user.id if user else None
    lead_workflow.updated_at = now
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="support_executor_update",
            note=support_executor_label(inherited, stage_name),
            details=json.dumps(
                {
                    "source": source,
                    "new_value": inherited,
                    "stage": stage_name,
                    "automatic": True,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_executor_update",
        field_name="support_executor",
        new_value=support_executor_label(inherited, stage_name),
        details={
            "lead_workflow_id": lead_workflow.id,
            "executor": inherited,
            "stage": stage_name,
            "source": source,
            "automatic": True,
        },
    )
    if inherited == SUPPORT_EXECUTOR_SUPPORT:
        assign_support_to_workflow(lead_workflow, requested_by=user, force=True)
    return True


def sync_support_executor_inheritance_for_lead(lead):
    if not lead or lead.deleted_at:
        return 0
    changed = 0
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .filter_by(lead_id=lead.id)
        .all()
    )
    for lead_workflow in lead_workflows:
        if default_support_executor_for_workflow(lead_workflow, source="support_executor_sync_default"):
            changed += 1
    return changed


def unlock_executor_label(value):
    labels = {
        UNLOCK_EXECUTOR_SELLER: "Vendedor fara o desbloqueio",
        UNLOCK_EXECUTOR_SUPPORT: "Apoio fara o desbloqueio",
    }
    return labels.get(value or "", "Ainda nao sinalizado")


def can_set_unlock_executor(user, lead_workflow):
    return bool(
        can_move_lead_workflow(user, lead_workflow)
        and is_principal_unlock_stage(lead_workflow)
        and (
            not unlock_completed_for_lead(lead_workflow.lead)
            or lead_workflow.unlock_executor not in UNLOCK_EXECUTOR_CHOICES
        )
    )


def unlock_executor_workflow_for_lead(lead, lead_workflows=None):
    if not lead or lead.deleted_at:
        return None
    if lead_workflows is None:
        principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    else:
        principal = lead_workflow_by_slug_from_list(lead_workflows, DEFAULT_LEAD_WORKFLOW_SLUG)
    if not principal or not is_principal_unlock_stage(principal):
        return None
    return principal


def set_unlock_executor_for_workflow(lead_workflow, user, executor):
    executor = clean_text(executor)
    if executor not in UNLOCK_EXECUTOR_CHOICES:
        raise ValueError("Escolha quem fara o desbloqueio.")
    if not can_set_unlock_executor(user, lead_workflow):
        raise ValueError("Seu usuario nao pode sinalizar o desbloqueio deste cliente.")

    now = datetime.now()
    old_value = lead_workflow.unlock_executor

    if old_value == executor:
        if executor == UNLOCK_EXECUTOR_SUPPORT and lead_workflow_requires_support(lead_workflow):
            return assign_support_to_workflow(lead_workflow, requested_by=user, force=not lead_workflow.assigned_user_id)
        return lead_workflow.assigned_user

    lead_workflow.unlock_executor = executor
    lead_workflow.unlock_executor_decided_at = now
    lead_workflow.unlock_executor_decided_by_id = user.id if user else None
    lead_workflow.updated_at = now

    if executor == UNLOCK_EXECUTOR_SELLER:
        reset_support_state_for_stage(lead_workflow)

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="unlock_executor_update",
            note=unlock_executor_label(executor),
            details=json.dumps(
                {
                    "source": "lead_detail_unlock_executor",
                    "old_value": old_value,
                    "new_value": executor,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "unlock_executor_update",
        field_name="unlock_executor",
        old_value=unlock_executor_label(old_value),
        new_value=unlock_executor_label(executor),
        details={
            "lead_workflow_id": lead_workflow.id,
            "executor": executor,
            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
        },
    )

    support_user = None
    if executor == UNLOCK_EXECUTOR_SUPPORT and lead_workflow_requires_support(lead_workflow):
        support_user = assign_support_to_workflow(lead_workflow, requested_by=user, force=True)
    return support_user


def ensure_support_assignments():
    changed = clear_automatic_executor_defaults()
    changed += repair_support_state_leaks()
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.assigned_user),
        )
        .join(Lead, LeadWorkflow.lead_id == Lead.id)
        .filter(
            Lead.deleted_at.is_(None),
            LeadWorkflow.support_completed_at.is_(None),
            LeadWorkflow.support_returned_at.is_(None),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        .all()
    )
    for lead_workflow in lead_workflows:
        if default_unlock_executor_for_workflow(lead_workflow):
            changed += 1
        if default_support_executor_for_workflow(lead_workflow, source="support_assignment_default"):
            changed += 1
        if not lead_workflow_requires_support(lead_workflow):
            continue
        if lead_workflow.assigned_user_id:
            continue
        if assign_support_to_workflow(lead_workflow):
            changed += 1
    if changed:
        db.session.commit()
    return changed


def restore_returned_support_assignments():
    returned_workflows = (
        LeadWorkflow.query.options(joinedload(LeadWorkflow.lead))
        .filter(
            LeadWorkflow.assigned_user_id.is_(None),
            LeadWorkflow.support_completed_at.is_(None),
            LeadWorkflow.support_returned_at.isnot(None),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        .all()
    )
    changed = 0
    for lead_workflow in returned_workflows:
        transition = (
            WorkflowTransition.query.filter(
                WorkflowTransition.lead_workflow_id == lead_workflow.id,
                WorkflowTransition.action == "support_return",
                WorkflowTransition.user_id.isnot(None),
            )
            .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
            .first()
        )
        support_user = db.session.get(User, transition.user_id) if transition else None
        if (
            not support_user
            or not support_user.active
            or not getattr(support_user, "can_act_as_support", False)
            or not user_matches_lead_franchise_scope(support_user, lead_workflow.lead)
        ):
            continue
        lead_workflow.assigned_user_id = support_user.id
        lead_workflow.assigned_user = support_user
        changed += 1
    if changed:
        db.session.commit()
    return changed


def workflow_transition_details(event):
    if not event or not event.details:
        return {}
    try:
        details = json.loads(event.details)
    except (TypeError, ValueError):
        return {}
    return details if isinstance(details, dict) else {}


def latest_executor_transition(lead_workflow, action):
    if not lead_workflow:
        return None
    return (
        WorkflowTransition.query.filter_by(
            lead_workflow_id=lead_workflow.id,
            action=action,
        )
        .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
        .first()
    )


def executor_transition_is_automatic_default(event):
    details = workflow_transition_details(event)
    source = clean_text(details.get("source")) or ""
    return bool(details.get("automatic") and (details.get("default") or "default" in source))


def clear_automatic_executor_defaults_for_workflow(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return False

    changed = False
    if (
        lead_workflow.support_executor in SUPPORT_EXECUTOR_CHOICES
        and not support_work_was_recorded(lead_workflow)
        and executor_transition_is_automatic_default(latest_executor_transition(lead_workflow, "support_executor_update"))
    ):
        reset_support_state_for_stage(lead_workflow, preserve_recorded_work=False)
        lead_workflow.updated_at = datetime.now()
        changed = True

    if (
        lead_workflow.unlock_executor in UNLOCK_EXECUTOR_CHOICES
        and is_principal_unlock_stage(lead_workflow)
        and not unlock_completed_for_lead(lead_workflow.lead)
        and not support_work_was_recorded(lead_workflow)
        and executor_transition_is_automatic_default(latest_executor_transition(lead_workflow, "unlock_executor_update"))
    ):
        if lead_workflow.unlock_executor == UNLOCK_EXECUTOR_SUPPORT:
            reset_support_state_for_stage(lead_workflow, preserve_recorded_work=False)
        lead_workflow.unlock_executor = None
        lead_workflow.unlock_executor_decided_at = None
        lead_workflow.unlock_executor_decided_by_id = None
        lead_workflow.updated_at = datetime.now()
        changed = True

    return changed


def clear_automatic_executor_defaults():
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .join(Lead, LeadWorkflow.lead_id == Lead.id)
        .filter(
            Lead.deleted_at.is_(None),
            or_(
                LeadWorkflow.support_executor.isnot(None),
                LeadWorkflow.unlock_executor.isnot(None),
            ),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        .all()
    )
    return sum(1 for lead_workflow in lead_workflows if clear_automatic_executor_defaults_for_workflow(lead_workflow))


def support_work_was_recorded(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.current_stage_id
        or not (lead_workflow.support_completed_at or lead_workflow.support_returned_at)
    ):
        return False

    actions = []
    recorded_at_values = []
    if lead_workflow.support_completed_at:
        actions.append("support_complete")
        recorded_at_values.append(lead_workflow.support_completed_at)
    if lead_workflow.support_returned_at:
        actions.append("support_return")
        recorded_at_values.append(lead_workflow.support_returned_at)

    has_current_stage_transition = db.session.query(
        WorkflowTransition.query.filter(
            WorkflowTransition.lead_workflow_id == lead_workflow.id,
            WorkflowTransition.to_stage_id == lead_workflow.current_stage_id,
            WorkflowTransition.action.in_(actions),
        ).exists()
    ).scalar()
    if has_current_stage_transition:
        return True

    # Legacy fallback: older rows may not have a transition, but a timestamp inside
    # the current stage window is still treated as work from this stage.
    stage_entered_at = lead_workflow.entered_stage_at
    if stage_entered_at:
        return any(recorded_at and recorded_at >= stage_entered_at for recorded_at in recorded_at_values)
    return False


def repair_support_state_for_lead_workflow(lead_workflow, user=None):
    if (
        not lead_workflow
        or not lead_workflow.current_stage
        or not (lead_workflow.support_completed_at or lead_workflow.support_returned_at)
        or support_work_was_recorded(lead_workflow)
    ):
        return False

    reset_support_state_for_stage(lead_workflow, preserve_recorded_work=False)
    lead_workflow.updated_at = datetime.now()
    if default_support_executor_for_workflow(
        lead_workflow,
        user=user,
        source="support_state_stage_repair",
    ):
        assign_support_to_workflow(lead_workflow, requested_by=user)
    return True


def repair_support_state_for_lead(lead, user=None):
    if not lead:
        return 0
    repaired = 0
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.assigned_user),
        )
        .filter(LeadWorkflow.lead_id == lead.id)
        .all()
    )
    for lead_workflow in lead_workflows:
        if clear_automatic_executor_defaults_for_workflow(lead_workflow):
            repaired += 1
        if repair_support_state_for_lead_workflow(lead_workflow, user=user):
            repaired += 1
    return repaired


def repair_support_state_leaks():
    repaired = 0
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.assigned_user),
        )
        .join(Lead, LeadWorkflow.lead_id == Lead.id)
        .filter(
            Lead.deleted_at.is_(None),
            or_(
                LeadWorkflow.support_completed_at.isnot(None),
                LeadWorkflow.support_returned_at.isnot(None),
            ),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        .all()
    )
    for lead_workflow in lead_workflows:
        if repair_support_state_for_lead_workflow(lead_workflow):
            repaired += 1
    return repaired


def reset_support_state_for_stage(lead_workflow, preserve_recorded_work=False):
    if preserve_recorded_work and support_work_was_recorded(lead_workflow):
        return False

    lead_workflow.assigned_user_id = None
    lead_workflow.assigned_user = None
    lead_workflow.support_requested_at = None
    lead_workflow.support_assigned_at = None
    lead_workflow.support_completed_at = None
    lead_workflow.support_returned_at = None
    lead_workflow.support_return_reason = None
    lead_workflow.support_return_resolution_requested_at = None
    lead_workflow.support_return_resolution_completed_at = None
    lead_workflow.support_return_resolution_completed_by_id = None
    lead_workflow.support_return_resolution_note = None
    lead_workflow.support_executor = None
    lead_workflow.support_executor_decided_at = None
    lead_workflow.support_executor_decided_by_id = None
    return True


def default_support_executor_for_workflow(lead_workflow, user=None, source="support_default"):
    return False


def record_default_support_executor_for_workflow(lead_workflow, user=None, source="support_default"):
    if (
        not support_decision_enabled_for_workflow(lead_workflow)
        or lead_workflow.support_executor in SUPPORT_EXECUTOR_CHOICES
    ):
        return False

    now = datetime.now()
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "etapa"
    lead_workflow.support_executor = SUPPORT_EXECUTOR_SUPPORT
    lead_workflow.support_executor_decided_at = now
    lead_workflow.support_executor_decided_by_id = user.id if user else None
    lead_workflow.updated_at = now
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="support_executor_update",
            note=support_executor_label(SUPPORT_EXECUTOR_SUPPORT, stage_name),
            details=json.dumps(
                {
                    "source": source,
                    "new_value": SUPPORT_EXECUTOR_SUPPORT,
                    "stage": stage_name,
                    "automatic": True,
                    "default": True,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_executor_update",
        field_name="support_executor",
        new_value=support_executor_label(SUPPORT_EXECUTOR_SUPPORT, stage_name),
        details={
            "lead_workflow_id": lead_workflow.id,
            "executor": SUPPORT_EXECUTOR_SUPPORT,
            "stage": stage_name,
            "source": source,
            "automatic": True,
            "default": True,
        },
    )
    return True


def default_unlock_executor_for_workflow(lead_workflow, source="unlock_default_support"):
    return False


def record_default_unlock_executor_for_workflow(lead_workflow, source="unlock_default_support"):
    if (
        not lead_workflow
        or not is_principal_unlock_stage(lead_workflow)
        or lead_workflow.unlock_executor in UNLOCK_EXECUTOR_CHOICES
        or unlock_completed_for_lead(lead_workflow.lead)
    ):
        return False
    mark_unlock_executor_automatically(lead_workflow, UNLOCK_EXECUTOR_SUPPORT, source)
    return True


def assign_support_to_workflow(lead_workflow, requested_by=None, force=False):
    if (
        not lead_workflow
        or not lead_workflow.lead
        or not lead_workflow.current_stage
    ):
        return None
    if support_decision_enabled_for_workflow(lead_workflow) and lead_workflow.support_executor not in SUPPORT_EXECUTOR_CHOICES:
        default_support_executor_for_workflow(
            lead_workflow,
            user=requested_by,
            source="support_assign_default_support",
        )
    if not lead_workflow_requires_support(lead_workflow):
        return None
    if lead_workflow.support_completed_at and not force:
        return lead_workflow.assigned_user
    if lead_workflow.assigned_user_id and not force:
        return lead_workflow.assigned_user

    support_user = select_support_user_for_team(lead_workflow.lead.equipe, lead=lead_workflow.lead)
    if not support_user:
        log_audit(
            lead_workflow.lead,
            requested_by,
            "support_assign_missing",
            details={
                "lead_workflow_id": lead_workflow.id,
                "team": lead_workflow.lead.equipe,
                "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
            },
        )
        return None

    now = datetime.now()
    previous_user_id = lead_workflow.assigned_user_id
    was_returned = bool(lead_workflow.support_returned_at)
    lead_workflow.assigned_user_id = support_user.id
    lead_workflow.assigned_user = support_user
    lead_workflow.support_requested_at = lead_workflow.support_requested_at or now
    lead_workflow.support_assigned_at = now
    lead_workflow.support_completed_at = None
    lead_workflow.support_returned_at = None
    lead_workflow.support_return_reason = None
    lead_workflow.updated_at = now
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=requested_by.id if requested_by else None,
            action="support_assign",
            note=f"Apoio atribuido para {support_user.name}.",
            details=json.dumps(
                {
                    "support_user_id": support_user.id,
                    "support_user": support_user.name,
                    "previous_user_id": previous_user_id,
                    "team": lead_workflow.lead.equipe,
                    "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        requested_by,
        "support_assign",
        field_name="support",
        old_value=previous_user_id,
        new_value=support_user.id,
        details={
            "lead_workflow_id": lead_workflow.id,
            "support_user": support_user.name,
            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
        },
    )
    notify_support_assigned(lead_workflow, support_user=support_user, actor=requested_by, resent=was_returned)
    return support_user


def support_task_can_be_completed_by(user, lead_workflow):
    if not user or not lead_workflow or not lead_workflow.lead:
        return False
    if user.can_view_all:
        return can_access_lead_scope(user, lead_workflow.lead)
    if getattr(user, "can_request_support", False) and can_access_lead_scope(user, lead_workflow.lead):
        return True
    return getattr(user, "can_act_as_support", False) and lead_workflow.assigned_user_id == user.id


def can_request_support_for_workflow(user, lead_workflow):
    return bool(
        user
        and lead_workflow
        and lead_workflow.lead
        and getattr(user, "can_request_support", False)
        and can_access_lead_scope(user, lead_workflow.lead)
    )


def can_resend_returned_support_for_workflow(user, lead_workflow):
    lead_consultor = clean_text(getattr(lead_workflow.lead, "consultor", None)) if lead_workflow and lead_workflow.lead else None
    return bool(
        user
        and lead_workflow
        and lead_workflow.lead
        and lead_workflow.support_returned_at
        and not lead_workflow.support_completed_at
        and can_access_lead_scope(user, lead_workflow.lead)
        and lead_consultor
        and lead_consultor.lower() == lead_assignment_name(user).lower()
    )


def support_return_resolution_pending(lead_workflow):
    return bool(
        lead_workflow
        and lead_workflow.support_returned_at
        and not lead_workflow.support_completed_at
        and not lead_workflow.support_return_resolution_completed_at
    )


def can_resolve_support_return(user, lead_workflow):
    if not user or not lead_workflow or not lead_workflow.lead:
        return False
    if not support_return_resolution_pending(lead_workflow):
        return False
    if not can_access_lead_scope(user, lead_workflow.lead):
        return False
    lead_consultor = clean_text(getattr(lead_workflow.lead, "consultor", None))
    if getattr(user, "can_view_all", False):
        return True
    if getattr(user, "can_manage_users", False):
        return True
    if getattr(user, "role", None) == Role.SUPERVISOR and lead_workflow.lead.equipe == user.team:
        return True
    return bool(
        (getattr(user, "can_request_support", False) or getattr(user, "can_move_workflows", False))
        and lead_consultor
        and normalize_option_key(lead_consultor) == normalize_option_key(lead_assignment_name(user))
    )


def can_send_support_for_workflow(user, lead_workflow):
    if support_return_resolution_pending(lead_workflow):
        return False
    return can_request_support_for_workflow(user, lead_workflow) or can_resend_returned_support_for_workflow(
        user,
        lead_workflow,
    )


def support_actor_for_workflow(user, lead_workflow):
    return bool(
        user
        and lead_workflow
        and getattr(user, "can_act_as_support", False)
        and lead_workflow.assigned_user_id == user.id
        and not lead_workflow.support_completed_at
        and not lead_workflow.support_returned_at
    )


def support_assignment_flash_message(lead_workflow):
    if not lead_workflow_requires_support(lead_workflow):
        return None
    if lead_workflow.assigned_user:
        return ("info", f"Apoio atribuido para {lead_workflow.assigned_user.name}.")
    return ("warning", "Esta etapa foi sinalizada para apoio, mas nao ha apoio ativo vinculado a equipe.")


def mark_current_stage_checklist_done(lead_workflow, user, now=None, source="support_complete"):
    if not lead_workflow or not lead_workflow.current_stage:
        return []
    now = now or datetime.now()
    changes = []
    checks = checklist_checks_by_item_id(lead_workflow)
    for item in checklist_items_for_workflow(lead_workflow):
        check = checks.get(item.id)
        if not check:
            check = LeadWorkflowChecklistCheck(
                lead_workflow_id=lead_workflow.id,
                checklist_item_id=item.id,
                checked=False,
            )
            add_configured_task(check)
            checks[item.id] = check

        if check.checked:
            continue

        check.checked = True
        check.checked_by_id = user.id if user else None
        check.checked_at = now
        check.updated_at = now
        if not clean_text(check.note):
            check.note = "Marcado automaticamente pela conclusao do apoio."
        changes.append({"item": item.label, "checked": True, "source": source})

    if changes:
        lead_workflow.last_action_at = now
        lead_workflow.updated_at = now
        log_audit(
            lead_workflow.lead,
            user,
            "workflow_checklist_update",
            details={
                "lead_workflow_id": lead_workflow.id,
                "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
                "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                "changes": changes,
                "automatic": True,
                "source": source,
            },
        )
    return changes


def complete_support_task(lead_workflow, user, note=None, source="support_complete"):
    if not support_task_can_be_completed_by(user, lead_workflow):
        raise ValueError("Seu usuario nao pode concluir este apoio.")
    if not lead_workflow_requires_support(lead_workflow):
        raise ValueError("Esta etapa nao requer apoio.")
    note = clean_text(note)
    if not note:
        raise ValueError("Informe o que foi feito pelo apoio antes de concluir.")

    now = datetime.now()
    checklist_changes = mark_current_stage_checklist_done(lead_workflow, user, now=now, source=source)
    lead_workflow.support_completed_at = now
    lead_workflow.support_returned_at = None
    lead_workflow.support_return_reason = None
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    if source != "support_activity":
        db.session.add(
            LeadActivity(
                lead_id=lead_workflow.lead_id,
                user_id=user.id,
                lead_workflow_id=lead_workflow.id,
                workflow_id=lead_workflow.workflow_id,
                stage_id=lead_workflow.current_stage_id,
                body=note,
                source=source,
                created_at=now,
                updated_at=now,
            )
        )
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id,
            action="support_complete",
            note=note,
            details=json.dumps(
                {
                    "source": source,
                    "checklist_changes": checklist_changes,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_complete",
        field_name="support",
        new_value=user.name,
        details={
            "lead_workflow_id": lead_workflow.id,
            "source": source,
            "checklist_changes": checklist_changes,
        },
    )


def return_support_task(lead_workflow, user, reason):
    reason = clean_text(reason)
    if not reason:
        raise ValueError("Informe o motivo da devolucao.")
    if not support_actor_for_workflow(user, lead_workflow):
        raise ValueError("Apenas o apoio atribuido pode devolver esta tarefa.")

    now = datetime.now()
    lead_workflow.support_completed_at = None
    lead_workflow.support_returned_at = now
    lead_workflow.support_return_reason = reason
    lead_workflow.support_return_resolution_requested_at = now
    lead_workflow.support_return_resolution_completed_at = None
    lead_workflow.support_return_resolution_completed_by_id = None
    lead_workflow.support_return_resolution_note = None
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    db.session.add(
        LeadActivity(
            lead_id=lead_workflow.lead_id,
            user_id=user.id,
            lead_workflow_id=lead_workflow.id,
            workflow_id=lead_workflow.workflow_id,
            stage_id=lead_workflow.current_stage_id,
            body=f"Apoio devolveu: {reason}",
            source="support_return",
            created_at=now,
            updated_at=now,
        )
    )
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id,
            action="support_return",
            note=reason,
            details=json.dumps(
                {
                    "source": "support_return",
                    "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_return",
        field_name="support",
        new_value=reason,
        details={"lead_workflow_id": lead_workflow.id},
    )


def complete_support_return_resolution(lead_workflow, user, note=None):
    if not can_resolve_support_return(user, lead_workflow):
        raise ValueError("Seu usuario nao pode resolver esta devolucao do apoio.")

    note = clean_text(note)
    if not note:
        raise ValueError("Informe o que foi corrigido antes de reenviar ao apoio.")

    support_user = select_support_user_for_team(lead_workflow.lead.equipe, lead=lead_workflow.lead)
    if not support_user:
        raise ValueError("Nao ha apoio ativo vinculado a esta equipe para reenviar.")

    now = datetime.now()
    lead_workflow.support_return_resolution_completed_at = now
    lead_workflow.support_return_resolution_completed_by_id = user.id if user else None
    lead_workflow.support_return_resolution_note = note
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="support_return_resolution",
            note=note,
            details=json.dumps(
                {
                    "source": "support_return_resolution",
                    "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                    "return_reason": lead_workflow.support_return_reason,
                    "support_user_id": support_user.id,
                    "support_user": support_user.name,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "support_return_resolution",
        field_name="support",
        new_value=note,
        details={
            "lead_workflow_id": lead_workflow.id,
            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
            "return_reason": lead_workflow.support_return_reason,
            "support_user": support_user.name,
        },
    )
    return assign_support_to_workflow(lead_workflow, requested_by=user, force=True)


def sync_sale_entry_workflows():
    changed = 0
    leads = Lead.query.filter(Lead.deleted_at.is_(None)).order_by(Lead.id.asc()).all()
    for lead in leads:
        if sync_sale_entry_workflow_for_lead(lead):
            changed += 1

    if changed:
        db.session.commit()
    return changed


def sync_sale_entry_workflow_for_lead(lead, user=None):
    if not lead or lead.deleted_at:
        return False

    principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if (
        not principal
        or not principal.workflow
        or not principal.current_stage
        or principal.workflow.slug != DEFAULT_LEAD_WORKFLOW_SLUG
        or principal.current_stage.slug != DEFAULT_LEAD_WORKFLOW_STAGE_SLUG
    ):
        return False

    missing = [field for field in ("data_venda", "concessao") if not lead_field_has_value(lead, field)]
    if missing:
        return False

    next_stage = WorkflowStage.query.filter_by(
        workflow_id=principal.workflow_id,
        slug="fidelizacao",
        active=True,
    ).first()
    if not next_stage:
        return False

    now = datetime.now()
    from_stage = principal.current_stage
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=principal.id,
            lead_id=principal.lead_id,
            workflow_id=principal.workflow_id,
            from_stage_id=from_stage.id if from_stage else None,
            to_stage_id=next_stage.id,
            user_id=None,
            action="auto_move",
            note="Campos obrigatorios da venda preenchidos.",
            details=json.dumps(
                {
                    "source": "sale_entry_auto_check",
                    "trigger_user_id": user.id if user else None,
                    "required_fields": ["data_venda", "concessao"],
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    principal.current_stage_id = next_stage.id
    principal.current_stage = next_stage
    principal.entered_stage_at = now
    principal.due_at = workflow_due_at_for_stage(lead, next_stage)
    principal.updated_at = now
    support_state_reset = reset_support_state_for_stage(principal, preserve_recorded_work=True)
    if support_state_reset:
        assign_support_to_workflow(principal, requested_by=user)
    ensure_temporary_workflow_for_principal(principal, reactivate=True)
    log_audit(
        lead,
        None,
        "workflow_auto_move",
        field_name="workflow_stage",
        old_value=from_stage.name if from_stage else None,
        new_value=next_stage.name,
        details={
            "lead_workflow_id": principal.id,
            "workflow": principal.workflow.name if principal.workflow else None,
            "source": "sale_entry_auto_check",
            "trigger_user_id": user.id if user else None,
            "preserved_support_work": not support_state_reset,
        },
    )
    return True


def sync_lead_workflow_due_dates():
    changed = 0
    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.current_stage).joinedload(WorkflowStage.workflow),
        )
        .all()
    )
    for lead_workflow in lead_workflows:
        expected_due_at = workflow_due_at_for_stage(lead_workflow.lead, lead_workflow.current_stage)
        if serialize_audit_value(lead_workflow.due_at) == serialize_audit_value(expected_due_at):
            continue
        lead_workflow.due_at = expected_due_at
        changed += 1

    if changed:
        db.session.commit()
    return changed


def sync_lead_workflow_due_dates_for_lead(lead):
    changed = 0
    for lead_workflow in LeadWorkflow.query.filter_by(lead_id=lead.id).all():
        expected_due_at = workflow_due_at_for_stage(lead, lead_workflow.current_stage)
        if serialize_audit_value(lead_workflow.due_at) == serialize_audit_value(expected_due_at):
            continue
        lead_workflow.due_at = expected_due_at
        changed += 1
    ensure_unlock_tracking_workflow_for_lead(lead)
    principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if principal:
        ensure_temporary_workflow_for_principal(principal)
    if sync_unlock_workflow_stage_for_lead(lead):
        changed += 1
    changed += sync_support_executor_inheritance_for_lead(lead)
    return changed


def sync_unlock_workflow_stages():
    workflow = Workflow.query.filter_by(slug=UNLOCK_WORKFLOW_SLUG).first()
    if not workflow:
        return 0

    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .filter(
            LeadWorkflow.workflow_id == workflow.id,
            or_(
                LeadWorkflow.status.is_(None),
                ~LeadWorkflow.status.in_(("concluido", "recusado", "inativo")),
            ),
        )
        .all()
    )

    changed = 0
    for lead_workflow in lead_workflows:
        if sync_unlock_workflow_stage(lead_workflow):
            changed += 1
        if sync_unlock_day_principal_stage_for_lead(lead_workflow.lead):
            changed += 1

    if changed:
        db.session.commit()
    return changed


def sync_unlock_workflow_stage_for_lead(lead):
    lead_workflow = lead_workflow_by_slug(lead, UNLOCK_WORKFLOW_SLUG)
    changed = False
    if lead_workflow and sync_unlock_workflow_stage(lead_workflow):
        changed = True
    if sync_unlock_day_principal_stage_for_lead(lead):
        changed = True
    return changed


def sync_unlock_workflow_stage(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.lead
        or lead_workflow.lead.deleted_at
        or lead_workflow.status in {"concluido", "recusado", "inativo"}
    ):
        return False
    expected = expected_unlock_stage(unlock_days_left(lead_workflow.lead))
    if not expected or expected["slug"] not in UNLOCK_AUTO_STAGE_SLUGS:
        return False
    if not lead_workflow.current_stage or lead_workflow.current_stage.slug == expected["slug"]:
        return False

    target_stage = WorkflowStage.query.filter_by(
        workflow_id=lead_workflow.workflow_id,
        slug=expected["slug"],
        active=True,
    ).first()
    if not target_stage:
        return False

    now = datetime.now()
    from_stage = lead_workflow.current_stage
    days_left = unlock_days_left(lead_workflow.lead)
    direction = "forward"
    if from_stage and target_stage.sort_order < from_stage.sort_order:
        direction = "backward"
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=from_stage.id if from_stage else None,
            to_stage_id=target_stage.id,
            user_id=None,
            action="auto_move",
            note=f"Ajuste automatico para janela de {expected['label']}.",
            details=json.dumps(
                {
                    "source": "unlock_window_sync",
                    "days_left": days_left,
                    "expected_stage": expected["slug"],
                    "direction": direction,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    lead_workflow.current_stage_id = target_stage.id
    lead_workflow.current_stage = target_stage
    lead_workflow.entered_stage_at = now
    lead_workflow.due_at = workflow_due_at_for_stage(lead_workflow.lead, target_stage)
    lead_workflow.updated_at = now
    support_state_reset = reset_support_state_for_stage(lead_workflow, preserve_recorded_work=True)
    if support_state_reset:
        if default_support_executor_for_workflow(
            lead_workflow,
            source="unlock_window_default_support",
        ):
            assign_support_to_workflow(lead_workflow)
    log_audit(
        lead_workflow.lead,
        None,
        "workflow_auto_move",
        field_name="workflow_stage",
        old_value=from_stage.name if from_stage else None,
        new_value=target_stage.name,
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "source": "unlock_window_sync",
            "days_left": days_left,
            "preserved_support_work": not support_state_reset,
        },
    )
    return True


def sync_unlock_day_principal_stage_for_lead(lead):
    if not lead or lead.deleted_at:
        return False

    principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if not principal or not principal.current_stage or principal.status in TERMINAL_WORKFLOW_STATUSES:
        return False

    changed = False
    if principal.current_stage.slug in {"dia-do-desbloqueio", "anuencia", "pos-venda"}:
        changed = auto_complete_unlock_tracking_workflow(lead, datetime.now())
    days_left = unlock_days_left(lead)
    if days_left is None or days_left > 0:
        return changed

    if principal.current_stage.slug in {"fidelizacao", "30-dias-desbloqueio"}:
        tracking_workflow = lead_workflow_by_slug(lead, UNLOCK_WORKFLOW_SLUG)
        if not tracking_workflow and not lead_is_unlock_workflow_eligible(lead, principal_lead_workflow=principal):
            return False
        checklist_ready = not missing_checklist_labels(principal) or (
            tracking_workflow is not None and not missing_checklist_labels(tracking_workflow)
        )
        has_executor_decision = principal.unlock_executor in UNLOCK_EXECUTOR_CHOICES
        should_enter_unlock_day = days_left <= 0 or checklist_ready or has_executor_decision
        if not should_enter_unlock_day:
            return False

        if move_principal_to_unlock_day_stage(principal, days_left):
            changed = True

    if principal.current_stage and principal.current_stage.slug == "dia-do-desbloqueio":
        if auto_complete_unlock_tracking_workflow(lead, datetime.now()):
            changed = True
        if (
            principal.unlock_executor == UNLOCK_EXECUTOR_SUPPORT
            and not principal.assigned_user_id
            and not support_work_was_recorded(principal)
        ):
            if assign_support_to_workflow(principal, force=True):
                changed = True

    return changed


def move_principal_to_unlock_day_stage(principal, days_left):
    target_stage = WorkflowStage.query.filter_by(
        workflow_id=principal.workflow_id,
        slug="dia-do-desbloqueio",
        active=True,
    ).first()
    if not target_stage or (principal.current_stage and principal.current_stage.id == target_stage.id):
        return False

    now = datetime.now()
    lead = principal.lead
    from_stage = principal.current_stage
    auto_complete_unlock_tracking_workflow(lead, now)
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=principal.id,
            lead_id=principal.lead_id,
            workflow_id=principal.workflow_id,
            from_stage_id=from_stage.id if from_stage else None,
            to_stage_id=target_stage.id,
            user_id=None,
            action="auto_move",
            note="Cliente entrou automaticamente no Dia do Desbloqueio.",
            details=json.dumps(
                {
                    "source": "unlock_day_sync",
                    "days_left": days_left,
                    "unlock_executor": principal.unlock_executor,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    principal.current_stage_id = target_stage.id
    principal.current_stage = target_stage
    principal.entered_stage_at = now
    principal.due_at = workflow_due_at_for_stage(lead, target_stage)
    principal.updated_at = now
    support_state_reset = reset_support_state_for_stage(principal, preserve_recorded_work=True)
    log_audit(
        lead,
        None,
        "workflow_auto_move",
        field_name="workflow_stage",
        old_value=from_stage.name if from_stage else None,
        new_value=target_stage.name,
        details={
            "lead_workflow_id": principal.id,
            "workflow": principal.workflow.name if principal.workflow else None,
            "source": "unlock_day_sync",
            "days_left": days_left,
            "unlock_executor": principal.unlock_executor,
            "preserved_support_work": not support_state_reset,
        },
    )
    if support_state_reset and principal.unlock_executor == UNLOCK_EXECUTOR_SUPPORT:
        assign_support_to_workflow(principal, force=True)
    return True


def auto_complete_unlock_tracking_workflow(lead, now):
    tracking = lead_workflow_by_slug(lead, UNLOCK_WORKFLOW_SLUG)
    if not tracking or tracking.status in TERMINAL_WORKFLOW_STATUSES:
        return False

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=tracking.id,
            lead_id=tracking.lead_id,
            workflow_id=tracking.workflow_id,
            from_stage_id=tracking.current_stage_id,
            to_stage_id=None,
            user_id=None,
            action="complete_temporary",
            note="Janela de Fidelizacao encerrada na data do desbloqueio; tarefas pendentes preservadas.",
            details=json.dumps({"source": "unlock_day_sync"}, ensure_ascii=False),
            created_at=now,
        )
    )
    tracking.status = "concluido"
    tracking.completed_at = now
    tracking.updated_at = now
    return True


def mark_unlock_executor_automatically(lead_workflow, executor, source):
    now = datetime.now()
    old_value = lead_workflow.unlock_executor
    lead_workflow.unlock_executor = executor
    lead_workflow.unlock_executor_decided_at = now
    lead_workflow.unlock_executor_decided_by_id = None
    lead_workflow.updated_at = now
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=None,
            action="unlock_executor_update",
            note=unlock_executor_label(executor),
            details=json.dumps(
                {
                    "source": source,
                    "old_value": old_value,
                    "new_value": executor,
                    "automatic": True,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        None,
        "unlock_executor_update",
        field_name="unlock_executor",
        old_value=unlock_executor_label(old_value),
        new_value=unlock_executor_label(executor),
        details={
            "lead_workflow_id": lead_workflow.id,
            "source": source,
            "automatic": True,
        },
    )


def ensure_initial_workflow_transitions():
    changed = 0
    lead_workflows = (
        LeadWorkflow.query.outerjoin(
            WorkflowTransition,
            WorkflowTransition.lead_workflow_id == LeadWorkflow.id,
        )
        .filter(WorkflowTransition.id.is_(None))
        .all()
    )
    for lead_workflow in lead_workflows:
        if ensure_initial_workflow_transition(lead_workflow, source="migration"):
            changed += 1

    if changed:
        db.session.commit()
    return changed


def ensure_initial_workflow_transition(lead_workflow, user=None, source="system"):
    if not lead_workflow.id:
        db.session.flush()

    existing = WorkflowTransition.query.filter_by(lead_workflow_id=lead_workflow.id).first()
    if existing:
        return False

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=None,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="stage_seed",
            note="Entrada inicial do cliente no fluxo.",
            details=json.dumps({"source": source}, ensure_ascii=False),
            created_at=lead_workflow.entered_stage_at or datetime.now(),
        )
    )
    return True


def ensure_legacy_lead_activities():
    changed = 0
    for lead in Lead.query.order_by(Lead.id.asc()).all():
        changed += ensure_legacy_lead_activities_for_lead(lead)

    if changed:
        db.session.commit()
    return changed


def ensure_legacy_lead_activities_for_lead(lead, fallback_user=None):
    lead_workflow = current_operational_lead_workflow_for_lead(lead)
    changed = 0
    latest_activity_at = None
    for field in ATUACAO_FIELDS:
        body = clean_text(getattr(lead, field, None))
        existing = LeadActivity.query.filter_by(
            lead_id=lead.id,
            source="legacy_field",
            source_field=field,
        ).first()

        if not body:
            if existing and not existing.deleted_at:
                existing.deleted_at = datetime.now()
                changed += 1
            continue

        if existing:
            if existing.body != body:
                existing.body = body
                existing.updated_at = datetime.now()
                changed += 1
            existing.deleted_at = None
            if lead_workflow and existing.lead_workflow_id is None and existing.workflow_id is None and existing.stage_id is None:
                existing.lead_workflow_id = lead_workflow.id
                existing.workflow_id = lead_workflow.workflow_id
                existing.stage_id = lead_workflow.current_stage_id
            latest_activity_at = max_datetime(latest_activity_at, existing.created_at)
            continue

        audit = latest_audit_for_field(lead, field)
        activity = LeadActivity(
            lead_id=lead.id,
            user_id=audit.user_id if audit else fallback_user.id if fallback_user else None,
            lead_workflow_id=lead_workflow.id if lead_workflow else None,
            workflow_id=lead_workflow.workflow_id if lead_workflow else None,
            stage_id=lead_workflow.current_stage_id if lead_workflow else None,
            body=body,
            source="legacy_field",
            source_field=field,
            created_at=audit.created_at if audit else lead.updated_at or lead.created_at or datetime.now(),
            updated_at=datetime.now(),
        )
        db.session.add(activity)
        latest_activity_at = max_datetime(latest_activity_at, activity.created_at)
        changed += 1

    if lead_workflow and latest_activity_at and (
        not lead_workflow.last_action_at or latest_activity_at > lead_workflow.last_action_at
    ):
        lead_workflow.last_action_at = latest_activity_at
        changed += 1

    return changed


def max_datetime(left, right):
    if not left:
        return right
    if not right:
        return left
    return left if left >= right else right


def sync_lead_workflow_activity_timestamps():
    rows = (
        db.session.query(LeadActivity.lead_workflow_id, func.max(LeadActivity.created_at))
        .filter(
            LeadActivity.lead_workflow_id.isnot(None),
            LeadActivity.deleted_at.is_(None),
        )
        .group_by(LeadActivity.lead_workflow_id)
        .all()
    )
    changed = 0
    for lead_workflow_id, latest_at in rows:
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        if not lead_workflow or not latest_at:
            continue
        if not lead_workflow.last_action_at or latest_at > lead_workflow.last_action_at:
            lead_workflow.last_action_at = latest_at
            changed += 1

    if changed:
        db.session.commit()
    return changed


def default_lead_workflow_stage():
    workflow = Workflow.query.filter_by(slug=DEFAULT_LEAD_WORKFLOW_SLUG).first()
    if not workflow:
        return None, None
    stage = WorkflowStage.query.filter_by(
        workflow_id=workflow.id,
        slug=DEFAULT_LEAD_WORKFLOW_STAGE_SLUG,
    ).first()
    return workflow, stage


def primary_lead_workflow_for_lead(lead, create_missing=True):
    workflow = Workflow.query.filter_by(slug=DEFAULT_LEAD_WORKFLOW_SLUG).first()
    if not workflow:
        return None

    lead_workflow = LeadWorkflow.query.filter_by(
        lead_id=lead.id,
        workflow_id=workflow.id,
    ).first()
    if lead_workflow or not create_missing:
        return lead_workflow

    lead_workflow = ensure_lead_default_workflows(lead)
    db.session.flush()
    return lead_workflow


def build_default_lead_workflow(lead, workflow, stage):
    entered_stage_at = lead.created_at or datetime.utcnow()
    return LeadWorkflow(
        lead_id=lead.id,
        workflow_id=workflow.id,
        current_stage_id=stage.id,
        status=workflow_status_for_lead(lead),
        entered_stage_at=entered_stage_at,
        due_at=workflow_due_at_for_stage(lead, stage),
        owner_user_id=workflow_owner_id_for_lead(lead),
        last_action_at=latest_activity_datetime_for_lead(lead),
        created_at=entered_stage_at,
        updated_at=datetime.utcnow(),
    )


def workflow_status_for_lead(lead):
    if lead.deleted_at:
        return "inativo"
    status_key = normalize_option_key(lead.status)
    if status_key in {"recusado", "perdido", "perda"}:
        return "recusado"
    if status_key == "concluido":
        return "concluido"
    return "em_andamento"


def status_option_for_action(action):
    action_key = normalize_option_key(action)
    if action_key in {"perdido", "perda"}:
        action_key = "recusado"

    config = STATUS_OPTION_ALIASES.get(action_key)
    if not config:
        raise ValueError("Acao de status invalida.")

    options = get_active_options("status")
    for desired_key in config["keys"]:
        for option in options:
            if normalize_option_key(option) == desired_key:
                return option
    return config["fallback"]


def is_new_status(value):
    return normalize_option_key(value) == "novo"


def ensure_lead_new_status(lead):
    if not lead or lead.deleted_at or clean_text(lead.status):
        return None

    lead.status = status_option_for_action("novo")
    lead.status_manual = False
    return lead.status


def ensure_lead_in_progress_status(lead, user=None, source="workflow_activity"):
    if not lead or lead.deleted_at:
        return None

    status_key = normalize_option_key(lead.status)
    if status_key and not is_new_status(lead.status):
        return None

    old_value = serialize_audit_value(lead.status)
    new_value = status_option_for_action("andamento")
    lead.status = new_value
    lead.status_manual = False
    log_audit(
        lead,
        user,
        "update",
        field_name="status",
        old_value=old_value,
        new_value=new_value,
        details={"source": source, "automatic": True},
    )
    return {"old_value": old_value, "new_value": new_value}


def backfill_in_progress_status_for_activity_leads():
    in_progress_status = status_option_for_action("andamento")
    leads = Lead.query.filter(
        Lead.deleted_at.is_(None),
        or_(Lead.status.is_(None), Lead.status == "", func.lower(Lead.status) == "novo"),
    ).all()

    changed = 0
    for lead in leads:
        if not lead_has_any_activity(lead):
            continue
        old_value = serialize_audit_value(lead.status)
        lead.status = in_progress_status
        lead.status_manual = False
        log_audit(
            lead,
            None,
            "update",
            field_name="status",
            old_value=old_value,
            new_value=in_progress_status,
            details={"source": "activity_status_backfill", "automatic": True},
        )
        changed += 1

    if changed:
        db.session.commit()
    return changed


def backfill_new_status_for_untouched_leads():
    new_status = status_option_for_action("novo")
    leads = Lead.query.filter(
        Lead.deleted_at.is_(None),
        or_(Lead.status.is_(None), Lead.status == ""),
    ).all()

    changed = 0
    for lead in leads:
        if lead_has_any_activity(lead):
            continue
        old_value = serialize_audit_value(lead.status)
        lead.status = new_status
        lead.status_manual = False
        log_audit(
            lead,
            None,
            "update",
            field_name="status",
            old_value=old_value,
            new_value=new_status,
            details={"source": "new_status_backfill", "automatic": True},
        )
        changed += 1

    if changed:
        db.session.commit()
    return changed


def lead_has_any_activity(lead):
    if not lead:
        return False
    has_timeline = db.session.query(
        LeadActivity.query.filter(
            LeadActivity.lead_id == lead.id,
            LeadActivity.deleted_at.is_(None),
        ).exists()
    ).scalar()
    if has_timeline:
        return True

    for field in ATUACAO_FIELDS:
        if clean_text(getattr(lead, field, None)):
            return True
    return False


def post_sale_workflow_for_lead(lead):
    lead_workflow = lead_workflow_by_slug(lead, DEFAULT_LEAD_WORKFLOW_SLUG)
    if not lead_workflow or not lead_workflow.current_stage:
        return None
    if lead_workflow.current_stage.slug != POST_SALE_STAGE_SLUG:
        return None
    return lead_workflow


def post_sale_completion_state(lead):
    lead_workflow = post_sale_workflow_for_lead(lead)
    if not lead_workflow:
        return {
            "complete": False,
            "lead_workflow": None,
            "message": "Cliente precisa estar na etapa Pos Venda.",
        }

    required_items = [
        item
        for item in checklist_items_for_workflow(lead_workflow)
        if item.required
    ]
    if not required_items:
        return {
            "complete": False,
            "lead_workflow": lead_workflow,
            "message": "Configure uma tarefa obrigatoria na etapa Pos Venda.",
        }

    checks = checklist_checks_by_item_id(lead_workflow)
    missing = [item.label for item in required_items if not checks.get(item.id) or not checks[item.id].checked]
    if missing:
        return {
            "complete": False,
            "lead_workflow": lead_workflow,
            "message": "Conclua as tarefas do Pos Venda antes de concluir.",
            "missing": missing,
        }

    if support_decision_enabled_for_workflow(lead_workflow) and not lead_workflow.support_executor:
        return {
            "complete": False,
            "lead_workflow": lead_workflow,
            "message": "Sinalize quem fara o Pos Venda antes de concluir.",
            "missing": ["responsavel pelo Pos Venda"],
        }

    if lead_workflow_requires_support(lead_workflow) and not lead_workflow.support_completed_at:
        return {
            "complete": False,
            "lead_workflow": lead_workflow,
            "message": "Aguarde o apoio concluir o Pos Venda antes de concluir o contrato.",
            "missing": ["apoio comercial"],
        }

    return {
        "complete": True,
        "lead_workflow": lead_workflow,
        "message": "Pos Venda concluido.",
        "missing": [],
    }


def can_conclude_lead_contract(lead):
    return post_sale_completion_state(lead)["complete"]


def can_user_conclude_lead_contract(user, lead):
    if not user or not getattr(user, "can_conclude_contract", False):
        return False
    return can_access_lead_scope(user, lead) and can_conclude_lead_contract(lead)


def latest_contract_status_action_audit(lead):
    current_status_key = normalize_option_key(lead.status)
    if current_status_key not in {"concluido", "recusado", "perdido", "perda"}:
        return None

    audits = (
        AuditLog.query.filter(
            AuditLog.lead_id == lead.id,
            AuditLog.action == "update",
            AuditLog.field_name == "status",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(12)
        .all()
    )
    for audit in audits:
        if normalize_option_key(audit.new_value) == current_status_key:
            return audit
    return None


def can_undo_lead_contract_status(lead):
    return latest_contract_status_action_audit(lead) is not None


def workflow_status_snapshot_for_lead(lead):
    return [
        {
            "id": lead_workflow.id,
            "status": lead_workflow.status,
            "completed_at": serialize_audit_value(lead_workflow.completed_at),
        }
        for lead_workflow in LeadWorkflow.query.filter_by(lead_id=lead.id).all()
    ]


def restore_workflow_status_snapshot(lead, snapshot):
    if not snapshot:
        status = workflow_status_for_lead(lead)
        changed = 0
        for lead_workflow in LeadWorkflow.query.filter_by(lead_id=lead.id).all():
            if lead_workflow.status == status:
                continue
            lead_workflow.status = status
            if status != "concluido":
                lead_workflow.completed_at = None
            lead_workflow.updated_at = datetime.now()
            changed += 1
        return changed

    changed = 0
    workflows_by_id = {
        lead_workflow.id: lead_workflow
        for lead_workflow in LeadWorkflow.query.filter_by(lead_id=lead.id).all()
    }
    for item in snapshot:
        lead_workflow = workflows_by_id.get(parse_int(item.get("id"), 0))
        if not lead_workflow:
            continue
        lead_workflow.status = item.get("status") or "em_andamento"
        completed_at = item.get("completed_at")
        try:
            lead_workflow.completed_at = datetime.fromisoformat(completed_at) if completed_at else None
        except (TypeError, ValueError):
            lead_workflow.completed_at = None
        lead_workflow.updated_at = datetime.now()
        changed += 1
    return changed


def audit_details_dict(audit):
    if not audit or not audit.details:
        return {}
    try:
        details = json.loads(audit.details)
        return details if isinstance(details, dict) else {}
    except (TypeError, ValueError):
        return {}


def set_lead_contract_status(lead, user, action, reason=None, note=None):
    action_key = normalize_option_key(action)
    if action_key == "concluido" and not can_conclude_lead_contract(lead):
        raise ValueError(post_sale_completion_state(lead)["message"])
    if action_key in {"recusado", "perdido", "perda"} and not clean_text(reason):
        raise ValueError("Informe o motivo da recusa antes de marcar como recusado.")

    new_value = status_option_for_action(action)
    old_value = serialize_audit_value(lead.status)
    if old_value == serialize_audit_value(new_value):
        return False

    workflow_snapshot = workflow_status_snapshot_for_lead(lead)
    lead.status = new_value
    lead.status_manual = True
    synced_workflows = sync_terminal_lead_workflow_statuses(lead)
    if workflow_status_for_lead(lead) in TERMINAL_WORKFLOW_STATUSES:
        for reversal in ReversalTask.query.filter(ReversalTask.lead_id == lead.id,
                                                   ReversalTask.status.in_(ReversalStatus.ACTIVE)).all():
            record_reversal_cancellation(reversal, user, f"Contrato marcado como {new_value}.", source="contract_status")
    reason = clean_text(reason)
    note = clean_text(note)
    if reason:
        body = f"Motivo da recusa: {reason}"
        if note:
            body = f"{body}\nObservacao: {note}"
        db.session.add(
            LeadActivity(
                lead_id=lead.id,
                user_id=user.id if user else None,
                body=body,
                source="loss_reason",
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
    log_audit(
        lead,
        user,
        "update",
        field_name="status",
        old_value=old_value,
        new_value=new_value,
        details={
            "source": "lead_detail_status_action",
            "status_action": normalize_option_key(action),
            "reason": reason,
            "note": note,
            "synced_workflows": len(synced_workflows),
            "workflow_status_snapshot": workflow_snapshot,
        },
    )
    return True


def sync_terminal_lead_workflow_statuses(lead):
    status = workflow_status_for_lead(lead)
    if status not in {"concluido", "recusado", "inativo"}:
        return []

    changed = 0
    now = datetime.now()
    snapshot = []
    for lead_workflow in LeadWorkflow.query.filter_by(lead_id=lead.id).all():
        snapshot.append(
            {
                "id": lead_workflow.id,
                "status": lead_workflow.status,
                "completed_at": serialize_audit_value(lead_workflow.completed_at),
            }
        )
        if lead_workflow.status == status:
            continue
        lead_workflow.status = status
        if status == "concluido" and not lead_workflow.completed_at:
            lead_workflow.completed_at = now
        lead_workflow.updated_at = now
        changed += 1
    return snapshot if changed else []


def undo_lead_contract_status(lead, user):
    audit = latest_contract_status_action_audit(lead)
    if not audit:
        raise ValueError("Nao encontrei um status recente para desfazer.")

    old_value = serialize_audit_value(lead.status)
    restore_value = audit.old_value
    if not restore_value or is_new_status(restore_value):
        restore_value = status_option_for_action("andamento")

    lead.status = restore_value
    lead.status_manual = normalize_option_key(restore_value) not in {"", "andamento", "em andamento"}
    details = audit_details_dict(audit)
    restored_workflows = restore_workflow_status_snapshot(
        lead,
        details.get("workflow_status_snapshot"),
    )
    log_audit(
        lead,
        user,
        "status_undo",
        field_name="status",
        old_value=old_value,
        new_value=restore_value,
        details={
            "source": "lead_detail_status_undo",
            "undone_audit_id": audit.id,
            "restored_workflows": restored_workflows,
        },
    )
    return restore_value


def workflow_due_at_for_stage(lead, stage):
    unlock_date = unlock_date_for_lead(lead)
    if not unlock_date or not stage:
        return None
    workflow_slug = stage.workflow.slug if stage.workflow else None
    if workflow_slug in {DEFAULT_LEAD_WORKFLOW_SLUG, UNLOCK_WORKFLOW_SLUG}:
        return datetime.combine(unlock_date, datetime.min.time())
    return None


def unlock_date_for_lead(lead):
    if not lead or not lead.concessao:
        return None
    return lead.concessao + timedelta(days=UNLOCK_TOTAL_DAYS)


def unlock_days_left(lead, today=None):
    unlock_date = unlock_date_for_lead(lead)
    if not unlock_date:
        return None
    reference_date = today or datetime.now().date()
    return (unlock_date - reference_date).days


def expected_unlock_stage(days_left):
    if days_left is None or days_left > UNLOCK_TRACKING_DAYS:
        return None
    for max_days, stage_slug, label in UNLOCK_STAGE_WINDOWS:
        if days_left <= max_days:
            return {"slug": stage_slug, "label": label}
    return None


def unlock_completed_for_lead(lead, lead_workflows=None):
    if not lead:
        return False

    if lead_workflows is None:
        lead_workflows = LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        ).filter_by(lead_id=lead.id).all()

    principal = lead_workflow_by_slug_from_list(lead_workflows, DEFAULT_LEAD_WORKFLOW_SLUG)
    if not principal:
        principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if not principal or not principal.current_stage:
        return False

    unlock_stage = WorkflowStage.query.filter_by(
        workflow_id=principal.workflow_id,
        slug="dia-do-desbloqueio",
    ).first()
    if not unlock_stage:
        return False

    if principal.current_stage.sort_order > unlock_stage.sort_order:
        return True
    if principal.current_stage.slug != "dia-do-desbloqueio":
        return False

    required_items = [
        item
        for item in checklist_items_for_workflow(principal)
        if item.required
    ]
    if not required_items:
        return False

    checks = checklist_checks_by_item_id(principal)
    return all(checks.get(item.id) and checks[item.id].checked for item in required_items)


def unlock_completion_details_for_lead(lead, lead_workflows=None):
    if not lead:
        return {}

    if lead_workflows is None:
        lead_workflows = LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        ).filter_by(lead_id=lead.id).all()

    principal = lead_workflow_by_slug_from_list(lead_workflows, DEFAULT_LEAD_WORKFLOW_SLUG)
    if not principal:
        principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if not principal:
        return {}

    unlock_stage = WorkflowStage.query.filter_by(
        workflow_id=principal.workflow_id,
        slug="dia-do-desbloqueio",
    ).first()
    if not unlock_stage:
        return {}

    checklist_details = completed_stage_checklist_details(principal, unlock_stage)
    support_transition = (
        WorkflowTransition.query.options(joinedload(WorkflowTransition.user))
        .filter_by(
            lead_workflow_id=principal.id,
            action="support_complete",
            to_stage_id=unlock_stage.id,
        )
        .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
        .first()
    )
    move_transition = (
        WorkflowTransition.query.options(joinedload(WorkflowTransition.user))
        .filter(
            WorkflowTransition.lead_workflow_id == principal.id,
            or_(
                WorkflowTransition.from_stage_id == unlock_stage.id,
                WorkflowTransition.to_stage_id == unlock_stage.id,
            ),
        )
        .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
        .first()
    )

    completed_at = (
        support_transition.created_at
        if support_transition
        else (checklist_details or {}).get("completed_at")
        or principal.support_completed_at
        or (move_transition.created_at if move_transition else None)
    )
    user = support_transition.user if support_transition else None
    checked_by_id = (checklist_details or {}).get("checked_by_id")
    if not user and checked_by_id:
        user = db.session.get(User, checked_by_id)

    return {
        "completed_at": completed_at,
        "user": user,
        "stage": unlock_stage,
    }


def reset_unlock_completion_after_concessao_change(lead, user=None):
    principal = primary_lead_workflow_for_lead(lead, create_missing=False)
    if not principal or not principal.current_stage or principal.current_stage.slug != "dia-do-desbloqueio":
        return 0
    if support_work_was_recorded(principal):
        return 0

    required_items = [
        item
        for item in checklist_items_for_workflow(principal)
        if item.required
    ]
    if not required_items:
        return 0

    checks = checklist_checks_by_item_id(principal)
    now = datetime.now()
    changes = []
    for item in required_items:
        check = checks.get(item.id)
        if not check or not check.checked:
            continue
        check.checked = False
        check.checked_by_id = None
        check.checked_at = None
        check.updated_at = now
        changes.append({"item": item.label, "checked": False})

    if changes:
        log_audit(
            lead,
            user,
            "workflow_checklist_update",
            details={
                "lead_workflow_id": principal.id,
                "workflow": principal.workflow.name if principal.workflow else None,
                "stage": principal.current_stage.name,
                "changes": changes,
                "source": "concessao_change_reset",
            },
        )
    return len(changes)


def unlock_alert_for_lead(lead, lead_workflows=None):
    if not lead.concessao:
        return {
            "active": True,
            "severity": "warning",
            "title": "Concessao vazia",
            "message": "Preencha a concessao para calcular o desbloqueio.",
            "unlock_date": None,
            "days_left": None,
            "expected_stage": None,
            "workflow_mismatch": False,
        }

    unlock_date = unlock_date_for_lead(lead)
    days_left = unlock_days_left(lead)
    expected_stage = expected_unlock_stage(days_left)

    if unlock_completed_for_lead(lead, lead_workflows):
        completion_details = unlock_completion_details_for_lead(lead, lead_workflows)
        completed_at = completion_details.get("completed_at")
        completed_by = completion_details.get("user")
        message_parts = []
        if completed_by:
            message_parts.append(f"Feito por {completed_by.name}.")
        elif completed_at:
            message_parts.append(f"Feito em {completed_at.strftime('%d/%m/%Y')}.")
        else:
            message_parts.append("Desbloqueio feito.")
        return {
            "active": True,
            "severity": "info",
            "title": "Desbloqueio feito",
            "message": " ".join(message_parts),
            "unlock_date": unlock_date,
            "days_left": days_left,
            "expected_stage": expected_stage,
            "workflow_mismatch": False,
            "completed": True,
            "completed_at": completed_at,
        }

    severity, title = unlock_alert_label(days_left)
    workflow_mismatch, _mismatch_message = unlock_workflow_mismatch(lead, lead_workflows, expected_stage)
    message_parts = []
    if days_left < 0:
        message_parts.append(f"Passou {abs(days_left)} dia(s) do desbloqueio.")
    elif days_left == 0:
        message_parts.append("O desbloqueio e hoje.")
    elif days_left == 1:
        message_parts.append("Falta 1 dia para o desbloqueio.")
    elif days_left <= UNLOCK_TRACKING_DAYS:
        message_parts.append(f"Faltam {days_left} dias para o desbloqueio.")
    else:
        message_parts.append(f"Faltam {days_left} dias para o desbloqueio.")
    return {
        "active": days_left <= UNLOCK_TRACKING_DAYS or workflow_mismatch,
        "severity": "critical" if workflow_mismatch and days_left <= 1 else severity,
        "title": title,
        "message": " ".join(message_parts),
        "unlock_date": unlock_date,
        "days_left": days_left,
        "expected_stage": expected_stage,
        "workflow_mismatch": workflow_mismatch,
    }


def unlock_alert_label(days_left):
    if days_left < 0:
        return "critical", "Desbloqueio vencido"
    if days_left == 0:
        return "critical", "Desbloqueio hoje"
    if days_left == 1:
        return "danger", "Desbloqueio amanha"
    if days_left <= 10:
        return "danger", "Janela de 10 dias"
    if days_left <= 20:
        return "warning", "Janela de 20 dias"
    if days_left <= 30:
        return "info", "Janela de 30 dias"
    if days_left <= 60:
        return "info", "Relacionamento: 60 a 31 dias"
    if days_left <= UNLOCK_TRACKING_DAYS:
        return "info", "Preparacao inicial: 90 a 61 dias"
    return "muted", "Desbloqueio programado"


def unlock_workflow_mismatch(lead, lead_workflows, expected_stage):
    if not expected_stage:
        return False, None

    lead_workflows = lead_workflows if lead_workflows is not None else LeadWorkflow.query.filter_by(lead_id=lead.id).all()
    workflow_by_slug = {
        lead_workflow.workflow.slug: lead_workflow
        for lead_workflow in lead_workflows
        if lead_workflow.workflow
    }

    if expected_stage["slug"] == "dia-do-desbloqueio":
        principal = workflow_by_slug.get(DEFAULT_LEAD_WORKFLOW_SLUG)
        if not principal or not principal.current_stage or principal.current_stage.slug != "dia-do-desbloqueio":
            current_label = principal.current_stage.name if principal and principal.current_stage else "sem etapa"
            return True, f"Esperado: Dia do Desbloqueio. Atual: {current_label}."
        return False, None

    unlock_workflow = workflow_by_slug.get(UNLOCK_WORKFLOW_SLUG)
    if not unlock_workflow:
        return True, f"Esperado: Fidelizacao na etapa {expected_stage['label']}."
    if not unlock_workflow.current_stage or unlock_workflow.current_stage.slug != expected_stage["slug"]:
        current_label = unlock_workflow.current_stage.name if unlock_workflow.current_stage else "sem etapa"
        return True, f"Esperado: {expected_stage['label']}. Atual: {current_label}."
    return False, None


def unlock_alerts_for_leads(leads):
    lead_ids = [lead.id for lead in leads]
    workflows_by_lead = {lead_id: [] for lead_id in lead_ids}
    if lead_ids:
        lead_workflows = (
            LeadWorkflow.query.options(
                joinedload(LeadWorkflow.workflow),
                joinedload(LeadWorkflow.current_stage),
            )
            .filter(LeadWorkflow.lead_id.in_(lead_ids))
            .all()
        )
        for lead_workflow in lead_workflows:
            workflows_by_lead.setdefault(lead_workflow.lead_id, []).append(lead_workflow)
    return {lead.id: unlock_alert_for_lead(lead, workflows_by_lead.get(lead.id, [])) for lead in leads}


def workflow_owner_id_for_lead(lead):
    if not lead.consultor:
        return None

    normalized_name = lead.consultor.lower()
    scoped_users = scope_user_query_to_lead(
        User.query.filter(User.active.is_(True)),
        lead,
    )
    user = scoped_users.filter(func.lower(User.lead_name) == normalized_name).order_by(User.id.asc()).first()
    if not user:
        user = scoped_users.filter(func.lower(User.name) == normalized_name).order_by(User.id.asc()).first()
    return user.id if user else None


def latest_audit_for_field(lead, field_name):
    return (
        AuditLog.query.filter(
            AuditLog.lead_id == lead.id,
            AuditLog.field_name == field_name,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )


def latest_activity_datetime_for_lead(lead):
    latest = (
        db.session.query(func.max(AuditLog.created_at))
        .filter(
            AuditLog.lead_id == lead.id,
            AuditLog.action.in_(OPERATIONAL_AUDIT_ACTIONS),
        )
        .scalar()
    )
    return latest or lead.created_at or datetime.utcnow()


def default_permissions_for_role(role):
    defaults = {
        Role.ADMIN: {
            "can_view_all": True,
            "can_view_all_franchises": False,
            "can_upload": True,
            "can_create_leads": True,
            "can_manage_users": True,
            "can_edit_core": True,
            "can_edit_contact": True,
            "can_edit_status": True,
            "can_edit_posicao": True,
            "can_edit_atuacoes": True,
            "can_bulk_manage_leads": True,
            "can_bulk_export_leads": True,
            "can_bulk_transfer_leads": True,
            "can_bulk_edit_status": True,
            "can_bulk_edit_posicao": True,
            "can_bulk_inactivate_leads": True,
            "can_bulk_reactivate_leads": True,
            "can_bulk_delete_leads": True,
            "can_export_leads": True,
            "can_transfer_leads": True,
            "can_move_workflows": True,
            "can_move_workflows_backwards": True,
            "can_delete_leads": True,
            "can_deactivate_leads": True,
            "can_reactivate_leads": True,
            "can_conclude_contract": True,
            "can_reject_contract": True,
            "can_undo_contract_status": True,
            "can_act_as_support": False,
            "can_request_support": True,
            "can_request_reversal": True,
            "can_work_reversal": True,
            "can_manage_reversals": True,
            "can_view_audit": True,
            "can_manage_settings": True,
            "can_manage_workflows": True,
            "can_manage_roles": True,
            "can_manage_teams": True,
        },
        Role.SUPERVISOR: {
            "can_view_all": False,
            "can_view_all_franchises": False,
            "can_upload": False,
            "can_create_leads": False,
            "can_manage_users": False,
            "can_edit_core": False,
            "can_edit_contact": True,
            "can_edit_status": True,
            "can_edit_posicao": True,
            "can_edit_atuacoes": True,
            "can_bulk_manage_leads": False,
            "can_bulk_export_leads": True,
            "can_bulk_transfer_leads": True,
            "can_bulk_edit_status": True,
            "can_bulk_edit_posicao": True,
            "can_bulk_inactivate_leads": True,
            "can_bulk_reactivate_leads": True,
            "can_bulk_delete_leads": False,
            "can_export_leads": True,
            "can_transfer_leads": True,
            "can_move_workflows": True,
            "can_move_workflows_backwards": False,
            "can_delete_leads": False,
            "can_deactivate_leads": True,
            "can_reactivate_leads": True,
            "can_conclude_contract": True,
            "can_reject_contract": True,
            "can_undo_contract_status": True,
            "can_act_as_support": False,
            "can_request_support": True,
            "can_request_reversal": True,
            "can_work_reversal": True,
            "can_manage_reversals": True,
            "can_view_audit": False,
            "can_manage_settings": False,
            "can_manage_workflows": False,
            "can_manage_roles": False,
            "can_manage_teams": False,
        },
        Role.OPERADOR: {
            "can_view_all": False,
            "can_view_all_franchises": False,
            "can_upload": False,
            "can_create_leads": False,
            "can_manage_users": False,
            "can_edit_core": False,
            "can_edit_contact": True,
            "can_edit_status": False,
            "can_edit_posicao": False,
            "can_edit_atuacoes": True,
            "can_bulk_manage_leads": False,
            "can_bulk_export_leads": True,
            "can_bulk_transfer_leads": False,
            "can_bulk_edit_status": False,
            "can_bulk_edit_posicao": False,
            "can_bulk_inactivate_leads": False,
            "can_bulk_reactivate_leads": False,
            "can_bulk_delete_leads": False,
            "can_export_leads": True,
            "can_transfer_leads": False,
            "can_move_workflows": True,
            "can_move_workflows_backwards": False,
            "can_delete_leads": False,
            "can_deactivate_leads": False,
            "can_reactivate_leads": False,
            "can_conclude_contract": False,
            "can_reject_contract": False,
            "can_undo_contract_status": False,
            "can_act_as_support": False,
            "can_request_support": False,
            "can_request_reversal": True,
            "can_work_reversal": True,
            "can_manage_reversals": False,
            "can_view_audit": False,
            "can_manage_settings": False,
            "can_manage_workflows": False,
            "can_manage_roles": False,
            "can_manage_teams": False,
        },
    }
    return defaults.get(role, defaults[Role.OPERADOR]).copy()


def support_commercial_permissions():
    permissions = default_permissions_for_role(Role.OPERADOR)
    permissions.update(
        {
            "can_act_as_support": True,
            "can_request_support": False,
            "can_move_workflows": False,
            "can_move_workflows_backwards": False,
            "can_transfer_leads": False,
            "can_edit_contact": False,
            "can_edit_atuacoes": True,
            "can_bulk_export_leads": False,
            "can_export_leads": False,
            "can_request_reversal": False,
            "can_work_reversal": False,
            "can_manage_reversals": False,
        }
    )
    return permissions


def manager_permissions():
    permissions = default_permissions_for_role(Role.ADMIN)
    permissions.update(
        {
            "can_manage_users": False,
            "can_manage_roles": False,
            "can_delete_leads": False,
            "can_bulk_delete_leads": False,
        }
    )
    return permissions


def seed_default_teams():
    company, franchise = ensure_default_company_structure()
    names = {team for team, _ in OPERATOR_SEEDS}
    names.update(
        row[0]
        for row in db.session.query(User.team).distinct().all()
        if row[0]
    )
    names.update(
        row[0]
        for row in db.session.query(Lead.equipe).filter(Lead.deleted_at.is_(None)).distinct().all()
        if row[0]
    )
    for name in sorted(names):
        team = Team.query.filter(func.lower(Team.name) == name.lower()).first()
        if not team:
            db.session.add(
                Team(
                    company_id=company.id,
                    franchise_id=franchise.id,
                    name=name,
                    active=True,
                )
            )
        else:
            team.company_id = team.company_id or company.id
            team.franchise_id = team.franchise_id or franchise.id
    db.session.commit()


def seed_default_reversal_configs():
    for company in Company.query.order_by(Company.id.asc()).all():
        setting = ReversalSettings.query.filter(
            ReversalSettings.company_id == company.id,
            ReversalSettings.franchise_id.is_(None),
        ).first()
        if not setting:
            db.session.add(
                ReversalSettings(
                    company_id=company.id,
                    franchise_id=None,
                    enabled=True,
                    default_distribution_mode=ReversalDistributionMode.MANUAL,
                    min_note_chars=40,
                    sla_30_days_hours=48,
                    sla_20_days_hours=24,
                    sla_10_days_hours=10,
                    standard_days_threshold=30,
                    attention_days_threshold=20,
                    urgent_days_threshold=10,
                    show_reversor_to_owner=True,
                )
            )
        elif setting.min_note_chars in (None, 100):
            setting.min_note_chars = 40

    for team in Team.query.order_by(Team.id.asc()).all():
        if not team.name:
            continue
        config = ReversalTeamConfig.query.filter_by(team_id=team.id).first()
        if config:
            config.company_id = config.company_id or team.company_id
            config.franchise_id = config.franchise_id or team.franchise_id
            config.team_name = config.team_name or team.name
            continue
        db.session.add(
            ReversalTeamConfig(
                company_id=team.company_id,
                franchise_id=team.franchise_id,
                team_id=team.id,
                team_name=team.name,
                enabled=True,
                distribution_mode=ReversalDistributionMode.MANUAL,
            )
        )
    db.session.flush()


def seed_default_role_profiles():
    company_id = default_company().id
    defaults = [
        ("Admin", Role.ADMIN, "Acesso total ao CRM.", 1),
        ("Gestor", Role.SUPERVISOR, "Visao de gestao com acoes comerciais ampliadas.", 2),
        ("Supervisor", Role.SUPERVISOR, "Gestao da propria equipe, reversoes e atuacoes.", 3),
        ("Operador", Role.OPERADOR, "Atendimento da propria carteira, contato e atuacoes.", 4),
        ("Apoio Comercial", Role.OPERADOR, "Fila de apoio da equipe, tarefas e anotacoes operacionais.", 5),
    ]
    for name, base_role, description, sort_order in defaults:
        profile = RoleProfile.query.filter(func.lower(RoleProfile.name) == name.lower()).first()
        if not profile:
            profile = RoleProfile(name=name, company_id=company_id)
            db.session.add(profile)
        else:
            profile.company_id = profile.company_id or company_id
        profile.base_role = base_role
        profile.description = description
        profile.sort_order = sort_order
        profile.active = True
        if name == "Apoio Comercial":
            permissions = support_commercial_permissions()
        elif name == "Gestor":
            permissions = manager_permissions()
        else:
            permissions = default_permissions_for_role(base_role)
        for field, value in permissions.items():
            if name in {"Apoio Comercial", "Gestor"}:
                setattr(profile, field, value)
            elif not getattr(profile, field, False):
                setattr(profile, field, value)
    db.session.commit()


def seed_default_users(app):
    company, franchise = ensure_default_company_structure()
    users = [
        {
            "username": "admin",
            "email": next(iter(app.config["ADMIN_EMAILS"]), "admin@example.invalid"),
            "name": "Admin",
            "lead_name": None,
            "role": Role.ADMIN,
            "team": None,
            "password": seed_local_password(),
        },
        {
            "username": "supervisor.a",
            "email": "supervisor.a@example.invalid",
            "name": "Equipe Demo A",
            "lead_name": None,
            "role": Role.SUPERVISOR,
            "team": "Equipe Demo A",
            "password": seed_local_password(),
        },
        {
            "username": "supervisor.b",
            "email": "supervisor.b@example.invalid",
            "name": "Equipe Demo B",
            "lead_name": None,
            "role": Role.SUPERVISOR,
            "team": "Equipe Demo B",
            "password": seed_local_password(),
        },
    ]
    users.extend(operator_seed_payload(team, name) for team, name in OPERATOR_SEEDS)
    reset_seed_passwords = env_truthy("RESET_SEED_PASSWORDS")

    for data in users:
        data["company_id"] = data.get("company_id") or company.id
        data["franchise_id"] = data.get("franchise_id") or franchise.id
        user = find_seed_user(data)
        profile = default_role_profile_for_role(data["role"])
        if not user:
            password = data.pop("password")
            user = User(**data)
            user.role_profile_id = profile.id if profile else None
            user.set_password(password)
            apply_role_profile_to_user(user, profile) if profile else user.apply_role_defaults()
            db.session.add(user)
            continue

        if user.username != data["username"] and not User.query.filter_by(username=data["username"]).first():
            user.username = data["username"]
        user.email = data["email"] if not data["email"].endswith("@local.test") else user.email or data["email"]
        user.name = data["name"]
        user.lead_name = data.get("lead_name")
        user.team = data["team"]
        user.role = Role.OPERADOR if user.role == "CONSULTOR" else data["role"]
        user.company_id = user.company_id or data["company_id"]
        user.franchise_id = user.franchise_id or data["franchise_id"]
        if profile and not user.role_profile_id:
            user.role_profile_id = profile.id
        if data.get("password") and (reset_seed_passwords or not user.password_hash):
            user.set_password(data["password"])
        apply_role_profile_to_user(user, profile) if profile else user.apply_role_defaults()

    db.session.commit()


def default_role_profile_for_role(role):
    default_names = {
        Role.ADMIN: "Admin",
        Role.SUPERVISOR: "Supervisor",
        Role.OPERADOR: "Operador",
    }
    name = default_names.get(role)
    if not name:
        return None
    return scoped_company_query(
        RoleProfile.query.filter(func.lower(RoleProfile.name) == name.lower()),
        RoleProfile,
        request_user_or_none(),
        include_unscoped=True,
    ).first()


def operator_seed_payload(team, name):
    email_local = email_name_from_person(name)
    return {
        "username": email_local,
        "email": f"{email_local}@example.invalid",
        "name": name,
        "lead_name": name,
        "role": Role.OPERADOR,
        "team": team,
        "password": seed_local_password(),
    }


def email_name_from_person(name):
    parts = [slugify_name_part(part) for part in name.split() if slugify_name_part(part)]
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}.{parts[-1]}"


def first_name_password(name):
    return slugify_name_part(name.split()[0])


def seed_local_password(_default_password=None):
    """Demo accounts require an explicitly chosen initial password."""
    password = os.environ.get("INITIAL_LOCAL_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "Defina INITIAL_LOCAL_PASSWORD para criar as contas ficticias de demonstracao."
        )
    validate_local_password(password)
    return password



def validate_local_password(password):
    if not password:
        return

    normalized = password.strip().lower()
    common_passwords = {
        "12345678",
        "123456789",
        "password",
        "senha123",
        "admin123",
        "qwerty123",
    }
    if len(password) < 8 or normalized in common_passwords:
        raise ValueError("A senha local precisa ter pelo menos 8 caracteres e não pode ser uma senha comum.")


def slugify_name_part(value):
    without_accents = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in without_accents if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def find_seed_user(data):
    user = User.query.filter_by(username=data["username"]).first()
    if user:
        return user
    if data.get("email"):
        user = User.query.filter(func.lower(User.email) == data["email"].lower()).first()
        if user:
            return user
    return User.query.filter(func.lower(User.name) == data["name"].lower()).first()


def request_remote_addr():
    remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    return remote_addr.split(",", 1)[0].strip()


def login_attempt_key(username):
    return f"{request_remote_addr()}:{(username or '').lower()}"


def prune_login_attempts(key):
    now = datetime.now()
    attempts = [
        attempt_at
        for attempt_at in LOGIN_ATTEMPTS.get(key, [])
        if now - attempt_at < LOGIN_ATTEMPT_WINDOW
    ]
    if attempts:
        LOGIN_ATTEMPTS[key] = attempts
    else:
        LOGIN_ATTEMPTS.pop(key, None)
    return attempts


def is_login_rate_limited(username):
    key = login_attempt_key(username)
    return len(prune_login_attempts(key)) >= MAX_LOGIN_ATTEMPTS


def record_login_failure(username):
    key = login_attempt_key(username)
    attempts = prune_login_attempts(key)
    attempts.append(datetime.now())
    LOGIN_ATTEMPTS[key] = attempts


def clear_login_failures(username):
    LOGIN_ATTEMPTS.pop(login_attempt_key(username), None)


def record_successful_login(user, method):
    user.last_login_at = datetime.now()
    user.last_login_method = method
    user.last_login_ip = request_remote_addr()
    log_audit(
        None,
        user,
        "user_login",
        details={
            "method": method,
            "ip": user.last_login_ip,
        },
    )
    db.session.commit()


def register_routes(app):
    @app.route("/")
    def index():
        return redirect(url_for("dashboard" if g.user else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            if is_login_rate_limited(username):
                flash("Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.", "danger")
                return (
                    render_template(
                        "login.html",
                        google_auth_available=app.config["GOOGLE_AUTH_AVAILABLE"],
                    ),
                    429,
                )

            user = User.query.filter(
                (func.lower(User.username) == username) | (func.lower(User.email) == username)
            ).first()

            if user and user.active and user.check_password(password):
                clear_login_failures(username)
                session.clear()
                session.permanent = True
                session["user_id"] = user.id
                session["_csrf_token"] = secrets.token_urlsafe(32)
                record_successful_login(user, "local")
                return redirect(url_for("dashboard"))

            record_login_failure(username)
            flash("Usuário ou senha inválidos.", "danger")

        return render_template(
            "login.html",
            google_auth_available=app.config["GOOGLE_AUTH_AVAILABLE"],
        )

    @app.route("/auth/google")
    def google_login():
        if not app.config["GOOGLE_AUTH_AVAILABLE"]:
            flash("Google Auth ainda não foi configurado neste ambiente.", "warning")
            return redirect(url_for("login"))

        redirect_uri = public_url_for(app, "google_callback")
        return oauth.google.authorize_redirect(redirect_uri)

    @app.route("/auth/google/callback")
    def google_callback():
        if not app.config["GOOGLE_AUTH_AVAILABLE"]:
            flash("Google Auth ainda não foi configurado neste ambiente.", "warning")
            return redirect(url_for("login"))

        token = oauth.google.authorize_access_token()
        profile = token.get("userinfo") or oauth.google.get("userinfo").json()
        user, access_status = find_or_request_google_user(
            profile,
            app.config["ADMIN_EMAILS"],
            app.config["GOOGLE_SIGNUP_ALLOWED_DOMAINS"],
        )

        if not user or not user.active:
            access_messages = {
                "pending": "Sua solicitação de acesso foi enviada. Aguarde a aprovação de um administrador.",
                "rejected": "Sua solicitação de acesso foi recusada. Fale com um administrador se precisar revisar.",
                "not_allowed": "Esse domínio de e-mail não está liberado para solicitar acesso ao CRM.",
            }
            flash(access_messages.get(access_status, "Seu e-mail Google não está liberado no CRM."), "warning")
            return redirect(url_for("login"))

        session.clear()
        session.permanent = True
        session["user_id"] = user.id
        session["_csrf_token"] = secrets.token_urlsafe(32)
        record_successful_login(user, "google")
        return redirect(url_for("dashboard"))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        session.clear()
        flash("Sessão encerrada.", "info")
        return redirect(url_for("login"))

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    def account_password():
        must_change = user_must_change_password(g.user)
        authenticated_with_google = g.user.last_login_method == "google"
        requires_current_password = bool(g.user.password_hash and not (must_change and authenticated_with_google))

        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            try:
                if new_password != confirm_password:
                    raise ValueError("A confirmação da nova senha não confere.")
                validate_local_password(new_password)
                if requires_current_password and not g.user.check_password(current_password):
                    raise ValueError("Senha atual incorreta.")
                if g.user.check_password(new_password):
                    raise ValueError("A nova senha precisa ser diferente da senha atual.")

                was_forced = bool(g.user.must_change_password)
                before = user_security_snapshot(g.user)
                g.user.set_password(new_password, must_change=False)
                after = user_security_snapshot(g.user)
                log_audit(
                    None,
                    g.user,
                    "user_password_change",
                    details={
                        "target_user_id": g.user.id,
                        "target_user_email": g.user.email,
                        "self_service": True,
                        "was_forced": was_forced,
                        "changes": snapshot_changes(before, after),
                    },
                )
                db.session.commit()
                flash("Senha local atualizada.", "success")
                return redirect(url_for("dashboard"))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

        return render_template(
            "account_password.html",
            current_user=g.user,
            must_change=must_change,
            requires_current_password=requires_current_password,
        )

    @app.route("/dashboard")
    @login_required
    def dashboard():
        sync_unlock_workflow_stages()
        filters = current_dashboard_filters()
        sanitize_franchise_filter(filters, g.user)
        if filters["state"] != "active" and not user_can_view_inactive_leads(g.user):
            filters["state"] = "active"
        base_query = visible_leads_query(g.user, state=filters["state"])
        filtered_query = apply_dashboard_filters(base_query, filters)
        total_count = filtered_query.count()
        pagination = build_pagination(filters, total_count)
        filters["page"] = pagination["page"]
        leads = (
            apply_dashboard_sort(filtered_query, filters)
            .offset(pagination["offset"])
            .limit(pagination["per_page"])
            .all()
        )
        latest_activity_audits = latest_activity_audits_for_leads(leads)
        latest_activities_by_lead = latest_activities_for_leads(leads)
        dashboard_workflows_by_lead = operational_lead_workflows_for_leads(leads)
        if ensure_scheduled_contact_tasks_for_workflows(dashboard_workflows_by_lead.values()):
            db.session.commit()
        latest_reversal_tasks_by_lead = latest_reversal_tasks_for_leads(leads)
        activity_alerts = activity_alerts_for_leads(leads, latest_activity_audits)
        unlock_alerts = unlock_alerts_for_leads(leads)
        return render_template(
            "dashboard.html",
            leads=leads,
            latest_activity_audits=latest_activity_audits,
            latest_activities_by_lead=latest_activities_by_lead,
            dashboard_workflows_by_lead=dashboard_workflows_by_lead,
            latest_reversal_tasks_by_lead=latest_reversal_tasks_by_lead,
            activity_alerts=activity_alerts,
            unlock_alerts=unlock_alerts,
            filters=filters,
            pagination=pagination,
            page_size_options=PAGE_SIZE_OPTIONS,
            dashboard_order_options=DASHBOARD_ORDER_OPTIONS,
            total_count=total_count,
            active_filter_count=active_filter_count(filters),
            current_user=g.user,
            Role=Role,
            franchises=franchise_filter_options(g.user),
            teams=dashboard_team_filter_options(g.user, filters["state"]),
            consultores=dashboard_consultor_filter_options(g.user, filters["equipe"], filters["state"]),
            status_options=get_active_options("status"),
            reversal_status_options=reversal_status_filter_options(),
            loss_reason_options=get_active_options("motivo_perda"),
            status_option_styles=get_option_style_map("status"),
            reversal_status_option_styles=reversal_status_option_styles(),
            reversal_status_label=reversal_status_label,
            empty_filter_value=FILTER_EMPTY_VALUE,
            show_bulk_actions=user_has_any_bulk_action(g.user),
            bulk_transfer_teams=get_teams_for_create(g.user) if g.user.can_bulk_transfer_leads else [],
            bulk_transfer_consultores_by_team=(
                get_consultores_by_team_for_create(g.user, get_teams_for_create(g.user))
                if g.user.can_bulk_transfer_leads
                else {}
            ),
            dashboard_url=dashboard_url,
            sort_direction_for=lambda column: sort_direction_for(filters, column),
            upload_franchises=get_company_franchises(g.user) if user_can_view_all_franchises(g.user) else [],
            selected_upload_franchise_id=franchise_id_for_user(g.user),
        )

    @app.route("/workflows")
    @login_required
    def workflows_board():
        sync_unlock_workflow_stages()
        ensure_support_assignments()
        filters = workflow_board_filters()
        sanitize_franchise_filter(filters, g.user)
        kanban_moves_enabled = workflow_kanban_moves_enabled()
        workflows = (
            Workflow.query.filter(Workflow.active.is_(True))
            .order_by(Workflow.sort_order.asc(), Workflow.name.asc())
            .all()
        )
        selected_workflow = selected_workflow_for_board(workflows, filters)
        stages = []
        cards_by_stage = {}
        stage_counts = {}
        stage_column_stats = {}
        workflow_alerts = {}

        if selected_workflow:
            stages = (
                WorkflowStage.query.filter_by(workflow_id=selected_workflow.id, active=True)
                .order_by(WorkflowStage.sort_order.asc(), WorkflowStage.id.asc())
                .all()
            )
            cards_by_stage = {stage.id: [] for stage in stages}
            query = apply_workflow_board_filters(
                visible_lead_workflows_query(g.user, selected_workflow).join(
                    WorkflowStage,
                    LeadWorkflow.current_stage_id == WorkflowStage.id,
                ),
                filters,
            )
            lead_workflows = (
                query.options(
                    joinedload(LeadWorkflow.lead),
                    joinedload(LeadWorkflow.current_stage),
                    joinedload(LeadWorkflow.owner_user),
                    joinedload(LeadWorkflow.assigned_user),
                )
                .order_by(WorkflowStage.sort_order.asc(), LeadWorkflow.entered_stage_at.desc(), LeadWorkflow.id.desc())
                .all()
            )
            if ensure_scheduled_contact_tasks_for_workflows(lead_workflows):
                db.session.commit()
            workflow_leads = [lead_workflow.lead for lead_workflow in lead_workflows]
            operational_workflows_by_lead = operational_lead_workflows_for_leads(workflow_leads)
            unlock_alerts_by_lead = unlock_alerts_for_leads(workflow_leads)
            latest_activities_by_lead = latest_activities_for_leads(workflow_leads)
            latest_reversal_tasks_by_lead = latest_reversal_tasks_for_leads(workflow_leads)
            for lead_workflow in lead_workflows:
                cards_by_stage.setdefault(lead_workflow.current_stage_id, []).append(lead_workflow)
                alert = workflow_alert_for(lead_workflow, unlock_alerts_by_lead.get(lead_workflow.lead_id))
                if alert:
                    workflow_alerts[lead_workflow.id] = alert
            workflow_card_actions = workflow_card_actions_for_board(lead_workflows, g.user)
            stage_counts = {stage.id: len(cards_by_stage.get(stage.id, [])) for stage in stages}
            stage_column_stats = {
                stage.id: workflow_column_stats(
                    cards_by_stage.get(stage.id, []),
                    workflow_alerts,
                    latest_reversal_tasks_by_lead,
                )
                for stage in stages
            }
        else:
            workflow_card_actions = {}
            operational_workflows_by_lead = {}
            latest_activities_by_lead = {}
            latest_reversal_tasks_by_lead = {}

        return render_template(
            "workflows.html",
            workflows=workflows,
            selected_workflow=selected_workflow,
            stages=stages,
            cards_by_stage=cards_by_stage,
            stage_counts=stage_counts,
            stage_column_stats=stage_column_stats,
            workflow_alerts=workflow_alerts,
            workflow_card_actions=workflow_card_actions,
            operational_workflows_by_lead=operational_workflows_by_lead,
            unlock_days_left=unlock_days_left,
            latest_activities_by_lead=latest_activities_by_lead,
            filters=filters,
            current_user=g.user,
            Role=Role,
            kanban_moves_enabled=kanban_moves_enabled,
            franchises=franchise_filter_options(g.user),
            teams=dashboard_team_filter_options(g.user),
            consultores=dashboard_consultor_filter_options(g.user, filters["equipe"]),
            status_options=get_active_options("status"),
            reversal_status_options=reversal_status_filter_options(),
            empty_filter_value=FILTER_EMPTY_VALUE,
            lead_workflow_requires_support=lead_workflow_requires_support,
            workflow_support_filter_match=workflow_support_filter_match,
            workflow_reversal_filter_match=workflow_reversal_filter_match,
            support_decision_enabled_for_workflow=support_decision_enabled_for_workflow,
            support_executor_label=support_executor_label,
            support_executor_display_label=support_executor_display_label,
            status_option_styles=get_option_style_map("status"),
            latest_reversal_tasks_by_lead=latest_reversal_tasks_by_lead,
            reversal_status_option_styles=reversal_status_option_styles(),
            reversal_status_label=reversal_status_label,
        )

    @app.route("/support")
    @login_required
    def support_queue():
        sync_unlock_workflow_stages()
        ensure_support_assignments()
        restore_returned_support_assignments()
        filters = support_queue_filters()
        tasks = (
            visible_support_tasks_query(g.user)
            .order_by(
                LeadWorkflow.support_assigned_at.asc(),
                LeadWorkflow.entered_stage_at.asc(),
                LeadWorkflow.id.asc(),
            )
            .all()
        )
        if sync_support_overdue_notifications(tasks):
            db.session.commit()
        support_alerts = {task.id: workflow_support_alert(task) for task in tasks}
        support_states = {task.id: support_task_state(task, support_alerts.get(task.id)) for task in tasks}
        support_deadlines = {task.id: support_task_deadline(task) for task in tasks}
        filtered_tasks = filter_support_queue_tasks(tasks, filters)
        columns = support_queue_columns(filtered_tasks, support_states, support_deadlines)
        return render_template(
            "support_queue.html",
            tasks=filtered_tasks,
            total_tasks=len(tasks),
            filters=filters,
            columns=columns,
            support_alerts=support_alerts,
            support_notifications=recent_support_notifications_for_user(g.user),
            support_states=support_states,
            support_deadlines=support_deadlines,
            current_user=g.user,
            can_request_support_for_workflow=can_request_support_for_workflow,
            can_send_support_for_workflow=can_send_support_for_workflow,
            support_task_can_be_completed_by=support_task_can_be_completed_by,
            support_actor_for_workflow=support_actor_for_workflow,
            support_task_status_label=support_task_status_label,
        )

    @app.route("/support/notifications/<int:notification_id>/read", methods=["POST"])
    @login_required
    def read_support_notification(notification_id):
        notification = db.session.get(SupportNotification, notification_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("support_queue")
        if not notification or notification.recipient_user_id != g.user.id:
            flash("Notificacao nao encontrada para o seu usuario.", "warning")
            return redirect(next_url)
        if not notification.read_at:
            notification.read_at = datetime.now()
            db.session.commit()
        return redirect(next_url)

    @app.route("/reversals")
    @login_required
    def reversal_queue():
        if not can_access_reversal_queue(g.user):
            flash("Seu usuario nao tem acesso a reversoes.", "danger")
            return redirect(url_for("dashboard"))

        if sync_reversal_overdue_statuses():
            db.session.commit()

        filters = reversal_queue_filters()
        sanitize_franchise_filter(filters, g.user)
        tasks = (
            visible_reversal_tasks_query(g.user)
            .filter(ReversalTask.status.in_(ReversalStatus.ACTIVE))
            .options(
                joinedload(ReversalTask.lead),
                joinedload(ReversalTask.assigned_user),
                joinedload(ReversalTask.owner_user),
                joinedload(ReversalTask.requested_by),
                joinedload(ReversalTask.franchise),
            )
            .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
            .all()
        )
        filtered_tasks = filter_reversal_queue_tasks(tasks, filters)
        columns = reversal_queue_columns(filtered_tasks)
        filter_options = reversal_filter_options(tasks)
        deadlines = {task.id: reversal_task_deadline(task) for task in filtered_tasks}
        workflows = (
            Workflow.query.filter(Workflow.active.is_(True))
            .order_by(Workflow.sort_order.asc(), Workflow.name.asc())
            .all()
        )

        return render_template(
            "reversals.html",
            workflows=workflows,
            tasks=filtered_tasks,
            total_tasks=len(tasks),
            reversal_notifications=recent_reversal_notifications_for_user(g.user),
            filters=filters,
            columns=columns,
            filter_options=filter_options,
            deadlines=deadlines,
            current_user=g.user,
            Role=Role,
            franchises=franchise_filter_options(g.user),
            can_distribute_reversal_queue=can_distribute_reversal_queue,
            can_assign_reversal_task=can_assign_reversal_task,
            can_work_reversal_task=can_work_reversal_task,
            reversal_assignment_options=reversal_assignment_options,
            reversal_status_label=reversal_status_label,
            reversal_priority_label=reversal_priority_label,
            reversal_distribution_mode_label=reversal_distribution_mode_label,
            reversal_tabulation_label=reversal_tabulation_label,
            reversal_result_options=reversal_result_options,
            reversal_min_note_chars=reversal_min_note_chars,
        )

    @app.route("/reversals/notifications/<int:notification_id>/read", methods=["POST"])
    @login_required
    def read_reversal_notification(notification_id):
        notification = db.session.get(ReversalNotification, notification_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("reversal_queue")
        if not notification or notification.recipient_user_id != g.user.id:
            flash("Notificacao nao encontrada para o seu usuario.", "warning")
            return redirect(next_url)
        if not notification.read_at:
            notification.read_at = datetime.now()
            db.session.commit()
        return redirect(next_url)

    @app.route("/leads/<int:lead_id>")
    @login_required
    def lead_detail(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead não encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))

        if sync_lead_workflow_due_dates_for_lead(lead):
            db.session.commit()
        if clear_automatic_executor_defaults_for_workflow(primary_lead_workflow_for_lead(lead, create_missing=False)):
            db.session.commit()
        if repair_support_state_for_lead(lead):
            db.session.commit()
        all_lead_workflows = lead_workflows_for_detail(lead)
        lead_workflows, workflow_changed = operational_lead_workflows_for_detail(lead, all_lead_workflows)
        if workflow_changed:
            db.session.commit()
            all_lead_workflows = lead_workflows_for_detail(lead)
            lead_workflows, _ = operational_lead_workflows_for_detail(lead, all_lead_workflows)
        if ensure_scheduled_contact_tasks_for_workflows(all_lead_workflows):
            db.session.commit()
        current_lead_workflow = lead_workflows[0] if lead_workflows else None
        unlock_executor_workflow = unlock_executor_workflow_for_lead(lead, all_lead_workflows)
        unlock_alert = unlock_alert_for_lead(lead, all_lead_workflows)
        user_can_view_lead_audit = can_view_lead_audit(g.user)
        active_reversal_task = active_reversal_task_for_lead(lead)
        latest_reversal_task = latest_reversal_task_for_lead(lead)
        reversal_only_view = lead_access_is_reversal_only(g.user, lead)
        if reversal_only_view:
            user_can_view_lead_audit = False
        reversal_candidate_count = len(reversal_available_users_for_lead(lead))
        workflow_alerts_by_id = {
            item.id: workflow_alert_for(item, unlock_alert)
            for item in lead_workflows
        }
        lead_workflow_ids = [item.id for item in lead_workflows]
        history_lead_workflow_ids = [
            item.id
            for item in all_lead_workflows
            if item.workflow
            and item.workflow.slug in {DEFAULT_LEAD_WORKFLOW_SLUG, "fidelizacao", UNLOCK_WORKFLOW_SLUG}
        ]
        back_url = safe_next_url(request.args.get("back")) or url_for("dashboard")
        lead_self_url = url_for("lead_detail", lead_id=lead.id, back=back_url)
        return render_template(
            "lead_detail.html",
            lead=lead,
            current_user=g.user,
            lead_workflows=lead_workflows,
            current_lead_workflow=current_lead_workflow,
            unlock_executor_workflow=unlock_executor_workflow,
            unlock_alert=unlock_alert,
            workflow_alerts_by_id=workflow_alerts_by_id,
            workflow_stage_tracks=workflow_stage_tracks_for_detail(lead_workflows, workflow_alerts_by_id),
            workflow_rules_by_id=workflow_rules_for_detail(lead_workflows),
            can_move_lead_workflow=can_move_lead_workflow,
            can_move_lead_workflow_backwards=can_move_lead_workflow_backwards,
            can_update_lead_workflow_checklist=can_update_lead_workflow_checklist,
            can_request_support_for_workflow=can_request_support_for_workflow,
            support_return_resolution_pending=support_return_resolution_pending,
            can_resolve_support_return=can_resolve_support_return,
            support_task_can_be_completed_by=support_task_can_be_completed_by,
            support_actor_for_workflow=support_actor_for_workflow,
            lead_workflow_requires_support=lead_workflow_requires_support,
            can_set_unlock_executor=can_set_unlock_executor,
            unlock_executor_label=unlock_executor_label,
            unlock_executor_display_label=unlock_executor_display_label,
            unlock_executor_select_options=unlock_executor_select_options,
            can_set_support_executor=can_set_support_executor,
            support_executor_label=support_executor_label,
            support_executor_display_label=support_executor_display_label,
            support_executor_select_options=support_executor_select_options,
            support_decision_prompt=support_decision_prompt,
            SUPPORT_EXECUTOR_SELLER=SUPPORT_EXECUTOR_SELLER,
            SUPPORT_EXECUTOR_SUPPORT=SUPPORT_EXECUTOR_SUPPORT,
            UNLOCK_EXECUTOR_SELLER=UNLOCK_EXECUTOR_SELLER,
            UNLOCK_EXECUTOR_SUPPORT=UNLOCK_EXECUTOR_SUPPORT,
            workflow_support_alert=workflow_support_alert,
            activity_timeline=lead_activity_timeline(lead),
            operational_history=lead_operational_history(lead, lead_workflow_ids=history_lead_workflow_ids),
            reversal_history=reversal_history_for_lead(lead),
            workflow_history=workflow_history_for_lead(lead, lead_workflow_ids=history_lead_workflow_ids),
            workflow_transition_label=workflow_transition_label,
            field_audits=latest_field_audits(lead) if user_can_view_lead_audit else {},
            audit_events=recent_lead_audit_events(lead) if user_can_view_lead_audit else [],
            can_view_lead_audit=user_can_view_lead_audit,
            action_label=audit_action_label,
            field_label=audit_field_label,
            active_reversal_task=active_reversal_task,
            latest_reversal_task=latest_reversal_task,
            reversal_only_view=reversal_only_view,
            reversal_candidate_count=reversal_candidate_count,
            can_request_lead_reversal=can_request_lead_reversal(g.user, lead),
            can_cancel_active_reversal=can_cancel_reversal_task(g.user, active_reversal_task),
            can_distribute_reversal_queue=can_distribute_reversal_queue,
            can_assign_reversal_task=can_assign_reversal_task,
            can_work_reversal_task=can_work_reversal_task,
            reversal_assignment_options=reversal_assignment_options,
            reversal_status_label=reversal_status_label,
            reversal_priority_label=reversal_priority_label,
            reversal_distribution_mode_label=reversal_distribution_mode_label,
            reversal_result_sentiment_label=reversal_result_sentiment_label,
            reversal_tabulation_label=reversal_tabulation_label,
            reversal_tabulation_options=reversal_tabulation_options(),
            reversal_result_options=reversal_result_options,
            reversal_min_note_chars=reversal_min_note_chars,
            status_options=get_active_options("status"),
            status_option_styles=get_option_style_map("status"),
            reversal_status_option_styles=reversal_status_option_styles(),
            post_sale_completion_state=post_sale_completion_state(lead),
            can_conclude_contract=can_conclude_lead_contract(lead),
            can_user_conclude_contract=can_user_conclude_lead_contract(g.user, lead),
            can_undo_contract_status=can_undo_lead_contract_status(lead),
            can_transfer_current_lead=can_transfer_lead(g.user, lead),
            can_edit_lead_cpf=can_edit_protected_lead_cpf(g.user),
            transfer_teams=get_transfer_team_options(g.user, lead),
            transfer_consultores=get_transfer_consultor_options(g.user, lead),
            transfer_consultores_by_team=get_transfer_consultores_by_team(g.user, lead),
            loss_reason_options=get_active_options("motivo_perda"),
            back_url=back_url,
            lead_self_url=lead_self_url,
        )

    @app.route("/leads/new", methods=["GET", "POST"])
    @login_required
    @permission_required("can_create_leads")
    def create_lead():
        if request.method == "POST":
            try:
                lead = build_manual_lead(request.form, g.user)
                initial_activity_body = clean_text(request.form.get("initial_activity")) if g.user.can_edit_atuacoes else None
                db.session.add(lead)
                db.session.flush()
                ensure_lead_default_workflows(lead)
                sync_sale_entry_workflow_for_lead(lead, user=g.user)
                sync_lead_workflow_due_dates_for_lead(lead)
                ensure_legacy_lead_activities_for_lead(lead, fallback_user=g.user)
                if initial_activity_body:
                    create_initial_lead_activity(lead, g.user, initial_activity_body)
                log_audit(lead, g.user, "create", details={"origin": "manual"})
                db.session.commit()
                flash("Cliente criado com sucesso.", "success")
                return redirect(url_for("lead_detail", lead_id=lead.id))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

        selected_franchise_id = target_franchise_id_from_form(request.values, g.user)
        teams_for_create = get_teams_for_create(g.user, franchise_id=selected_franchise_id)
        selected_team = lead_team_for_form(g.user, request.form.get("equipe"), franchise_id=selected_franchise_id)
        create_franchises = get_company_franchises(g.user) if user_can_view_all_franchises(g.user) else []
        teams_by_franchise = {
            str(franchise.id): get_teams_for_create(g.user, franchise_id=franchise.id)
            for franchise in create_franchises
        }
        consultores_by_franchise_team = {
            str(franchise.id): get_consultores_by_team_for_create(
                g.user,
                teams_by_franchise.get(str(franchise.id), []),
                franchise_id=franchise.id,
            )
            for franchise in create_franchises
        }
        return render_template(
            "lead_create.html",
            current_user=g.user,
            franchises=create_franchises,
            selected_franchise_id=selected_franchise_id,
            teams=teams_for_create,
            teams_by_franchise=teams_by_franchise,
            consultores=get_consultores_for_create(g.user, selected_team, franchise_id=selected_franchise_id),
            consultores_by_team=get_consultores_by_team_for_create(
                g.user,
                teams_for_create,
                franchise_id=selected_franchise_id,
            ),
            consultores_by_franchise_team=consultores_by_franchise_team,
            assigned_consultor=lead_assignment_name(g.user),
            selected_team=selected_team,
            status_options=get_active_options("status"),
        )

    @app.route("/upload", methods=["POST"])
    @login_required
    @permission_required("can_upload")
    def upload_excel():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Selecione um arquivo .xlsx.", "warning")
            return redirect(url_for("dashboard"))

        filename = secure_filename(file.filename)
        extension = os.path.splitext(filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            flash("Formato inválido. Envie apenas arquivos .xlsx.", "danger")
            return redirect(url_for("dashboard"))

        try:
            target_franchise_id = target_franchise_id_from_form(request.form, g.user)
            df = read_crm_dataframe(file)
            validate_columns(df)
            import_batch = ImportBatch(
                company_id=company_id_for_user(g.user),
                franchise_id=target_franchise_id,
                user_id=g.user.id,
                filename=filename,
                status="processing",
                created_at=datetime.now(),
            )
            db.session.add(import_batch)
            db.session.flush()
            result = import_leads_from_dataframe(
                df,
                user=g.user,
                import_batch=import_batch,
                franchise_id=target_franchise_id,
                commit=False,
            )
            import_batch.created_count = result["created"]
            import_batch.duplicate_count = result["duplicates"]
            import_batch.skipped_count = result["skipped"]
            import_batch.status = "success"
            import_batch.finished_at = datetime.now()
            log_audit(
                None,
                g.user,
                "import",
                import_batch=import_batch,
                details={"filename": filename, **result},
            )
            db.session.commit()
            flash(
                (
                    f"Upload concluído: {result['created']} novos clientes, "
                    f"{result['duplicates']} duplicados, {result['skipped']} ignorados."
                ),
                "success",
            )
        except Exception as exc:
            db.session.rollback()
            import_batch = ImportBatch(
                company_id=company_id_for_user(g.user),
                franchise_id=franchise_id_for_user(g.user),
                user_id=g.user.id,
                filename=filename,
                status="error",
                message=str(exc),
                created_at=datetime.now(),
                finished_at=datetime.now(),
            )
            db.session.add(import_batch)
            log_audit(
                None,
                g.user,
                "import_error",
                import_batch=import_batch,
                details={"filename": filename, "error": str(exc)},
            )
            db.session.commit()
            flash("Erro ao processar Excel. Confira se o arquivo segue o modelo esperado.", "danger")

        return redirect(url_for("dashboard"))

    @app.route("/leads/bulk-maintenance", methods=["POST"])
    @login_required
    @permission_required(*BULK_ACTION_PERMISSION_FIELDS)
    def bulk_maintain_leads():
        raw_action = clean_text(request.form.get("action")) or ""
        action = normalize_maintenance_action(raw_action) or normalize_option_key(raw_action)
        lead_ids = parse_selected_lead_ids(request.form.getlist("lead_ids"))
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")

        if action not in BULK_ACTION_PERMISSIONS:
            flash("Acao em massa invalida.", "danger")
            return redirect(next_url)
        if not user_can_perform_bulk_action(g.user, action):
            flash("Seu usuario nao tem permissao para essa acao em massa.", "danger")
            return redirect(next_url)
        if not lead_ids:
            flash("Selecione pelo menos um cliente.", "warning")
            return redirect(next_url)
        try:
            if action == "export":
                leads = selected_leads_for_bulk(lead_ids, g.user)
                if not leads:
                    flash("Nenhum cliente selecionado esta disponivel para exportacao.", "warning")
                    return redirect(next_url)
                log_audit(
                    None,
                    g.user,
                    "lead_bulk_export",
                    details={"count": len(leads), "lead_ids": [lead.id for lead in leads]},
                )
                db.session.commit()
                return export_bulk_leads_csv(leads)

            if action in {"inactivate", "reactivate"}:
                result = apply_lead_maintenance_actions(
                    [
                        {
                            "cpf": None,
                            "lead_id": lead_id,
                            "action": action,
                            "reason": request.form.get("reason") or "Acao em massa",
                        }
                        for lead_id in lead_ids
                    ],
                    user=g.user,
                    source="selection",
                )
            elif action == "set_status":
                result = apply_bulk_status_update(lead_ids, request.form.get("status_value"), g.user)
            elif action == "transfer":
                result = apply_bulk_transfer(lead_ids, request.form, g.user)
            elif action == "delete":
                result = apply_bulk_delete(lead_ids, g.user)
            db.session.commit()
            flash(bulk_action_message(result), "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(next_url)

    @app.route("/leads/maintenance-import", methods=["POST"])
    @login_required
    @permission_required("can_bulk_inactivate_leads", "can_bulk_reactivate_leads")
    def import_lead_maintenance():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Selecione um arquivo .xlsx para manutenção.", "warning")
            return redirect(url_for("dashboard"))

        filename = secure_filename(file.filename)
        extension = os.path.splitext(filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            flash("Formato inválido. Envie apenas arquivos .xlsx.", "danger")
            return redirect(url_for("dashboard"))

        try:
            df = read_maintenance_dataframe(file)
            actions = maintenance_actions_from_dataframe(df)
            import_batch = ImportBatch(
                company_id=company_id_for_user(g.user),
                franchise_id=franchise_id_for_user(g.user),
                user_id=g.user.id,
                filename=filename,
                status="processing",
                created_at=datetime.now(),
            )
            db.session.add(import_batch)
            db.session.flush()
            result = apply_lead_maintenance_actions(
                actions,
                user=g.user,
                import_batch=import_batch,
                source="import",
            )
            import_batch.created_count = result["changed"]
            import_batch.duplicate_count = result["duplicates"]
            import_batch.skipped_count = result["skipped"]
            import_batch.status = "success"
            import_batch.message = (
                f"Manutenção: {result['changed']} alterados, {result['unchanged']} sem mudança, "
                f"{result['missing']} não encontrados, {result['invalid']} inválidos."
            )
            import_batch.finished_at = datetime.now()
            log_audit(
                None,
                g.user,
                "lead_maintenance_import",
                import_batch=import_batch,
                details={"filename": filename, **result},
            )
            db.session.commit()
            flash(import_batch.message, "success")
        except Exception as exc:
            db.session.rollback()
            import_batch = ImportBatch(
                company_id=company_id_for_user(g.user),
                franchise_id=franchise_id_for_user(g.user),
                user_id=g.user.id,
                filename=filename,
                status="error",
                message=str(exc),
                created_at=datetime.now(),
                finished_at=datetime.now(),
            )
            db.session.add(import_batch)
            log_audit(
                None,
                g.user,
                "lead_maintenance_import_error",
                import_batch=import_batch,
                details={"filename": filename, "error": str(exc)},
            )
            db.session.commit()
            flash("Erro ao processar manutenção. Confira as colunas CPF, Ação e Motivo.", "danger")

        return redirect(url_for("dashboard"))

    @app.route("/leads/<int:lead_id>/update", methods=["POST"])
    @login_required
    def update_lead(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead não encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if lead_access_is_reversal_only(g.user, lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(url_for("lead_detail", lead_id=lead.id))

        try:
            changes = update_allowed_fields(lead, request.form, editable_fields_for(g.user))
            transfer_change = transfer_lead_consultor(lead, request.form, g.user)
            for change in changes:
                log_audit(
                    lead,
                    g.user,
                    "update",
                    field_name=change["field_name"],
                    old_value=change["old_value"],
                    new_value=change["new_value"],
                )
                if change["field_name"] in ATUACAO_FIELDS:
                    sync_legacy_activity_from_change(lead, change["field_name"], g.user)
            if any(change["field_name"] == "concessao" for change in changes):
                sync_lead_workflow_due_dates_for_lead(lead)
                reset_unlock_completion_after_concessao_change(lead, g.user)
            if any(change["field_name"] in {"data_venda", "concessao"} for change in changes):
                sync_sale_entry_workflow_for_lead(lead, user=g.user)
            if transfer_change:
                log_audit(
                    lead,
                    g.user,
                    "lead_transfer",
                    field_name="consultor",
                    old_value=transfer_change["old_value"],
                    new_value=transfer_change["new_value"],
                    details=transfer_change["details"],
                )
                team_change = transfer_change.get("team_change")
                if team_change:
                    log_audit(
                        lead,
                        g.user,
                        "lead_transfer",
                        field_name="equipe",
                        old_value=team_change["old_value"],
                        new_value=team_change["new_value"],
                        details=transfer_change["details"],
                    )
            if any(change["field_name"] in ATUACAO_FIELDS for change in changes):
                ensure_lead_in_progress_status(lead, g.user, source="legacy_activity_update")
            db.session.commit()
            flash("Lead atualizado." if changes or transfer_change else "Nenhuma mudanÃ§a detectada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

        return redirect(safe_next_url(request.form.get("next")) or url_for("lead_detail", lead_id=lead.id))

    @app.route("/leads/<int:lead_id>/status", methods=["POST"])
    @login_required
    def update_lead_status(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead nÃ£o encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if lead_access_is_reversal_only(g.user, lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(url_for("lead_detail", lead_id=lead.id))

        next_url = safe_next_url(request.form.get("next")) or url_for("lead_detail", lead_id=lead.id)
        action = clean_text(request.form.get("status_action"))
        try:
            if action == "undo":
                if not g.user.can_undo_contract_status:
                    raise ValueError("Seu usuario nao tem permissao para desfazer status.")
                restored_status = undo_lead_contract_status(lead, g.user)
                db.session.commit()
                flash(f"Status desfeito. Cliente voltou para {restored_status}.", "success")
                return redirect(next_url)
            if action not in {"concluido", "recusado", "perdido"}:
                raise ValueError("Acao de status invalida.")
            if action == "concluido" and not can_user_conclude_lead_contract(g.user, lead):
                raise ValueError("Seu usuario nao pode concluir este contrato agora.")
            if action in {"recusado", "perdido"} and not g.user.can_reject_contract:
                raise ValueError("Seu usuario nao tem permissao para marcar contrato como recusado.")
            changed = set_lead_contract_status(
                lead,
                g.user,
                action,
                reason=request.form.get("status_reason"),
                note=request.form.get("status_note"),
            )
            db.session.commit()
            flash("Status do contrato atualizado." if changed else "Status ja estava atualizado.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/leads/<int:lead_id>/reversal", methods=["POST"])
    @login_required
    def request_lead_reversal(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))

        next_url = safe_next_url(request.form.get("next")) or url_for("lead_detail", lead_id=lead.id)
        try:
            task = create_reversal_task_for_lead(
                lead,
                g.user,
                note=request.form.get("reversal_note"),
                tabulation=request.form.get("reversal_tabulation"),
            )
            db.session.commit()
            if task.assigned_user:
                flash(f"Cliente enviado para reversao e atribuido para {task.assigned_user.name}.", "success")
            else:
                mode_label = reversal_distribution_mode_label(task.distribution_mode).lower()
                flash(f"Cliente enviado para reversao. Ficou aguardando {mode_label}.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/reversals/<int:task_id>/cancel", methods=["POST"])
    @login_required
    def cancel_reversal(task_id):
        task = db.session.get(ReversalTask, task_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")
        if not task or not task.lead or not can_view_lead(g.user, task.lead):
            flash("Reversao nao encontrada para o seu perfil.", "danger")
            return redirect(next_url)

        try:
            cancel_reversal_task(task, g.user, request.form.get("cancel_reason"))
            db.session.commit()
            flash("Reversao cancelada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/reversals/distribute", methods=["POST"])
    @login_required
    def distribute_reversals():
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")
        team_name = clean_text(request.form.get("team_name"))
        franchise_id = parse_int(request.form.get("franchise_id"), None)
        try:
            batch = distribute_manual_reversal_queue(team_name, g.user, franchise_id=franchise_id)
            db.session.commit()
            if batch.total_assigned == batch.total_tasks:
                flash(f"Sorteio concluido: {batch.total_assigned} reversao(oes) distribuidas para {batch.team_name}.", "success")
            else:
                pending = batch.total_tasks - batch.total_assigned
                flash(f"Sorteio parcial: {batch.total_assigned} distribuida(s), {pending} ficaram na fila sem vendedor disponivel.", "warning")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/reversals/<int:task_id>/assign", methods=["POST"])
    @login_required
    def assign_reversal(task_id):
        task = db.session.get(ReversalTask, task_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("reversal_queue")
        if not task or not task.lead or not can_view_lead(g.user, task.lead):
            flash("Reversao nao encontrada para o seu perfil.", "danger")
            return redirect(next_url)
        if not can_assign_reversal_task(g.user, task):
            flash("Seu usuario nao pode atribuir reversoes desta equipe.", "danger")
            return redirect(next_url)

        try:
            assigned_user_id = parse_int(request.form.get("assigned_user_id"), 0)
            assigned_user = db.session.get(User, assigned_user_id)
            source = "reversal_manual_select" if task.status == ReversalStatus.QUEUED else "reversal_manual_transfer"
            assign_reversal_task(task, assigned_user, assigned_by=g.user, source=source)
            db.session.commit()
            flash(f"Reversao atribuida para {assigned_user.name}.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/reversals/<int:task_id>/complete", methods=["POST"])
    @login_required
    def complete_reversal(task_id):
        task = db.session.get(ReversalTask, task_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("reversal_queue")
        if not task or not task.lead or not can_view_lead(g.user, task.lead):
            flash("Reversao nao encontrada para o seu perfil.", "danger")
            return redirect(next_url)

        try:
            complete_reversal_task(
                task,
                g.user,
                result=request.form.get("reversal_result"),
                note=request.form.get("reversal_note"),
            )
            db.session.commit()
            flash("Reversao devolvida para o dono do contrato.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/reversals/<int:task_id>/return", methods=["POST"])
    @login_required
    def return_reversal(task_id):
        task = db.session.get(ReversalTask, task_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("reversal_queue")
        if not task or not task.lead or not can_view_lead(g.user, task.lead):
            flash("Reversao nao encontrada para o seu perfil.", "danger")
            return redirect(next_url)

        try:
            return_reversal_task(
                task,
                g.user,
                result=request.form.get("reversal_result") or request.form.get("return_result"),
                note=request.form.get("reversal_note") or request.form.get("return_note"),
            )
            db.session.commit()
            flash("Reversao devolvida para o dono do contrato.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/leads/<int:lead_id>/activities", methods=["POST"])
    @login_required
    def create_lead_activity(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead não encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if lead_access_is_reversal_only(g.user, lead):
            flash("Use as acoes da reversao atribuida para registrar o retorno deste cliente.", "danger")
            return redirect(url_for("lead_detail", lead_id=lead.id))
        if not g.user.can_edit_atuacoes:
            flash("Seu usuário não tem permissão para registrar atuação.", "danger")
            return redirect(url_for("lead_detail", lead_id=lead.id))

        body = clean_text(request.form.get("body"))
        if not body:
            flash("Escreva a atuação antes de salvar.", "warning")
            return redirect(url_for("lead_detail", lead_id=lead.id))

        try:
            lead_workflow = lead_workflow_from_activity_form(lead, request.form)
            now = datetime.now()
            activity = LeadActivity(
                lead_id=lead.id,
                user_id=g.user.id,
                lead_workflow_id=lead_workflow.id if lead_workflow else None,
                workflow_id=lead_workflow.workflow_id if lead_workflow else None,
                stage_id=lead_workflow.current_stage_id if lead_workflow else None,
                body=body,
                source="manual",
                created_at=now,
                updated_at=now,
            )
            db.session.add(activity)
            if lead_workflow:
                lead_workflow.last_action_at = now
            ensure_lead_in_progress_status(lead, g.user, source="manual_activity")
            if support_actor_for_workflow(g.user, lead_workflow):
                complete_support_task(lead_workflow, g.user, note=body, source="support_activity")
            log_audit(
                lead,
                g.user,
                "activity_create",
                field_name="timeline",
                new_value=body,
                details=activity_details(lead_workflow),
            )
            db.session.commit()
            flash("Atuação registrada na timeline.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

        return redirect(safe_next_url(request.form.get("next")) or url_for("lead_detail", lead_id=lead.id))

    @app.route("/lead-workflows/<int:lead_workflow_id>/progress", methods=["POST"])
    @login_required
    def update_lead_workflow_progress(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next"))
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo não encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        next_url = next_url or url_for("lead_detail", lead_id=lead_workflow.lead_id)
        if lead_access_is_reversal_only(g.user, lead_workflow.lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(next_url)
        action = clean_text(request.form.get("workflow_action")) or "advance"
        if action == "advance" and not can_move_lead_workflow(g.user, lead_workflow):
            flash("Seu usuario nao tem permissao para avancar etapa deste cliente.", "danger")
            return redirect(next_url)
        if action == "resolve_support_return" and not can_resolve_support_return(g.user, lead_workflow):
            flash("Seu usuario nao pode resolver esta devolucao do apoio.", "danger")
            return redirect(next_url)
        if action in {"create_fidelity_task", "save_checklist", "complete_checklist_item", "complete_scheduled_task", "complete_seller_execution_task"} and not can_update_lead_workflow_checklist(g.user, lead_workflow):
            flash("Seu usuario nao tem permissao para alterar tarefas deste cliente.", "danger")
            return redirect(next_url)

        current_operational_workflows, workflow_changed = operational_lead_workflows_for_detail(
            lead_workflow.lead,
            lead_workflows_for_detail(lead_workflow.lead),
        )
        if workflow_changed:
            db.session.commit()
        if lead_workflow.id not in {item.id for item in current_operational_workflows}:
            flash("Este cliente está em outro fluxo operacional no momento.", "warning")
            return redirect(url_for("lead_detail", lead_id=lead_workflow.lead_id))

        try:
            checklist_changes = []
            if action == "create_fidelity_task":
                try:
                    create_fidelity_task(lead_workflow, g.user, request.form.get("task_label"), request.form.get("task_due_date"))
                except ValueError:
                    store_modal_form_state(f"new-fidelity-task-{lead_workflow.id}")
                    raise
                db.session.commit()
                flash("Tarefa adicionada.", "success")
                return redirect(next_url)
            if action == "resolve_support_return":
                support_user = complete_support_return_resolution(
                    lead_workflow,
                    g.user,
                    note=request.form.get("task_note"),
                )
                ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="support_return_resolution")
                db.session.commit()
                if support_user:
                    flash(f"Devolucao resolvida e apoio reenviado para {support_user.name}.", "success")
                else:
                    flash("Devolucao resolvida, mas nao ha apoio ativo vinculado a esta equipe.", "warning")
                return redirect(next_url)

            if action == "complete_checklist_item":
                checklist_changes = [
                    complete_lead_workflow_checklist_item(
                        lead_workflow,
                        request.form.get("checklist_item_id"),
                        g.user,
                        note=request.form.get("task_note"),
                    )
                ]
                log_audit(
                    lead_workflow.lead,
                    g.user,
                    "workflow_checklist_update",
                    details={
                        "lead_workflow_id": lead_workflow.id,
                        "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
                        "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                        "changes": checklist_changes,
                    },
                )
                ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_checklist_item_complete")
                if support_actor_for_workflow(g.user, lead_workflow) and not missing_checklist_labels(lead_workflow):
                    complete_support_task(
                        lead_workflow,
                        g.user,
                        note=request.form.get("task_note"),
                        source="support_checklist",
                    )
                db.session.commit()
                flash("Tarefa concluida.", "success")
                return redirect(next_url)

            if action == "complete_scheduled_task":
                complete_scheduled_contact_task(
                    lead_workflow,
                    request.form.get("scheduled_task_id"),
                    g.user,
                    note=request.form.get("task_note"),
                )
                ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="scheduled_contact_task")
                db.session.commit()
                flash("Tarefa programada concluida.", "success")
                return redirect(next_url)

            if action == "complete_seller_execution_task":
                complete_seller_execution_task(
                    lead_workflow,
                    g.user,
                    task_key=request.form.get("seller_task_key"),
                    note=request.form.get("task_note"),
                )
                ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="seller_execution_task")
                db.session.commit()
                flash("Tarefa concluida.", "success")
                return redirect(next_url)

            if action == "save_checklist":
                checklist_changes = apply_lead_workflow_checklist_form(lead_workflow, request.form, g.user)
            if action == "save_checklist":
                if checklist_changes:
                    log_audit(
                        lead_workflow.lead,
                        g.user,
                        "workflow_checklist_update",
                        details={
                            "lead_workflow_id": lead_workflow.id,
                            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
                            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                            "changes": checklist_changes,
                        },
                    )
                    ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_checklist_update")
                    if support_actor_for_workflow(g.user, lead_workflow):
                        complete_support_task(
                            lead_workflow,
                            g.user,
                            note=request.form.get("support_note") or request.form.get("advance_note"),
                            source="support_checklist",
                        )
                db.session.commit()
                flash("Tarefas salvas." if checklist_changes else "Nenhuma mudança nas tarefas.", "success")
                return redirect(next_url)

            if action != "advance":
                raise ValueError("Ação de fluxo inválida.")

            note = clean_text(request.form.get("advance_note"))
            missing = missing_requirements_for_advance(lead_workflow, note)
            if missing:
                if checklist_changes:
                    ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_checklist_update")
                    db.session.commit()
                flash("Não deu para avançar ainda. Falta: " + "; ".join(missing), "warning")
                return redirect(next_url)

            move_lead_workflow_to_next_stage(lead_workflow, g.user, note, checklist_changes)
            ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_advance")
            support_message = support_assignment_flash_message(lead_workflow)
            if support_message:
                flash(support_message[1], support_message[0])
            db.session.commit()
            flash("Cliente avançado para a próxima etapa.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/stage", methods=["POST"])
    @login_required
    def move_lead_workflow_stage(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo não encontrado para o seu perfil.", "danger")
            return redirect(next_url)
        if lead_access_is_reversal_only(g.user, lead_workflow.lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(next_url)
        if not can_move_lead_workflow(g.user, lead_workflow):
            flash("Seu usuário não tem permissão para mover etapa deste cliente.", "danger")
            return redirect(next_url)

        try:
            ensure_lead_workflow_is_operational(lead_workflow)
            target_stage_id = parse_int(request.form.get("target_stage_id"), 0)
            target_stage = db.session.get(WorkflowStage, target_stage_id)
            if (
                not target_stage
                or target_stage.workflow_id != lead_workflow.workflow_id
                or not target_stage.active
            ):
                raise ValueError("Etapa inválida para este fluxo.")
            if not lead_workflow.current_stage:
                raise ValueError("Este cliente ainda não tem etapa atual.")
            if target_stage.id == lead_workflow.current_stage_id:
                flash("Cliente já está nesta etapa.", "info")
                return redirect(next_url)

            checklist_changes = []
            if (
                can_update_lead_workflow_checklist(g.user, lead_workflow)
                and clean_text(request.form.get("checklist_sync")) == "1"
            ):
                checklist_changes = apply_lead_workflow_checklist_form(lead_workflow, request.form, g.user)

            current_sort_order = lead_workflow.current_stage.sort_order
            if target_stage.sort_order < current_sort_order:
                if not can_move_lead_workflow_backwards(g.user, lead_workflow):
                    raise ValueError("Seu usuario nao tem permissao para voltar etapa deste cliente.")
                move_lead_workflow_to_stage(
                    lead_workflow,
                    target_stage,
                    g.user,
                    source="lead_detail_stage_back",
                    checklist_changes=checklist_changes,
                )
                ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_stage_back")
                db.session.commit()
                flash(f"Cliente voltou para {target_stage.name}.", "success")
                return redirect(next_url)

            allowed_target = workflow_target_stage_for_advance(lead_workflow)
            if not allowed_target or allowed_target.id != target_stage.id:
                raise ValueError("Para avançar, mova apenas para a próxima etapa permitida.")

            missing = missing_requirements_for_advance(lead_workflow, note=None)
            if missing:
                if checklist_changes:
                    ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_checklist_update")
                    db.session.commit()
                flash("Não deu para avançar ainda. Falta: " + "; ".join(missing), "warning")
                return redirect(next_url)

            move_lead_workflow_to_next_stage(lead_workflow, g.user, note=None, checklist_changes=checklist_changes)
            ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_stage_advance")
            support_message = support_assignment_flash_message(lead_workflow)
            if support_message:
                flash(support_message[1], support_message[0])
            db.session.commit()
            flash(f"Cliente avançado para {target_stage.name}.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/quick-advance", methods=["POST"])
    @login_required
    def quick_advance_lead_workflow(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("workflows_board")

        if not workflow_kanban_moves_enabled():
            flash("Movimentação rápida pelo Kanban está desativada por enquanto.", "warning")
            return redirect(next_url)

        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo não encontrado para o seu perfil.", "danger")
            return redirect(next_url)
        if lead_access_is_reversal_only(g.user, lead_workflow.lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(next_url)
        if not can_move_lead_workflow(g.user, lead_workflow):
            flash("Seu usuário não tem permissão para avançar etapa deste cliente.", "danger")
            return redirect(next_url)

        try:
            ensure_lead_workflow_is_operational(lead_workflow)
            target_stage = workflow_target_stage_for_advance(lead_workflow)
            if not target_stage:
                raise ValueError("Este fluxo já está na última etapa.")

            requested_stage_id = parse_int(request.form.get("target_stage_id"), 0)
            if requested_stage_id and requested_stage_id != target_stage.id:
                raise ValueError("Essa movimentação não é a próxima etapa permitida.")

            missing = missing_requirements_for_advance(lead_workflow, note=None)
            if missing:
                flash("Não deu para avançar ainda. Falta: " + "; ".join(missing), "warning")
                return redirect(next_url)

            move_lead_workflow_to_next_stage(lead_workflow, g.user, note=None, checklist_changes=[])
            ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="workflow_quick_advance")
            support_message = support_assignment_flash_message(lead_workflow)
            if support_message:
                flash(support_message[1], support_message[0])
            db.session.commit()
            flash(f"Cliente avançado para {target_stage.name}.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/unlock-executor", methods=["POST"])
    @login_required
    def update_unlock_executor(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if lead_access_is_reversal_only(g.user, lead_workflow.lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(next_url)

        try:
            executor = clean_text(request.form.get("unlock_executor"))
            support_user = set_unlock_executor_for_workflow(lead_workflow, g.user, executor)
            if sync_unlock_day_principal_stage_for_lead(lead_workflow.lead):
                lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
                if lead_workflow and lead_workflow.unlock_executor == UNLOCK_EXECUTOR_SUPPORT and not support_user:
                    support_user = lead_workflow.assigned_user
            db.session.commit()
            if executor == UNLOCK_EXECUTOR_SUPPORT:
                if support_user:
                    flash(f"Desbloqueio sinalizado para o apoio. Responsavel: {support_user.name}.", "success")
                elif lead_workflow and lead_workflow_requires_support(lead_workflow):
                    flash("Desbloqueio sinalizado para o apoio, mas nao ha apoio ativo vinculado a esta equipe.", "warning")
                else:
                    flash("Desbloqueio sinalizado para o apoio. Se ainda nao estiver no dia, a fila sera montada automaticamente.", "info")
            else:
                flash("Desbloqueio sinalizado para o vendedor.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/support-executor", methods=["POST"])
    @login_required
    def update_support_executor(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("dashboard")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if lead_access_is_reversal_only(g.user, lead_workflow.lead):
            flash("Seu acesso a este cliente e somente para executar a reversao atribuida.", "danger")
            return redirect(next_url)

        try:
            executor = clean_text(request.form.get("support_executor"))
            support_user = set_support_executor_for_workflow(lead_workflow, g.user, executor)
            db.session.commit()
            stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "etapa"
            if executor == SUPPORT_EXECUTOR_SUPPORT:
                if support_user:
                    flash(f"{stage_name} sinalizada para o apoio. Responsavel: {support_user.name}.", "success")
                else:
                    flash(f"{stage_name} sinalizada para o apoio, mas nao ha apoio ativo vinculado a esta equipe.", "warning")
            else:
                flash(f"{stage_name} sinalizada para o vendedor.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")

        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/support/assign", methods=["POST"])
    @login_required
    def assign_lead_workflow_support(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("support_queue")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        if support_return_resolution_pending(lead_workflow):
            flash("Resolva a devolucao do apoio na ficha antes de reenviar.", "warning")
            return redirect(next_url)
        if not can_send_support_for_workflow(g.user, lead_workflow):
            flash("Seu usuario nao pode solicitar apoio para este cliente.", "danger")
            return redirect(next_url)
        if support_decision_enabled_for_workflow(lead_workflow):
            lead_workflow.support_executor = SUPPORT_EXECUTOR_SUPPORT
            lead_workflow.support_executor_decided_at = datetime.now()
            lead_workflow.support_executor_decided_by_id = g.user.id
        if not lead_workflow_requires_support(lead_workflow):
            flash("Esta etapa nao esta configurada para apoio.", "warning")
            return redirect(next_url)

        support_user = assign_support_to_workflow(lead_workflow, requested_by=g.user, force=True)
        db.session.commit()
        if support_user:
            flash(f"Apoio atribuido para {support_user.name}.", "success")
        else:
            flash("Nao ha apoio ativo vinculado a esta equipe.", "warning")
        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/support/complete", methods=["POST"])
    @login_required
    def complete_lead_workflow_support(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("support_queue")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        try:
            complete_support_task(
                lead_workflow,
                g.user,
                note=clean_text(request.form.get("support_note")),
                source="support_complete",
            )
            ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="support_complete")
            db.session.commit()
            flash("Apoio concluido. O vendedor ja pode avancar a etapa.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")
        return redirect(next_url)

    @app.route("/lead-workflows/<int:lead_workflow_id>/support/return", methods=["POST"])
    @login_required
    def return_lead_workflow_support(lead_workflow_id):
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        next_url = safe_next_url(request.form.get("next")) or url_for("support_queue")
        if not lead_workflow or not lead_workflow.lead or not can_view_lead(g.user, lead_workflow.lead):
            flash("Fluxo nao encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))
        try:
            return_support_task(lead_workflow, g.user, request.form.get("return_reason"))
            ensure_lead_in_progress_status(lead_workflow.lead, g.user, source="support_return")
            db.session.commit()
            flash("Tarefa devolvida para o vendedor.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")
        return redirect(next_url)

    @app.route("/leads/<int:lead_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_delete_leads")
    def delete_lead(lead_id):
        lead = db.session.get(Lead, lead_id)
        if not lead or not can_view_lead(g.user, lead):
            flash("Lead não encontrado para o seu perfil.", "danger")
            return redirect(url_for("dashboard"))

        lead.deleted_at = datetime.now()
        lead.deleted_by_id = g.user.id
        lead.deleted_reason = clean_text(request.form.get("reason"))
        log_audit(
            lead,
            g.user,
            "delete",
            details={"reason": lead.deleted_reason},
        )
        db.session.commit()
        flash("Lead excluído.", "success")
        return redirect(safe_next_url(request.form.get("next")) or url_for("dashboard"))

    @app.route("/admin/audit")
    @login_required
    @permission_required("can_view_audit")
    def audit_admin():
        import_batches = (
            scoped_company_query(ImportBatch.query, ImportBatch, g.user, include_unscoped=True)
            .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
            .limit(30)
            .all()
        )
        audit_events = (
            scoped_company_query(AuditLog.query, AuditLog, g.user, include_unscoped=True)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(150)
            .all()
        )
        return render_template(
            "audit.html",
            current_user=g.user,
            import_batches=import_batches,
            audit_events=audit_events,
            action_label=audit_action_label,
            field_label=audit_field_label,
        )

    @app.route("/admin/settings")
    @login_required
    @permission_required("can_manage_settings", "can_manage_teams", "can_manage_roles", "can_manage_users")
    def settings_admin():
        field_options = {
            field_name: (
                scoped_company_query(
                    FieldOption.query.filter_by(field_name=field_name),
                    FieldOption,
                    g.user,
                    include_unscoped=True,
                )
                .order_by(FieldOption.active.desc(), FieldOption.sort_order.asc(), FieldOption.value.asc())
                .all()
            )
            for field_name in ("status", "motivo_perda")
        }
        teams = (
            scoped_company_query(Team.query, Team, g.user, include_unscoped=True)
            .order_by(Team.active.desc(), Team.name.asc())
            .all()
        )
        role_profiles = scoped_company_query(
            RoleProfile.query,
            RoleProfile,
            g.user,
            include_unscoped=True,
        ).order_by(
            RoleProfile.active.desc(),
            RoleProfile.sort_order.asc(),
            RoleProfile.name.asc(),
        ).all()
        return render_template(
            "settings.html",
            current_user=g.user,
            field_options=field_options,
            company=default_company_for_user(g.user),
            franchise=default_franchise_for_user(g.user),
        )

    @app.route("/admin/company")
    @login_required
    @permission_required("can_manage_users", "can_manage_teams", "can_manage_roles", "can_manage_settings")
    def company_admin():
        company = default_company_for_user(g.user)
        franchise = default_franchise_for_user(g.user)
        users_query = scoped_company_query(User.query, User, g.user, include_unscoped=True)
        teams_query = scoped_company_query(Team.query, Team, g.user, include_unscoped=True)
        roles_query = scoped_company_query(RoleProfile.query, RoleProfile, g.user, include_unscoped=True)
        franchises_query = scoped_company_query(Franchise.query, Franchise, g.user, include_unscoped=True)
        users = users_query.order_by(User.active.desc(), User.name.asc()).all()
        teams = teams_query.order_by(Team.active.desc(), Team.name.asc()).all()
        role_profiles = roles_query.order_by(
            RoleProfile.active.desc(),
            RoleProfile.sort_order.asc(),
            RoleProfile.name.asc(),
        ).all()
        franchises = franchises_query.order_by(Franchise.active.desc(), Franchise.name.asc()).all()
        pending_requests = (
            scoped_company_query(
                GoogleAccessRequest.query.filter(GoogleAccessRequest.status == "PENDING"),
                GoogleAccessRequest,
                g.user,
                include_unscoped=True,
            )
            .order_by(GoogleAccessRequest.last_attempt_at.desc())
            .limit(6)
            .all()
        )
        users_without_team = [
            user
            for user in users
            if user.active and user.role != Role.ADMIN and not clean_text(user.team)
        ]
        users_without_franchise = [
            user
            for user in users
            if user.active and not user.franchise_id
        ]
        teams_without_franchise = [
            team
            for team in teams
            if team.active and not team.franchise_id
        ]
        empty_teams = [
            team
            for team in teams
            if team.active and team_usage_counts(team)["users"] == 0
        ]
        inactive_or_empty_franchises = [
            franchise_item
            for franchise_item in franchises
            if (not franchise_item.active) or not any(franchise_usage_counts(franchise_item).values())
        ]
        sensitive_roles = [
            profile
            for profile in role_profiles
            if profile.active and (
                profile.can_view_all
                or profile.can_manage_users
                or profile.can_manage_roles
                or profile.can_manage_settings
                or profile.can_manage_workflows
                or profile.can_bulk_delete_leads
            )
        ]
        role_user_counts = get_role_profile_user_counts()
        unused_roles = [
            profile
            for profile in role_profiles
            if profile.active and role_user_counts.get(profile.id, 0) == 0
        ]

        def unique_by_id(items):
            seen = set()
            unique_items = []
            for item in items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                unique_items.append(item)
            return unique_items

        users_to_review = unique_by_id(users_without_team + users_without_franchise)
        teams_to_review = unique_by_id(teams_without_franchise + empty_teams)
        roles_to_review = unique_by_id(sensitive_roles + unused_roles)
        health_checks = [
            {
                "label": "Solicitacoes Google pendentes",
                "count": len(pending_requests),
                "severity": "warning" if pending_requests else "success",
                "href": url_for("users_admin"),
                "action": "Analisar acessos",
            },
            {
                "label": "Usuarios ativos sem equipe",
                "count": len(users_without_team),
                "severity": "warning" if users_without_team else "success",
                "href": url_for("users_admin", active="active"),
                "action": "Organizar usuarios",
            },
            {
                "label": "Usuarios ativos sem franquia",
                "count": len(users_without_franchise),
                "severity": "warning" if users_without_franchise else "success",
                "href": url_for("users_admin", active="active"),
                "action": "Vincular franquia",
            },
            {
                "label": "Equipes sem franquia",
                "count": len(teams_without_franchise),
                "severity": "warning" if teams_without_franchise else "success",
                "href": url_for("company_teams_admin"),
                "action": "Revisar equipes",
            },
            {
                "label": "Equipes ativas sem usuarios",
                "count": len(empty_teams),
                "severity": "info" if empty_teams else "success",
                "href": url_for("company_teams_admin"),
                "action": "Ver equipes",
            },
            {
                "label": "Cargos sensiveis ativos",
                "count": len(sensitive_roles),
                "severity": "danger" if sensitive_roles else "success",
                "href": url_for("company_roles_admin"),
                "action": "Auditar cargos",
            },
        ]
        return render_template(
            "company_overview.html",
            current_user=g.user,
            company=company,
            franchise=franchise,
            metrics={
                "users": users_query.count(),
                "active_users": users_query.filter(User.active.is_(True)).count(),
                "teams": teams_query.count(),
                "franchises": franchises_query.count(),
                "roles": roles_query.count(),
                "pending_requests": scoped_company_query(
                    GoogleAccessRequest.query.filter(GoogleAccessRequest.status == "PENDING"),
                    GoogleAccessRequest,
                    g.user,
                    include_unscoped=True,
                ).count(),
            },
            health_checks=health_checks,
            pending_requests=pending_requests,
            users_without_team=users_without_team,
            users_without_franchise=users_without_franchise,
            users_to_review=users_to_review,
            teams_without_franchise=teams_without_franchise,
            empty_teams=empty_teams,
            teams_to_review=teams_to_review,
            inactive_or_empty_franchises=inactive_or_empty_franchises,
            sensitive_roles=sensitive_roles,
            unused_roles=unused_roles,
            roles_to_review=roles_to_review,
            role_profile_user_counts=role_user_counts,
        )

    @app.route("/admin/company/teams")
    @login_required
    @permission_required("can_manage_teams")
    def company_teams_admin():
        teams = (
            scoped_company_query(Team.query, Team, g.user, include_unscoped=True)
            .order_by(Team.active.desc(), Team.name.asc())
            .all()
        )
        team_reversal_configs = {}
        config_changed = False
        for team in teams:
            config, created = get_or_create_reversal_team_config(team)
            team_reversal_configs[team.id] = config
            config_changed = config_changed or created
        if config_changed:
            db.session.commit()
        team_members_by_team = {team.id: team_members(team) for team in teams}
        available_users_by_team = {team.id: team_available_users(team) for team in teams}
        return render_template(
            "company_teams.html",
            current_user=g.user,
            company=default_company_for_user(g.user),
            franchises=get_company_franchises(g.user, include_inactive=True),
            teams=teams,
            team_usage_counts=team_usage_counts,
            team_reversal_configs=team_reversal_configs,
            reversal_distribution_modes=[
                {"value": mode, "label": reversal_distribution_mode_label(mode)}
                for mode in ReversalDistributionMode.ALL
            ],
            reversal_distribution_mode_label=reversal_distribution_mode_label,
            team_members_by_team=team_members_by_team,
            available_users_by_team=available_users_by_team,
            lead_assignment_name=lead_assignment_name,
            lead_counts_by_consultor=get_lead_counts_by_consultor(),
        )

    @app.route("/admin/company/franchises")
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def company_franchises_admin():
        franchises = get_company_franchises(g.user, include_inactive=True)
        franchise_teams_by_franchise = {franchise.id: franchise_teams(franchise) for franchise in franchises}
        franchise_users_by_franchise = {franchise.id: franchise_users(franchise) for franchise in franchises}
        available_teams_by_franchise = {franchise.id: franchise_available_teams(franchise) for franchise in franchises}
        available_users_by_franchise = {franchise.id: franchise_available_users(franchise) for franchise in franchises}
        return render_template(
            "company_franchises.html",
            current_user=g.user,
            company=default_company_for_user(g.user),
            franchises=franchises,
            franchise_usage_counts=franchise_usage_counts,
            franchise_teams_by_franchise=franchise_teams_by_franchise,
            franchise_users_by_franchise=franchise_users_by_franchise,
            available_teams_by_franchise=available_teams_by_franchise,
            available_users_by_franchise=available_users_by_franchise,
            team_usage_counts=team_usage_counts,
            lead_assignment_name=lead_assignment_name,
            lead_counts_by_consultor=get_lead_counts_by_consultor(),
        )

    @app.route("/admin/company/roles")
    @login_required
    @permission_required("can_manage_roles")
    def company_roles_admin():
        role_profiles = scoped_company_query(
            RoleProfile.query,
            RoleProfile,
            g.user,
            include_unscoped=True,
        ).order_by(
            RoleProfile.active.desc(),
            RoleProfile.sort_order.asc(),
            RoleProfile.name.asc(),
        ).all()
        return render_template(
            "company_roles.html",
            current_user=g.user,
            company=default_company_for_user(g.user),
            roles=Role.ALL,
            role_profiles=role_profiles,
            permission_fields=PERMISSION_FIELDS,
            permission_labels=PERMISSION_LABELS,
            permission_groups=PERMISSION_GROUPS,
            role_profile_user_counts=get_role_profile_user_counts(),
            role_profile_permission_counts={
                profile.id: enabled_permission_count(profile)
                for profile in role_profiles
            },
            role_profile_users_by_profile={
                profile.id: role_profile_users(profile)
                for profile in role_profiles
            },
            available_users_by_profile={
                profile.id: role_profile_available_users(profile)
                for profile in role_profiles
            },
            enabled_permission_count=enabled_permission_count,
        )

    @app.route("/admin/workflows")
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def workflows_admin():
        if request.args.get("view") != "stages" and not request.args.get("workflow_id"):
            tracks = (LeadWorkflow.query.join(Workflow, LeadWorkflow.workflow_id == Workflow.id)
                      .join(Lead, LeadWorkflow.lead_id == Lead.id)
                      .filter(Workflow.slug == UNLOCK_WORKFLOW_SLUG, Lead.company_id == g.user.company_id,
                              Lead.deleted_at.is_(None), LeadWorkflow.status.notin_(TERMINAL_WORKFLOW_STATUSES))
                      .order_by(Lead.nome_cliente).all())
            return render_template("task_configuration_admin.html", current_user=g.user,
                                   panel=task_admin_panel_state(g.user),
                                   agenda_contracts=[{"id": track.lead_id, "name": track.lead.nome_cliente}
                                                     for track in tracks if can_view_lead(g.user, track.lead)])
        workflows = (
            scoped_company_query(Workflow.query, Workflow, g.user, include_unscoped=True)
            .order_by(
                Workflow.active.desc(),
                Workflow.sort_order.asc(),
                Workflow.name.asc(),
            ).all()
        )
        selected_workflow = selected_admin_workflow(workflows)
        stages = []
        checklist_by_stage = {}
        usage_by_stage = {}
        if selected_workflow:
            stages = (
                WorkflowStage.query.filter_by(workflow_id=selected_workflow.id)
                .order_by(WorkflowStage.active.desc(), WorkflowStage.sort_order.asc(), WorkflowStage.id.asc())
                .all()
            )
            checklist_by_stage = workflow_checklists_by_stage(stages)
            usage_by_stage = {stage.id: workflow_stage_usage_counts(stage) for stage in stages}

        return render_template(
            "workflows_admin.html",
            current_user=g.user,
            workflows=workflows,
            selected_workflow=selected_workflow,
            stages=stages,
            checklist_by_stage=checklist_by_stage,
            usage_by_stage=usage_by_stage,
            required_field_choices=WORKFLOW_REQUIRED_FIELD_CHOICES,
            workflow_usage_counts={workflow.id: workflow_usage_counts(workflow) for workflow in workflows},
            stage_required_fields=stage_required_fields,
        )

    @app.route("/admin/workflows/task-draft", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def create_task_draft_admin():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return {"error": "Envie os dados do rascunho."}, 400
        try:
            start_draft(g.user, data.get("fingerprint"))
            db.session.commit()
            return task_admin_panel_state(g.user)
        except PermissionError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 403
        except (DraftConflict, IntegrityError):
            db.session.rollback()
            return {"error": "A configuracao mudou. Atualize a pagina e tente novamente."}, 409
        except ValueError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 400

    @app.route("/admin/workflows/task-draft/<int:revision_id>/task", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def save_task_draft_admin(revision_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or (data.get("key") is not None and not isinstance(data.get("key"), str)):
            return {"error": "Dados da tarefa invalidos."}, 400
        try:
            save_task(g.user, revision_id, data.get("fingerprint"), data.get("key"), data.get("task"))
            db.session.commit()
            return task_admin_panel_state(g.user)
        except PermissionError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 403
        except DraftConflict as exc:
            db.session.rollback()
            return {"error": str(exc)}, 409
        except ValueError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 400

    @app.route("/admin/workflows/task-draft/<int:revision_id>/deactivate-all", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def deactivate_draft_tasks_admin(revision_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return {"error": "Dados invalidos."}, 400
        try:
            deactivate_all_draft_tasks(g.user, revision_id, data.get("fingerprint"), SELLER_EXECUTION_TASKS)
            db.session.commit()
            return task_admin_panel_state(g.user)
        except PermissionError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 403
        except DraftConflict as exc:
            db.session.rollback()
            return {"error": str(exc)}, 409
        except ValueError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 400

    @app.route("/admin/workflows/task-agenda/preview", methods=["POST"])
    @app.route("/admin/workflows/task-agenda/apply", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def preview_task_agenda_admin():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or type(data.get("revision_id")) is not int:
            return {"error": "Selecione uma versao valida."}, 400
        revision = db.session.get(TaskConfigurationRevision, data["revision_id"])
        scope = editor_scope(g.user)
        if not revision or revision.scope_id != scope.id:
            return {"error": "Versao indisponivel neste escopo."}, 403
        if revision.fingerprint != data.get("fingerprint"):
            return {"error": "A versao mudou. Atualize a pagina antes de simular."}, 409
        try:
            content = json.loads(revision.content)
            applying = request.path.endswith("/apply")
            if applying and (not data.get("lead_id") or data.get("confirm") is not True):
                raise ValueError("Confirme a previa de um contrato antes de aplicar.")
            if data.get("lead_id") is not None:
                lead_id = data["lead_id"]
                lead = db.session.get(Lead, lead_id) if type(lead_id) is int else None
                if not lead or lead.deleted_at or lead.company_id != g.user.company_id or not can_view_lead(g.user, lead):
                    return {"error": "Contrato indisponivel neste escopo."}, 403
                tracking = (LeadWorkflow.query.join(Workflow, LeadWorkflow.workflow_id == Workflow.id)
                            .filter(LeadWorkflow.lead_id == lead.id, Workflow.slug == UNLOCK_WORKFLOW_SLUG,
                                    LeadWorkflow.status.notin_(TERMINAL_WORKFLOW_STATUSES)).first())
                if not tracking or not lead.concessao or not tracking.fidelity_started_on:
                    raise ValueError("Contrato sem agenda de fidelizacao ativa ou sem datas validas.")
                effective_from = parse_day(data.get("effective_from"), "inicio do replanejamento")
                if applying:
                    result = apply_replan(g.user, revision, tracking, effective_from, data.get("confirmation"), datetime.now().date())
                    db.session.commit()
                    result.update(applied=True, can_apply=False)
                else:
                    result = replan_state(g.user, revision, tracking, effective_from, datetime.now().date())
                result.update(lead_id=lead.id, lead_name=lead.nome_cliente, mode="replan")
            else:
                result = simulate_schedule(content, parse_day(data.get("concession"), "concessao"),
                                           parse_day(data.get("started_on"), "entrada no fluxo"))
                result.update(mode="simulation", preview_only=True)
            result.update(revision_number=revision.number, is_draft=revision.status == "draft")
            return result
        except PermissionError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 403
        except DraftConflict as exc:
            db.session.rollback()
            return {"error": str(exc)}, 409
        except ValueError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 400

    @app.route("/admin/workflows/task-draft/<int:revision_id>/save", methods=["POST"])
    @app.route("/admin/workflows/task-draft/<int:revision_id>/publish", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def persist_task_draft_admin(revision_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return {"error": "Dados invalidos."}, 400
        try:
            if request.path.endswith("/publish"):
                if data.get("confirm") is not True:
                    raise ValueError("Confirme a publicacao apos revisar as alteracoes.")
                publish_draft(g.user, revision_id, data.get("fingerprint"), SELLER_EXECUTION_TASKS,
                              checklist_items_for_workflow, contact_agenda_revision)
            else:
                draft = editable_draft(g.user, revision_id, data.get("fingerprint"))
                write_draft(g.user, draft, data.get("fingerprint"), json.loads(draft.content),
                            "task_configuration_draft_saved", {})
            db.session.commit()
            return task_admin_panel_state(g.user)
        except PermissionError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 403
        except (DraftConflict, IntegrityError) as exc:
            db.session.rollback()
            return {"error": "A configuracao mudou. Atualize a pagina antes de continuar."}, 409
        except ValueError as exc:
            db.session.rollback()
            return {"error": str(exc)}, 400

    @app.route("/admin/workflows/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def create_workflow_admin():
        try:
            workflow = build_workflow_from_form(Workflow(), request.form)
            workflow.company_id = company_id_for_user(g.user)
            if workflow_slug_exists(workflow.slug):
                raise ValueError("Ja existe um fluxo com esse identificador.")
            db.session.add(workflow)
            db.session.flush()
            log_audit(None, g.user, "workflow_config_create", details={"workflow": workflow_snapshot(workflow)})
            db.session.commit()
            flash("Fluxo criado.", "success")
            return redirect(url_for("workflows_admin", workflow_id=workflow.id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("workflows_admin"))

    @app.route("/admin/workflows/<int:workflow_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def update_workflow_admin(workflow_id):
        workflow = db.session.get(Workflow, workflow_id)
        if not workflow or not in_same_company_scope(g.user, workflow):
            flash("Fluxo nao encontrado.", "danger")
            return redirect(url_for("workflows_admin"))

        try:
            before = workflow_snapshot(workflow)
            build_workflow_from_form(workflow, request.form)
            if workflow_slug_exists(workflow.slug, workflow.id):
                raise ValueError("Ja existe outro fluxo com esse identificador.")
            after = workflow_snapshot(workflow)
            log_audit(
                None,
                g.user,
                "workflow_config_update",
                details={"workflow_id": workflow.id, "changes": snapshot_changes(before, after)},
            )
            db.session.commit()
            flash("Fluxo atualizado.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("workflows_admin", workflow_id=workflow.id))

    @app.route("/admin/workflows/<int:workflow_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def delete_workflow_admin(workflow_id):
        workflow = db.session.get(Workflow, workflow_id)
        if not workflow or not in_same_company_scope(g.user, workflow):
            flash("Fluxo nao encontrado.", "danger")
            return redirect(url_for("workflows_admin"))
        usage = workflow_usage_counts(workflow)
        if any(usage.values()):
            flash("Esse fluxo tem clientes, etapas ou historico. Desative para preservar auditoria.", "danger")
            return redirect(url_for("workflows_admin", workflow_id=workflow.id))

        snapshot = workflow_snapshot(workflow)
        db.session.delete(workflow)
        log_audit(None, g.user, "workflow_config_delete", details={"workflow": snapshot})
        db.session.commit()
        flash("Fluxo excluido.", "success")
        return redirect(url_for("workflows_admin"))

    @app.route("/admin/workflows/<int:workflow_id>/stages/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def create_workflow_stage_admin(workflow_id):
        workflow = db.session.get(Workflow, workflow_id)
        if not workflow or not in_same_company_scope(g.user, workflow):
            flash("Fluxo nao encontrado.", "danger")
            return redirect(url_for("workflows_admin"))
        try:
            stage = build_workflow_stage_from_form(WorkflowStage(workflow_id=workflow.id), request.form)
            if workflow_stage_slug_exists(workflow.id, stage.slug):
                raise ValueError("Ja existe uma etapa com esse identificador neste fluxo.")
            db.session.add(stage)
            db.session.flush()
            log_audit(
                None,
                g.user,
                "workflow_stage_create",
                details={"workflow_id": workflow.id, "stage": workflow_stage_snapshot(stage)},
            )
            db.session.commit()
            flash("Etapa criada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("workflows_admin", workflow_id=workflow.id))

    @app.route("/admin/workflow-stages/<int:stage_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def update_workflow_stage_admin(stage_id):
        stage = db.session.get(WorkflowStage, stage_id)
        if not stage or not in_same_company_scope(g.user, stage.workflow):
            flash("Etapa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))
        try:
            before = workflow_stage_snapshot(stage)
            build_workflow_stage_from_form(stage, request.form)
            if workflow_stage_slug_exists(stage.workflow_id, stage.slug, stage.id):
                raise ValueError("Ja existe outra etapa com esse identificador neste fluxo.")
            after = workflow_stage_snapshot(stage)
            if before.get("active") and not after.get("active"):
                active_clients = workflow_stage_active_client_count(stage)
                if active_clients:
                    raise ValueError(
                        f"Essa etapa tem {active_clients} cliente(s) ativo(s). Mova os clientes antes de inativar."
                    )
            log_audit(
                None,
                g.user,
                "workflow_stage_update",
                details={"stage_id": stage.id, "changes": snapshot_changes(before, after)},
            )
            db.session.commit()
            sync_lead_workflow_due_dates()
            flash("Etapa atualizada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("workflows_admin", workflow_id=stage.workflow_id))

    @app.route("/admin/workflow-stages/<int:stage_id>/toggle", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def toggle_workflow_stage_admin(stage_id):
        stage = db.session.get(WorkflowStage, stage_id)
        if not stage or not in_same_company_scope(g.user, stage.workflow):
            flash("Etapa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))

        workflow_id = stage.workflow_id
        activate = request.form.get("active") == "1"
        try:
            if not activate:
                active_clients = workflow_stage_active_client_count(stage)
                if active_clients:
                    raise ValueError(
                        f"Essa etapa tem {active_clients} cliente(s) ativo(s). Mova os clientes antes de inativar."
                    )
            before = workflow_stage_snapshot(stage)
            stage.active = activate
            after = workflow_stage_snapshot(stage)
            log_audit(
                None,
                g.user,
                "workflow_stage_update",
                details={"stage_id": stage.id, "changes": snapshot_changes(before, after), "source": "toggle"},
            )
            db.session.commit()
            flash("Etapa ativada." if activate else "Etapa inativada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "warning")
        return redirect(url_for("workflows_admin", workflow_id=workflow_id))

    @app.route("/admin/workflow-stages/<int:stage_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def delete_workflow_stage_admin(stage_id):
        stage = db.session.get(WorkflowStage, stage_id)
        if not stage or not in_same_company_scope(g.user, stage.workflow):
            flash("Etapa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))
        workflow_id = stage.workflow_id
        usage = workflow_stage_usage_counts(stage)
        if any(usage.values()):
            flash("Essa etapa tem clientes, historico ou tarefas. Desative para preservar auditoria.", "danger")
            return redirect(url_for("workflows_admin", workflow_id=workflow_id))

        snapshot = workflow_stage_snapshot(stage)
        db.session.delete(stage)
        log_audit(None, g.user, "workflow_stage_delete", details={"stage": snapshot})
        db.session.commit()
        flash("Etapa excluida.", "success")
        return redirect(url_for("workflows_admin", workflow_id=workflow_id))

    @app.route("/admin/workflow-stages/<int:stage_id>/checklist/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def create_workflow_checklist_item_admin(stage_id):
        stage = db.session.get(WorkflowStage, stage_id)
        if not stage or not in_same_company_scope(g.user, stage.workflow):
            flash("Etapa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))
        try:
            item = build_workflow_checklist_item_from_form(
                WorkflowChecklistItem(stage_id=stage.id),
                request.form,
            )
            if workflow_checklist_label_exists(stage.id, item.label):
                raise ValueError("Ja existe uma tarefa com esse nome nesta etapa.")
            db.session.add(item)
            db.session.flush()
            log_audit(
                None,
                g.user,
                "workflow_checklist_create",
                details={"stage_id": stage.id, "item": workflow_checklist_item_snapshot(item)},
            )
            db.session.commit()
            flash("Tarefa criada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("workflows_admin", workflow_id=stage.workflow_id))

    @app.route("/admin/workflow-checklist-items/<int:item_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def update_workflow_checklist_item_admin(item_id):
        item = db.session.get(WorkflowChecklistItem, item_id)
        if not item or not in_same_company_scope(g.user, item.stage.workflow if item.stage else None):
            flash("Tarefa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))
        try:
            before = workflow_checklist_item_snapshot(item)
            build_workflow_checklist_item_from_form(item, request.form)
            if workflow_checklist_label_exists(item.stage_id, item.label, item.id):
                raise ValueError("Ja existe outra tarefa com esse nome nesta etapa.")
            after = workflow_checklist_item_snapshot(item)
            log_audit(
                None,
                g.user,
                "workflow_checklist_update",
                details={"item_id": item.id, "changes": snapshot_changes(before, after)},
            )
            db.session.commit()
            flash("Tarefa atualizada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("workflows_admin", workflow_id=item.stage.workflow_id if item.stage else None))

    @app.route("/admin/workflow-checklist-items/<int:item_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_workflows")
    @matrix_configuration_required
    def delete_workflow_checklist_item_admin(item_id):
        item = db.session.get(WorkflowChecklistItem, item_id)
        if not item or not in_same_company_scope(g.user, item.stage.workflow if item.stage else None):
            flash("Tarefa nao encontrada.", "danger")
            return redirect(url_for("workflows_admin"))
        workflow_id = item.stage.workflow_id if item.stage else None
        usage = workflow_checklist_item_usage_count(item)
        if usage:
            flash("Esse item ja foi marcado em clientes. Desative para preservar auditoria.", "danger")
            return redirect(url_for("workflows_admin", workflow_id=workflow_id))

        snapshot = workflow_checklist_item_snapshot(item)
        db.session.delete(item)
        log_audit(None, g.user, "workflow_checklist_delete", details={"item": snapshot})
        db.session.commit()
        flash("Tarefa excluida.", "success")
        return redirect(url_for("workflows_admin", workflow_id=workflow_id))

    @app.route("/admin/settings/options/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_settings")
    def create_field_option():
        field_name = clean_text(request.form.get("field_name"))
        value = clean_text(request.form.get("value"))
        try:
            if field_name not in DEFAULT_OPTIONS:
                raise ValueError("Campo de opcao invalido.")
            if not value:
                raise ValueError("Informe o nome da opcao.")
            if field_option_is_reserved(field_name, value):
                raise ValueError("Reversao agora e controlada pela fila propria, nao por lista manual.")
            if field_option_exists(field_name, value):
                raise ValueError("Essa opcao ja existe.")

            option = FieldOption(
                company_id=company_id_for_user(g.user),
                field_name=field_name,
                value=value,
                color_hex=normalize_hex_color(request.form.get("color_hex"))
                or default_option_color(field_name, value),
                sort_order=parse_int(request.form.get("sort_order"), 0),
                active=True,
            )
            db.session.add(option)
            db.session.flush()
            log_audit(None, g.user, "config_option_create", details={"option": field_option_snapshot(option)})
            db.session.commit()
            flash("Opcao criada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("settings_admin"))

    @app.route("/admin/settings/options/<int:option_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_settings")
    def update_field_option(option_id):
        option = db.session.get(FieldOption, option_id)
        if not option or not in_same_company_scope(g.user, option):
            flash("Opcao nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))

        try:
            before = field_option_snapshot(option)
            if option.field_name not in DEFAULT_OPTIONS:
                raise ValueError("Essa lista nao esta mais disponivel para edicao.")
            value = clean_text(request.form.get("value"))
            if not value:
                raise ValueError("Informe o nome da opcao.")
            if field_option_is_reserved(option.field_name, value):
                raise ValueError("Reversao agora e controlada pela fila propria, nao por lista manual.")
            if field_option_exists(option.field_name, value, option.id):
                raise ValueError("Essa opcao ja existe.")
            option.value = value
            option.color_hex = normalize_hex_color(request.form.get("color_hex")) or option.color_hex
            option.sort_order = parse_int(request.form.get("sort_order"), option.sort_order)
            option.active = request.form.get("active") == "on"
            after = field_option_snapshot(option)
            log_audit(
                None,
                g.user,
                "config_option_update",
                details={"option_id": option.id, "changes": snapshot_changes(before, after)},
            )
            db.session.commit()
            flash("Opcao atualizada.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("settings_admin"))

    @app.route("/admin/settings/options/<int:option_id>/deactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_settings")
    def deactivate_field_option(option_id):
        option = db.session.get(FieldOption, option_id)
        if not option or not in_same_company_scope(g.user, option):
            flash("Opcao nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))
        if option.field_name not in DEFAULT_OPTIONS:
            flash("Essa lista nao esta mais disponivel para edicao.", "danger")
            return redirect(url_for("settings_admin"))

        before = field_option_snapshot(option)
        option.active = False
        after = field_option_snapshot(option)
        log_audit(
            None,
            g.user,
            "config_option_deactivate",
            details={"option_id": option.id, "changes": snapshot_changes(before, after)},
        )
        db.session.commit()
        flash("Opcao desativada.", "success")
        return redirect(url_for("settings_admin"))

    @app.route("/admin/settings/options/<int:option_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_settings")
    def delete_field_option(option_id):
        option = db.session.get(FieldOption, option_id)
        if not option or not in_same_company_scope(g.user, option):
            flash("Opcao nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))
        if option.field_name not in DEFAULT_OPTIONS:
            flash("Essa lista nao esta mais disponivel para edicao.", "danger")
            return redirect(url_for("settings_admin"))

        used_count = field_option_usage_count(option)
        if used_count:
            flash("Essa opcao esta em uso em clientes. Desative para esconder dos selects sem perder historico.", "danger")
            return redirect(url_for("settings_admin"))

        snapshot = field_option_snapshot(option)
        db.session.delete(option)
        log_audit(None, g.user, "config_option_delete", details={"option": snapshot})
        db.session.commit()
        flash("Opcao excluida.", "success")
        return redirect(url_for("settings_admin"))

    @app.route("/admin/settings/teams/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def create_team():
        try:
            name = clean_text(request.form.get("name"))
            if not name:
                raise ValueError("Informe o nome da equipe.")
            franchise_id = validate_franchise_id_for_form(
                request.form.get("franchise_id"),
                g.user,
                fallback_id=franchise_id_for_user(g.user),
            )
            if team_exists(name, franchise_id=franchise_id):
                raise ValueError("Essa equipe ja existe nesta franquia.")
            reversal_mode = clean_text(request.form.get("reversal_distribution_mode")) or ReversalDistributionMode.MANUAL
            if reversal_mode not in ReversalDistributionMode.ALL:
                raise ValueError("Modo de sorteio da reversao invalido.")
            team = Team(
                company_id=company_id_for_user(g.user),
                franchise_id=franchise_id,
                name=name,
                description=clean_text(request.form.get("description")),
                active=True,
            )
            db.session.add(team)
            db.session.flush()
            reversal_config, _created = get_or_create_reversal_team_config(team)
            reversal_config.enabled = request.form.get("reversal_enabled") == "on"
            reversal_config.distribution_mode = reversal_mode
            log_audit(None, g.user, "team_create", details={"team": team_snapshot(team)})
            db.session.commit()
            flash("Equipe criada.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state("team-create-dialog")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state("team-create-dialog")
            flash("Nao foi possivel criar a equipe. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("settings_admin"))

    @app.route("/admin/settings/teams/<int:team_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def update_team(team_id):
        team = db.session.get(Team, team_id)
        if not team or not in_same_company_scope(g.user, team):
            flash("Equipe nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))

        try:
            before = team_snapshot(team)
            old_name = team.name
            name = clean_text(request.form.get("name"))
            if not name:
                raise ValueError("Informe o nome da equipe.")
            franchise_id = validate_franchise_id_for_form(
                request.form.get("franchise_id"),
                g.user,
                fallback_id=team.franchise_id or franchise_id_for_user(g.user),
            )
            if team_exists(name, team.id, franchise_id=franchise_id):
                raise ValueError("Essa equipe ja existe nesta franquia.")

            team.name = name
            team.description = clean_text(request.form.get("description"))
            team.franchise_id = franchise_id
            team.active = request.form.get("active") == "on"
            reversal_mode = clean_text(request.form.get("reversal_distribution_mode")) or ReversalDistributionMode.MANUAL
            if reversal_mode not in ReversalDistributionMode.ALL:
                raise ValueError("Modo de sorteio da reversao invalido.")
            reversal_enabled = request.form.get("reversal_enabled") == "on"
            reversal_config, _created = get_or_create_reversal_team_config(team)
            reversal_before = reversal_team_config_snapshot(reversal_config)
            reversal_config.enabled = reversal_enabled
            reversal_config.distribution_mode = reversal_mode
            reversal_config.updated_at = datetime.now()
            renamed_users = 0
            renamed_leads = 0
            if old_name != name:
                renamed_users_query = scoped_company_query(
                    User.query.filter(User.team == old_name),
                    User,
                    g.user,
                    include_unscoped=True,
                )
                renamed_leads_query = scoped_company_query(
                    Lead.query.filter(Lead.equipe == old_name),
                    Lead,
                    g.user,
                    include_unscoped=True,
                )
                if before.get("franchise_id"):
                    renamed_users_query = renamed_users_query.filter(User.franchise_id == before["franchise_id"])
                    renamed_leads_query = renamed_leads_query.filter(Lead.franchise_id == before["franchise_id"])
                renamed_users = renamed_users_query.update(
                    {"team": name},
                    synchronize_session=False,
                )
                renamed_leads = renamed_leads_query.update(
                    {"equipe": name},
                    synchronize_session=False,
                )

            after = team_snapshot(team)
            reversal_after = reversal_team_config_snapshot(reversal_config)
            log_audit(
                None,
                g.user,
                "team_update",
                details={
                    "team_id": team.id,
                    "changes": snapshot_changes(before, after),
                    "reversal_config_changes": snapshot_changes(reversal_before, reversal_after),
                    "renamed_users": renamed_users,
                    "renamed_leads": renamed_leads,
                },
            )
            db.session.commit()
            flash("Equipe atualizada.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"team-dialog-{team.id}")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state(f"team-dialog-{team.id}")
            flash("Nao foi possivel atualizar a equipe. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("settings_admin"))

    @app.route("/admin/company/teams/<int:team_id>/members/add", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def add_team_members(team_id):
        team = db.session.get(Team, team_id)
        if not team or not in_same_company_scope(g.user, team):
            flash("Equipe nao encontrada.", "danger")
            return redirect(form_next_url("company_teams_admin"))

        selected_ids = [
            parse_int(user_id, 0)
            for user_id in request.form.getlist("user_ids")
        ]
        selected_ids = [user_id for user_id in selected_ids if user_id]
        if not selected_ids:
            store_modal_form_state(f"team-dialog-{team.id}")
            flash("Selecione pelo menos um usuario para incluir.", "warning")
            return redirect(form_next_url("company_teams_admin"))

        updated_users = []
        try:
            for user_id in selected_ids:
                user = db.session.get(User, user_id)
                if not user or not in_same_company_scope(g.user, user):
                    continue
                if user.id == g.user.id:
                    raise ValueError("Nao altere sua propria equipe por aqui.")
                before = user_security_snapshot(user)
                user.team = team.name
                if team.franchise_id:
                    user.franchise_id = team.franchise_id
                after = user_security_snapshot(user)
                changes = snapshot_changes(before, after)
                if changes:
                    log_audit(
                        None,
                        g.user,
                        "team_member_add",
                        details={
                            "team_id": team.id,
                            "team": team.name,
                            "target_user_id": user.id,
                            "target_user_email": user.email,
                            "changes": changes,
                        },
                    )
                    updated_users.append(user.name)

            db.session.commit()
            if updated_users:
                flash(f"{len(updated_users)} usuario(s) incluido(s) na equipe {team.name}.", "success")
            else:
                flash("Nenhum usuario foi alterado.", "info")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"team-dialog-{team.id}")
            flash(str(exc), "danger")
        return redirect(form_next_url("company_teams_admin"))

    @app.route("/admin/company/teams/<int:team_id>/members/<int:user_id>/remove", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def remove_team_member(team_id, user_id):
        team = db.session.get(Team, team_id)
        user = db.session.get(User, user_id)
        if not team or not in_same_company_scope(g.user, team):
            flash("Equipe nao encontrada.", "danger")
            return redirect(form_next_url("company_teams_admin"))
        if not user or not in_same_company_scope(g.user, user) or user.team != team.name:
            flash("Usuario nao encontrado nesta equipe.", "danger")
            return redirect(form_next_url("company_teams_admin"))
        if user.id == g.user.id:
            flash("Nao remova seu proprio usuario da equipe por aqui.", "danger")
            return redirect(form_next_url("company_teams_admin"))

        before = user_security_snapshot(user)
        user.team = None
        after = user_security_snapshot(user)
        log_audit(
            None,
            g.user,
            "team_member_remove",
            details={
                "team_id": team.id,
                "team": team.name,
                "target_user_id": user.id,
                "target_user_email": user.email,
                "changes": snapshot_changes(before, after),
            },
        )
        db.session.commit()
        flash(f"{user.name} removido da equipe {team.name}.", "success")
        return redirect(form_next_url("company_teams_admin"))

    @app.route("/admin/settings/teams/<int:team_id>/deactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def deactivate_team(team_id):
        team = db.session.get(Team, team_id)
        if not team or not in_same_company_scope(g.user, team):
            flash("Equipe nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))

        before = team_snapshot(team)
        team.active = False
        after = team_snapshot(team)
        log_audit(
            None,
            g.user,
            "team_deactivate",
            details={"team_id": team.id, "changes": snapshot_changes(before, after)},
        )
        db.session.commit()
        flash("Equipe desativada.", "success")
        return redirect(form_next_url("settings_admin"))

    @app.route("/admin/settings/teams/<int:team_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams")
    def delete_team(team_id):
        team = db.session.get(Team, team_id)
        if not team or not in_same_company_scope(g.user, team):
            flash("Equipe nao encontrada.", "danger")
            return redirect(url_for("settings_admin"))

        usage = team_usage_counts(team)
        if usage["users"] or usage["leads"]:
            flash("Essa equipe esta em uso por usuarios ou clientes. Desative ou renomeie para preservar o historico.", "danger")
            return redirect(url_for("settings_admin"))

        snapshot = team_snapshot(team)
        db.session.delete(team)
        log_audit(None, g.user, "team_delete", details={"team": snapshot})
        db.session.commit()
        flash("Equipe excluida.", "success")
        return redirect(form_next_url("settings_admin"))

    @app.route("/admin/company/franchises/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def create_franchise():
        try:
            name = clean_text(request.form.get("name"))
            if not name:
                raise ValueError("Informe o nome da franquia.")
            if franchise_exists(name):
                raise ValueError("Essa franquia ja existe.")

            franchise = Franchise(
                company_id=company_id_for_user(g.user),
                name=name,
                document=normalize_numeric_text(request.form.get("document")),
                email=clean_text(request.form.get("email")),
                phone=normalize_numeric_text(request.form.get("phone")),
                active=True,
            )
            db.session.add(franchise)
            db.session.flush()
            log_audit(None, g.user, "franchise_create", details={"franchise": franchise_snapshot(franchise)})
            db.session.commit()
            flash("Franquia criada.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state("franchise-create-dialog")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state("franchise-create-dialog")
            flash("Nao foi possivel criar a franquia. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/company/franchises/<int:franchise_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def update_franchise(franchise_id):
        franchise = db.session.get(Franchise, franchise_id)
        if not franchise or not in_same_company_scope(g.user, franchise):
            flash("Franquia nao encontrada.", "danger")
            return redirect(form_next_url("company_franchises_admin"))

        try:
            before = franchise_snapshot(franchise)
            name = clean_text(request.form.get("name"))
            if not name:
                raise ValueError("Informe o nome da franquia.")
            if franchise_exists(name, franchise.id):
                raise ValueError("Essa franquia ja existe.")

            franchise.name = name
            franchise.document = normalize_numeric_text(request.form.get("document"))
            franchise.email = clean_text(request.form.get("email"))
            franchise.phone = normalize_numeric_text(request.form.get("phone"))
            franchise.active = request.form.get("active") == "on"
            after = franchise_snapshot(franchise)
            log_audit(
                None,
                g.user,
                "franchise_update",
                details={"franchise_id": franchise.id, "changes": snapshot_changes(before, after)},
            )
            db.session.commit()
            flash("Franquia atualizada.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash("Nao foi possivel atualizar a franquia. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/company/franchises/<int:franchise_id>/teams/add", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def add_franchise_teams(franchise_id):
        franchise = db.session.get(Franchise, franchise_id)
        if not franchise or not in_same_company_scope(g.user, franchise):
            flash("Franquia nao encontrada.", "danger")
            return redirect(form_next_url("company_franchises_admin"))
        if not franchise.active:
            flash("Ative a franquia antes de vincular equipes.", "warning")
            return redirect(form_next_url("company_franchises_admin"))

        selected_ids = [
            parse_int(team_id, 0)
            for team_id in request.form.getlist("team_ids")
        ]
        selected_ids = [team_id for team_id in selected_ids if team_id]
        if not selected_ids:
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash("Selecione pelo menos uma equipe.", "warning")
            return redirect(form_next_url("company_franchises_admin"))

        sync_members = request.form.get("sync_members") == "on"
        updated_teams = []
        total_users = 0
        total_leads = 0
        try:
            for team_id in selected_ids:
                team = db.session.get(Team, team_id)
                if not team or not in_same_company_scope(g.user, team):
                    continue

                before = team_snapshot(team)
                previous_franchise_id = team.franchise_id
                team.franchise_id = franchise.id
                synced_users = 0
                synced_leads = 0
                if sync_members:
                    synced_users = scoped_company_query(
                        User.query.filter(User.team == team.name),
                        User,
                        g.user,
                        include_unscoped=True,
                    ).update(
                        {"franchise_id": franchise.id},
                        synchronize_session=False,
                    )
                    synced_leads = scoped_company_query(
                        Lead.query.filter(Lead.equipe == team.name),
                        Lead,
                        g.user,
                        include_unscoped=True,
                    ).update(
                        {"franchise_id": franchise.id},
                        synchronize_session=False,
                    )
                after = team_snapshot(team)
                changes = snapshot_changes(before, after)
                if changes or synced_users or synced_leads:
                    log_audit(
                        None,
                        g.user,
                        "franchise_team_attach",
                        details={
                            "franchise_id": franchise.id,
                            "franchise": franchise.name,
                            "team_id": team.id,
                            "team": team.name,
                            "previous_franchise_id": previous_franchise_id,
                            "changes": changes,
                            "synced_users": synced_users,
                            "synced_leads": synced_leads,
                        },
                    )
                    updated_teams.append(team.name)
                    total_users += synced_users
                    total_leads += synced_leads

            db.session.commit()
            if updated_teams:
                flash(
                    f"{len(updated_teams)} equipe(s) vinculada(s) a {franchise.name}. "
                    f"{total_users} usuario(s) e {total_leads} cliente(s) sincronizados.",
                    "success",
                )
            else:
                flash("Nenhuma equipe foi alterada.", "info")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash(str(exc), "danger")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/company/franchises/<int:franchise_id>/users/add", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def add_franchise_users(franchise_id):
        franchise = db.session.get(Franchise, franchise_id)
        if not franchise or not in_same_company_scope(g.user, franchise):
            flash("Franquia nao encontrada.", "danger")
            return redirect(form_next_url("company_franchises_admin"))
        if not franchise.active:
            flash("Ative a franquia antes de vincular usuarios.", "warning")
            return redirect(form_next_url("company_franchises_admin"))

        selected_ids = [
            parse_int(user_id, 0)
            for user_id in request.form.getlist("user_ids")
        ]
        selected_ids = [user_id for user_id in selected_ids if user_id]
        if not selected_ids:
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash("Selecione pelo menos um usuario.", "warning")
            return redirect(form_next_url("company_franchises_admin"))

        updated_users = []
        try:
            for user_id in selected_ids:
                user = db.session.get(User, user_id)
                if not user or not in_same_company_scope(g.user, user):
                    continue
                if user.id == g.user.id:
                    raise ValueError("Nao altere sua propria franquia por aqui.")
                before = user_security_snapshot(user)
                user.franchise_id = franchise.id
                after = user_security_snapshot(user)
                changes = snapshot_changes(before, after)
                if changes:
                    log_audit(
                        None,
                        g.user,
                        "franchise_user_attach",
                        details={
                            "franchise_id": franchise.id,
                            "franchise": franchise.name,
                            "target_user_id": user.id,
                            "target_user_email": user.email,
                            "changes": changes,
                        },
                    )
                    updated_users.append(user.name)

            db.session.commit()
            if updated_users:
                flash(f"{len(updated_users)} usuario(s) vinculado(s) a {franchise.name}.", "success")
            else:
                flash("Nenhum usuario foi alterado.", "info")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"franchise-dialog-{franchise.id}")
            flash(str(exc), "danger")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/company/franchises/<int:franchise_id>/deactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def deactivate_franchise(franchise_id):
        franchise = db.session.get(Franchise, franchise_id)
        if not franchise or not in_same_company_scope(g.user, franchise):
            flash("Franquia nao encontrada.", "danger")
            return redirect(form_next_url("company_franchises_admin"))

        before = franchise_snapshot(franchise)
        franchise.active = False
        after = franchise_snapshot(franchise)
        log_audit(
            None,
            g.user,
            "franchise_deactivate",
            details={"franchise_id": franchise.id, "changes": snapshot_changes(before, after)},
        )
        db.session.commit()
        flash("Franquia desativada.", "success")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/company/franchises/<int:franchise_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_teams", "can_manage_users")
    def delete_franchise(franchise_id):
        franchise = db.session.get(Franchise, franchise_id)
        if not franchise or not in_same_company_scope(g.user, franchise):
            flash("Franquia nao encontrada.", "danger")
            return redirect(form_next_url("company_franchises_admin"))

        usage = franchise_usage_counts(franchise)
        if any(usage.values()):
            flash("Essa franquia esta em uso. Desative para preservar usuarios, equipes e clientes.", "danger")
            return redirect(form_next_url("company_franchises_admin"))

        snapshot = franchise_snapshot(franchise)
        db.session.delete(franchise)
        log_audit(None, g.user, "franchise_delete", details={"franchise": snapshot})
        db.session.commit()
        flash("Franquia excluida.", "success")
        return redirect(form_next_url("company_franchises_admin"))

    @app.route("/admin/settings/role-profiles/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles")
    def create_role_profile():
        try:
            profile = build_role_profile_from_form(RoleProfile(), request.form)
            profile.company_id = company_id_for_user(g.user)
            assert_actor_can_assign_permissions(g.user, profile)
            if role_profile_exists(profile.name):
                raise ValueError("Esse cargo ja existe.")
            db.session.add(profile)
            db.session.flush()
            log_audit(None, g.user, "role_profile_create", details={"role_profile": role_profile_snapshot(profile)})
            db.session.commit()
            flash("Cargo criado.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state("role-create-dialog")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state("role-create-dialog")
            flash("Nao foi possivel criar o cargo. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/settings/role-profiles/<int:profile_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles")
    def update_role_profile(profile_id):
        profile = db.session.get(RoleProfile, profile_id)
        if not profile or not in_same_company_scope(g.user, profile):
            flash("Cargo nao encontrado.", "danger")
            return redirect(form_next_url("company_roles_admin"))

        try:
            before = role_profile_snapshot(profile)
            build_role_profile_from_form(profile, request.form)
            assert_actor_can_assign_permissions(g.user, profile)
            if role_profile_exists(profile.name, profile.id):
                raise ValueError("Esse cargo ja existe.")

            updated_users = 0
            if request.form.get("apply_existing") == "on":
                linked_users = User.query.filter(User.role_profile_id == profile.id).all()
                for user in linked_users:
                    apply_role_profile_to_user(user, profile)
                updated_users = len(linked_users)
                assert_system_has_admin_control()

            after = role_profile_snapshot(profile)
            log_audit(
                None,
                g.user,
                "role_profile_update",
                details={
                    "role_profile_id": profile.id,
                    "changes": snapshot_changes(before, after),
                    "updated_users": updated_users,
                },
            )
            db.session.commit()
            flash("Cargo atualizado.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"role-dialog-{profile.id}")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state(f"role-dialog-{profile.id}")
            flash("Nao foi possivel atualizar o cargo. Verifique se o nome ja esta em uso.", "danger")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/company/roles/<int:profile_id>/users/add", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles", "can_manage_users")
    def add_role_profile_users(profile_id):
        profile = db.session.get(RoleProfile, profile_id)
        if not profile or not in_same_company_scope(g.user, profile):
            flash("Cargo nao encontrado.", "danger")
            return redirect(form_next_url("company_roles_admin"))
        if not profile.active:
            flash("Ative o cargo antes de vincular usuarios.", "warning")
            return redirect(form_next_url("company_roles_admin"))

        selected_ids = [
            parse_int(user_id, 0)
            for user_id in request.form.getlist("user_ids")
        ]
        selected_ids = [user_id for user_id in selected_ids if user_id]
        if not selected_ids:
            store_modal_form_state(f"role-dialog-{profile.id}")
            flash("Selecione pelo menos um usuario.", "warning")
            return redirect(form_next_url("company_roles_admin"))

        updated_users = []
        try:
            for user_id in selected_ids:
                user = db.session.get(User, user_id)
                if not user or not in_same_company_scope(g.user, user):
                    continue
                if user.id == g.user.id:
                    raise ValueError("Nao altere seu proprio cargo por aqui.")

                before = user_security_snapshot(user)
                apply_role_profile_to_user(user, profile)
                assert_actor_can_assign_permissions(g.user, user)
                assert_not_removing_last_admin(before, user)
                after = user_security_snapshot(user)
                changes = snapshot_changes(before, after)
                if changes:
                    log_audit(
                        None,
                        g.user,
                        "role_profile_user_attach",
                        details={
                            "role_profile_id": profile.id,
                            "role_profile": profile.name,
                            "target_user_id": user.id,
                            "target_user_email": user.email,
                            "changes": changes,
                        },
                    )
                    updated_users.append(user.name)

            assert_system_has_admin_control()
            db.session.commit()
            if updated_users:
                flash(f"{len(updated_users)} usuario(s) vinculado(s) ao cargo {profile.name}.", "success")
            else:
                flash("Nenhum usuario foi alterado.", "info")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"role-dialog-{profile.id}")
            flash(str(exc), "danger")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/company/roles/<int:profile_id>/users/<int:user_id>/remove", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles", "can_manage_users")
    def remove_role_profile_user(profile_id, user_id):
        profile = db.session.get(RoleProfile, profile_id)
        user = db.session.get(User, user_id)
        if not profile or not in_same_company_scope(g.user, profile):
            flash("Cargo nao encontrado.", "danger")
            return redirect(form_next_url("company_roles_admin"))
        if not user or not in_same_company_scope(g.user, user) or user.role_profile_id != profile.id:
            flash("Usuario nao encontrado neste cargo.", "danger")
            return redirect(form_next_url("company_roles_admin"))
        if user.id == g.user.id:
            flash("Nao remova seu proprio cargo por aqui.", "danger")
            return redirect(form_next_url("company_roles_admin"))

        before = user_security_snapshot(user)
        user.role_profile_id = None
        after = user_security_snapshot(user)
        log_audit(
            None,
            g.user,
            "role_profile_user_detach",
            details={
                "role_profile_id": profile.id,
                "role_profile": profile.name,
                "target_user_id": user.id,
                "target_user_email": user.email,
                "changes": snapshot_changes(before, after),
            },
        )
        db.session.commit()
        flash(f"{user.name} agora esta com permissoes personalizadas.", "success")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/settings/role-profiles/<int:profile_id>/deactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles")
    def deactivate_role_profile(profile_id):
        profile = db.session.get(RoleProfile, profile_id)
        if not profile or not in_same_company_scope(g.user, profile):
            flash("Cargo nao encontrado.", "danger")
            return redirect(form_next_url("company_roles_admin"))

        before = role_profile_snapshot(profile)
        profile.active = False
        after = role_profile_snapshot(profile)
        log_audit(
            None,
            g.user,
            "role_profile_deactivate",
            details={"role_profile_id": profile.id, "changes": snapshot_changes(before, after)},
        )
        db.session.commit()
        flash("Cargo desativado.", "success")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/settings/role-profiles/<int:profile_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_roles")
    def delete_role_profile(profile_id):
        profile = db.session.get(RoleProfile, profile_id)
        if not profile or not in_same_company_scope(g.user, profile):
            flash("Cargo nao encontrado.", "danger")
            return redirect(form_next_url("company_roles_admin"))

        linked_users = User.query.filter(User.role_profile_id == profile.id).count()
        if linked_users:
            flash("Esse cargo esta vinculado a usuarios. Desative ou remova o cargo dos usuarios antes de excluir.", "danger")
            return redirect(form_next_url("company_roles_admin"))

        snapshot = role_profile_snapshot(profile)
        db.session.delete(profile)
        log_audit(None, g.user, "role_profile_delete", details={"role_profile": snapshot})
        db.session.commit()
        flash("Cargo excluido.", "success")
        return redirect(form_next_url("company_roles_admin"))

    @app.route("/admin/company/users")
    @app.route("/admin/users")
    @login_required
    @permission_required("can_manage_users")
    def users_admin():
        user_filters = {
            "q": clean_text(request.args.get("q")),
            "role": clean_text(request.args.get("role")),
            "role_profile_id": clean_text(request.args.get("role_profile_id")),
            "franchise_id": clean_text(request.args.get("franchise_id")),
            "team": clean_text(request.args.get("team")),
            "active": clean_text(request.args.get("active")) or "active",
        }
        users_query = scoped_company_query(User.query, User, g.user, include_unscoped=True)
        if user_filters["q"]:
            search = f"%{user_filters['q'].lower()}%"
            users_query = users_query.filter(
                or_(
                    func.lower(User.name).like(search),
                    func.lower(User.lead_name).like(search),
                    func.lower(User.email).like(search),
                    func.lower(User.username).like(search),
                )
            )
        if user_filters["role"] in Role.ALL:
            users_query = users_query.filter(User.role == user_filters["role"])
        role_profile_filter_id = parse_int(user_filters["role_profile_id"], 0)
        if role_profile_filter_id:
            users_query = users_query.filter(User.role_profile_id == role_profile_filter_id)
        franchise_filter_id = parse_int(user_filters["franchise_id"], 0)
        if franchise_filter_id:
            users_query = users_query.filter(User.franchise_id == franchise_filter_id)
        if user_filters["team"]:
            users_query = users_query.filter(User.team == user_filters["team"])
        if user_filters["active"] == "active":
            users_query = users_query.filter(User.active.is_(True))
        elif user_filters["active"] == "inactive":
            users_query = users_query.filter(User.active.is_(False))

        users = users_query.order_by(User.active.desc(), User.role.asc(), User.name.asc()).all()
        access_requests = scoped_company_query(
            GoogleAccessRequest.query,
            GoogleAccessRequest,
            g.user,
            include_unscoped=True,
        ).order_by(
            GoogleAccessRequest.last_attempt_at.desc(),
            GoogleAccessRequest.requested_at.desc(),
        ).all()
        access_requests = sorted(
            access_requests,
            key=lambda access_request: {
                "PENDING": 0,
                "REJECTED": 1,
                "APPROVED": 2,
            }.get(access_request.status, 3),
        )
        teams = get_active_team_names()
        franchises = get_company_franchises(g.user, include_inactive=True)
        return render_template(
            "users.html",
            users=users,
            access_requests=access_requests,
            current_user=g.user,
            teams=teams,
            franchises=franchises,
            consultores=get_known_consultores(),
            lead_counts_by_consultor=get_lead_counts_by_consultor(),
            user_filters=user_filters,
            roles=Role.ALL,
            role_profiles=active_role_profiles(),
            permission_fields=PERMISSION_FIELDS,
            permission_labels=PERMISSION_LABELS,
            permission_groups=PERMISSION_GROUPS,
            user_permission_counts={
                user.id: enabled_permission_count(user)
                for user in users
            },
        )

    @app.route("/admin/access-requests/<int:request_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def update_access_request(request_id):
        access_request = db.session.get(GoogleAccessRequest, request_id)
        if not access_request or not in_same_company_scope(g.user, access_request):
            flash("Solicitação não encontrada.", "danger")
            return redirect(form_next_url("users_admin"))

        decision = request.form.get("decision")
        try:
            if decision == "approve":
                profile = get_role_profile_from_form(request.form)
                role = profile.base_role if profile else (
                    request.form.get("role") if request.form.get("role") in Role.ALL else Role.OPERADOR
                )
                franchise_id = validate_franchise_id_for_form(
                    request.form.get("franchise_id"),
                    g.user,
                    fallback_id=access_request.franchise_id or franchise_id_for_user(g.user),
                )
                team = validate_user_team_for_form(request.form.get("team"), role, franchise_id=franchise_id)
                lead_name = clean_text(request.form.get("lead_name"))

                user = user_for_google_access_request(access_request)
                if not user:
                    user = User(
                        company_id=access_request.company_id or company_id_for_user(g.user),
                        franchise_id=franchise_id,
                        username=unique_username(access_request.email),
                        email=access_request.email,
                        google_sub=access_request.google_sub,
                        name=access_request.name,
                        lead_name=lead_name,
                        role=role,
                        role_profile_id=profile.id if profile else None,
                        team=team,
                        active=True,
                    )
                    user.set_password("")
                    db.session.add(user)
                else:
                    user.company_id = user.company_id or access_request.company_id or company_id_for_user(g.user)
                    user.franchise_id = franchise_id
                    user.email = access_request.email
                    user.google_sub = access_request.google_sub or user.google_sub
                    user.name = user.name or access_request.name
                    user.lead_name = lead_name
                    user.active = True
                    user.role = role
                    user.role_profile_id = profile.id if profile else None
                    user.team = team

                if profile:
                    apply_role_profile_to_user(user, profile)
                else:
                    user.apply_role_defaults()
                assert_actor_can_assign_permissions(g.user, user)
                access_request.status = "APPROVED"
                access_request.franchise_id = franchise_id
                access_request.approved_by_id = g.user.id
                access_request.approved_at = datetime.now()
                access_request.notes = clean_text(request.form.get("notes"))
                db.session.flush()
                log_audit(
                    None,
                    g.user,
                    "google_access_approve",
                    details={
                        "request_id": access_request.id,
                        "target_user_id": user.id,
                        "email": access_request.email,
                        "role": user.role,
                        "role_profile": user.role_profile.name if user.role_profile else None,
                        "team": user.team,
                        "lead_name": user.lead_name,
                    },
                )
                db.session.commit()
                flash("Acesso Google aprovado. O usuário já pode entrar.", "success")
            elif decision == "reject":
                access_request.status = "REJECTED"
                access_request.rejected_by_id = g.user.id
                access_request.rejected_at = datetime.now()
                access_request.notes = clean_text(request.form.get("notes"))
                log_audit(
                    None,
                    g.user,
                    "google_access_reject",
                    details={
                        "request_id": access_request.id,
                        "email": access_request.email,
                        "notes": access_request.notes,
                    },
                )
                db.session.commit()
                flash("Solicitação de acesso recusada.", "info")
            else:
                raise ValueError("Ação inválida para a solicitação.")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"access-request-dialog-{access_request.id}")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state(f"access-request-dialog-{access_request.id}")
            flash("Nao foi possivel aprovar este acesso. Verifique se o e-mail ja esta em uso.", "danger")

        return redirect(form_next_url("users_admin"))

    @app.route("/admin/users/create", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def create_user():
        try:
            password = request.form.get("password")
            validate_local_password(password)
            profile = get_role_profile_from_form(request.form)
            role = profile.base_role if profile else (
                request.form.get("role") if request.form.get("role") in Role.ALL else Role.OPERADOR
            )
            franchise_id = validate_franchise_id_for_form(
                request.form.get("franchise_id"),
                g.user,
                fallback_id=franchise_id_for_user(g.user),
            )
            team = validate_user_team_for_form(request.form.get("team"), role, franchise_id=franchise_id)
            user = User(
                company_id=company_id_for_user(g.user),
                franchise_id=franchise_id,
                username=unique_username(request.form.get("email") or request.form.get("username")),
                email=validate_unique_user_email(request.form.get("email")),
                name=clean_text(request.form.get("name")) or "",
                lead_name=clean_text(request.form.get("lead_name")),
                team=team,
                role=role,
                role_profile_id=profile.id if profile else None,
                active=True,
            )
            user.set_password(
                password,
                must_change=bool(password and request.form.get("force_password_change") == "on"),
            )
            if profile:
                apply_role_profile_to_user(user, profile)
            else:
                user.apply_role_defaults()
            assert_actor_can_assign_permissions(g.user, user)
            db.session.add(user)
            db.session.flush()
            log_audit(
                None,
                g.user,
                "user_create",
                details={
                    "target_user": user_security_snapshot(user),
                    "password_set": bool(password),
                },
            )
            db.session.commit()
            flash("Usuário criado. Ajuste permissões específicas abaixo, se precisar.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state("user-create-dialog")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state("user-create-dialog")
            flash("Nao foi possivel criar o usuario. Verifique se e-mail ou login ja estao em uso.", "danger")
        return redirect(form_next_url("users_admin"))

    @app.route("/admin/users/<int:user_id>/update", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def update_user(user_id):
        user = db.session.get(User, user_id)
        if not user or not in_same_company_scope(g.user, user):
            flash("Usuário não encontrado.", "danger")
            return redirect(form_next_url("users_admin"))

        try:
            before = user_security_snapshot(user)
            is_self_update = user.id == g.user.id
            user.name = clean_text(request.form.get("name")) or user.name
            user.lead_name = clean_text(request.form.get("lead_name"))
            user.email = validate_unique_user_email(request.form.get("email"), user.id)

            if is_self_update:
                user.active = True
            else:
                profile = get_role_profile_from_form(request.form)
                target_role = profile.base_role if profile else (
                    request.form.get("role") if request.form.get("role") in Role.ALL else user.role
                )
                franchise_id = validate_franchise_id_for_form(
                    request.form.get("franchise_id"),
                    g.user,
                    fallback_id=user.franchise_id or franchise_id_for_user(g.user),
                )
                user.team = validate_user_team_for_form(
                    request.form.get("team"),
                    target_role,
                    current_team=user.team if franchise_id == user.franchise_id else None,
                    franchise_id=franchise_id,
                )
                user.franchise_id = franchise_id
                if profile:
                    apply_role_profile_to_user(user, profile)
                else:
                    user.role_profile_id = None
                    user.role = target_role
                    for field in PERMISSION_FIELDS:
                        setattr(user, field, request.form.get(field) == "on")
                user.active = request.form.get("active") == "on"
                assert_actor_can_assign_permissions(g.user, user)
                assert_not_removing_last_admin(before, user)

            password = request.form.get("password")
            if password:
                validate_local_password(password)
                user.set_password(
                    password,
                    must_change=bool(
                        not is_self_update and request.form.get("force_password_change") == "on"
                    ),
                )
            elif not is_self_update:
                user.must_change_password = bool(
                    user.password_hash and request.form.get("force_password_change") == "on"
                )

            after = user_security_snapshot(user)
            changes = snapshot_changes(before, after)
            if changes or password:
                log_audit(
                    None,
                    g.user,
                    "user_update",
                    details={
                        "target_user_id": user.id,
                        "target_user_email": user.email,
                        "changes": changes,
                        "password_changed": bool(password),
                    },
                )

            db.session.commit()
            flash("Usuário atualizado.", "success")
        except ValueError as exc:
            db.session.rollback()
            store_modal_form_state(f"user-dialog-{user.id}")
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            store_modal_form_state(f"user-dialog-{user.id}")
            flash("Nao foi possivel atualizar o usuario. Verifique se e-mail ou login ja estao em uso.", "danger")
        return redirect(form_next_url("users_admin"))

    @app.route("/admin/users/<int:user_id>/deactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def deactivate_user(user_id):
        user = db.session.get(User, user_id)
        if not user or not in_same_company_scope(g.user, user):
            flash("Usuario nao encontrado.", "danger")
            return redirect(form_next_url("users_admin"))

        if user.id == g.user.id:
            flash("Voce nao pode desativar seu proprio usuario.", "danger")
            return redirect(form_next_url("users_admin"))

        if not user.active:
            flash("Esse usuario ja esta desativado.", "info")
            return redirect(form_next_url("users_admin"))

        before = user_security_snapshot(user)
        user.active = False
        try:
            assert_not_removing_last_admin(before, user)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(form_next_url("users_admin"))
        after = user_security_snapshot(user)
        log_audit(
            None,
            g.user,
            "user_deactivate",
            details={
                "target_user_id": user.id,
                "target_user_email": user.email,
                "changes": snapshot_changes(before, after),
            },
        )
        db.session.commit()
        flash("Acesso desativado sem apagar historico.", "success")
        return redirect(form_next_url("users_admin", active="inactive"))

    @app.route("/admin/users/<int:user_id>/reactivate", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def reactivate_user(user_id):
        user = db.session.get(User, user_id)
        if not user or not in_same_company_scope(g.user, user):
            flash("Usuario nao encontrado.", "danger")
            return redirect(form_next_url("users_admin"))

        if user.active:
            flash("Esse usuario ja esta ativo.", "info")
            return redirect(form_next_url("users_admin"))

        before = user_security_snapshot(user)
        user.active = True
        try:
            assert_actor_can_assign_permissions(g.user, user)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(form_next_url("users_admin", active="inactive"))

        after = user_security_snapshot(user)
        log_audit(
            None,
            g.user,
            "user_reactivate",
            details={
                "target_user_id": user.id,
                "target_user_email": user.email,
                "changes": snapshot_changes(before, after),
            },
        )
        db.session.commit()
        flash("Usuario reativado.", "success")
        return redirect(form_next_url("users_admin"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @login_required
    @permission_required("can_manage_users")
    def delete_user(user_id):
        user = db.session.get(User, user_id)
        if not user or not in_same_company_scope(g.user, user):
            flash("Usuario nao encontrado.", "danger")
            return redirect(form_next_url("users_admin"))

        if user.id == g.user.id:
            flash("Voce nao pode excluir seu proprio usuario.", "danger")
            return redirect(form_next_url("users_admin"))

        usage = user_usage_counts(user)
        if any(usage.values()):
            flash("Esse usuario tem historico no CRM. Desative o acesso para preservar auditoria.", "danger")
            return redirect(form_next_url("users_admin"))
        if is_admin_control_user(user) and active_admin_control_count(exclude_user_id=user.id) == 0:
            flash("Mantenha pelo menos um ADMIN ativo com acesso total antes de excluir este usuario.", "danger")
            return redirect(form_next_url("users_admin"))

        snapshot = user_security_snapshot(user)
        db.session.delete(user)
        log_audit(None, g.user, "user_delete", details={"target_user": snapshot})
        db.session.commit()
        flash("Usuario excluido.", "success")
        return redirect(form_next_url("users_admin"))


def user_for_google_access_request(access_request):
    if access_request.google_sub:
        user = User.query.filter_by(google_sub=access_request.google_sub).first()
        if user:
            return user
    return User.query.filter(func.lower(User.email) == access_request.email.lower()).first()


def get_or_create_customer_for_contract(company_id, cpf, nome=None, contato=None):
    if not company_id or not cpf:
        return None

    customer = Customer.query.filter(
        Customer.company_id == company_id,
        Customer.cpf == cpf,
    ).first()
    if not customer:
        customer = Customer(
            company_id=company_id,
            cpf=cpf,
            nome=clean_text(nome),
            contato=normalize_numeric_text(contato) or clean_text(contato),
        )
        db.session.add(customer)
    else:
        customer.nome = customer.nome or clean_text(nome)
        customer.contato = customer.contato or normalize_numeric_text(contato) or clean_text(contato)
    return customer


def lead_contract_query(cpf, company_id, franchise_id):
    return Lead.query.filter(
        Lead.company_id == company_id,
        Lead.franchise_id == franchise_id,
        Lead.cpf == cpf,
    )


def lead_exists_in_contract_scope(cpf, company_id, franchise_id, current_id=None):
    if not cpf or not company_id or not franchise_id:
        return False
    query = lead_contract_query(cpf, company_id, franchise_id)
    if current_id:
        query = query.filter(Lead.id != current_id)
    return db.session.query(query.exists()).scalar()


def visible_lead_by_cpf_for_maintenance(user, cpf, action):
    state = "inactive" if action == "reactivate" else "active"
    matches = visible_leads_query(user, state=state).filter(Lead.cpf == cpf).limit(2).all()
    if len(matches) > 1:
        raise ValueError(
            "CPF encontrado em mais de uma franquia visivel. Use o ID do cliente para esta acao."
        )
    return matches[0] if matches else None


def build_manual_lead(form, user):
    cpf = normalize_cpf(form.get("cpf"))
    if not cpf:
        raise ValueError("Informe um CPF válido.")

    nome_cliente = clean_text(form.get("nome_cliente"))
    if not nome_cliente:
        raise ValueError("Informe o nome do cliente.")

    company_id = company_id_for_user(user)
    franchise_id = target_franchise_id_from_form(form, user)
    equipe, consultor = scoped_lead_assignment(form, user, franchise_id=franchise_id)
    if not equipe or not consultor:
        raise ValueError("Informe equipe e consultor para o cliente.")

    if lead_exists_in_contract_scope(cpf, company_id, franchise_id):
        raise ValueError("Já existe um cliente cadastrado com esse CPF nesta franquia.")

    lead = Lead(
        company_id=company_id,
        franchise_id=franchise_id,
        equipe=equipe,
        consultor=consultor,
        nome_cliente=nome_cliente,
        cpf=cpf,
        data_venda=parse_date(form.get("data_venda")),
        concessao=parse_date(form.get("concessao")),
    )

    if user.can_edit_contact:
        lead.contato = normalize_numeric_text(form.get("contato"))
        lead.contato_manual = bool(lead.contato)

    lead.customer = get_or_create_customer_for_contract(
        company_id,
        cpf,
        nome=lead.nome_cliente,
        contato=lead.contato,
    )

    if user.can_edit_status:
        lead.status = selected_option("status", form.get("status"))
        lead.status_manual = bool(lead.status)

    ensure_lead_new_status(lead)
    return lead


def create_initial_lead_activity(lead, user, body, lead_workflow=None):
    body = clean_text(body)
    if not body:
        return None

    lead_workflow = lead_workflow or current_operational_lead_workflow_for_lead(lead)
    now = datetime.now()
    activity = LeadActivity(
        lead_id=lead.id,
        user_id=user.id if user else None,
        lead_workflow_id=lead_workflow.id if lead_workflow else None,
        workflow_id=lead_workflow.workflow_id if lead_workflow else None,
        stage_id=lead_workflow.current_stage_id if lead_workflow else None,
        body=body,
        source="manual_create",
        created_at=now,
        updated_at=now,
    )
    db.session.add(activity)
    if lead_workflow:
        lead_workflow.last_action_at = now
    ensure_lead_in_progress_status(lead, user, source="manual_create_activity")
    log_audit(
        lead,
        user,
        "activity_create",
        field_name="timeline",
        new_value=body,
        details=activity_details(lead_workflow),
    )
    return activity


def user_security_snapshot(user):
    snapshot = {
        "id": user.id,
        "company_id": user.company_id,
        "company": user.company.name if user.company else None,
        "franchise_id": user.franchise_id,
        "franchise": user.franchise.name if user.franchise else None,
        "name": user.name,
        "lead_name": user.lead_name,
        "email": user.email,
        "role": user.role,
        "role_profile_id": user.role_profile_id,
        "role_profile": user.role_profile.name if user.role_profile else None,
        "team": user.team,
        "active": user.active,
        "must_change_password": bool(user.must_change_password),
        "last_login_method": user.last_login_method,
        "last_login_at": user.last_login_at,
        "password_changed_at": user.password_changed_at,
    }
    for field in PERMISSION_FIELDS:
        snapshot[field] = bool(getattr(user, field, False))
    return snapshot


def snapshot_changes(before, after):
    changes = {}
    for key, old_value in before.items():
        new_value = after.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    return changes


def selected_admin_workflow(workflows):
    if not workflows:
        return None
    requested_id = parse_int(request.args.get("workflow_id"), 0)
    for workflow in workflows:
        if workflow.id == requested_id:
            return workflow
    for workflow in workflows:
        if workflow.active:
            return workflow
    return workflows[0]


def build_workflow_from_form(workflow, form):
    name = clean_text(form.get("name"))
    if not name:
        raise ValueError("Informe o nome do fluxo.")
    slug = normalize_slug(form.get("slug")) or normalize_slug(name)
    if not slug:
        raise ValueError("Informe um identificador valido para o fluxo.")

    workflow.name = name
    workflow.slug = slug
    workflow.description = clean_text(form.get("description"))
    workflow.sort_order = parse_int(form.get("sort_order"), workflow.sort_order or 0)
    workflow.active = form.get("active") == "on"
    workflow.auto_move_enabled = form.get("auto_move_enabled") == "on"
    return workflow


def build_workflow_stage_from_form(stage, form):
    name = clean_text(form.get("name"))
    if not name:
        raise ValueError("Informe o nome da etapa.")
    slug = normalize_slug(form.get("slug")) or normalize_slug(name)
    if not slug:
        raise ValueError("Informe um identificador valido para a etapa.")

    stage.name = name
    stage.slug = slug
    stage.description = clean_text(form.get("description"))
    stage.sort_order = parse_int(form.get("sort_order"), stage.sort_order or 0)
    stage.active = form.get("active") == "on"
    stage.max_days = parse_optional_int(form.get("max_days"))
    stage.alert_days_before = parse_optional_int(form.get("alert_days_before"))
    stage.first_action_hours = parse_optional_int(form.get("first_action_hours"))
    stage.requires_support = form.get("requires_support") == "on"
    stage.support_sla_hours = parse_optional_int(form.get("support_sla_hours"))
    stage.requires_note = form.get("requires_note") == "on"
    stage.requires_checklist = form.get("requires_checklist") == "on"
    stage.allow_auto_move = form.get("allow_auto_move") == "on"
    stage.required_fields = serialize_required_fields(required_fields_from_form(form))
    return stage


def build_workflow_checklist_item_from_form(item, form):
    label = clean_text(form.get("label"))
    if not label:
        raise ValueError("Informe o texto da tarefa.")
    item.label = label
    item.description = clean_text(form.get("description"))
    item.sort_order = parse_int(form.get("sort_order"), item.sort_order or 0)
    item.required = form.get("required") == "on"
    item.active = form.get("active") == "on"
    return item


def required_fields_from_form(form):
    return [
        field
        for field, _label in WORKFLOW_REQUIRED_FIELD_CHOICES
        if form.get(f"required_field_{field}") == "on"
    ]


def parse_optional_int(value):
    if value is None:
        return None
    value = clean_text(value)
    if value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("Informe numeros validos nos campos de prazo.")
    return max(parsed, 0)


def normalize_slug(value):
    value = clean_text(value)
    if not value:
        return ""
    without_accents = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in without_accents if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:80]


def workflow_slug_exists(slug, current_id=None):
    query = Workflow.query.filter(func.lower(Workflow.slug) == slug.lower())
    if current_id:
        query = query.filter(Workflow.id != current_id)
    return db.session.query(query.exists()).scalar()


def workflow_stage_slug_exists(workflow_id, slug, current_id=None):
    query = WorkflowStage.query.filter(
        WorkflowStage.workflow_id == workflow_id,
        func.lower(WorkflowStage.slug) == slug.lower(),
    )
    if current_id:
        query = query.filter(WorkflowStage.id != current_id)
    return db.session.query(query.exists()).scalar()


def workflow_checklist_label_exists(stage_id, label, current_id=None):
    query = WorkflowChecklistItem.query.filter(
        WorkflowChecklistItem.stage_id == stage_id,
        func.lower(WorkflowChecklistItem.label) == label.lower(),
    )
    if current_id:
        query = query.filter(WorkflowChecklistItem.id != current_id)
    return db.session.query(query.exists()).scalar()


def workflow_checklists_by_stage(stages):
    stage_ids = [stage.id for stage in stages]
    if not stage_ids:
        return {}
    items = (
        WorkflowChecklistItem.query.filter(WorkflowChecklistItem.stage_id.in_(stage_ids))
        .order_by(
            WorkflowChecklistItem.active.desc(),
            WorkflowChecklistItem.sort_order.asc(),
            WorkflowChecklistItem.id.asc(),
        )
        .all()
    )
    result = {stage.id: [] for stage in stages}
    for item in items:
        result.setdefault(item.stage_id, []).append(item)
    return result


def workflow_snapshot(workflow):
    return {
        "id": workflow.id,
        "company_id": workflow.company_id,
        "slug": workflow.slug,
        "name": workflow.name,
        "description": workflow.description,
        "active": workflow.active,
        "sort_order": workflow.sort_order,
        "auto_move_enabled": workflow.auto_move_enabled,
    }


def workflow_stage_snapshot(stage):
    return {
        "id": stage.id,
        "workflow_id": stage.workflow_id,
        "slug": stage.slug,
        "name": stage.name,
        "description": stage.description,
        "active": stage.active,
        "sort_order": stage.sort_order,
        "max_days": stage.max_days,
        "alert_days_before": stage.alert_days_before,
        "first_action_hours": stage.first_action_hours,
        "requires_support": stage.requires_support,
        "support_sla_hours": stage.support_sla_hours,
        "requires_note": stage.requires_note,
        "requires_checklist": stage.requires_checklist,
        "required_fields": stage_required_fields(stage),
        "allow_auto_move": stage.allow_auto_move,
    }


def workflow_checklist_item_snapshot(item):
    return {
        "id": item.id,
        "stage_id": item.stage_id,
        "label": item.label,
        "description": item.description,
        "sort_order": item.sort_order,
        "required": item.required,
        "active": item.active,
    }


def workflow_usage_counts(workflow):
    return {
        "lead_workflows": LeadWorkflow.query.filter_by(workflow_id=workflow.id).count(),
        "stages": WorkflowStage.query.filter_by(workflow_id=workflow.id).count(),
        "transitions": WorkflowTransition.query.filter_by(workflow_id=workflow.id).count(),
        "activities": LeadActivity.query.filter_by(workflow_id=workflow.id).count(),
    }


def workflow_stage_usage_counts(stage):
    return {
        "task_snapshots": ChecklistStageConfiguration.query.filter_by(stage_id=stage.id).count(),
        "lead_workflows": LeadWorkflow.query.filter_by(current_stage_id=stage.id).count(),
        "transitions": WorkflowTransition.query.filter(
            or_(WorkflowTransition.from_stage_id == stage.id, WorkflowTransition.to_stage_id == stage.id)
        ).count(),
        "activities": LeadActivity.query.filter_by(stage_id=stage.id).count(),
        "checklist_items": WorkflowChecklistItem.query.filter_by(stage_id=stage.id).count(),
    }


def workflow_stage_active_client_count(stage):
    return (
        LeadWorkflow.query.join(Lead, LeadWorkflow.lead_id == Lead.id)
        .filter(
            LeadWorkflow.current_stage_id == stage.id,
            Lead.deleted_at.is_(None),
            ~LeadWorkflow.status.in_(("concluido", "recusado", "inativo")),
        )
        .count()
    )


def workflow_checklist_item_usage_count(item):
    return LeadWorkflowChecklistCheck.query.filter_by(checklist_item_id=item.id).count()


def active_role_profiles():
    return (
        scoped_company_query(
            RoleProfile.query.filter(RoleProfile.active.is_(True)),
            RoleProfile,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(RoleProfile.sort_order.asc(), RoleProfile.name.asc())
        .all()
    )


def get_role_profile_from_form(form):
    profile_id = parse_int(form.get("role_profile_id"), 0)
    if not profile_id:
        return None
    profile = db.session.get(RoleProfile, profile_id)
    if not profile or not profile.active or not in_same_company_scope(request_user_or_none(), profile):
        raise ValueError("Cargo/perfil invalido.")
    return profile


def apply_role_profile_to_user(user, profile):
    user.role_profile_id = profile.id
    user.role = profile.base_role
    for field in PERMISSION_FIELDS:
        setattr(user, field, bool(getattr(profile, field, False)))
    return user


def scoped_lead_assignment(form, user, franchise_id=None):
    if user.can_view_all:
        equipe = clean_text(form.get("equipe"))
        consultor = clean_text(form.get("consultor"))
        if equipe not in get_teams_for_create(user, franchise_id=franchise_id):
            raise ValueError("Essa equipe nao esta disponivel para cadastro.")
        if consultor not in get_consultores_for_create(user, equipe, franchise_id=franchise_id):
            raise ValueError("Esse consultor nao esta disponivel para essa equipe.")
        return equipe, consultor

    if user.role == Role.SUPERVISOR:
        consultor = clean_text(form.get("consultor"))
        allowed_consultores = get_consultores_for_create(user, user.team, franchise_id=franchise_id)
        if consultor not in allowed_consultores:
            raise ValueError("Esse consultor não pertence à sua equipe.")
        return user.team, consultor

    return user.team, lead_assignment_name(user)


def selected_option(field_name, value):
    value = clean_text(value)
    if not value:
        return None
    if value not in get_active_options(field_name):
        raise ValueError("Opção inválida enviada no formulário.")
    return value


def lead_team_for_form(user, requested_team, franchise_id=None):
    if user.can_view_all:
        team = clean_text(requested_team)
        return team if team in get_teams_for_create(user, franchise_id=franchise_id) else None
    return user.team


def get_teams_for_create(user, franchise_id=None):
    if user.can_view_all:
        return get_active_team_names(user=user, franchise_id=franchise_id or franchise_id_for_user(user))
    if user.team:
        return [user.team]
    return []


def get_consultores_by_team_for_create(user, teams=None, franchise_id=None):
    teams = teams or get_teams_for_create(user, franchise_id=franchise_id)
    return {team: get_consultores_for_create(user, team, franchise_id=franchise_id) for team in teams if team}


def validate_user_team_for_form(team_value, role, current_team=None, franchise_id=None):
    team = clean_text(team_value)
    current_team = clean_text(current_team)
    if role == Role.ADMIN and not team:
        return None
    if not team:
        raise ValueError("Informe uma equipe para este usuario.")
    if team != current_team and team not in get_active_team_names(franchise_id=franchise_id):
        raise ValueError("Essa equipe nao esta ativa ou nao existe no painel de equipes.")
    return team


def find_or_request_google_user(profile, admin_emails, allowed_domains):
    email = (profile.get("email") or "").strip().lower()
    google_sub = profile.get("sub")
    if not email:
        return None, "not_allowed"

    user = None
    if google_sub:
        user = User.query.filter_by(google_sub=google_sub).first()
    if not user:
        user = User.query.filter(func.lower(User.email) == email).first()

    if user:
        company, franchise = ensure_default_company_structure()
        user.company_id = user.company_id or company.id
        user.franchise_id = user.franchise_id or franchise.id
        user.google_sub = google_sub or user.google_sub
        user.email = email
        db.session.commit()
        return user, "active"

    if email not in admin_emails:
        if not google_signup_domain_allowed(email, allowed_domains):
            return None, "not_allowed"

        access_request = find_google_access_request(email, google_sub)
        now = datetime.now()
        if not access_request:
            company, franchise = ensure_default_company_structure()
            access_request = GoogleAccessRequest(
                company_id=company.id,
                franchise_id=franchise.id,
                email=email,
                google_sub=google_sub,
                name=profile.get("name") or email,
                picture=profile.get("picture"),
                status="PENDING",
                requested_at=now,
                last_attempt_at=now,
            )
            db.session.add(access_request)
            db.session.flush()
            log_audit(
                None,
                None,
                "google_access_request",
                details={
                    "email": email,
                    "name": access_request.name,
                    "status": access_request.status,
                },
            )
        else:
            access_request.email = email
            access_request.google_sub = google_sub or access_request.google_sub
            access_request.name = profile.get("name") or access_request.name
            access_request.picture = profile.get("picture") or access_request.picture
            if not access_request.company_id or not access_request.franchise_id:
                company, franchise = ensure_default_company_structure()
                access_request.company_id = access_request.company_id or company.id
                access_request.franchise_id = access_request.franchise_id or franchise.id
            access_request.last_attempt_at = now

        db.session.commit()
        return None, "rejected" if access_request.status == "REJECTED" else "pending"

    user = User(
        company_id=default_company().id,
        franchise_id=default_franchise().id,
        username=unique_username(email),
        email=email,
        google_sub=google_sub,
        name=profile.get("name") or email,
        role=Role.ADMIN,
        active=True,
    )
    user.set_password("")
    user.apply_role_defaults()
    db.session.add(user)
    db.session.flush()
    log_audit(
        None,
        user,
        "google_admin_auto_create",
        details={"email": email, "name": user.name},
    )
    db.session.commit()
    return user, "active"


def google_signup_domain_allowed(email, allowed_domains):
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain in allowed_domains


def find_google_access_request(email, google_sub=None):
    if google_sub:
        access_request = GoogleAccessRequest.query.filter_by(google_sub=google_sub).first()
        if access_request:
            return access_request
    return GoogleAccessRequest.query.filter(func.lower(GoogleAccessRequest.email) == email).first()


def lead_assignment_name(user):
    return clean_text(getattr(user, "lead_name", None)) or user.name


TERMINAL_WORKFLOW_STATUSES = ("concluido", "recusado", "inativo")
SUPPORT_QUEUE_COLUMNS = (
    {"key": "overdue", "label": "Em atraso", "description": "Apoios fora do prazo ou sem responsavel ativo."},
    {"key": "active", "label": "Em andamento", "description": "Tarefas com apoio atribuido dentro do prazo."},
    {"key": "returned", "label": "Devolvidos", "description": "Continuam visiveis ate o vendedor reenviar."},
)


def support_lead_exists_for_user(user):
    return (
        db.session.query(LeadWorkflow.id)
        .filter(
            LeadWorkflow.lead_id == Lead.id,
            LeadWorkflow.assigned_user_id == user.id,
            LeadWorkflow.support_completed_at.is_(None),
            LeadWorkflow.support_returned_at.is_(None),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        .exists()
    )


def support_assignment_exists_for_user(user, lead):
    if not user or not lead:
        return False
    return db.session.query(
        LeadWorkflow.query.filter(
            LeadWorkflow.lead_id == lead.id,
            LeadWorkflow.assigned_user_id == user.id,
            LeadWorkflow.support_completed_at.is_(None),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        ).exists()
    ).scalar()


def reversal_status_label(value):
    return REVERSAL_STATUS_LABELS.get(value or "", value or "Sem reversao")


def reversal_status_filter_options():
    return [
        {"value": status, "label": reversal_status_label(status)}
        for status in REVERSAL_PUBLIC_STATUS_ORDER
    ]


def reversal_status_option_styles():
    return {
        reversal_status_label(status): {
            "style": f"--chip-color: {REVERSAL_STATUS_COLORS.get(status, '#64748b')};"
        }
        for status in REVERSAL_PUBLIC_STATUS_ORDER
    }


def reversal_priority_label(value):
    return REVERSAL_PRIORITY_LABELS.get(value or "", value or "Padrao")


def reversal_distribution_mode_label(value):
    return REVERSAL_DISTRIBUTION_LABELS.get(value or "", value or "Sorteio pelo supervisor")


def reversal_tabulation_label(value):
    data = REVERSAL_TABULATIONS.get(normalize_reversal_tabulation(value))
    return data["label"] if data else "Sem tabulacao"


def reversal_tabulation_options():
    return [
        {"value": key, "label": data["label"], "priority": data["priority"], "sla_hours": data["sla_hours"]}
        for key, data in REVERSAL_TABULATIONS.items()
    ]


def normalize_reversal_tabulation(value):
    value = clean_text(value)
    return REVERSAL_TABULATION_ALIASES.get(value, value)


def reversal_tabulation_config(value):
    value = normalize_reversal_tabulation(value)
    if value not in REVERSAL_TABULATIONS:
        raise ValueError("Escolha a tabulacao da reversao antes de enviar o cliente.")
    return value, REVERSAL_TABULATIONS[value]


def reversal_result_label(value):
    return REVERSAL_RESULT_LABELS.get(value or "", value or "Sem resultado")


def reversal_result_sentiment(value, tabulation=None):
    tabulation = normalize_reversal_tabulation(tabulation)
    context_sentiments = REVERSAL_RESULT_SENTIMENTS_BY_TABULATION.get(tabulation, {})
    if value in context_sentiments:
        return context_sentiments[value]
    return REVERSAL_RESULT_SENTIMENTS.get(value or "", REVERSAL_RESULT_NEGATIVE)


def reversal_result_sentiment_label(value, tabulation=None):
    return REVERSAL_RESULT_SENTIMENT_LABELS.get(reversal_result_sentiment(value, tabulation), "Negativa")


def reversal_result_options(tabulation=None):
    return [
        {
            "value": result,
            "label": reversal_result_label(result),
            "sentiment": reversal_result_sentiment(result, tabulation),
            "sentiment_label": reversal_result_sentiment_label(result, tabulation),
        }
        for result in ReversalResult.ACTIVE
    ]


def active_reversal_task_for_lead(lead):
    if not lead:
        return None
    return (
        ReversalTask.query.options(
            joinedload(ReversalTask.assigned_user),
            joinedload(ReversalTask.requested_by),
            joinedload(ReversalTask.owner_user),
        )
        .filter(
            ReversalTask.lead_id == lead.id,
            ReversalTask.status.in_(ReversalStatus.ACTIVE),
        )
        .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
        .first()
    )


def latest_reversal_task_for_lead(lead):
    if not lead:
        return None
    return (
        ReversalTask.query.options(
            joinedload(ReversalTask.assigned_user),
            joinedload(ReversalTask.requested_by),
            joinedload(ReversalTask.owner_user),
            joinedload(ReversalTask.canceled_by),
        )
        .filter(ReversalTask.lead_id == lead.id)
        .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
        .first()
    )


def latest_reversal_tasks_for_leads(leads):
    lead_ids = [lead.id for lead in leads if lead]
    if not lead_ids:
        return {}

    tasks_by_lead = {}
    tasks = (
        ReversalTask.query.options(
            joinedload(ReversalTask.assigned_user),
            joinedload(ReversalTask.requested_by),
            joinedload(ReversalTask.owner_user),
            joinedload(ReversalTask.canceled_by),
        )
        .filter(ReversalTask.lead_id.in_(lead_ids))
        .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
        .all()
    )
    for task in tasks:
        tasks_by_lead.setdefault(task.lead_id, task)
    return tasks_by_lead


def latest_reversal_status_subquery():
    return (
        db.session.query(ReversalTask.status)
        .filter(ReversalTask.lead_id == Lead.id)
        .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
        .limit(1)
        .correlate(Lead)
        .scalar_subquery()
    )


def reversal_assignment_exists_for_user(user, lead):
    if not user or not lead:
        return False
    return db.session.query(
        ReversalTask.query.filter(
            ReversalTask.lead_id == lead.id,
            ReversalTask.assigned_user_id == user.id,
            ReversalTask.status.in_(ReversalStatus.ACTIVE),
        ).exists()
    ).scalar()


def lead_access_is_reversal_only(user, lead):
    if not user or not lead or lead.deleted_at:
        return False
    if not getattr(user, "can_work_reversal", False):
        return False
    if not reversal_assignment_exists_for_user(user, lead):
        return False
    if getattr(user, "can_view_all", False):
        return False
    if getattr(user, "can_manage_reversals", False) and user.team and lead.equipe == user.team:
        return False
    if user.role == Role.SUPERVISOR and user.team and lead.equipe == user.team:
        return False
    if getattr(user, "can_act_as_support", False) and support_assignment_exists_for_user(user, lead):
        return False
    return normalize_option_key(lead.consultor) != normalize_option_key(lead_assignment_name(user))


def reversal_team_for_lead(lead):
    team_name = clean_text(getattr(lead, "equipe", None))
    if not lead or not team_name:
        return None

    query = Team.query.filter(Team.name == team_name)
    if lead.company_id:
        query = query.filter(or_(Team.company_id == lead.company_id, Team.company_id.is_(None)))
    if lead.franchise_id:
        query = query.filter(Team.franchise_id == lead.franchise_id)
    else:
        query = query.filter(Team.franchise_id.is_(None))

    return query.order_by(Team.active.desc(), Team.id.asc()).first()


def reversal_settings_for_lead(lead):
    company_id = lead.company_id if lead and lead.company_id else default_company().id
    franchise_id = lead.franchise_id if lead else None
    query = ReversalSettings.query.filter(
        ReversalSettings.company_id == company_id,
        ReversalSettings.enabled.is_(True),
    )
    if franchise_id:
        query = query.filter(or_(ReversalSettings.franchise_id == franchise_id, ReversalSettings.franchise_id.is_(None)))
        setting = query.order_by(ReversalSettings.franchise_id.is_(None).asc(), ReversalSettings.id.asc()).first()
    else:
        setting = query.filter(ReversalSettings.franchise_id.is_(None)).order_by(ReversalSettings.id.asc()).first()
    return setting or ReversalSettings(company_id=company_id, franchise_id=None, enabled=True)


def reversal_team_config_for_lead(lead):
    team_name = clean_text(getattr(lead, "equipe", None))
    if not lead or not team_name:
        return None

    team = reversal_team_for_lead(lead)
    if team:
        config = ReversalTeamConfig.query.filter_by(team_id=team.id).first()
        if config:
            return config

    query = ReversalTeamConfig.query.filter(
        ReversalTeamConfig.team_name == team_name,
        ReversalTeamConfig.enabled.is_(True),
    )
    if lead.company_id:
        query = query.filter(or_(ReversalTeamConfig.company_id == lead.company_id, ReversalTeamConfig.company_id.is_(None)))
    if lead.franchise_id:
        query = query.filter(ReversalTeamConfig.franchise_id == lead.franchise_id)
    else:
        query = query.filter(ReversalTeamConfig.franchise_id.is_(None))
    return query.order_by(ReversalTeamConfig.id.asc()).first()


def reversal_distribution_mode_for_lead(lead):
    config = reversal_team_config_for_lead(lead)
    if config and config.enabled and config.distribution_mode in ReversalDistributionMode.ALL:
        return config.distribution_mode

    setting = reversal_settings_for_lead(lead)
    if setting.default_distribution_mode in ReversalDistributionMode.ALL:
        return setting.default_distribution_mode
    return ReversalDistributionMode.MANUAL


def reversal_setting_int(setting, field_name, fallback):
    value = getattr(setting, field_name, None)
    try:
        return int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def reversal_priority_for_days(days_to_unlock, setting=None):
    setting = setting or ReversalSettings()
    urgent_threshold = reversal_setting_int(setting, "urgent_days_threshold", 10)
    attention_threshold = reversal_setting_int(setting, "attention_days_threshold", 20)
    if days_to_unlock is not None and days_to_unlock <= urgent_threshold:
        return ReversalPriority.URGENT
    if days_to_unlock is not None and days_to_unlock <= attention_threshold:
        return ReversalPriority.ATTENTION
    return ReversalPriority.STANDARD


def reversal_sla_hours_for_priority(priority, setting=None):
    setting = setting or ReversalSettings()
    if priority == ReversalPriority.URGENT:
        return max(reversal_setting_int(setting, "sla_10_days_hours", 10), 1)
    if priority == ReversalPriority.ATTENTION:
        return max(reversal_setting_int(setting, "sla_20_days_hours", 24), 1)
    return max(reversal_setting_int(setting, "sla_30_days_hours", 48), 1)


def reversal_time_bucket_for_days(days_to_unlock):
    if days_to_unlock is None:
        return "days_30"
    try:
        days = int(days_to_unlock)
    except (TypeError, ValueError):
        return "days_30"
    if days <= 10:
        return "days_10"
    if days <= 20:
        return "days_20"
    return "days_30"


def reversal_min_note_chars(task_or_lead):
    lead = getattr(task_or_lead, "lead", None) or task_or_lead
    setting = reversal_settings_for_lead(lead)
    return max(reversal_setting_int(setting, "min_note_chars", 40), 1)


def reversal_owner_user(task):
    if not task or not task.lead:
        return None
    if task.owner_user and task.owner_user.active:
        return task.owner_user
    owner_id = task.owner_user_id or workflow_owner_id_for_lead(task.lead)
    if not owner_id:
        return None
    user = db.session.get(User, owner_id)
    return user if user and user.active else None


def reversal_supervisor_recipients(task):
    if not task or not task.lead:
        return []
    query = User.query.filter(
        User.active.is_(True),
        User.can_manage_reversals.is_(True),
    )
    if task.company_id:
        query = query.filter(User.company_id == task.company_id)
    if task.franchise_id:
        query = query.filter(or_(User.franchise_id == task.franchise_id, User.can_view_all_franchises.is_(True)))
    else:
        query = query.filter(or_(User.franchise_id.is_(None), User.can_view_all_franchises.is_(True)))
    query = query.filter(or_(User.can_view_all.is_(True), User.team == task.team_name))
    recipients = []
    seen = set()
    for user in query.order_by(User.id.asc()).all():
        if user.id in seen or not can_access_lead_scope(user, task.lead):
            continue
        recipients.append(user)
        seen.add(user.id)
    return recipients


def create_reversal_notification(task, recipient, notification_type, severity, title, message, event_key):
    if not task or not task.lead or not recipient or not recipient.active:
        return None
    if not can_access_lead_scope(recipient, task.lead):
        return None
    existing = ReversalNotification.query.filter_by(event_key=event_key).first()
    if existing:
        return existing
    notification = ReversalNotification(
        company_id=task.company_id,
        franchise_id=task.franchise_id,
        lead_id=task.lead_id,
        reversal_task_id=task.id,
        recipient_user_id=recipient.id,
        notification_type=notification_type,
        severity=severity,
        title=title,
        message=message,
        event_key=event_key,
        created_at=datetime.now(),
    )
    db.session.add(notification)
    return notification


def create_reversal_notifications(task, recipients, notification_type, severity, title, message, event_suffix):
    created = []
    seen = set()
    for recipient in recipients:
        if not recipient or recipient.id in seen:
            continue
        seen.add(recipient.id)
        event_key = f"reversal:{task.id}:{notification_type}:{recipient.id}:{event_suffix}"
        notification = create_reversal_notification(
            task,
            recipient,
            notification_type,
            severity,
            title,
            message,
            event_key,
        )
        if notification:
            created.append(notification)
    return created


def notify_reversal_assigned(task, actor=None):
    if not task or not task.assigned_user:
        return []
    title = "Reversao recebida"
    message = f"{task.lead.nome_cliente} entrou na sua fila de reversao."
    notifications = create_reversal_notifications(
        task,
        [task.assigned_user],
        REVERSAL_NOTIFICATION_RECEIVED,
        "info",
        title,
        message,
        task.assigned_at.isoformat() if task.assigned_at else "assigned",
    )
    log_audit(
        task.lead,
        actor,
        "reversal_notification",
        field_name="reversal_notification",
        new_value=title,
        details={
            "reversal_task_id": task.id,
            "notification_type": REVERSAL_NOTIFICATION_RECEIVED,
            "recipient_user_ids": [item.recipient_user_id for item in notifications],
        },
    )
    return notifications


def notify_reversal_overdue(task):
    recipients = []
    if task.assigned_user:
        recipients.append(task.assigned_user)
    recipients.extend(reversal_supervisor_recipients(task))
    title = "Reversao em atraso"
    message = f"{task.lead.nome_cliente} ultrapassou o SLA de reversao."
    notifications = create_reversal_notifications(
        task,
        recipients,
        REVERSAL_NOTIFICATION_OVERDUE,
        "critical",
        title,
        message,
        task.due_at.isoformat() if task.due_at else "overdue",
    )
    supervisor_ids = {user.id for user in reversal_supervisor_recipients(task)}
    if any(item.recipient_user_id in supervisor_ids for item in notifications):
        task.supervisor_notified_at = task.supervisor_notified_at or datetime.now()
    return notifications


def reversal_result_notification_payload(result, tabulation=None):
    sentiment = reversal_result_sentiment(result, tabulation)
    if sentiment == REVERSAL_RESULT_CRITICAL and result in {ReversalResult.VENDA_FECHADA, ReversalResult.CLIENTE_VENDIDO_PERDIDO}:
        return (
            REVERSAL_NOTIFICATION_CLIENT_SOLD_LOST,
            "critical",
            "Venda fechada na reversao",
        )
    if sentiment == REVERSAL_RESULT_CRITICAL:
        return (
            REVERSAL_NOTIFICATION_CRITICAL_RESULT,
            "critical",
            "Resultado critico na reversao",
        )
    if sentiment == REVERSAL_RESULT_POSITIVE:
        return (
            REVERSAL_NOTIFICATION_RESULT,
            "info",
            "Resultado positivo da reversao",
        )
    if sentiment == REVERSAL_RESULT_ALERT:
        return (
            REVERSAL_NOTIFICATION_RESULT,
            "warning",
            "Resultado em alerta na reversao",
        )
    return (
        REVERSAL_NOTIFICATION_RESULT,
        "warning",
        "Resultado negativo da reversao",
    )


def notify_reversal_result(task, attempt, actor=None):
    if not task or not attempt:
        return []
    result_sentiment = reversal_result_sentiment(attempt.result, task.tabulation)
    notification_type, severity, title = reversal_result_notification_payload(attempt.result, task.tabulation)
    result_label = reversal_result_label(attempt.result)
    message = f"{task.lead.nome_cliente}: {result_label}. {attempt.note}"
    recipients = []
    owner = reversal_owner_user(task)
    if owner:
        recipients.append(owner)
    if result_sentiment == REVERSAL_RESULT_CRITICAL:
        recipients.extend(reversal_supervisor_recipients(task))
    notifications = create_reversal_notifications(
        task,
        recipients,
        notification_type,
        severity,
        title,
        message,
        f"attempt:{attempt.id}",
    )
    owner_id = owner.id if owner else None
    supervisor_ids = {user.id for user in reversal_supervisor_recipients(task)}
    if any(item.recipient_user_id == owner_id for item in notifications):
        task.owner_notified_at = task.owner_notified_at or datetime.now()
    if any(item.recipient_user_id in supervisor_ids for item in notifications):
        task.supervisor_notified_at = task.supervisor_notified_at or datetime.now()
    log_audit(
        task.lead,
        actor,
        "reversal_notification",
        field_name="reversal_notification",
        new_value=title,
        details={
            "reversal_task_id": task.id,
            "reversal_attempt_id": attempt.id,
            "notification_type": notification_type,
            "recipient_user_ids": [item.recipient_user_id for item in notifications],
            "result": attempt.result,
            "result_sentiment": result_sentiment,
            "result_sentiment_label": reversal_result_sentiment_label(attempt.result, task.tabulation),
            "tabulation": task.tabulation,
            "tabulation_label": reversal_tabulation_label(task.tabulation),
        },
    )
    return notifications


def recent_reversal_notifications_for_user(user, limit=6):
    if not user:
        return []
    return (
        ReversalNotification.query.options(
            joinedload(ReversalNotification.lead),
            joinedload(ReversalNotification.reversal_task),
        )
        .filter(
            ReversalNotification.recipient_user_id == user.id,
            ReversalNotification.read_at.is_(None),
        )
        .order_by(ReversalNotification.created_at.desc(), ReversalNotification.id.desc())
        .limit(limit)
        .all()
    )


def unread_reversal_notification_count(user):
    if not user:
        return 0
    return ReversalNotification.query.filter(
        ReversalNotification.recipient_user_id == user.id,
        ReversalNotification.read_at.is_(None),
    ).count()


def reversal_column_stats(tasks):
    tasks = list(tasks or [])
    deadlines = {task.id: reversal_task_deadline(task) for task in tasks}
    assigned_statuses = {ReversalStatus.ASSIGNED, ReversalStatus.IN_PROGRESS}
    return [
        {
            "label": "Em andamento",
            "value": sum(1 for task in tasks if task.status in assigned_statuses),
            "tone": "primary",
            "filter": "active",
        },
        {
            "label": "Aguardando sorteio",
            "value": sum(1 for task in tasks if task.status == ReversalStatus.QUEUED),
            "tone": "muted",
            "filter": "queued",
        },
        {
            "label": "Em atraso",
            "value": sum(1 for task in tasks if deadlines.get(task.id) and deadlines[task.id]["is_overdue"]),
            "tone": "danger",
            "filter": "overdue",
        },
        {
            "label": "Urgentes",
            "value": sum(1 for task in tasks if task.priority_level == ReversalPriority.URGENT),
            "tone": "warning",
            "filter": "urgent",
        },
    ]


def reversal_available_users_for_lead(lead):
    if not lead or not clean_text(lead.equipe):
        return []

    query = scope_user_query_to_lead(
        User.query.filter(
            User.active.is_(True),
            User.role == Role.OPERADOR,
            User.team == lead.equipe,
            User.can_work_reversal.is_(True),
        ),
        lead,
    )
    owner_user_id = workflow_owner_id_for_lead(lead)
    if owner_user_id:
        query = query.filter(User.id != owner_user_id)

    owner_name = normalize_option_key(lead.consultor)
    users = []
    seen = set()
    for candidate in query.order_by(User.name.asc()).all():
        candidate_name_key = normalize_option_key(lead_assignment_name(candidate))
        if owner_name and candidate_name_key == owner_name:
            continue
        if candidate.id in seen:
            continue
        users.append(candidate)
        seen.add(candidate.id)
    return users


def active_reversal_loads_for_users(user_ids):
    user_ids = [user_id for user_id in user_ids if user_id]
    if not user_ids:
        return {}
    rows = (
        db.session.query(ReversalTask.assigned_user_id, func.count(ReversalTask.id))
        .filter(
            ReversalTask.assigned_user_id.in_(user_ids),
            ReversalTask.status.in_((
                ReversalStatus.ASSIGNED,
                ReversalStatus.IN_PROGRESS,
                ReversalStatus.OVERDUE,
            )),
        )
        .group_by(ReversalTask.assigned_user_id)
        .all()
    )
    return {user_id: count for user_id, count in rows}


def last_reversal_assignment_at_for_users(user_ids):
    user_ids = [user_id for user_id in user_ids if user_id]
    if not user_ids:
        return {}
    rows = (
        db.session.query(ReversalTask.assigned_user_id, func.max(ReversalTask.assigned_at))
        .filter(
            ReversalTask.assigned_user_id.in_(user_ids),
            ReversalTask.assigned_at.isnot(None),
        )
        .group_by(ReversalTask.assigned_user_id)
        .all()
    )
    return {user_id: assigned_at for user_id, assigned_at in rows}


def choose_reversal_assignee(lead, local_loads=None, local_last_assigned=None):
    candidates = reversal_available_users_for_lead(lead)
    if not candidates:
        return None

    candidate_ids = [candidate.id for candidate in candidates]
    base_loads = active_reversal_loads_for_users(candidate_ids)
    base_last_assigned = last_reversal_assignment_at_for_users(candidate_ids)
    local_loads = local_loads if local_loads is not None else {}
    local_last_assigned = local_last_assigned if local_last_assigned is not None else {}

    def sort_key(candidate):
        load = base_loads.get(candidate.id, 0) + local_loads.get(candidate.id, 0)
        last_assigned = local_last_assigned.get(candidate.id) or base_last_assigned.get(candidate.id)
        return (
            load,
            last_assigned or datetime.min,
            candidate.id,
        )

    return sorted(candidates, key=sort_key)[0]


def assign_reversal_task(task, assigned_user, assigned_by=None, batch=None, source="reversal_assign"):
    if not task or task.status not in {ReversalStatus.QUEUED, ReversalStatus.ASSIGNED, ReversalStatus.IN_PROGRESS, ReversalStatus.OVERDUE}:
        raise ValueError("Esta reversao nao pode ser atribuida.")
    available_ids = {candidate.id for candidate in reversal_available_users_for_lead(task.lead)}
    if not assigned_user or assigned_user.id not in available_ids:
        raise ValueError("Esse vendedor nao pode receber esta reversao.")

    now = datetime.now()
    old_status = task.status
    old_assigned_user_id = task.assigned_user_id
    task.assigned_user_id = assigned_user.id
    task.assigned_user = assigned_user
    task.assigned_by_id = assigned_by.id if assigned_by else None
    if batch:
        task.assignment_batch_id = batch.id
        task.assignment_batch = batch
    task.status = ReversalStatus.ASSIGNED
    task.assigned_at = now
    task.due_at = now + timedelta(hours=max(int(task.sla_hours or 48), 1))
    task.updated_at = now
    log_audit(
        task.lead,
        assigned_by,
        "reversal_assign",
        field_name="reversal_assigned_user",
        old_value=str(old_assigned_user_id) if old_assigned_user_id else None,
        new_value=assigned_user.name,
        details={
            "reversal_task_id": task.id,
            "assigned_user_id": assigned_user.id,
            "batch_id": batch.id if batch else None,
            "old_status": old_status,
            "new_status": task.status,
            "source": source,
            "due_at": task.due_at,
        },
    )
    notify_reversal_assigned(task, assigned_by)
    return task


def assign_reversal_task_to_best_candidate(task, assigned_by=None, source="reversal_auto_assign"):
    assignee = choose_reversal_assignee(task.lead)
    if not assignee:
        raise ValueError("Nao existe outro vendedor ativo desta equipe para receber a reversao.")
    return assign_reversal_task(task, assignee, assigned_by=assigned_by, source=source)


def can_manage_reversal_queue_for_team(user, team_name, franchise_id=None):
    if not user or not getattr(user, "can_manage_reversals", False):
        return False
    team_name = clean_text(team_name)
    if not team_name:
        return False
    if user.can_view_all:
        if franchise_id and not user_can_view_all_franchises(user) and user.franchise_id != franchise_id:
            return False
        return True
    return bool(user.team and user.team == team_name)


def can_distribute_reversal_queue(user, task):
    if not task or task.status != ReversalStatus.QUEUED:
        return False
    if task.distribution_mode != ReversalDistributionMode.MANUAL:
        return False
    return can_manage_reversal_queue_for_team(user, task.team_name, task.franchise_id)


def can_assign_reversal_task(user, task):
    if not task or task.status not in ReversalStatus.ACTIVE:
        return False
    return can_manage_reversal_queue_for_team(user, task.team_name, task.franchise_id)


def reversal_assignment_options(task):
    if not task or not task.lead:
        return []
    return reversal_available_users_for_lead(task.lead)


def queued_manual_reversal_tasks_for_team(team_name, user, franchise_id=None):
    team_name = clean_text(team_name)
    if not can_manage_reversal_queue_for_team(user, team_name, franchise_id):
        raise ValueError("Seu usuario nao pode sortear reversoes desta equipe.")
    if user_can_view_all_franchises(user) and not franchise_id:
        raise ValueError("Escolha a franquia antes de sortear reversoes desta equipe.")

    query = scoped_company_query(
        ReversalTask.query.options(
            joinedload(ReversalTask.lead),
            joinedload(ReversalTask.assigned_user),
        ),
        ReversalTask,
        user,
        include_unscoped=False,
    ).filter(
        ReversalTask.team_name == team_name,
        ReversalTask.status == ReversalStatus.QUEUED,
        ReversalTask.distribution_mode == ReversalDistributionMode.MANUAL,
    )
    if franchise_id:
        query = query.filter(ReversalTask.franchise_id == franchise_id)
    return query.all()


def sort_reversal_tasks_for_distribution(tasks):
    return sorted(
        tasks,
        key=lambda task: (
            REVERSAL_PRIORITY_SORT.get(task.priority_level, 9),
            task.days_to_unlock if task.days_to_unlock is not None else 9999,
            task.queued_at or task.created_at or datetime.max,
            task.id,
        ),
    )


def distribute_manual_reversal_queue(team_name, user, franchise_id=None):
    tasks = queued_manual_reversal_tasks_for_team(team_name, user, franchise_id=franchise_id)
    if not tasks:
        raise ValueError("Nao ha reversoes manuais na fila desta equipe.")

    now = datetime.now()
    team = None
    for task in tasks:
        if task.team:
            team = task.team
            break
    batch = ReversalAssignmentBatch(
        company_id=company_id_for_user(user),
        franchise_id=franchise_id or (tasks[0].franchise_id if tasks else franchise_id_for_user(user)),
        team_id=team.id if team else None,
        team_name=clean_text(team_name),
        mode=ReversalDistributionMode.MANUAL,
        status="running",
        requested_by_id=user.id if user else None,
        assigned_by_id=user.id if user else None,
        total_tasks=len(tasks),
        total_assigned=0,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(batch)
    db.session.flush()

    local_loads = {}
    local_last_assigned = {}
    assigned = []
    missing = []
    for task in sort_reversal_tasks_for_distribution(tasks):
        assignee = choose_reversal_assignee(
            task.lead,
            local_loads=local_loads,
            local_last_assigned=local_last_assigned,
        )
        if not assignee:
            missing.append(task.id)
            continue
        assign_reversal_task(
            task,
            assignee,
            assigned_by=user,
            batch=batch,
            source="reversal_manual_batch",
        )
        assigned.append(task.id)
        local_loads[assignee.id] = local_loads.get(assignee.id, 0) + 1
        local_last_assigned[assignee.id] = now

    batch.total_assigned = len(assigned)
    batch.finished_at = datetime.now()
    batch.status = "finished" if not missing else "partial"
    batch.details = json.dumps(
        {
            "assigned_task_ids": assigned,
            "missing_task_ids": missing,
            "source": "manual_distribution",
        },
        ensure_ascii=False,
        default=str,
    )
    batch.updated_at = batch.finished_at
    log_audit(
        None,
        user,
        "reversal_batch_distribute",
        details={
            "batch_id": batch.id,
            "team_name": team_name,
            "franchise_id": batch.franchise_id,
            "total_tasks": len(tasks),
            "total_assigned": len(assigned),
            "missing_task_ids": missing,
        },
    )
    return batch


def can_work_reversal_task(user, task):
    if not user or not task or task.status not in ReversalStatus.ACTIVE:
        return False
    if not task.lead or task.lead.deleted_at:
        return False
    if workflow_status_for_lead(task.lead) in TERMINAL_WORKFLOW_STATUSES:
        return False
    if getattr(user, "can_manage_reversals", False) and can_access_lead_scope(user, task.lead):
        return True
    return bool(
        getattr(user, "can_work_reversal", False)
        and task.assigned_user_id == user.id
        and can_access_lead_scope(user, task.lead)
    )


def validate_reversal_attempt_payload(task, result, note):
    result = clean_text(result)
    if result not in ReversalResult.ALL:
        raise ValueError("Escolha o resultado da reversao antes de continuar.")

    note = clean_text(note)
    min_chars = reversal_min_note_chars(task)
    note_chars = len(note or "")
    if note_chars < min_chars:
        raise ValueError(f"A anotacao da reversao precisa ter pelo menos {min_chars} caracteres.")

    return result, note, note_chars


def create_reversal_attempt(task, user, result, note, note_chars):
    attempt = ReversalAttempt(
        reversal_task_id=task.id,
        company_id=task.company_id,
        franchise_id=task.franchise_id,
        lead_id=task.lead_id,
        user_id=user.id if user else None,
        result=result,
        note=note,
        note_char_count=note_chars,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.session.add(attempt)
    db.session.flush()
    return attempt


def complete_reversal_task(task, user, result=None, note=None):
    if not can_work_reversal_task(user, task):
        raise ValueError("Seu usuario nao pode concluir esta reversao.")

    result, note, note_chars = validate_reversal_attempt_payload(task, result, note)
    now = datetime.now()
    old_status = task.status
    result_sentiment = reversal_result_sentiment(result, task.tabulation)
    task.status = ReversalStatus.RETURNED
    task.final_result = result
    task.final_note = note
    task.completed_at = now
    task.updated_at = now
    if not task.first_attempt_at:
        task.first_attempt_at = now
    task.last_attempt_at = now
    attempt = create_reversal_attempt(task, user, result, note, note_chars)
    notify_reversal_result(task, attempt, actor=user)
    log_audit(
        task.lead,
        user,
        "reversal_complete",
        field_name="reversal_status",
        old_value=reversal_status_label(old_status),
        new_value=reversal_status_label(task.status),
        details={
            "reversal_task_id": task.id,
            "reversal_attempt_id": attempt.id,
            "result": task.final_result,
            "result_label": reversal_result_label(task.final_result),
            "result_sentiment": result_sentiment,
            "result_sentiment_label": reversal_result_sentiment_label(task.final_result, task.tabulation),
            "tabulation": task.tabulation,
            "tabulation_label": reversal_tabulation_label(task.tabulation),
            "note": note,
            "note_char_count": note_chars,
            "source": "reversal_queue",
        },
    )
    return task


def return_reversal_task(task, user, note=None, result=None):
    if not can_work_reversal_task(user, task):
        raise ValueError("Seu usuario nao pode devolver esta reversao.")
    result, note, note_chars = validate_reversal_attempt_payload(task, result, note)

    now = datetime.now()
    old_status = task.status
    task.status = ReversalStatus.RETURNED
    task.final_result = result
    task.final_note = note
    task.completed_at = now
    task.updated_at = now
    if not task.first_attempt_at:
        task.first_attempt_at = now
    task.last_attempt_at = now
    attempt = create_reversal_attempt(task, user, result, note, note_chars)
    notify_reversal_result(task, attempt, actor=user)
    log_audit(
        task.lead,
        user,
        "reversal_return",
        field_name="reversal_status",
        old_value=reversal_status_label(old_status),
        new_value=reversal_status_label(task.status),
        details={
            "reversal_task_id": task.id,
            "reversal_attempt_id": attempt.id,
            "result": task.final_result,
            "result_label": reversal_result_label(task.final_result),
            "result_sentiment": reversal_result_sentiment(task.final_result, task.tabulation),
            "result_sentiment_label": reversal_result_sentiment_label(task.final_result, task.tabulation),
            "tabulation": task.tabulation,
            "tabulation_label": reversal_tabulation_label(task.tabulation),
            "note": note,
            "note_char_count": note_chars,
            "source": "reversal_queue",
        },
    )
    return task


def can_access_reversal_queue(user):
    return bool(
        user
        and (
            getattr(user, "can_manage_reversals", False)
            or getattr(user, "can_work_reversal", False)
        )
    )


def sync_reversal_overdue_statuses():
    now = datetime.now()
    tasks = (
        ReversalTask.query.options(joinedload(ReversalTask.lead))
        .filter(
            ReversalTask.status.in_((ReversalStatus.ASSIGNED, ReversalStatus.IN_PROGRESS)),
            ReversalTask.due_at.isnot(None),
            ReversalTask.due_at < now,
        )
        .all()
    )
    for task in tasks:
        old_status = task.status
        task.status = ReversalStatus.OVERDUE
        task.updated_at = now
        notifications = notify_reversal_overdue(task)
        log_audit(
            task.lead,
            None,
            "reversal_overdue",
            field_name="reversal_status",
            old_value=reversal_status_label(old_status),
            new_value=reversal_status_label(task.status),
            details={
                "reversal_task_id": task.id,
                "due_at": task.due_at,
                "notification_ids": [item.id for item in notifications],
                "source": "reversal_queue_sync",
            },
        )
    return len(tasks)


def visible_reversal_tasks_query(user):
    if not can_access_reversal_queue(user):
        return ReversalTask.query.filter(False)

    query = scoped_company_query(
        ReversalTask.query.join(Lead, ReversalTask.lead_id == Lead.id).filter(Lead.deleted_at.is_(None)),
        ReversalTask,
        user,
        include_unscoped=False,
    )

    if getattr(user, "can_manage_reversals", False) and getattr(user, "can_view_all", False):
        return query

    conditions = []
    if getattr(user, "can_manage_reversals", False) and user.team:
        conditions.append(ReversalTask.team_name == user.team)
    if getattr(user, "can_work_reversal", False):
        conditions.append(ReversalTask.assigned_user_id == user.id)

    if not conditions:
        return query.filter(False)
    return query.filter(or_(*conditions))


def reversal_queue_filters():
    return {
        "q": clean_text(request.args.get("q")),
        "franchise_id": clean_arg_values("franchise_id"),
        "equipe": clean_arg_values("equipe"),
        "consultor": clean_arg_values("consultor"),
        "assigned_user_id": clean_arg_values("assigned_user_id"),
        "status": [value for value in clean_arg_values("status") if value in REVERSAL_PUBLIC_STATUS_GROUPS],
        "priority": clean_arg_values("priority"),
    }


def reversal_task_state(task):
    if not task:
        return "days_30"
    return reversal_time_bucket_for_days(task.days_to_unlock)


def reversal_task_deadline(task):
    if not task or not task.due_at:
        return None
    now = datetime.now()
    is_overdue = task.due_at < now and task.status not in ReversalStatus.TERMINAL
    overdue_hours = max(int((now - task.due_at).total_seconds() + 3599) // 3600, 1) if is_overdue else 0
    return {
        "at": task.due_at,
        "sla_hours": task.sla_hours,
        "is_overdue": is_overdue,
        "overdue_hours": overdue_hours,
    }


def reversal_task_matches_search(task, search, digits=None):
    lead = task.lead if task else None
    if not lead:
        return False
    searchable = " ".join(
        clean_text(value) or ""
        for value in (
            lead.nome_cliente,
            lead.cpf,
            lead.equipe,
            lead.consultor,
            task.owner_name,
            task.assigned_user.name if task.assigned_user else None,
            task.requested_by.name if task.requested_by else None,
            reversal_status_label(task.status),
            reversal_priority_label(task.priority_level),
            reversal_tabulation_label(task.tabulation),
        )
    ).lower()
    if search and search in searchable:
        return True
    if digits and digits in re.sub(r"\D", "", lead.cpf or ""):
        return True
    return False


def filter_reversal_queue_tasks(tasks, filters):
    filtered = list(tasks)
    term = clean_text(filters.get("q"))
    if term:
        search = term.lower()
        digits = re.sub(r"\D", "", term)
        filtered = [task for task in filtered if reversal_task_matches_search(task, search, digits)]

    franchise_ids = set(filter_values(filters.get("franchise_id")))
    if franchise_ids:
        filtered = [task for task in filtered if str(task.franchise_id or "") in franchise_ids]

    equipes = set(filter_values(filters.get("equipe")))
    if equipes:
        filtered = [task for task in filtered if task.team_name in equipes]

    consultores = set(filter_values(filters.get("consultor")))
    if consultores:
        filtered = [task for task in filtered if (task.owner_name or (task.lead.consultor if task.lead else None)) in consultores]

    assigned_ids = set(filter_values(filters.get("assigned_user_id")))
    if assigned_ids:
        filtered = [task for task in filtered if str(task.assigned_user_id or "") in assigned_ids]

    statuses = set(filter_values(filters.get("status")))
    if statuses:
        selected_statuses = set()
        for status in statuses:
            selected_statuses.update(REVERSAL_PUBLIC_STATUS_GROUPS.get(status, ()))
        filtered = [task for task in filtered if task.status in selected_statuses]

    priorities = set(filter_values(filters.get("priority")))
    if priorities:
        filtered = [task for task in filtered if task.priority_level in priorities]

    return filtered


def sort_reversal_queue_tasks(tasks):
    return sorted(
        tasks,
        key=lambda task: (
            REVERSAL_PRIORITY_SORT.get(task.priority_level, 9),
            task.due_at or datetime.max,
            task.queued_at or task.created_at or datetime.max,
            task.id,
        ),
    )


def reversal_queue_columns(tasks):
    states = {task.id: reversal_task_state(task) for task in tasks}
    columns = []
    for column in REVERSAL_QUEUE_COLUMNS:
        column_tasks = sort_reversal_queue_tasks(
            [task for task in tasks if states.get(task.id) == column["key"]]
        )
        columns.append({
            **column,
            "tasks": column_tasks,
            "count": len(column_tasks),
            "stats": reversal_column_stats(column_tasks),
        })
    return columns


def reversal_filter_options(tasks):
    teams = sorted({task.team_name for task in tasks if task.team_name})
    consultores = sorted({
        task.owner_name or (task.lead.consultor if task.lead else "")
        for task in tasks
        if task.owner_name or (task.lead and task.lead.consultor)
    })
    assigned_users = []
    seen_user_ids = set()
    for task in sorted(tasks, key=lambda item: (item.assigned_user.name if item.assigned_user else "", item.assigned_user_id or 0)):
        if not task.assigned_user_id or not task.assigned_user or task.assigned_user_id in seen_user_ids:
            continue
        assigned_users.append({"value": str(task.assigned_user_id), "label": task.assigned_user.name})
        seen_user_ids.add(task.assigned_user_id)
    return {
        "teams": teams,
        "consultores": consultores,
        "assigned_users": assigned_users,
        "statuses": reversal_status_filter_options(),
        "priorities": [{"value": priority, "label": reversal_priority_label(priority)} for priority in ReversalPriority.ALL],
    }


def can_request_lead_reversal(user, lead):
    return bool(
        user
        and lead
        and getattr(user, "can_request_reversal", False)
        and not lead.deleted_at
        and workflow_status_for_lead(lead) not in TERMINAL_WORKFLOW_STATUSES
        and can_access_lead_scope(user, lead)
    )


def can_cancel_reversal_task(user, task):
    if not user or not task or task.status not in ReversalStatus.ACTIVE:
        return False
    lead = task.lead
    if not lead or lead.deleted_at or not can_access_lead_scope(user, lead):
        return False
    if getattr(user, "can_manage_reversals", False):
        return True
    if not getattr(user, "can_request_reversal", False):
        return False
    if getattr(user, "can_view_all", False):
        return True
    if user.role == Role.SUPERVISOR and lead.equipe == user.team:
        return True
    return normalize_option_key(lead.consultor) == normalize_option_key(lead_assignment_name(user))


def create_reversal_task_for_lead(lead, user, note=None, tabulation=None):
    if not can_request_lead_reversal(user, lead):
        raise ValueError("Seu usuario nao pode enviar este cliente para reversao.")

    setting = reversal_settings_for_lead(lead)
    if not setting.enabled:
        raise ValueError("A reversao esta desativada para esta empresa.")

    team_config = reversal_team_config_for_lead(lead)
    if team_config and not team_config.enabled:
        raise ValueError("A reversao esta desativada para esta equipe.")

    if active_reversal_task_for_lead(lead):
        raise ValueError("Este cliente ja esta em reversao ativa.")

    candidates = reversal_available_users_for_lead(lead)
    if not candidates:
        raise ValueError("Nao existe outro vendedor ativo desta equipe para receber a reversao.")

    tabulation, tabulation_config = reversal_tabulation_config(tabulation)
    now = datetime.now()
    days_to_unlock = unlock_days_left(lead)
    priority = tabulation_config["priority"]
    sla_hours = tabulation_config["sla_hours"]
    distribution_mode = reversal_distribution_mode_for_lead(lead)
    team = reversal_team_for_lead(lead)

    task = ReversalTask(
        company_id=lead.company_id or company_id_for_user(user),
        franchise_id=lead.franchise_id or franchise_id_for_user(user),
        lead_id=lead.id,
        team_id=team.id if team else None,
        team_name=clean_text(lead.equipe) or "Sem equipe",
        owner_user_id=workflow_owner_id_for_lead(lead),
        owner_name=clean_text(lead.consultor),
        requested_by_id=user.id if user else None,
        status=ReversalStatus.QUEUED,
        priority_level=priority,
        distribution_mode=distribution_mode,
        tabulation=tabulation,
        days_to_unlock=days_to_unlock,
        sla_hours=sla_hours,
        due_at=None,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(task)
    db.session.flush()

    ensure_lead_in_progress_status(lead, user, source="reversal_request")
    log_audit(
        lead,
        user,
        "reversal_request",
        field_name="reversal_status",
        old_value=None,
        new_value=reversal_status_label(task.status),
        details={
            "reversal_task_id": task.id,
            "distribution_mode": distribution_mode,
            "tabulation": tabulation,
            "tabulation_label": reversal_tabulation_label(tabulation),
            "priority": priority,
            "days_to_unlock": days_to_unlock,
            "sla_hours": sla_hours,
            "candidate_count": len(candidates),
            "note": clean_text(note),
        },
    )
    if distribution_mode == ReversalDistributionMode.AUTOMATIC:
        assign_reversal_task_to_best_candidate(task, assigned_by=user, source="reversal_auto_assign")
    return task


def cancel_reversal_task(task, user, reason=None):
    if not can_cancel_reversal_task(user, task):
        raise ValueError("Seu usuario nao pode cancelar esta reversao.")
    return record_reversal_cancellation(task, user, reason)


def record_reversal_cancellation(task, user, reason=None, source="lead_detail"):
    now = datetime.now()
    old_status = task.status
    task.status = ReversalStatus.CANCELED
    task.canceled_at = now
    task.canceled_by_id = user.id if user else None
    task.cancel_reason = clean_text(reason)
    task.updated_at = now
    log_audit(
        task.lead,
        user,
        "reversal_cancel",
        field_name="reversal_status",
        old_value=reversal_status_label(old_status),
        new_value=reversal_status_label(task.status),
        details={
            "reversal_task_id": task.id,
            "reason": task.cancel_reason,
            "source": source,
        },
    )
    return task


def support_users_for_team(team, lead=None, user=None):
    if not team:
        return []
    query = User.query.filter(
        User.active.is_(True),
        User.can_act_as_support.is_(True),
        User.team == team,
    )
    if lead:
        query = scope_user_query_to_lead(query, lead)
    else:
        query = scoped_company_query(
            query,
            User,
            user or request_user_or_none(),
            include_unscoped=True,
        )
    return query.order_by(User.name.asc()).all()


def open_support_task_count(user):
    return LeadWorkflow.query.filter(
        LeadWorkflow.assigned_user_id == user.id,
        LeadWorkflow.support_completed_at.is_(None),
        LeadWorkflow.support_returned_at.is_(None),
        ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
    ).count()


def select_support_user_for_team(team, lead=None, user=None):
    candidates = support_users_for_team(team, lead=lead, user=user)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda user: (
            open_support_task_count(user),
            user.role == Role.ADMIN,
            user.name.lower(),
        ),
    )[0]


def visible_leads_query(user, state="active"):
    query = scoped_company_query(Lead.query, Lead, user, include_unscoped=True)
    if state == "inactive":
        query = query.filter(Lead.deleted_at.isnot(None))
    else:
        query = query.filter(Lead.deleted_at.is_(None))

    query = query.order_by(
        Lead.equipe.asc(),
        Lead.consultor.asc(),
        Lead.nome_cliente.asc(),
    )

    if user.can_view_all:
        return query

    if user.role == Role.SUPERVISOR:
        return query.filter(Lead.equipe == user.team)

    own_lead = func.lower(Lead.consultor) == lead_assignment_name(user).lower()
    if getattr(user, "can_act_as_support", False):
        return query.filter(or_(own_lead, support_lead_exists_for_user(user)))
    return query.filter(own_lead)


def current_dashboard_filters():
    date_field = clean_text(request.args.get("date_field")) or "data_venda"
    if date_field not in {"data_venda", "concessao"}:
        date_field = "data_venda"

    per_page = parse_int(request.args.get("per_page"), 15)
    if per_page not in PAGE_SIZE_OPTIONS:
        per_page = 15
    state = clean_text(request.args.get("state")) or "active"
    if state not in {"active", "inactive"}:
        state = "active"

    sort = clean_text(request.args.get("sort")) or "data_venda"
    if sort == "posicao":
        sort = "reversal_status"
    direction = clean_text(request.args.get("direction")) or "desc"
    direction = "asc" if direction == "asc" else "desc"
    order_by = clean_text(request.args.get("order_by"))
    order_option = DASHBOARD_ORDER_MAP.get(order_by)
    if order_option:
        sort = order_option["sort"]
        direction = order_option["direction"]
    else:
        order_by = dashboard_order_value(sort, direction)

    return {
        "state": state,
        "q": clean_text(request.args.get("q")),
        "franchise_id": clean_arg_values("franchise_id"),
        "equipe": clean_arg_values("equipe"),
        "consultor": clean_arg_values("consultor"),
        "cpf": clean_text(request.args.get("cpf")),
        "nome": clean_text(request.args.get("nome")),
        "status": clean_arg_values("status"),
        "reversal_status": clean_reversal_status_arg_values(),
        "date_field": date_field,
        "date_start": clean_text(request.args.get("date_start"))
        or clean_text(request.args.get("data_venda_inicio")),
        "date_end": clean_text(request.args.get("date_end"))
        or clean_text(request.args.get("data_venda_fim")),
        "data_venda_inicio": clean_text(request.args.get("data_venda_inicio")),
        "data_venda_fim": clean_text(request.args.get("data_venda_fim")),
        "concessao_inicio": clean_text(request.args.get("concessao_inicio")),
        "concessao_fim": clean_text(request.args.get("concessao_fim")),
        "sort": sort,
        "direction": direction,
        "order_by": order_by,
        "page": max(parse_int(request.args.get("page"), 1), 1),
        "per_page": per_page,
    }


def dashboard_order_value(sort, direction):
    direction = "asc" if direction == "asc" else "desc"
    for option in DASHBOARD_ORDER_OPTIONS:
        if option["sort"] == sort and option["direction"] == direction:
            return option["value"]
    return ""


def clean_arg_values(name):
    values = []
    seen = set()
    for raw_value in request.args.getlist(name):
        value = clean_text(raw_value)
        if not value or value in seen:
            continue
        values.append(value)
        seen.add(value)
    return values


def clean_reversal_status_arg_values():
    allowed_values = set(REVERSAL_PUBLIC_STATUS_GROUPS) | {FILTER_EMPTY_VALUE}
    values = clean_arg_values("reversal_status") or clean_arg_values("posicao")
    return [value for value in values if value in allowed_values]


def filter_values(value):
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item]
    return [value] if value else []


def sanitize_franchise_filter(filters, user):
    if not user_can_view_all_franchises(user):
        filters["franchise_id"] = []
        return filters

    allowed_values = {str(franchise.id) for franchise in get_company_franchises(user)}
    filters["franchise_id"] = [
        value for value in filter_values(filters.get("franchise_id")) if value in allowed_values
    ]
    return filters


def franchise_filter_options(user):
    if not user_can_view_all_franchises(user):
        return []
    return [
        {"value": str(franchise.id), "label": franchise.name}
        for franchise in get_company_franchises(user)
    ]


def dashboard_team_filter_options(user, state="active"):
    if user_can_view_all_franchises(user):
        return sorted(set(get_active_team_names(user=user) + get_visible_teams(user, state)))
    return get_visible_teams(user, state)


def dashboard_consultor_filter_options(user, equipe=None, state="active"):
    if user_can_view_all_franchises(user):
        options = set(get_visible_consultores(user, equipe, state))
        equipes = filter_values(equipe)
        if equipes:
            for team in equipes:
                options.update(get_active_operator_names(team, user=user))
        else:
            options.update(get_known_consultores())
        return sorted(value for value in options if value)
    return get_visible_consultores(user, equipe, state)


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_pagination(filters, total_count):
    per_page = filters["per_page"]
    total_pages = max((total_count + per_page - 1) // per_page, 1)
    page = min(max(filters["page"], 1), total_pages)
    offset = (page - 1) * per_page
    start_item = offset + 1 if total_count else 0
    end_item = min(offset + per_page, total_count)

    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "offset": offset,
        "start_item": start_item,
        "end_item": end_item,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "pages": pagination_pages(page, total_pages),
    }


def pagination_pages(current_page, total_pages):
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    pages = {1, total_pages, current_page - 1, current_page, current_page + 1}
    if current_page <= 3:
        pages.update({2, 3, 4})
    if current_page >= total_pages - 2:
        pages.update({total_pages - 3, total_pages - 2, total_pages - 1})

    ordered_pages = sorted(page for page in pages if 1 <= page <= total_pages)
    result = []
    previous = None
    for page in ordered_pages:
        if previous is not None and page - previous > 1:
            result.append(None)
        result.append(page)
        previous = page
    return result


def apply_dashboard_filters(query, filters):
    if filters["q"]:
        search = f"%{filters['q'].lower()}%"
        digits = re.sub(r"\D", "", filters["q"])
        conditions = [func.lower(Lead.nome_cliente).like(search)]
        if digits:
            conditions.append(Lead.cpf.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))

    query = apply_int_multi_value_filter(query, Lead.franchise_id, filters["franchise_id"])

    query = apply_multi_value_filter(query, Lead.equipe, filters["equipe"])

    query = apply_multi_value_filter(query, Lead.consultor, filters["consultor"])

    if filters["cpf"]:
        digits = re.sub(r"\D", "", filters["cpf"])
        if digits:
            query = query.filter(Lead.cpf.like(f"%{digits}%"))

    if filters["nome"]:
        search = f"%{filters['nome'].lower()}%"
        query = query.filter(func.lower(Lead.nome_cliente).like(search))

    query = apply_multi_value_filter(query, Lead.status, filters["status"])

    query = apply_reversal_status_filter(query, filters["reversal_status"])

    date_start = parse_date(filters["date_start"])
    date_end = parse_date(filters["date_end"])
    date_column = Lead.concessao if filters["date_field"] == "concessao" else Lead.data_venda

    if date_start:
        query = query.filter(date_column >= date_start)
    if date_end:
        query = query.filter(date_column <= date_end)

    # Compatibilidade com URLs antigas dos filtros separados.
    data_venda_inicio = parse_date(filters["data_venda_inicio"]) if not filters["date_start"] else None
    data_venda_fim = parse_date(filters["data_venda_fim"]) if not filters["date_end"] else None
    concessao_inicio = parse_date(filters["concessao_inicio"])
    concessao_fim = parse_date(filters["concessao_fim"])

    if data_venda_inicio:
        query = query.filter(Lead.data_venda >= data_venda_inicio)
    if data_venda_fim:
        query = query.filter(Lead.data_venda <= data_venda_fim)
    if concessao_inicio:
        query = query.filter(Lead.concessao >= concessao_inicio)
    if concessao_fim:
        query = query.filter(Lead.concessao <= concessao_fim)

    return query


def apply_multi_value_filter(query, column, selected_values):
    values = filter_values(selected_values)
    if not values:
        return query

    include_empty = FILTER_EMPTY_VALUE in values
    concrete_values = [value for value in values if value != FILTER_EMPTY_VALUE]
    conditions = []

    if concrete_values:
        conditions.append(column.in_(concrete_values))
    if include_empty:
        conditions.append(or_(column.is_(None), column == ""))

    if not conditions:
        return query
    return query.filter(or_(*conditions))


def apply_reversal_status_filter(query, selected_values):
    values = filter_values(selected_values)
    if not values:
        return query

    include_empty = FILTER_EMPTY_VALUE in values
    concrete_values = []
    for value in values:
        if value in REVERSAL_PUBLIC_STATUS_GROUPS:
            concrete_values.extend(REVERSAL_PUBLIC_STATUS_GROUPS[value])
    latest_status = latest_reversal_status_subquery()
    conditions = []

    if concrete_values:
        conditions.append(latest_status.in_(concrete_values))
    if include_empty:
        conditions.append(latest_status.is_(None))

    if not conditions:
        return query
    return query.filter(or_(*conditions))


def apply_int_multi_value_filter(query, column, selected_values):
    values = []
    seen = set()
    for value in filter_values(selected_values):
        parsed = parse_int(value, 0)
        if parsed and parsed not in seen:
            values.append(parsed)
            seen.add(parsed)
    if not values:
        return query
    return query.filter(column.in_(values))


def active_filter_count(filters):
    keys = [
        "q",
        "franchise_id",
        "equipe",
        "consultor",
        "status",
        "reversal_status",
        "date_start",
        "date_end",
    ]
    count = sum(1 for key in keys if filters.get(key))
    if filters.get("state") == "inactive":
        count += 1
    return count


def apply_dashboard_sort(query, filters):
    latest_atuacao_at = latest_atuacao_at_subquery()
    sort_columns = {
        "data_venda": Lead.data_venda,
        "concessao": Lead.concessao,
        "nome_cliente": Lead.nome_cliente,
        "equipe": Lead.equipe,
        "consultor": Lead.consultor,
        "status": Lead.status,
        "reversal_status": latest_reversal_status_subquery(),
        "created_at": Lead.created_at,
        "ultima_atualizacao": latest_atuacao_at,
        "ultima_atuacao": latest_atuacao_at,
    }
    column = sort_columns.get(filters["sort"], Lead.data_venda)
    direction = "asc" if filters["direction"] == "asc" else "desc"
    ordered_column = column.asc() if direction == "asc" else column.desc()

    return query.order_by(None).order_by(ordered_column, Lead.nome_cliente.asc())


def sort_direction_for(filters, column):
    if filters["sort"] == column:
        return "asc" if filters["direction"] == "desc" else "desc"
    if column in {"nome_cliente", "equipe", "consultor", "status", "reversal_status"}:
        return "asc"
    return "desc"


def latest_atuacao_at_subquery():
    return (
        db.session.query(func.max(AuditLog.created_at))
        .filter(
            AuditLog.lead_id == Lead.id,
            or_(
                AuditLog.action.in_(OPERATIONAL_AUDIT_ACTIONS),
                (
                    (AuditLog.action == "update")
                    & AuditLog.field_name.in_(LAST_ACTIVITY_FIELDS)
                ),
            ),
        )
        .correlate(Lead)
        .scalar_subquery()
    )


def dashboard_url(**overrides):
    args = {
        key: [value for value in request.args.getlist(key) if value]
        for key in request.args.keys()
    }
    if ("sort" in overrides or "direction" in overrides) and "order_by" not in overrides:
        args.pop("order_by", None)
    for key, value in overrides.items():
        if value is None or value == "" or value == []:
            args.pop(key, None)
        elif isinstance(value, (list, tuple, set)):
            values = [item for item in value if item]
            if values:
                args[key] = list(values)
            else:
                args.pop(key, None)
        else:
            args[key] = [value]

    query_string = urlencode(args, doseq=True)
    base_url = url_for("dashboard")
    return f"{base_url}?{query_string}" if query_string else base_url


def workflow_board_filters():
    return {
        "workflow": clean_text(request.args.get("workflow")) or DEFAULT_LEAD_WORKFLOW_SLUG,
        "q": clean_text(request.args.get("q")),
        "franchise_id": clean_arg_values("franchise_id"),
        "equipe": clean_arg_values("equipe"),
        "consultor": clean_arg_values("consultor"),
        "status": clean_arg_values("status"),
        "reversal_status": clean_reversal_status_arg_values(),
    }


def selected_workflow_for_board(workflows, filters):
    if not workflows:
        return None

    selected_slug = filters["workflow"] or DEFAULT_LEAD_WORKFLOW_SLUG
    if selected_slug == "fidelizacao":
        selected_slug = UNLOCK_WORKFLOW_SLUG
        filters["workflow"] = selected_slug
    for workflow in workflows:
        if workflow.slug == selected_slug:
            return workflow
    for workflow in workflows:
        if workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG:
            filters["workflow"] = workflow.slug
            return workflow

    filters["workflow"] = workflows[0].slug
    return workflows[0]


def visible_lead_workflows_query(user, workflow):
    query = LeadWorkflow.query.join(Lead, LeadWorkflow.lead_id == Lead.id).filter(
        LeadWorkflow.workflow_id == workflow.id,
        LeadWorkflow.status != "concluido",
        Lead.deleted_at.is_(None),
    )
    query = apply_joined_lead_scope(query, user, include_unscoped=True)
    if workflow.slug == UNLOCK_WORKFLOW_SLUG:
        principals = db.session.query(LeadWorkflow.lead_id).filter(
            LeadWorkflow.workflow.has(Workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG),
            LeadWorkflow.current_stage.has(WorkflowStage.slug == "fidelizacao"),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
        query = query.filter(LeadWorkflow.lead_id.in_(principals), ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES))
    if user.can_view_all:
        return query
    if user.role == Role.SUPERVISOR:
        return query.filter(Lead.equipe == user.team)
    own_lead = func.lower(Lead.consultor) == lead_assignment_name(user).lower()
    if getattr(user, "can_act_as_support", False):
        return query.filter(or_(own_lead, LeadWorkflow.assigned_user_id == user.id))
    return query.filter(own_lead)


def visible_support_tasks_query(user):
    query = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.lead),
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.assigned_user),
        )
        .join(Lead, LeadWorkflow.lead_id == Lead.id)
        .join(WorkflowStage, LeadWorkflow.current_stage_id == WorkflowStage.id)
        .filter(
            or_(
                (
                    WorkflowStage.requires_support.is_(True)
                    & or_(
                        LeadWorkflow.support_executor == SUPPORT_EXECUTOR_SUPPORT,
                        LeadWorkflow.assigned_user_id.isnot(None),
                    )
                ),
                (
                    (LeadWorkflow.unlock_executor == UNLOCK_EXECUTOR_SUPPORT)
                    & (WorkflowStage.slug == "dia-do-desbloqueio")
                ),
            ),
            Lead.deleted_at.is_(None),
            LeadWorkflow.support_completed_at.is_(None),
            ~LeadWorkflow.status.in_(TERMINAL_WORKFLOW_STATUSES),
        )
    )
    query = apply_joined_lead_scope(query, user, include_unscoped=True)

    own_lead = func.lower(Lead.consultor) == lead_assignment_name(user).lower()
    if user.can_view_all:
        return query
    if user.role == Role.SUPERVISOR:
        return query.filter(Lead.equipe == user.team)
    if getattr(user, "can_act_as_support", False):
        return query.filter(or_(LeadWorkflow.assigned_user_id == user.id, own_lead & LeadWorkflow.support_returned_at.isnot(None)))
    if getattr(user, "can_request_support", False):
        return query.filter(own_lead)
    return query.filter(own_lead, LeadWorkflow.support_returned_at.isnot(None))


def support_queue_filters():
    return {
        "q": clean_text(request.args.get("q")),
    }


def support_task_state(lead_workflow, alert=None):
    if not lead_workflow:
        return "overdue"
    if lead_workflow.support_returned_at:
        return "returned"
    if alert and alert.get("severity") == "critical":
        return "overdue"
    if not lead_workflow.assigned_user_id:
        return "overdue"
    return "active"


def support_task_status_label(lead_workflow, alert=None):
    state = support_task_state(lead_workflow, alert)
    if alert and state in {"overdue", "returned"}:
        return alert.get("label") or state
    return {
        "overdue": "Apoio em atraso",
        "active": "Apoio em andamento",
        "returned": "Apoio devolvido",
    }.get(state, "Apoio")


def support_task_deadline(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return None
    reference_at = lead_workflow.support_assigned_at or lead_workflow.support_requested_at
    sla_hours = lead_workflow.current_stage.support_sla_hours or DEFAULT_STAGE_OPERATIONAL_SLA_HOURS
    if not reference_at or not sla_hours or sla_hours <= 0:
        return None

    deadline_at = reference_at + timedelta(hours=sla_hours)
    now = datetime.now()
    is_overdue = deadline_at < now and not lead_workflow.support_returned_at
    if is_overdue:
        overdue_hours = max(int((now - deadline_at).total_seconds() + 3599) // 3600, 1)
    else:
        overdue_hours = 0
    return {
        "at": deadline_at,
        "sla_hours": sla_hours,
        "is_overdue": is_overdue,
        "overdue_hours": overdue_hours,
    }


def create_support_notification(lead_workflow, recipient, notification_type, severity, title, message, event_suffix):
    if not lead_workflow or not lead_workflow.lead or not recipient:
        return None
    event_key = f"support:{lead_workflow.id}:{notification_type}:{recipient.id}:{event_suffix}"
    if SupportNotification.query.filter_by(event_key=event_key).first():
        return None
    notification = SupportNotification(
        company_id=lead_workflow.lead.company_id,
        franchise_id=lead_workflow.lead.franchise_id,
        lead_id=lead_workflow.lead_id,
        lead_workflow_id=lead_workflow.id,
        recipient_user_id=recipient.id,
        notification_type=notification_type,
        severity=severity,
        title=title,
        message=message,
        event_key=event_key,
        created_at=datetime.now(),
    )
    db.session.add(notification)
    return notification


def notify_support_assigned(lead_workflow, support_user=None, actor=None, resent=False):
    support_user = support_user or (lead_workflow.assigned_user if lead_workflow else None)
    if not lead_workflow or not lead_workflow.lead or not support_user:
        return None
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"
    notification_type = SUPPORT_NOTIFICATION_RESENT if resent else SUPPORT_NOTIFICATION_ASSIGNED
    title = "Apoio reenviado" if resent else "Apoio recebido"
    message = f"{lead_workflow.lead.nome_cliente} entrou na sua fila de apoio em {stage_name}."
    notification = create_support_notification(
        lead_workflow,
        support_user,
        notification_type,
        "info",
        title,
        message,
        lead_workflow.support_assigned_at.isoformat() if lead_workflow.support_assigned_at else "assigned",
    )
    if notification:
        log_audit(
            lead_workflow.lead,
            actor,
            "support_notification",
            field_name="support_notification",
            new_value=title,
            details={
                "lead_workflow_id": lead_workflow.id,
                "notification_type": notification_type,
                "recipient_user_id": support_user.id,
            },
        )
    return notification


def notify_support_overdue(lead_workflow):
    if not lead_workflow or not lead_workflow.assigned_user:
        return None
    deadline = support_task_deadline(lead_workflow)
    if not deadline or not deadline["is_overdue"]:
        return None
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"
    title = "Apoio em atraso"
    message = f"{lead_workflow.lead.nome_cliente}: {stage_name} esta ha {deadline['overdue_hours']}h alem do SLA de {deadline['sla_hours']}h."
    notification = create_support_notification(
        lead_workflow,
        lead_workflow.assigned_user,
        SUPPORT_NOTIFICATION_OVERDUE,
        "critical",
        title,
        message,
        deadline["at"].isoformat(),
    )
    if notification:
        log_audit(
            lead_workflow.lead,
            None,
            "support_notification",
            field_name="support_notification",
            new_value=title,
            details={
                "lead_workflow_id": lead_workflow.id,
                "notification_type": SUPPORT_NOTIFICATION_OVERDUE,
                "recipient_user_id": lead_workflow.assigned_user_id,
                "deadline_at": deadline["at"],
            },
        )
    return notification


def sync_support_overdue_notifications(tasks=None):
    if tasks is None:
        tasks = visible_support_tasks_query(request_user_or_none()).all()
    created = 0
    for task in tasks:
        if notify_support_overdue(task):
            created += 1
    return created


def recent_support_notifications_for_user(user, limit=6):
    if not user:
        return []
    return (
        SupportNotification.query.options(
            joinedload(SupportNotification.lead),
            joinedload(SupportNotification.lead_workflow),
        )
        .filter(
            SupportNotification.recipient_user_id == user.id,
            SupportNotification.read_at.is_(None),
        )
        .order_by(SupportNotification.created_at.desc(), SupportNotification.id.desc())
        .limit(limit)
        .all()
    )


def unread_support_notification_count(user):
    if not user:
        return 0
    return SupportNotification.query.filter(
        SupportNotification.recipient_user_id == user.id,
        SupportNotification.read_at.is_(None),
    ).count()


def filter_support_queue_tasks(tasks, filters):
    filtered_tasks = list(tasks)
    term = clean_text(filters.get("q"))
    if term:
        search = term.lower()
        digits = re.sub(r"\D", "", term)
        filtered_tasks = [
            task
            for task in filtered_tasks
            if support_task_matches_search(task, search, digits)
        ]
    return filtered_tasks


def support_task_matches_search(task, search, digits=None):
    lead = task.lead if task else None
    if not lead:
        return False
    searchable = " ".join(
        clean_text(value) or ""
        for value in (
            lead.nome_cliente,
            lead.cpf,
            lead.consultor,
            lead.equipe,
            task.current_stage.name if task.current_stage else None,
        )
    ).lower()
    if search and search in searchable:
        return True
    if digits and digits in re.sub(r"\D", "", lead.cpf or ""):
        return True
    return False


def support_column_stats(tasks, states, deadlines):
    total = len(tasks)
    overdue = 0
    returned = 0
    active = 0
    unassigned = 0
    for task in tasks:
        state = states.get(task.id)
        deadline = deadlines.get(task.id)
        if state == "returned":
            returned += 1
        elif state == "overdue" or (deadline and deadline.get("is_overdue")):
            overdue += 1
        elif state == "active":
            active += 1
        if not task.assigned_user_id and not task.support_returned_at:
            unassigned += 1

    return [
        {"label": "Em andamento", "value": active, "tone": "info", "filter": "active"},
        {"label": "Em atraso", "value": overdue, "tone": "danger", "filter": "overdue"},
        {"label": "Devolvidos", "value": returned, "tone": "warning", "filter": "returned"},
        {"label": "Sem responsavel", "value": unassigned, "tone": "warning", "filter": "unassigned"},
        {"label": "Total na coluna", "value": total, "tone": "muted", "filter": "all"},
    ]


def support_queue_columns(tasks, states, deadlines=None):
    deadlines = deadlines or {}
    columns = []
    for column in SUPPORT_QUEUE_COLUMNS:
        column_tasks = [task for task in tasks if states.get(task.id) == column["key"]]
        columns.append({
            **column,
            "tasks": column_tasks,
            "count": len(column_tasks),
            "stats": support_column_stats(column_tasks, states, deadlines),
        })
    return columns


def workflow_support_filter_match(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.current_stage
        or lead_workflow.support_completed_at
        or lead_workflow.status in TERMINAL_WORKFLOW_STATUSES
    ):
        return False
    return bool(
        lead_workflow_requires_support(lead_workflow)
        or lead_workflow.assigned_user_id
        or lead_workflow.support_requested_at
        or lead_workflow.support_assigned_at
        or lead_workflow.support_returned_at
    )


def workflow_reversal_filter_match(lead_workflow, latest_reversal_tasks_by_lead=None):
    if not lead_workflow or not lead_workflow.lead_id:
        return False
    task = None
    if latest_reversal_tasks_by_lead is not None:
        task = latest_reversal_tasks_by_lead.get(lead_workflow.lead_id)
    if task is None and lead_workflow.lead:
        task = active_reversal_task_for_lead(lead_workflow.lead)
    return bool(task and task.status in ReversalStatus.ACTIVE)


def workflow_column_stats(lead_workflows, alerts, latest_reversal_tasks_by_lead=None):
    total = len(lead_workflows)
    critical = 0
    warning = 0
    support = 0
    reversal = 0

    for lead_workflow in lead_workflows:
        alert = alerts.get(lead_workflow.id)
        if alert:
            severity = alert.get("severity")
            if severity in {"critical", "danger"}:
                critical += 1
            elif severity == "warning":
                warning += 1
        if workflow_support_filter_match(lead_workflow):
            support += 1
        if workflow_reversal_filter_match(lead_workflow, latest_reversal_tasks_by_lead):
            reversal += 1

    return [
        {"label": "Alertas criticos", "value": critical, "tone": "danger", "filter": "critical"},
        {"label": "Atencao", "value": warning, "tone": "warning", "filter": "warning"},
        {"label": "Relacionados ao apoio", "value": support, "tone": "warning", "filter": "support"},
        {"label": "Em reversao", "value": reversal, "tone": "info", "filter": "reversal"},
        {"label": "Total na etapa", "value": total, "tone": "muted", "filter": "all"},
    ]


def apply_workflow_board_filters(query, filters):
    if filters["q"]:
        search = f"%{filters['q'].lower()}%"
        digits = re.sub(r"\D", "", filters["q"])
        conditions = [func.lower(Lead.nome_cliente).like(search)]
        if digits:
            conditions.append(Lead.cpf.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))

    query = apply_int_multi_value_filter(query, Lead.franchise_id, filters["franchise_id"])

    query = apply_multi_value_filter(query, Lead.equipe, filters["equipe"])

    query = apply_multi_value_filter(query, Lead.consultor, filters["consultor"])

    query = apply_multi_value_filter(query, Lead.status, filters["status"])
    query = apply_reversal_status_filter(query, filters["reversal_status"])

    return query


def workflow_stage_requirement_alert(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.workflow
        or not lead_workflow.current_stage
        or lead_workflow.workflow.slug != DEFAULT_LEAD_WORKFLOW_SLUG
        or lead_workflow.current_stage.slug != DEFAULT_LEAD_WORKFLOW_STAGE_SLUG
    ):
        return None

    missing = [
        audit_field_label(field)
        for field in ("data_venda", "concessao")
        if not lead_field_has_value(lead_workflow.lead, field)
    ]
    if not missing:
        return None

    return {
        "label": "Pendencia",
        "message": "Cliente ainda nao pode ir para Fidelizacao. Falta: " + "; ".join(missing),
        "severity": "warning",
    }


def unlock_executor_alert_for_workflow(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.lead
        or not is_principal_unlock_stage(lead_workflow)
        or lead_workflow.unlock_executor in UNLOCK_EXECUTOR_CHOICES
        or unlock_completed_for_lead(lead_workflow.lead)
    ):
        return None

    days_left = unlock_days_left(lead_workflow.lead)
    if days_left is None or days_left > 1:
        return None

    if days_left <= 0:
        return {
            "label": "Sinalizar desbloqueio",
            "message": "Sinalize quem fara o desbloqueio. Se ninguem escolher, o sistema envia para o apoio ativo da equipe.",
            "severity": "warning",
        }
    return {
        "label": "Desbloqueio amanha",
        "message": "Sinalize quem fara o desbloqueio: vendedor ou apoio.",
        "severity": "warning",
    }


def support_executor_alert_for_workflow(lead_workflow):
    if (
        not support_decision_enabled_for_workflow(lead_workflow)
        or lead_workflow.support_executor in SUPPORT_EXECUTOR_CHOICES
    ):
        return None

    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"
    return {
        "label": f"{stage_name} sem responsavel",
        "message": f"Sinalize quem fara {stage_name}: vendedor ou apoio.",
        "severity": "warning",
    }


def workflow_first_action_state(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.lead
        or not lead_workflow.current_stage
        or normalize_option_key(lead_workflow.lead.status) in {"concluido", "recusado", "perdido", "perda"}
    ):
        return None

    is_new_lead = is_new_status(lead_workflow.lead.status)
    operational_action_at = latest_operational_action_at_for_stage(lead_workflow)
    reference_at = operational_action_at or lead_workflow.entered_stage_at or lead_workflow.created_at or datetime.now()
    sla_hours = operational_sla_hours_for_stage(lead_workflow.current_stage, is_new_lead, operational_action_at)
    if not sla_hours or sla_hours <= 0:
        return None

    deadline_at = reference_at + timedelta(hours=sla_hours)
    now = datetime.now()
    seconds_delta = int((deadline_at - now).total_seconds())
    if seconds_delta >= 0:
        return None

    hours = max((abs(seconds_delta) + 3599) // 3600, 1)
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"

    return {
        "label": f"{stage_name} em atraso",
        "message": f"{hours}h de atraso nesta etapa. Prazo configurado: {sla_hours}h.",
        "severity": "critical",
        "is_overdue": True,
        "deadline_at": deadline_at,
        "hours_left": None,
        "hours_overdue": hours,
    }


def operational_sla_hours_for_stage(stage, is_new_lead=False, operational_action_at=None):
    if is_new_lead and not operational_action_at:
        return NEW_LEAD_OPERATIONAL_SLA_HOURS
    if stage and stage.first_action_hours and stage.first_action_hours > 0:
        return stage.first_action_hours
    return DEFAULT_STAGE_OPERATIONAL_SLA_HOURS


def latest_operational_action_at_for_stage(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return None

    activity_at = (
        db.session.query(func.max(LeadActivity.created_at))
        .filter(
            LeadActivity.lead_id == lead_workflow.lead_id,
            LeadActivity.lead_workflow_id == lead_workflow.id,
            LeadActivity.stage_id == lead_workflow.current_stage_id,
            LeadActivity.deleted_at.is_(None),
        )
        .scalar()
    )
    checklist_at = (
        db.session.query(func.max(LeadWorkflowChecklistCheck.checked_at))
        .join(
            WorkflowChecklistItem,
            LeadWorkflowChecklistCheck.checklist_item_id == WorkflowChecklistItem.id,
        )
        .filter(
            LeadWorkflowChecklistCheck.lead_workflow_id == lead_workflow.id,
            LeadWorkflowChecklistCheck.checked.is_(True),
            WorkflowChecklistItem.stage_id == lead_workflow.current_stage_id,
        )
        .scalar()
    )
    scheduled_task_at = (
        db.session.query(func.max(LeadWorkflowScheduledTask.completed_at))
        .filter(
            LeadWorkflowScheduledTask.lead_workflow_id == lead_workflow.id,
            LeadWorkflowScheduledTask.stage_id == lead_workflow.current_stage_id,
            LeadWorkflowScheduledTask.completed_at.isnot(None),
            LeadWorkflowScheduledTask.active.is_(True),
        )
        .scalar()
    )
    transition_at = (
        db.session.query(func.max(WorkflowTransition.created_at))
        .filter(
            WorkflowTransition.lead_workflow_id == lead_workflow.id,
            WorkflowTransition.user_id.isnot(None),
            WorkflowTransition.action.in_(OPERATIONAL_TRANSITION_ACTIONS),
            or_(
                WorkflowTransition.to_stage_id == lead_workflow.current_stage_id,
                WorkflowTransition.from_stage_id == lead_workflow.current_stage_id,
            ),
        )
        .scalar()
    )
    values = [value for value in (activity_at, checklist_at, scheduled_task_at, transition_at) if value]
    return max(values) if values else None


def workflow_support_alert(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.current_stage
        or not lead_workflow_requires_support(lead_workflow)
        or lead_workflow.support_completed_at
        or (
            is_principal_unlock_stage(lead_workflow, {"dia-do-desbloqueio"})
            and unlock_completed_for_lead(lead_workflow.lead)
        )
    ):
        return None

    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"
    if lead_workflow.support_returned_at:
        return {
            "label": "Apoio devolvido",
            "message": lead_workflow.support_return_reason or "Apoio devolveu a tarefa para o vendedor.",
            "severity": "warning",
        }

    if not lead_workflow.assigned_user_id:
        return {
            "label": "Apoio pendente",
            "message": "Esta etapa requer apoio, mas ainda nao ha apoio disponivel atribuido.",
            "severity": "warning",
        }

    reference_at = lead_workflow.support_assigned_at or lead_workflow.support_requested_at
    sla_hours = lead_workflow.current_stage.support_sla_hours or DEFAULT_STAGE_OPERATIONAL_SLA_HOURS
    if reference_at and sla_hours and sla_hours > 0:
        deadline_at = reference_at + timedelta(hours=sla_hours)
        now = datetime.now()
        if deadline_at < now:
            overdue_hours = max(int((now - deadline_at).total_seconds() + 3599) // 3600, 1)
            # Future notification hook: notify the supervisor and contract owner when support SLA is overdue.
            return {
                "label": f"Apoio em atraso",
                "message": f"{stage_name}: apoio esta ha {overdue_hours}h alem do SLA de {sla_hours}h.",
                "severity": "critical",
            }

    support_name = lead_workflow.assigned_user.name if lead_workflow.assigned_user else "Apoio"
    return {
        "label": "Apoio em andamento",
        "message": f"{stage_name}: tarefa com {support_name}.",
        "severity": "info",
    }


def scheduled_contact_task_alert(lead_workflow):
    today = datetime.now().date()
    tasks = scheduled_contact_tasks_for_workflow(lead_workflow)
    overdue = [task for task in tasks if not task.completed_at and task.scheduled_for < today]
    if overdue:
        oldest = min(task.scheduled_for for task in overdue)
        days = max((today - oldest).days, 1)
        return {
            "label": "Tarefa em atraso",
            "message": f"{len(overdue)} tarefa(s) em atraso desde {oldest.strftime('%d/%m/%Y')}.",
            "severity": "critical",
            "is_overdue": True,
            "days_overdue": days,
        }

    due_today = [task for task in tasks if not task.completed_at and task.scheduled_for == today]
    if due_today:
        return {
            "label": "Tarefas de hoje",
            "message": f"{len(due_today)} tarefa(s) para concluir hoje.",
            "severity": "warning",
        }
    return None


def workflow_stage_deadline(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return None, None
    if lead_workflow.due_at:
        return lead_workflow.due_at, "due_at"
    if lead_workflow.current_stage.max_days and lead_workflow.entered_stage_at:
        return lead_workflow.entered_stage_at + timedelta(days=lead_workflow.current_stage.max_days), "max_days"
    return None, None


def workflow_stage_deadline_alert(lead_workflow):
    deadline_at, source = workflow_stage_deadline(lead_workflow)
    if not deadline_at:
        return None

    now = datetime.now()
    stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "Etapa"
    is_overdue = deadline_at < now
    if source == "due_at" and deadline_at.time() == datetime.min.time():
        is_overdue = deadline_at.date() < now.date()

    if is_overdue:
        if source == "due_at" and deadline_at.time() == datetime.min.time():
            overdue_days = max((now.date() - deadline_at.date()).days, 1)
            message = f"Etapa vencida ha {overdue_days} dia(s). Prazo: {deadline_at.strftime('%d/%m/%Y')}."
        else:
            overdue_hours = max(int((now - deadline_at).total_seconds() + 3599) // 3600, 1)
            if overdue_hours >= 24:
                overdue_days = max(overdue_hours // 24, 1)
                message = f"Etapa vencida ha {overdue_days} dia(s). Prazo: {deadline_at.strftime('%d/%m/%Y %H:%M')}."
            else:
                message = f"Etapa vencida ha {overdue_hours}h. Prazo: {deadline_at.strftime('%d/%m/%Y %H:%M')}."
        return {
            "label": f"{stage_name} em atraso",
            "message": message,
            "severity": "critical",
        }

    alert_days_before = (
        lead_workflow.current_stage.alert_days_before
        if lead_workflow.current_stage and lead_workflow.current_stage.alert_days_before is not None
        else 3
    )
    if alert_days_before and deadline_at <= now + timedelta(days=alert_days_before):
        if source == "due_at" and deadline_at.time() == datetime.min.time():
            message = f"Prazo da etapa em {deadline_at.strftime('%d/%m/%Y')}."
        else:
            message = f"Prazo da etapa em {deadline_at.strftime('%d/%m/%Y %H:%M')}."
        return {
            "label": f"{stage_name} perto do prazo",
            "message": message,
            "severity": "warning",
        }

    return None


def workflow_alert_for(lead_workflow, unlock_alert=None):
    support_alert = workflow_support_alert(lead_workflow)
    if support_alert and support_alert["severity"] in {"critical", "warning"}:
        return support_alert

    scheduled_alert = scheduled_contact_task_alert(lead_workflow)
    if scheduled_alert:
        return scheduled_alert

    executor_alert = unlock_executor_alert_for_workflow(lead_workflow)
    if executor_alert:
        return executor_alert

    support_executor_alert = support_executor_alert_for_workflow(lead_workflow)
    if support_executor_alert:
        return support_executor_alert

    first_action_state = workflow_first_action_state(lead_workflow)
    if first_action_state:
        return {
            "label": first_action_state["label"],
            "message": first_action_state["message"],
            "severity": first_action_state["severity"],
        }

    unlock_alert = unlock_alert or unlock_alert_for_lead(lead_workflow.lead)
    if unlock_alert.get("completed") and lead_workflow.workflow and lead_workflow.workflow.slug in {
        DEFAULT_LEAD_WORKFLOW_SLUG,
        UNLOCK_WORKFLOW_SLUG,
        }:
        return None

    requirement_alert = workflow_stage_requirement_alert(lead_workflow)
    if requirement_alert:
        return requirement_alert

    deadline_alert = workflow_stage_deadline_alert(lead_workflow)
    if deadline_alert and deadline_alert["severity"] == "critical":
        return deadline_alert

    if unlock_alert["active"]:
        return {
            "label": unlock_alert["title"],
            "message": unlock_alert["message"],
            "severity": unlock_alert["severity"],
        }

    if support_alert:
        return support_alert

    if deadline_alert:
        return deadline_alert
    return None


def can_move_lead_workflow(user, lead_workflow):
    if not getattr(user, "can_move_workflows", False):
        return False
    if not lead_workflow or not lead_workflow.lead or lead_workflow.lead.deleted_at:
        return False
    return can_access_lead_scope(user, lead_workflow.lead)


def can_move_lead_workflow_backwards(user, lead_workflow):
    return bool(
        can_move_lead_workflow(user, lead_workflow)
        and getattr(user, "can_move_workflows_backwards", False)
    )


def can_update_lead_workflow_checklist(user, lead_workflow):
    if can_move_lead_workflow(user, lead_workflow):
        return True
    return support_actor_for_workflow(user, lead_workflow)


def can_view_lead_audit(user):
    return bool(user and (getattr(user, "is_admin", False) or getattr(user, "can_view_audit", False)))


def workflow_kanban_moves_enabled():
    return env_truthy(WORKFLOW_KANBAN_MOVES_ENABLED)


def workflow_target_stage_for_advance(lead_workflow):
    return next_stage_for_lead_workflow(lead_workflow) or temporary_return_stage_for_lead_workflow(lead_workflow)


def ensure_lead_workflow_is_operational(lead_workflow):
    current_operational_workflows, _ = operational_lead_workflows_for_detail(
        lead_workflow.lead,
        lead_workflows_for_detail(lead_workflow.lead),
    )
    if lead_workflow.id not in {item.id for item in current_operational_workflows}:
        raise ValueError("Este cliente está em outro fluxo operacional no momento.")
    return True


def workflow_card_actions_for_board(lead_workflows, user):
    kanban_moves_enabled = workflow_kanban_moves_enabled()
    operational_ids = operational_lead_workflow_ids_for_board([item.lead for item in lead_workflows])
    actions = {}
    for lead_workflow in lead_workflows:
        next_stage = next_stage_for_lead_workflow(lead_workflow)
        target_stage = next_stage or temporary_return_stage_for_lead_workflow(lead_workflow)
        is_operational = lead_workflow.id in operational_ids
        can_move = (
            kanban_moves_enabled
            and can_move_lead_workflow(user, lead_workflow)
            and is_operational
            and bool(target_stage)
        )
        actions[lead_workflow.id] = {
            "next_stage": next_stage,
            "target_stage": target_stage,
            "is_return_to_principal": bool(target_stage and not next_stage),
            "is_operational": is_operational,
            "can_move": can_move,
            "can_drag": can_move and bool(next_stage),
            "blocked_reason": workflow_card_blocked_reason(lead_workflow, user, is_operational, target_stage),
        }
    return actions


def workflow_card_blocked_reason(lead_workflow, user, is_operational, target_stage):
    if not can_move_lead_workflow(user, lead_workflow):
        return "Sem permissão para mover"
    if not is_operational:
        return "Ativo em outro fluxo"
    if not target_stage:
        return "Última etapa"
    return None


def operational_lead_workflow_ids_for_board(leads):
    lead_ids = sorted({lead.id for lead in leads if lead})
    if not lead_ids:
        return set()

    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .filter(LeadWorkflow.lead_id.in_(lead_ids))
        .all()
    )
    workflows_by_lead = {lead_id: [] for lead_id in lead_ids}
    for lead_workflow in lead_workflows:
        workflows_by_lead.setdefault(lead_workflow.lead_id, []).append(lead_workflow)

    operational_ids = set()
    for lead_id, items in workflows_by_lead.items():
        principal = lead_workflow_by_slug_from_list(items, DEFAULT_LEAD_WORKFLOW_SLUG)
        if not principal or not principal.current_stage:
            continue

        config = temporary_workflow_config_for_principal_stage(principal.current_stage.slug)
        if config:
            child = lead_workflow_by_slug_from_list(items, config["workflow_slug"])
            if child and child.status != "concluido":
                operational_ids.add(child.id)
            continue

        operational_ids.add(principal.id)

    return operational_ids


def operational_lead_workflows_for_leads(leads):
    operational_ids = operational_lead_workflow_ids_for_board(leads)
    if not operational_ids:
        return {}

    lead_workflows = (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
        )
        .filter(LeadWorkflow.id.in_(operational_ids))
        .all()
    )
    return {lead_workflow.lead_id: lead_workflow for lead_workflow in lead_workflows}


def workflow_rules_for_detail(lead_workflows):
    rules = {}
    for lead_workflow in lead_workflows:
        next_stage = next_stage_for_lead_workflow(lead_workflow)
        return_stage = None if next_stage else temporary_return_stage_for_lead_workflow(lead_workflow)
        seller_execution_task = seller_execution_task_state(lead_workflow)
        rules[lead_workflow.id] = {
            "next_stage": next_stage,
            "return_stage": return_stage,
            "target_stage": next_stage or return_stage,
            "is_return_to_principal": bool(return_stage and not next_stage),
            "checklist": workflow_checklist_state(lead_workflow),
            "scheduled_tasks": scheduled_contact_task_state(lead_workflow),
            "is_fidelity": lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG,
            "can_create_task": can_create_fidelity_task(g.user, lead_workflow) if has_request_context() else False,
            "seller_execution_task": seller_execution_task,
            "required_field_labels": required_field_labels_for_stage(lead_workflow.current_stage),
            "requires_note": bool(lead_workflow.current_stage and lead_workflow.current_stage.requires_note),
            "missing": missing_requirements_for_advance(lead_workflow, note=None),
        }
    return rules


def workflow_stage_tracks_for_detail(lead_workflows, workflow_alerts_by_id=None):
    workflow_alerts_by_id = workflow_alerts_by_id or {}
    workflow_ids = sorted({
        lead_workflow.workflow_id
        for lead_workflow in lead_workflows
        if lead_workflow.workflow_id
    })
    if not workflow_ids:
        return {}

    stages_by_workflow_id = {}
    stages = (
        WorkflowStage.query.filter(
            WorkflowStage.workflow_id.in_(workflow_ids),
            WorkflowStage.active.is_(True),
        )
        .order_by(WorkflowStage.workflow_id.asc(), WorkflowStage.sort_order.asc(), WorkflowStage.id.asc())
        .all()
    )
    for stage in stages:
        stages_by_workflow_id.setdefault(stage.workflow_id, []).append(stage)

    tracks = {}
    for lead_workflow in lead_workflows:
        workflow_stages = list(stages_by_workflow_id.get(lead_workflow.workflow_id, []))
        current_stage = lead_workflow.current_stage
        target_stage = workflow_target_stage_for_advance(lead_workflow)
        if current_stage and current_stage.id not in {stage.id for stage in workflow_stages}:
            workflow_stages.append(current_stage)
            workflow_stages.sort(key=lambda stage: (stage.sort_order, stage.id))

        current_sort_order = current_stage.sort_order if current_stage else None
        stage_track = []
        for stage in workflow_stages:
            if current_stage and stage.id == current_stage.id:
                state = "current"
            elif current_sort_order is not None and stage.sort_order < current_sort_order:
                state = "done"
            else:
                state = "future"

            navigation = None
            if lead_workflow.workflow.slug != UNLOCK_WORKFLOW_SLUG and current_stage and stage.id != current_stage.id and stage.workflow_id == lead_workflow.workflow_id:
                if stage.sort_order < current_sort_order:
                    navigation = "back"
                elif target_stage and target_stage.workflow_id == lead_workflow.workflow_id and stage.id == target_stage.id:
                    navigation = "next"

            stage_track.append({
                "stage": stage,
                "state": state,
                "has_alert": bool(state == "current" and workflow_alerts_by_id.get(lead_workflow.id)),
                "navigation": navigation,
            })
        tracks[lead_workflow.id] = stage_track
    return tracks


def workflow_checklist_state(lead_workflow):
    if unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return []
    items = checklist_items_for_workflow(lead_workflow)
    checks = checklist_checks_by_item_id(lead_workflow)
    return [
        {
            "item": item,
            "checked": bool(checks.get(item.id) and checks[item.id].checked),
            "check": checks.get(item.id),
            "blocked": bool(getattr(item, "depends_on_id", None) and
                            (not checks.get(item.depends_on_id) or not checks[item.depends_on_id].checked)),
            "due_at": checklist_due_at(checks.get(item.id), lead_workflow.due_at),
        }
        for item in items
    ]


def unlock_contact_tasks_enabled_for_workflow(lead_workflow):
    return bool(
        lead_workflow
        and lead_workflow.workflow
        and lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG
        and lead_workflow.current_stage
        and lead_workflow.current_stage.slug in UNLOCK_CONTACT_TASK_RULES
        and lead_workflow.status not in TERMINAL_WORKFLOW_STATUSES
    )


def is_business_day(value):
    return bool(value and value.weekday() < 5)


def previous_business_day(value):
    if not value:
        return None
    current = value
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def business_days_between(start_date, end_date):
    if not start_date or not end_date or start_date > end_date:
        return []
    days = []
    current = start_date
    while current <= end_date:
        if is_business_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def select_evenly_spaced_dates(dates, count):
    dates = list(dates or [])
    if not dates or not count or count <= 0:
        return []
    if len(dates) <= count:
        return dates
    if count == 1:
        return [dates[0]]
    selected = []
    for index in range(count):
        position = round(index * (len(dates) - 1) / (count - 1))
        selected.append(dates[position])
    return sorted(set(selected))


def unlock_contact_stage_end_date(lead_workflow):
    if not unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return None
    unlock_date = unlock_date_for_lead(lead_workflow.lead)
    if not unlock_date:
        return None
    stage_slug = lead_workflow.current_stage.slug
    rule = UNLOCK_CONTACT_TASK_RULES.get(stage_slug) or {}
    floor_days = rule.get("window_floor_days")
    if stage_slug == "10-dias":
        return previous_business_day(unlock_date - timedelta(days=1))
    if floor_days is None:
        return previous_business_day(unlock_date - timedelta(days=1))
    return previous_business_day(unlock_date - timedelta(days=floor_days))


def unlock_contact_stage_start_date(lead_workflow):
    if not unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return None
    unlock_date = unlock_date_for_lead(lead_workflow.lead)
    if not unlock_date:
        return None
    rule = UNLOCK_CONTACT_TASK_RULES.get(lead_workflow.current_stage.slug) or {}
    ceiling_days = rule.get("window_ceiling_days")
    if ceiling_days is None:
        return None
    return unlock_date - timedelta(days=ceiling_days)


def desired_unlock_contact_dates(lead_workflow, today=None):
    if not unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return []
    today = today or datetime.now().date()
    window_start_date = unlock_contact_stage_start_date(lead_workflow)
    end_date = unlock_contact_stage_end_date(lead_workflow)
    if not end_date:
        return []
    if window_start_date and today < window_start_date:
        return []
    start_candidates = [lead_workflow.fidelity_started_on or today]
    if window_start_date:
        start_candidates.append(window_start_date)
    start_date = max(start_candidates)
    dates = business_days_between(start_date, end_date)
    if not dates:
        return []

    rule = UNLOCK_CONTACT_TASK_RULES.get(lead_workflow.current_stage.slug) or {}
    contacts_per_week = rule.get("contacts_per_week")
    if not contacts_per_week:
        return dates

    by_week = {}
    for date_value in dates:
        by_week.setdefault(date_value.isocalendar()[:2], []).append(date_value)

    selected = []
    for week_dates in by_week.values():
        selected.extend(select_evenly_spaced_dates(week_dates, contacts_per_week))
    return sorted(set(selected))


def scheduled_contact_task_key(stage_slug, scheduled_for):
    return f"unlock-contact:{stage_slug}:{scheduled_for.isoformat()}"


def scheduled_contact_task_label(stage_name, scheduled_for):
    return f"Contato com cliente - {scheduled_for.strftime('%d/%m/%Y')}"


def ensure_scheduled_contact_tasks_for_workflow(lead_workflow, today=None):
    if not unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return 0
    today = today or datetime.now().date()
    changed = 0
    if lead_workflow.fidelity_started_on is None:
        lead_workflow.fidelity_started_on = today
        changed += 1
    changed += ensure_fidelity_inss_tasks(lead_workflow, today=today)
    unlock_date = unlock_date_for_lead(lead_workflow.lead)
    if not unlock_date:
        return changed
    desired_keys = {}
    stages = {stage.slug: stage for stage in WorkflowStage.query.filter_by(workflow_id=lead_workflow.workflow_id, active=True).all()}
    revision, effective_from = contact_agenda_revision(lead_workflow)
    rules = schedule_configuration(json.loads(revision.content))["contacts"] if revision else UNLOCK_CONTACT_TASK_RULES
    for occurrence in contact_calendar(unlock_date, lead_workflow.fidelity_started_on, rules):
        stage = stages.get(occurrence["stage_slug"])
        if stage and (not effective_from or occurrence["date"] >= effective_from):
            desired_keys[occurrence["key"]] = (occurrence["date"], stage, occurrence["label"])
    existing_tasks = (
        LeadWorkflowScheduledTask.query.filter_by(
            lead_workflow_id=lead_workflow.id,
        ).filter(LeadWorkflowScheduledTask.task_key.like("unlock-contact:%"))
        .all()
    )
    existing_by_key = {task.task_key: task for task in existing_tasks}
    now = datetime.now()
    for task_key, (scheduled_for, stage, label) in desired_keys.items():
        task = existing_by_key.get(task_key)
        if task:
            # Inactive keys remain occupied, including explicitly replanned occurrences.
            continue
        if scheduled_for < today:
            continue
        add_configured_task(
            LeadWorkflowScheduledTask(
                lead_workflow_id=lead_workflow.id,
                lead_id=lead_workflow.lead_id,
                workflow_id=lead_workflow.workflow_id,
                stage_id=stage.id,
                task_key=task_key,
                label=f"{label} - {scheduled_for:%d/%m/%Y}",
                scheduled_for=scheduled_for,
                active=True,
                created_at=now,
                updated_at=now,
            ), revision=revision
        )
        changed += 1

    for task in existing_tasks:
        if effective_from and task.scheduled_for < effective_from:
            continue
        if task.note or task.completed_by_id:
            continue
        if task.scheduled_for <= today and not task.completed_at:
            continue
        if task.task_key in desired_keys or task.completed_at or not task.active:
            continue
        task.active = False
        task.updated_at = now
        changed += 1
    return changed


def ensure_fidelity_inss_tasks(tracking, today=None):
    today = today or datetime.now().date()
    existing = {task.task_key: task for task in LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).all()}
    legacy = lead_workflow_by_slug(tracking.lead, "fidelizacao")
    checks = checklist_checks_by_item_id(legacy) if legacy else {}
    revision = effective_revision(tracking.lead.company_id, tracking.lead.franchise_id)
    rules = {rule["key"]: rule for rule in schedule_configuration(json.loads(revision.content))["inss"]} if revision else {}
    changed = 0
    for key, label, legacy_slug in FIDELITY_INSS_TASKS:
        task = existing.get(key)
        if task:
            if task.completed_at:
                continue
            break
        rule = rules.get(key, {"active": True, "label": label, "delay_business_days": 0, "depends_on": None})
        if not rule["active"]:
            break
        parent = existing.get(rule.get("depends_on"))
        if rule.get("depends_on") and (not parent or not parent.completed_at):
            break
        released_on = parent.completed_at.date() if parent else tracking.fidelity_started_on or today
        # Restore only concrete completion evidence, never infer it from the current column.
        completed_checks = [check for check in checks.values() if check.checked and check.checked_at and check.checklist_item.stage.slug == legacy_slug]
        transition = None
        if legacy:
            candidates = (WorkflowTransition.query.join(WorkflowStage, WorkflowTransition.from_stage_id == WorkflowStage.id)
                          .filter(WorkflowTransition.lead_workflow_id == legacy.id,
                                  WorkflowStage.slug == legacy_slug,
                                  WorkflowTransition.action.in_(("move", "complete_temporary")))
                          .order_by(WorkflowTransition.created_at.desc()).all())
            transition = next((event for event in candidates if event.action == "complete_temporary"
                               or (event.to_stage and event.to_stage.sort_order > event.from_stage.sort_order
                                   and (event.note or workflow_transition_details(event).get("checklist_changes")))), None)
        evidence = max(completed_checks, key=lambda check: check.checked_at) if completed_checks else None
        completed_at = evidence.checked_at if evidence else transition.created_at if transition else None
        if not completed_at and (unlock_days_left(tracking.lead) or 0) <= 0 and not existing:
            break
        task = LeadWorkflowScheduledTask(
            lead_workflow_id=tracking.id, lead_id=tracking.lead_id,
            workflow_id=tracking.workflow_id, stage_id=tracking.current_stage_id,
            task_key=key, label=rule["label"], scheduled_for=business_deadline(max(today, released_on), rule["delay_business_days"]),
            completed_at=completed_at,
            completed_by_id=evidence.checked_by_id if evidence else transition.user_id if transition else None,
            note=evidence.note if evidence else transition.note if transition else None,
            active=True,
        )
        add_configured_task(task, revision=revision)
        db.session.flush()
        existing[key] = task
        changed += 1
        if not completed_at:
            break
    return changed


def scheduled_task_workflow_ids(lead_workflow):
    if not lead_workflow or not lead_workflow.workflow or lead_workflow.status in TERMINAL_WORKFLOW_STATUSES:
        return []
    ids = [lead_workflow.id]
    if lead_workflow.workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG:
        tracking = lead_workflow_by_slug(lead_workflow.lead, UNLOCK_WORKFLOW_SLUG)
        if tracking:
            ids.append(tracking.id)
    return ids


def can_create_fidelity_task(user, lead_workflow):
    return bool(lead_workflow and lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG
                and lead_workflow.status not in TERMINAL_WORKFLOW_STATUSES
                and not lead_access_is_reversal_only(user, lead_workflow.lead)
                and can_move_lead_workflow(user, lead_workflow))


def create_fidelity_task(lead_workflow, user, label, due_date):
    if not can_create_fidelity_task(user, lead_workflow):
        raise ValueError("Seu usuario nao pode criar tarefas para este cliente.")
    label = clean_text(label)
    if not label or len(label) > 180:
        raise ValueError("Informe uma tarefa com ate 180 caracteres.")
    try:
        due = datetime.strptime(due_date or "", "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Informe uma data valida para a tarefa.")
    if due < datetime.now().date():
        raise ValueError("O prazo da nova tarefa nao pode estar no passado.")
    task = LeadWorkflowScheduledTask(
        lead_workflow_id=lead_workflow.id, lead_id=lead_workflow.lead_id,
        workflow_id=lead_workflow.workflow_id, stage_id=lead_workflow.current_stage_id,
        task_key=f"custom:{secrets.token_hex(16)}", label=label, scheduled_for=due,
    )
    add_configured_task(task)
    db.session.flush()
    db.session.add(WorkflowTransition(
        lead_workflow_id=lead_workflow.id, lead_id=lead_workflow.lead_id,
        workflow_id=lead_workflow.workflow_id, from_stage_id=lead_workflow.current_stage_id,
        to_stage_id=lead_workflow.current_stage_id, user_id=user.id, action="scheduled_task_create",
        details=json.dumps({"task_id": task.id, "task_label": label, "scheduled_for": due.isoformat()}),
        note=f"{label}. Prazo: {due.strftime('%d/%m/%Y')}.",
    ))
    log_audit(lead_workflow.lead, user, "workflow_task_create", details={"task_id": task.id, "label": label, "scheduled_for": due.isoformat()})
    return task


def ensure_scheduled_contact_tasks_for_workflows(lead_workflows):
    changed = 0
    for lead_workflow in lead_workflows or []:
        changed += ensure_scheduled_contact_tasks_for_workflow(lead_workflow)
    return changed


def scheduled_contact_tasks_for_workflow(lead_workflow):
    ids = scheduled_task_workflow_ids(lead_workflow)
    if not ids:
        return []
    ensure_scheduled_contact_tasks_for_workflow(lead_workflow)
    query = (
        LeadWorkflowScheduledTask.query.options(joinedload(LeadWorkflowScheduledTask.completed_by))
        .filter(
            LeadWorkflowScheduledTask.lead_workflow_id.in_(ids),
            LeadWorkflowScheduledTask.active.is_(True),
            or_(LeadWorkflowScheduledTask.stage_id == lead_workflow.current_stage_id,
                LeadWorkflowScheduledTask.scheduled_for <= datetime.now().date(),
                ~LeadWorkflowScheduledTask.task_key.like("unlock-contact:%")),
        )
    )
    if lead_workflow.workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG:
        query = query.filter(LeadWorkflowScheduledTask.completed_at.is_(None))
    return query.order_by(LeadWorkflowScheduledTask.scheduled_for.asc(), LeadWorkflowScheduledTask.id.asc()).all()


def scheduled_contact_task_status(task, today=None):
    today = today or datetime.now().date()
    if task.completed_at:
        return "done"
    if task.scheduled_for < today:
        return "overdue"
    if task.scheduled_for == today:
        return "today"
    return "future"


def scheduled_contact_task_status_label(task, today=None):
    labels = {
        "done": "Concluida",
        "overdue": "Atrasada",
        "today": "Pendente hoje",
        "future": "Agendada",
    }
    return labels.get(scheduled_contact_task_status(task, today=today), "Pendente")


def scheduled_contact_task_state(lead_workflow):
    today = datetime.now().date()
    return [
        {
            "task": task,
            "checked": bool(task.completed_at),
            "is_due": bool(task.scheduled_for <= today),
            "is_overdue": bool(not task.completed_at and task.scheduled_for < today),
            "status": scheduled_contact_task_status(task, today=today),
            "status_label": scheduled_contact_task_status_label(task, today=today),
            "due_at": datetime.combine(task.scheduled_for, datetime.min.time()),
        }
        for task in scheduled_contact_tasks_for_workflow(lead_workflow)
    ]


def missing_scheduled_contact_task_labels(lead_workflow, include_future=False):
    today = datetime.now().date()
    missing = []
    for task in scheduled_contact_tasks_for_workflow(lead_workflow):
        if task.completed_at:
            continue
        if task.scheduled_for <= today or include_future:
            missing.append(task.label)
    return missing


def seller_execution_task_config(lead_workflow):
    if (
        not lead_workflow
        or not lead_workflow.workflow
        or not lead_workflow.current_stage
        or lead_workflow.status in TERMINAL_WORKFLOW_STATUSES
    ):
        return None

    workflow_slug = clean_text(lead_workflow.workflow.slug)
    stage_slug = clean_text(lead_workflow.current_stage.slug)
    config = SELLER_EXECUTION_TASKS.get((workflow_slug, stage_slug))
    if not config:
        return None

    if stage_slug == "dia-do-desbloqueio":
        if lead_workflow.unlock_executor != UNLOCK_EXECUTOR_SELLER:
            return None
    elif lead_workflow.support_executor != SUPPORT_EXECUTOR_SELLER:
        return None

    return config


def seller_execution_task_completion(lead_workflow, task_key=None):
    if not lead_workflow or not lead_workflow.current_stage:
        return None
    config = seller_execution_task_config(lead_workflow)
    task_key = task_key or (config or {}).get("key")
    if not task_key:
        return None

    events = (
        WorkflowTransition.query.options(joinedload(WorkflowTransition.user))
        .filter_by(
            lead_workflow_id=lead_workflow.id,
            to_stage_id=lead_workflow.current_stage_id,
            action="seller_execution_complete",
        )
        .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
        .limit(20)
        .all()
    )
    for event in events:
        details = workflow_transition_details(event)
        if details.get("task_key") == task_key:
            return event
    return None


def seller_execution_task_state(lead_workflow):
    config = seller_execution_task_config(lead_workflow)
    if not config:
        return None
    completed_event = seller_execution_task_completion(lead_workflow, config["key"])
    return {
        "key": config["key"],
        "label": config["label"],
        "checked": bool(completed_event),
        "completed_at": completed_event.created_at if completed_event else None,
        "completed_by": completed_event.user if completed_event else None,
        "due_at": lead_workflow.due_at,
    }


def checklist_items_for_stage(stage):
    if not stage:
        return []
    return (
        WorkflowChecklistItem.query.filter_by(stage_id=stage.id, active=True)
        .order_by(WorkflowChecklistItem.sort_order.asc(), WorkflowChecklistItem.id.asc())
        .all()
    )


def checklist_due_at(check, fallback):
    snapshot = task_snapshot(check) if check else {}
    if "due_at" not in snapshot:
        return fallback
    return datetime.fromisoformat(snapshot["due_at"]) if snapshot["due_at"] else None


def checklist_items_for_workflow(lead_workflow, stage=None):
    stage = stage or lead_workflow.current_stage
    if not stage:
        return []
    checks = checklist_checks_by_item_id(lead_workflow)
    captured = ChecklistStageConfiguration.query.filter_by(lead_workflow_id=lead_workflow.id, stage_id=stage.id).first()
    if captured:
        views = [checklist_item_view(checks[item_id], checks[item_id].checklist_item)
                 for item_id in json.loads(captured.item_ids) if item_id in checks]
        return sorted(views, key=lambda item: (item.sort_order, item.id))
    items = checklist_items_for_stage(stage)
    if stage.id == lead_workflow.current_stage_id and lead_workflow.status not in TERMINAL_WORKFLOW_STATUSES:
        for item in items:
            if item.id not in checks:
                checks[item.id] = add_configured_task(LeadWorkflowChecklistCheck(
                    lead_workflow_id=lead_workflow.id, checklist_item_id=item.id, checked=False))
        db.session.add(ChecklistStageConfiguration(lead_workflow_id=lead_workflow.id, stage_id=stage.id,
                                                  item_ids=json.dumps([item.id for item in items])))
        db.session.flush()
    return [checklist_item_view(checks.get(item.id), item) for item in items]


def checklist_checks_by_item_id(lead_workflow):
    checks = LeadWorkflowChecklistCheck.query.filter_by(lead_workflow_id=lead_workflow.id).all()
    return {check.checklist_item_id: check for check in checks}


def next_stage_for_lead_workflow(lead_workflow):
    if not lead_workflow or not lead_workflow.current_stage:
        return None
    if lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG:
        return None
    return (
        WorkflowStage.query.filter(
            WorkflowStage.workflow_id == lead_workflow.workflow_id,
            WorkflowStage.active.is_(True),
            WorkflowStage.sort_order > lead_workflow.current_stage.sort_order,
        )
        .order_by(WorkflowStage.sort_order.asc(), WorkflowStage.id.asc())
        .first()
    )


def temporary_return_stage_for_lead_workflow(lead_workflow):
    if lead_workflow and lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG:
        return None
    if (
        not lead_workflow
        or not lead_workflow.workflow
        or not lead_workflow.current_stage
        or next_stage_for_lead_workflow(lead_workflow)
    ):
        return None

    principal_stage_slug, config = temporary_workflow_config_for_child_workflow(lead_workflow.workflow.slug)
    if not config:
        return None

    principal = primary_lead_workflow_for_lead(lead_workflow.lead, create_missing=False)
    if (
        not principal
        or not principal.current_stage
        or principal.current_stage.slug != principal_stage_slug
    ):
        return None

    return WorkflowStage.query.filter_by(
        workflow_id=principal.workflow_id,
        slug=config["return_stage_slug"],
    ).first()


def stage_required_fields(stage):
    if not stage or not clean_text(stage.required_fields):
        return []
    try:
        fields = json.loads(stage.required_fields)
    except (TypeError, ValueError):
        return []
    return [field for field in fields if isinstance(field, str) and field]


def required_field_labels_for_stage(stage):
    return [audit_field_label(field) for field in stage_required_fields(stage)]


def missing_requirements_for_advance(lead_workflow, note=None):
    missing = []
    if not lead_workflow or not lead_workflow.current_stage:
        return ["etapa atual do fluxo"]
    if not next_stage_for_lead_workflow(lead_workflow) and not temporary_return_stage_for_lead_workflow(lead_workflow):
        return ["próxima etapa configurada"]

    lead = lead_workflow.lead
    for field in stage_required_fields(lead_workflow.current_stage):
        if not lead_field_has_value(lead, field):
            missing.append(audit_field_label(field))

    checklist_missing = missing_checklist_labels(lead_workflow)
    if checklist_missing:
        missing.extend(f"tarefa: {label}" for label in checklist_missing)

    scheduled_missing = missing_scheduled_contact_task_labels(lead_workflow, include_future=True)
    if scheduled_missing:
        missing.extend(f"tarefa: {label}" for label in scheduled_missing)

    seller_task = seller_execution_task_state(lead_workflow)
    if seller_task and not seller_task.get("checked"):
        missing.append(f"tarefa: {seller_task['label']}")

    has_stage_tasks = bool(checklist_items_for_workflow(lead_workflow))
    has_seller_task = bool(seller_task)
    if lead_workflow.current_stage.requires_note and not has_stage_tasks and not has_seller_task and not clean_text(note):
        missing.append("anotacao da etapa")

    if support_return_resolution_pending(lead_workflow):
        missing.append("resolver devolucao do apoio")

    if (
        is_principal_unlock_stage(lead_workflow, {"dia-do-desbloqueio"})
        and lead_workflow.unlock_executor not in UNLOCK_EXECUTOR_CHOICES
    ):
        missing.append("responsavel pelo desbloqueio")

    if (
        support_decision_enabled_for_workflow(lead_workflow)
        and lead_workflow.support_executor not in SUPPORT_EXECUTOR_CHOICES
    ):
        stage_name = lead_workflow.current_stage.name if lead_workflow.current_stage else "etapa"
        missing.append(f"responsavel por {stage_name}")

    if lead_workflow_requires_support(lead_workflow) and not lead_workflow.support_completed_at:
        missing.append("apoio comercial")

    return missing


def lead_field_has_value(lead, field):
    if not hasattr(lead, field):
        return False
    value = getattr(lead, field)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def stage_has_activity(lead_workflow):
    return db.session.query(
        LeadActivity.query.filter(
            LeadActivity.lead_id == lead_workflow.lead_id,
            LeadActivity.lead_workflow_id == lead_workflow.id,
            LeadActivity.stage_id == lead_workflow.current_stage_id,
            LeadActivity.deleted_at.is_(None),
        ).exists()
    ).scalar()


def missing_checklist_labels(lead_workflow):
    if unlock_contact_tasks_enabled_for_workflow(lead_workflow):
        return []
    items = [
        item
        for item in checklist_items_for_workflow(lead_workflow)
        if item.required
    ]
    if not items:
        return []
    checks = checklist_checks_by_item_id(lead_workflow)
    return [item.label for item in items if not checks.get(item.id) or not checks[item.id].checked]


def completed_stage_checklist_details(lead_workflow, stage):
    required_items = [
        item
        for item in checklist_items_for_workflow(lead_workflow, stage)
        if item.required
    ]
    if not required_items:
        return None

    checks = checklist_checks_by_item_id(lead_workflow)
    checked_at_values = []
    checked_by_id = None
    for item in required_items:
        check = checks.get(item.id)
        if not check or not check.checked:
            return None
        if check.checked_at:
            checked_at_values.append(check.checked_at)
        if check.checked_by_id:
            checked_by_id = check.checked_by_id

    return {
        "completed_at": max(checked_at_values) if checked_at_values else None,
        "checked_by_id": checked_by_id,
    }


def restore_unlock_support_completion_from_checklist(lead_workflow, stage):
    if (
        not lead_workflow
        or not stage
        or stage.slug != "dia-do-desbloqueio"
        or lead_workflow.unlock_executor != UNLOCK_EXECUTOR_SUPPORT
        or lead_workflow.support_completed_at
    ):
        return False

    checklist_details = completed_stage_checklist_details(lead_workflow, stage)
    if not checklist_details:
        return False

    support_transition = (
        WorkflowTransition.query.filter_by(
            lead_workflow_id=lead_workflow.id,
            action="support_complete",
            to_stage_id=stage.id,
        )
        .order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc())
        .first()
    )
    completed_at = (
        support_transition.created_at
        if support_transition
        else checklist_details.get("completed_at")
        or lead_workflow.last_action_at
        or datetime.now()
    )

    lead_workflow.support_completed_at = completed_at
    lead_workflow.support_returned_at = None
    lead_workflow.support_return_reason = None
    lead_workflow.support_requested_at = lead_workflow.support_requested_at or completed_at
    lead_workflow.support_assigned_at = lead_workflow.support_assigned_at or completed_at
    support_user_id = support_transition.user_id if support_transition else checklist_details.get("checked_by_id")
    if support_user_id and not lead_workflow.assigned_user_id:
        lead_workflow.assigned_user_id = support_user_id
    lead_workflow.updated_at = datetime.now()
    return True


def apply_lead_workflow_checklist_form(lead_workflow, form, user):
    checklist_items = checklist_items_for_workflow(lead_workflow)
    if not checklist_items:
        return []

    selected_ids = {parse_int(value, 0) for value in form.getlist("checklist_item_ids")}
    selected_ids.discard(0)
    checks = checklist_checks_by_item_id(lead_workflow)
    for item in checklist_items:
        dependency_id = getattr(item, "depends_on_id", None)
        if dependency_id and item.id in selected_ids and (
                dependency_id not in selected_ids or not checks.get(dependency_id) or not checks[dependency_id].checked):
            raise ValueError("Conclua a tarefa anterior antes de concluir esta tarefa.")
    now = datetime.now()
    changes = []

    for item in checklist_items:
        checked = item.id in selected_ids
        check = checks.get(item.id)
        if not check:
            check = LeadWorkflowChecklistCheck(
                lead_workflow_id=lead_workflow.id,
                checklist_item_id=item.id,
                checked=False,
            )
            add_configured_task(check)

        if bool(check.checked) == checked:
            continue

        check.checked = checked
        check.checked_by_id = user.id if checked else None
        check.checked_at = now if checked else None
        check.updated_at = now
        changes.append({"item": item.label, "checked": checked})

    if changes:
        lead_workflow.last_action_at = now
        lead_workflow.updated_at = now

    return changes


def complete_lead_workflow_checklist_item(lead_workflow, checklist_item_id, user, note=None):
    if not lead_workflow or not lead_workflow.current_stage:
        raise ValueError("Fluxo invalido para concluir tarefa.")
    item_id = parse_int(checklist_item_id, 0)
    item = next((item for item in checklist_items_for_workflow(lead_workflow) if item.id == item_id), None)
    if (
        not item
        or not item.active
        or item.stage_id != lead_workflow.current_stage_id
    ):
        raise ValueError("Tarefa invalida para esta etapa.")

    note = clean_text(note)
    if not note:
        raise ValueError("Informe uma anotacao para concluir a tarefa.")

    checks = checklist_checks_by_item_id(lead_workflow)
    check = checks.get(item.id)
    dependency_id = getattr(item, "depends_on_id", None)
    if dependency_id and (not checks.get(dependency_id) or not checks[dependency_id].checked):
        raise ValueError("Conclua a tarefa anterior antes de concluir esta tarefa.")
    if not check:
        check = LeadWorkflowChecklistCheck(
            lead_workflow_id=lead_workflow.id,
            checklist_item_id=item.id,
            checked=False,
        )
        add_configured_task(check)

    if check.checked:
        raise ValueError("Esta tarefa ja foi concluida.")

    now = datetime.now()
    check.checked = True
    check.checked_by_id = user.id if user else None
    check.checked_at = now
    check.note = note
    check.updated_at = now
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    return {"item": item.label, "checked": True, "note": note}


def complete_scheduled_contact_task(lead_workflow, scheduled_task_id, user, note=None):
    if not lead_workflow or not lead_workflow.current_stage:
        raise ValueError("Fluxo invalido para concluir tarefa.")
    if not can_update_lead_workflow_checklist(user, lead_workflow):
        raise ValueError("Seu usuario nao tem permissao para concluir esta tarefa.")
    ensure_scheduled_contact_tasks_for_workflow(lead_workflow)

    task_id = parse_int(scheduled_task_id, 0)
    task = db.session.get(LeadWorkflowScheduledTask, task_id)
    if (
        not task
        or not task.active
        or task.lead_id != lead_workflow.lead_id
        or task.lead_workflow_id not in scheduled_task_workflow_ids(lead_workflow)
    ):
        raise ValueError("Tarefa programada invalida para esta etapa.")
    if task.completed_at:
        raise ValueError("Esta tarefa ja foi concluida.")
    today = datetime.now().date()
    if task.scheduled_for > today:
        raise ValueError(f"Esta tarefa esta agendada para {task.scheduled_for.strftime('%d/%m/%Y')}.")
    inss_keys = [row[0] for row in FIDELITY_INSS_TASKS]
    if task.task_key in inss_keys:
        for previous_key in inss_keys[:inss_keys.index(task.task_key)]:
            previous = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=task.lead_workflow_id, task_key=previous_key).first()
            if not previous or not previous.completed_at:
                raise ValueError("Conclua a tarefa anterior do INSS primeiro.")

    note = clean_text(note)
    if not note:
        raise ValueError("Informe uma anotacao para concluir a tarefa.")

    now = datetime.now()
    task.completed_at = now
    task.completed_by_id = user.id if user else None
    task.note = note
    task.updated_at = now
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    db.session.flush()
    if task.task_key in inss_keys:
        ensure_fidelity_inss_tasks(task.lead_workflow)
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="scheduled_task_complete",
            note=note,
            details=json.dumps(
                {
                    "source": "scheduled_contact_task",
                    "task_id": task.id,
                    "task_key": task.task_key,
                    "task_label": task.label,
                    "scheduled_for": task.scheduled_for.isoformat(),
                    "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "workflow_checklist_update",
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
            "changes": [
                {
                    "item": task.label,
                    "checked": True,
                    "note": note,
                    "scheduled_for": task.scheduled_for.isoformat(),
                    "scheduled": True,
                }
            ],
        },
    )
    return {"item": task.label, "checked": True, "note": note, "scheduled": True}


def complete_seller_execution_task(lead_workflow, user, task_key=None, note=None):
    if not lead_workflow or not lead_workflow.current_stage:
        raise ValueError("Fluxo invalido para concluir tarefa.")
    if not can_update_lead_workflow_checklist(user, lead_workflow):
        raise ValueError("Seu usuario nao tem permissao para concluir esta tarefa.")

    config = seller_execution_task_config(lead_workflow)
    if not config or (task_key and clean_text(task_key) != config["key"]):
        raise ValueError("Esta tarefa nao esta disponivel nesta etapa.")

    note = clean_text(note)
    if not note:
        raise ValueError("Informe uma anotacao para concluir a tarefa.")
    if seller_execution_task_completion(lead_workflow, config["key"]):
        raise ValueError("Esta tarefa ja foi concluida.")

    now = datetime.now()
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=lead_workflow.current_stage_id,
            user_id=user.id if user else None,
            action="seller_execution_complete",
            note=note,
            details=json.dumps(
                {
                    "source": "seller_execution_task",
                    "task_key": config["key"],
                    "task_label": config["label"],
                    "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )
    log_audit(
        lead_workflow.lead,
        user,
        "workflow_checklist_update",
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
            "changes": [{"item": config["label"], "checked": True, "note": note, "virtual": True}],
        },
    )
    return {"item": config["label"], "checked": True, "note": note, "virtual": True}


def move_lead_workflow_to_next_stage(lead_workflow, user, note=None, checklist_changes=None):
    if lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG:
        raise ValueError("A janela da Fidelizacao muda automaticamente pela data do desbloqueio.")
    next_stage = next_stage_for_lead_workflow(lead_workflow)
    if not next_stage:
        return_stage = temporary_return_stage_for_lead_workflow(lead_workflow)
        if return_stage:
            complete_temporary_workflow_and_return(lead_workflow, return_stage, user, note, checklist_changes)
            return
        raise ValueError("Este fluxo já está na última etapa.")

    now = datetime.now()
    from_stage = lead_workflow.current_stage
    preserve_recorded_support_work = support_work_was_recorded(lead_workflow)
    if note:
        db.session.add(
            LeadActivity(
                lead_id=lead_workflow.lead_id,
                user_id=user.id,
                lead_workflow_id=lead_workflow.id,
                workflow_id=lead_workflow.workflow_id,
                stage_id=lead_workflow.current_stage_id,
                body=note,
                source="workflow_move_note",
                created_at=now,
                updated_at=now,
            )
        )
        ensure_lead_in_progress_status(lead_workflow.lead, user, source="workflow_move_note")

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=next_stage.id,
            user_id=user.id,
            action="move",
            note=note,
            details=json.dumps(
                {
                    "source": "lead_detail",
                    "checklist_changes": checklist_changes or [],
                    "preserved_support_work": preserve_recorded_support_work,
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )

    lead_workflow.current_stage_id = next_stage.id
    lead_workflow.current_stage = next_stage
    lead_workflow.entered_stage_at = now
    lead_workflow.due_at = workflow_due_at_for_stage(lead_workflow.lead, next_stage)
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    support_state_reset = reset_support_state_for_stage(lead_workflow, preserve_recorded_work=True)
    if support_state_reset:
        if default_support_executor_for_workflow(
            lead_workflow,
            user=user,
            source="workflow_move_default_support",
        ):
            assign_support_to_workflow(lead_workflow, requested_by=user)
    if lead_workflow.workflow and lead_workflow.workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG:
        ensure_temporary_workflow_for_principal(lead_workflow, reactivate=True)

    log_audit(
        lead_workflow.lead,
        user,
        "workflow_move",
        field_name="workflow_stage",
        old_value=from_stage.name if from_stage else None,
        new_value=next_stage.name,
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "from_stage_id": from_stage.id if from_stage else None,
            "to_stage_id": next_stage.id,
            "note": note,
            "preserved_support_work": not support_state_reset,
        },
    )


def move_lead_workflow_to_stage(lead_workflow, target_stage, user, source="lead_detail_stage_button", checklist_changes=None):
    if not lead_workflow or not target_stage:
        raise ValueError("Movimentação inválida.")
    if lead_workflow.workflow.slug == UNLOCK_WORKFLOW_SLUG:
        raise ValueError("A janela da Fidelizacao muda automaticamente pela data do desbloqueio.")
    if target_stage.workflow_id != lead_workflow.workflow_id or not target_stage.active:
        raise ValueError("Etapa inválida para este fluxo.")
    if not lead_workflow.current_stage:
        raise ValueError("Este cliente ainda não tem etapa atual.")
    if target_stage.id == lead_workflow.current_stage_id:
        return False

    now = datetime.now()
    from_stage = lead_workflow.current_stage
    direction = "backward" if target_stage.sort_order < from_stage.sort_order else "forward"
    preserve_support_state = direction == "backward" or support_work_was_recorded(lead_workflow)
    restored_unlock_support_completion = False

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=target_stage.id,
            user_id=user.id if user else None,
            action="move",
            details=json.dumps(
                {
                    "source": source,
                    "direction": direction,
                    "preserve_support_state": preserve_support_state,
                    "checklist_changes": checklist_changes or [],
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )

    lead_workflow.current_stage_id = target_stage.id
    lead_workflow.current_stage = target_stage
    lead_workflow.entered_stage_at = now
    lead_workflow.due_at = workflow_due_at_for_stage(lead_workflow.lead, target_stage)
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now
    if not preserve_support_state:
        if reset_support_state_for_stage(lead_workflow, preserve_recorded_work=True):
            if default_support_executor_for_workflow(
                lead_workflow,
                user=user,
                source=f"{source}_default_support",
            ):
                assign_support_to_workflow(lead_workflow, requested_by=user)
    elif target_stage.slug == "dia-do-desbloqueio":
        restored_unlock_support_completion = restore_unlock_support_completion_from_checklist(
            lead_workflow,
            target_stage,
        )
    if lead_workflow.workflow and lead_workflow.workflow.slug == DEFAULT_LEAD_WORKFLOW_SLUG:
        ensure_temporary_workflow_for_principal(lead_workflow, reactivate=True)

    log_audit(
        lead_workflow.lead,
        user,
        "workflow_move",
        field_name="workflow_stage",
        old_value=from_stage.name if from_stage else None,
        new_value=target_stage.name,
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "from_stage_id": from_stage.id if from_stage else None,
            "to_stage_id": target_stage.id,
            "source": source,
            "direction": direction,
            "preserve_support_state": preserve_support_state,
            "restored_unlock_support_completion": restored_unlock_support_completion,
        },
    )
    return True


def complete_temporary_workflow_and_return(lead_workflow, return_stage, user, note=None, checklist_changes=None):
    if not lead_workflow or not lead_workflow.workflow:
        raise ValueError("Fluxo temporario invalido.")

    principal = primary_lead_workflow_for_lead(lead_workflow.lead, create_missing=False)
    if not principal:
        raise ValueError("Fluxo principal nao encontrado para este cliente.")
    if return_stage.workflow_id != principal.workflow_id:
        raise ValueError("Etapa de retorno invalida para o fluxo principal.")

    now = datetime.now()
    temporary_from_stage = lead_workflow.current_stage
    principal_from_stage = principal.current_stage

    if note:
        db.session.add(
            LeadActivity(
                lead_id=lead_workflow.lead_id,
                user_id=user.id,
                lead_workflow_id=lead_workflow.id,
                workflow_id=lead_workflow.workflow_id,
                stage_id=lead_workflow.current_stage_id,
                body=note,
                source="workflow_move_note",
                created_at=now,
                updated_at=now,
            )
        )
        ensure_lead_in_progress_status(lead_workflow.lead, user, source="temporary_workflow_return_note")

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=lead_workflow.id,
            lead_id=lead_workflow.lead_id,
            workflow_id=lead_workflow.workflow_id,
            from_stage_id=lead_workflow.current_stage_id,
            to_stage_id=None,
            user_id=user.id,
            action="complete_temporary",
            note=note,
            details=json.dumps(
                {
                    "source": "lead_detail",
                    "return_stage_id": return_stage.id,
                    "return_stage": return_stage.name,
                    "checklist_changes": checklist_changes or [],
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )

    lead_workflow.status = "concluido"
    lead_workflow.completed_at = now
    lead_workflow.last_action_at = now
    lead_workflow.updated_at = now

    db.session.add(
        WorkflowTransition(
            lead_workflow_id=principal.id,
            lead_id=principal.lead_id,
            workflow_id=principal.workflow_id,
            from_stage_id=principal.current_stage_id,
            to_stage_id=return_stage.id,
            user_id=user.id,
            action="return_from_temporary",
            note=note,
            details=json.dumps(
                {
                    "source": "temporary_workflow",
                    "temporary_lead_workflow_id": lead_workflow.id,
                    "temporary_workflow": lead_workflow.workflow.name,
                    "temporary_stage": temporary_from_stage.name if temporary_from_stage else None,
                    "checklist_changes": checklist_changes or [],
                },
                ensure_ascii=False,
                default=str,
            ),
            created_at=now,
        )
    )

    principal.current_stage_id = return_stage.id
    principal.current_stage = return_stage
    principal.entered_stage_at = now
    principal.due_at = workflow_due_at_for_stage(principal.lead, return_stage)
    principal.last_action_at = now
    principal.updated_at = now
    support_state_reset = reset_support_state_for_stage(principal, preserve_recorded_work=True)
    if support_state_reset:
        if default_support_executor_for_workflow(
            principal,
            user=user,
            source="temporary_return_default_support",
        ):
            assign_support_to_workflow(principal, requested_by=user)
    ensure_temporary_workflow_for_principal(principal, reactivate=True)

    log_audit(
        lead_workflow.lead,
        user,
        "workflow_move",
        field_name="workflow_stage",
        old_value=temporary_from_stage.name if temporary_from_stage else None,
        new_value=return_stage.name,
        details={
            "lead_workflow_id": lead_workflow.id,
            "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
            "principal_lead_workflow_id": principal.id,
            "principal_from_stage_id": principal_from_stage.id if principal_from_stage else None,
            "return_stage_id": return_stage.id,
            "source": "temporary_return",
            "note": note,
            "preserved_support_work": not support_state_reset,
        },
    )


def lead_workflows_for_detail(lead):
    return (
        LeadWorkflow.query.options(
            joinedload(LeadWorkflow.workflow),
            joinedload(LeadWorkflow.current_stage),
            joinedload(LeadWorkflow.owner_user),
        )
        .join(Workflow, LeadWorkflow.workflow_id == Workflow.id)
        .filter(LeadWorkflow.lead_id == lead.id)
        .order_by(Workflow.sort_order.asc(), LeadWorkflow.id.asc())
        .all()
    )


def operational_lead_workflows_for_detail(lead, lead_workflows):
    principal = lead_workflow_by_slug_from_list(lead_workflows, DEFAULT_LEAD_WORKFLOW_SLUG)
    if not principal or not principal.current_stage:
        return list(lead_workflows or []), False

    config = temporary_workflow_config_for_principal_stage(principal.current_stage.slug)
    if not config:
        return [principal], False

    child = lead_workflow_by_slug_from_list(lead_workflows, config["workflow_slug"])
    if child:
        return [child], False

    child, changed = ensure_temporary_workflow_for_principal(principal)
    if child:
        return [child], changed
    return [principal], changed


def current_operational_lead_workflow_for_lead(lead):
    lead_workflows = lead_workflows_for_detail(lead)
    operational_lead_workflows, _workflow_changed = operational_lead_workflows_for_detail(lead, lead_workflows)
    if operational_lead_workflows:
        return operational_lead_workflows[0]
    return primary_lead_workflow_for_lead(lead)


def lead_activity_timeline(lead, limit=80):
    return (
        LeadActivity.query.options(
            joinedload(LeadActivity.user),
            joinedload(LeadActivity.workflow),
            joinedload(LeadActivity.stage),
        )
        .filter(
            LeadActivity.lead_id == lead.id,
            LeadActivity.deleted_at.is_(None),
        )
        .order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
        .limit(limit)
        .all()
    )


def latest_activities_for_leads(leads):
    lead_ids = [lead.id for lead in leads if lead]
    if not lead_ids:
        return {}

    activities = (
        LeadActivity.query.options(joinedload(LeadActivity.user))
        .filter(
            LeadActivity.lead_id.in_(lead_ids),
            LeadActivity.deleted_at.is_(None),
        )
        .order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
        .all()
    )
    latest = {}
    for activity in activities:
        latest.setdefault(activity.lead_id, activity)
    return latest


def workflow_history_for_lead(lead, lead_workflow_ids=None, limit=50):
    query = (
        WorkflowTransition.query.options(
            joinedload(WorkflowTransition.user),
            joinedload(WorkflowTransition.workflow),
            joinedload(WorkflowTransition.from_stage),
            joinedload(WorkflowTransition.to_stage),
            joinedload(WorkflowTransition.lead_workflow).joinedload(LeadWorkflow.lead),
            joinedload(WorkflowTransition.lead_workflow).joinedload(LeadWorkflow.owner_user),
            joinedload(WorkflowTransition.lead_workflow).joinedload(LeadWorkflow.assigned_user),
        )
        .filter(WorkflowTransition.lead_id == lead.id)
    )
    if lead_workflow_ids:
        query = query.filter(WorkflowTransition.lead_workflow_id.in_(lead_workflow_ids))
    return query.order_by(WorkflowTransition.created_at.desc(), WorkflowTransition.id.desc()).limit(limit).all()


def workflow_checklist_history_for_lead(lead, lead_workflow_ids=None, limit=120):
    query = (
        LeadWorkflowChecklistCheck.query.options(
            joinedload(LeadWorkflowChecklistCheck.checked_by),
            joinedload(LeadWorkflowChecklistCheck.checklist_item).joinedload(WorkflowChecklistItem.stage),
            joinedload(LeadWorkflowChecklistCheck.lead_workflow).joinedload(LeadWorkflow.workflow),
        )
        .join(LeadWorkflow, LeadWorkflowChecklistCheck.lead_workflow_id == LeadWorkflow.id)
        .filter(
            LeadWorkflow.lead_id == lead.id,
            LeadWorkflowChecklistCheck.checked.is_(True),
            LeadWorkflowChecklistCheck.checked_at.isnot(None),
        )
    )
    if lead_workflow_ids:
        query = query.filter(LeadWorkflowChecklistCheck.lead_workflow_id.in_(lead_workflow_ids))
    return query.order_by(
        LeadWorkflowChecklistCheck.checked_at.desc(),
        LeadWorkflowChecklistCheck.id.desc(),
    ).limit(limit).all()


def lead_operational_history(lead, lead_workflow_ids=None, limit=120):
    activities = lead_activity_timeline(lead, limit=limit)
    transitions = workflow_history_for_lead(lead, lead_workflow_ids=lead_workflow_ids, limit=limit)
    checklist_checks = workflow_checklist_history_for_lead(lead, lead_workflow_ids=lead_workflow_ids, limit=limit)
    activity_notes = []
    entries = []
    for activity in activities:
        activity_notes.append(
            {
                "body": clean_text(activity.body),
                "created_at": activity.created_at,
                "lead_workflow_id": activity.lead_workflow_id,
                "user_id": activity.user_id,
            }
        )
    for activity in activities:
        if activity.source in {"workflow_move_note", "support_complete", "support_return"}:
            continue
        if activity_note_matches_transition(activity, transitions):
            continue
        entries.append(
            {
                "kind": "activity",
                "created_at": activity.created_at,
                "title": "Atuacao registrada",
                "subtitle": "Atuacao",
                "badge": "Campo antigo" if activity.source == "legacy_field" else "Atuacao",
                "workflow": activity.workflow.name if activity.workflow else None,
                "stage": activity.stage.name if activity.stage else None,
                "user": activity.user.name if activity.user else "Sistema",
                "body": activity.body,
            }
        )
    for event in transitions:
        from_stage = event.from_stage.name if event.from_stage else "Inicio"
        to_stage = event.to_stage.name if event.to_stage else "-"
        if workflow_transition_is_redundant_same_stage(event, from_stage, to_stage):
            continue
        event_note = clean_text(event.note)
        transition_title, transition_badge = workflow_history_title_and_badge(event)
        entries.append(
            {
                "kind": "stage",
                "created_at": event.created_at,
                "title": transition_title,
                "subtitle": workflow_history_subtitle(event, from_stage, to_stage),
                "badge": transition_badge,
                "workflow": event.workflow.name if event.workflow else None,
                "stage": event.to_stage.name if event.to_stage else None,
                "user": event.user.name if event.user else "Sistema",
                "body": workflow_history_body(event, event_note),
            }
        )
    for check in checklist_checks:
        if checklist_check_matches_support_transition(check, transitions):
            continue
        item = check.checklist_item
        stage = item.stage if item else None
        lead_workflow = check.lead_workflow
        entries.append(
            {
                "kind": "checklist",
                "created_at": check.checked_at,
                "title": "Tarefa concluida",
                "subtitle": task_snapshot(check).get("label") or (item.label if item else "Tarefa da etapa"),
                "badge": "Tarefa",
                "workflow": lead_workflow.workflow.name if lead_workflow and lead_workflow.workflow else None,
                "stage": stage.name if stage else None,
                "user": check.checked_by.name if check.checked_by else "Sistema",
                "body": clean_text(check.note),
            }
        )
    sorted_entries = sorted(entries, key=lambda item: item["created_at"] or datetime.min, reverse=True)[:limit]
    for entry in sorted_entries:
        entry["context_label"] = workflow_history_context_label(entry)
    return sorted_entries


def workflow_history_title_and_badge(event):
    if not event:
        return ("Movimento registrado", "Fluxo")
    if event.action == "stage_seed":
        return ("Entrada no fluxo", "Fluxo")
    if event.action == "return_from_temporary":
        return ("Retorno ao fluxo principal", "Fluxo")
    if event.action == "complete_temporary":
        return ("Fluxo temporario concluido", "Fluxo")
    if event.action == "move":
        return ("Mudanca de etapa", "Fluxo")
    if event.action == "support_complete":
        return ("Retorno apoio: concluido", "Apoio")
    if event.action == "support_return":
        return ("Retorno apoio: devolvido", "Apoio")
    if event.action == "support_assign":
        return ("Apoio atribuido", "Apoio")
    if event.action == "support_return_resolution":
        return ("Tarefa concluida", "Tarefa")
    if event.action == "seller_execution_complete":
        return ("Tarefa concluida", "Tarefa")
    if event.action == "scheduled_task_complete":
        return ("Tarefa concluida", "Tarefa")
    if event.action == "scheduled_task_create":
        return ("Tarefa adicionada", "Tarefa")
    if event.action == "fidelity_migration":
        return ("Unificacao da Fidelizacao", "Fluxo")
    if event.action in {"support_executor_update", "unlock_executor_update"}:
        return ("Responsavel definido", "Apoio")
    return (workflow_transition_label(event.action), "Fluxo")


def workflow_history_subtitle(event, from_stage, to_stage):
    if event and event.action == "scheduled_task_create":
        return None
    if event and event.action in {"move", "stage_seed", "return_from_temporary", "complete_temporary"}:
        return f"De etapa {from_stage} para etapa {to_stage}"
    if event and event.action in {"support_complete", "support_return", "support_assign", "support_return_resolution", "seller_execution_complete", "scheduled_task_complete", "support_executor_update", "unlock_executor_update"}:
        return None
    return f"De etapa {from_stage} para etapa {to_stage}"


def workflow_history_context_label(entry):
    workflow = clean_text(entry.get("workflow")) or "Fluxo"
    stage = clean_text(entry.get("stage"))
    return f"{workflow} / {stage}" if stage else workflow


def workflow_transition_is_redundant_same_stage(event, from_stage, to_stage):
    if not event or event.action not in {"move", "auto_move", "return", "return_from_temporary", "complete_temporary"}:
        return False
    return bool(from_stage and to_stage and from_stage == to_stage)


def support_history_action_name(event):
    if not event:
        return "esta etapa"
    details = workflow_transition_details(event)
    stage_name = clean_text(details.get("stage")) or clean_text(getattr(event.to_stage, "name", None))
    stage_key = normalize_option_key(stage_name)
    if event.action == "unlock_executor_update" or stage_key in {"dia do desbloqueio", "30 dias desbloqueio"}:
        return "desbloqueio"
    return stage_name or "esta etapa"


def support_history_assignment_phrase(action_name, support_name):
    action_key = normalize_option_key(action_name)
    if action_key in {"anuencia"}:
        return f"{action_name} atribuida para {support_name}."
    if action_key in {"pos venda", "pos-venda"}:
        return f"{action_name} atribuido para {support_name}."
    if action_key == "desbloqueio":
        return f"Desbloqueio atribuido para {support_name}."
    return f"{action_name} atribuido para {support_name}."


def workflow_history_body(event, fallback_note=None):
    if not event:
        return fallback_note
    if event.action in {"support_complete", "support_return"}:
        actor_name = clean_text(getattr(getattr(event, "user", None), "name", None)) or "Sistema"
        action_name = support_history_action_name(event)
        verb = "concluiu" if event.action == "support_complete" else "devolveu"
        note = clean_text(fallback_note)
        body = f"{actor_name} {verb} {action_name}"
        return f"{body}\n{note}" if note else body

    if event.action == "support_return_resolution":
        action_name = support_history_action_name(event)
        note = clean_text(fallback_note)
        title = f"Resolver devolucao do apoio - {action_name}"
        return f"{title}\n{note}" if note else title

    if event.action in {"seller_execution_complete", "scheduled_task_complete"}:
        details = workflow_transition_details(event)
        task_label = clean_text(details.get("task_label")) or "Tarefa concluida"
        note = clean_text(fallback_note)
        return f"{task_label}\n{note}" if note else task_label

    if event.action == "support_assign":
        details = workflow_transition_details(event)
        lead_workflow = event.lead_workflow
        support_name = (
            clean_text(getattr(getattr(lead_workflow, "assigned_user", None), "name", None))
            or clean_text(details.get("support_user"))
            or "Apoio"
        )
        action_name = support_history_action_name(event)
        return support_history_assignment_phrase(action_name, support_name)

    if event.action not in {"support_executor_update", "unlock_executor_update"}:
        return fallback_note

    details = workflow_transition_details(event)
    executor = clean_text(details.get("new_value"))
    lead_workflow = event.lead_workflow
    lead = event.lead or (lead_workflow.lead if lead_workflow else None)
    action_name = support_history_action_name(event)

    responsible_name = ""
    if executor in {SUPPORT_EXECUTOR_SELLER, UNLOCK_EXECUTOR_SELLER}:
        responsible_name = (
            clean_text(getattr(lead, "consultor", None))
            or clean_text(getattr(getattr(lead_workflow, "owner_user", None), "name", None))
        )
    elif executor in {SUPPORT_EXECUTOR_SUPPORT, UNLOCK_EXECUTOR_SUPPORT}:
        responsible_name = clean_text(getattr(getattr(lead_workflow, "assigned_user", None), "name", None))

    if responsible_name:
        return f"{responsible_name} fara {action_name}"
    return fallback_note


def activity_note_matches_transition(activity, transitions):
    note = clean_text(activity.body)
    if not note:
        return False
    activity_payload = {
        "body": note,
        "created_at": activity.created_at,
        "lead_workflow_id": activity.lead_workflow_id,
        "user_id": activity.user_id,
    }
    for event in transitions:
        if workflow_transition_note_matches_single_activity(event, note, activity_payload):
            return True
    return False


def workflow_transition_note_matches_activity(event, note, activity_notes):
    if not note or event.action not in {"move", "complete_temporary", "return_from_temporary", "support_complete", "support_return"}:
        return False
    for activity in activity_notes:
        if workflow_transition_note_matches_single_activity(event, note, activity):
            return True
    return False


def workflow_transition_note_matches_single_activity(event, note, activity):
    if not event or not note or event.action not in {"move", "complete_temporary", "return_from_temporary", "support_complete", "support_return"}:
        return False
    if activity["body"] != note:
        return False
    if activity["user_id"] != event.user_id:
        return False
    if (
        event.action != "return_from_temporary"
        and activity["lead_workflow_id"]
        and event.lead_workflow_id
        and activity["lead_workflow_id"] != event.lead_workflow_id
    ):
        return False
    if activity["created_at"] and event.created_at:
        seconds = abs((activity["created_at"] - event.created_at).total_seconds())
        if seconds > 5:
            return False
    return True


def checklist_check_matches_support_transition(check, transitions):
    if not check or not check.checked_at:
        return False
    note = clean_text(check.note)
    if note == "Marcado automaticamente pela conclusao do apoio.":
        return True
    stage_id = check.checklist_item.stage_id if check.checklist_item else None
    for event in transitions:
        if event.action != "support_complete":
            continue
        if event.lead_workflow_id != check.lead_workflow_id:
            continue
        if stage_id and event.to_stage_id != stage_id:
            continue
        if check.checked_by_id and event.user_id != check.checked_by_id:
            continue
        if abs((check.checked_at - event.created_at).total_seconds()) <= 5:
            return True
    return False


def reversal_history_for_lead(lead, limit=80):
    tasks = (
        ReversalTask.query.options(
            joinedload(ReversalTask.assigned_user),
            joinedload(ReversalTask.assigned_by),
            joinedload(ReversalTask.requested_by),
            joinedload(ReversalTask.owner_user),
            joinedload(ReversalTask.canceled_by),
        )
        .filter(ReversalTask.lead_id == lead.id)
        .order_by(ReversalTask.created_at.desc(), ReversalTask.id.desc())
        .limit(limit)
        .all()
    )
    task_ids = [task.id for task in tasks]
    attempts_by_task = {task_id: [] for task_id in task_ids}
    if task_ids:
        attempts = (
            ReversalAttempt.query.options(joinedload(ReversalAttempt.user))
            .filter(ReversalAttempt.reversal_task_id.in_(task_ids))
            .order_by(ReversalAttempt.created_at.desc(), ReversalAttempt.id.desc())
            .all()
        )
        for attempt in attempts:
            attempts_by_task.setdefault(attempt.reversal_task_id, []).append(attempt)

    entries = []
    for task in tasks:
        entries.append(
            {
                "created_at": task.queued_at or task.created_at,
                "title": "Reversao solicitada",
                "badge": reversal_priority_label(task.priority_level),
                "subtitle": f"{reversal_tabulation_label(task.tabulation)} - {reversal_distribution_mode_label(task.distribution_mode)}",
                "user": task.requested_by.name if task.requested_by else "Sistema",
                "body": None,
            }
        )
        if task.assigned_at:
            entries.append(
                {
                    "created_at": task.assigned_at,
                    "title": "Reversao atribuida",
                    "badge": reversal_status_label(task.status),
                    "subtitle": f"Reversor: {task.assigned_user.name if task.assigned_user else 'Sem reversor'}",
                    "user": task.assigned_by.name if task.assigned_by else "Sistema",
                    "body": f"SLA: {task.sla_hours or 0}h" if task.sla_hours else None,
                }
            )
        if task.status == ReversalStatus.OVERDUE:
            deadline = reversal_task_deadline(task)
            entries.append(
                {
                    "created_at": task.due_at or task.updated_at,
                    "title": "Reversao em atraso",
                    "badge": "Atraso",
                    "subtitle": f"Venceu ha {deadline['overdue_hours']}h" if deadline else "SLA vencido",
                    "user": "Sistema",
                    "body": f"SLA configurado: {task.sla_hours or 0}h" if task.sla_hours else None,
                }
            )
        if task.canceled_at:
            entries.append(
                {
                    "created_at": task.canceled_at,
                    "title": "Reversao cancelada",
                    "badge": reversal_status_label(task.status),
                    "subtitle": f"Por {task.canceled_by.name if task.canceled_by else 'Sistema'}",
                    "user": task.canceled_by.name if task.canceled_by else "Sistema",
                    "body": task.cancel_reason,
                }
            )
        for attempt in attempts_by_task.get(task.id, []):
            entries.append(
                {
                    "created_at": attempt.created_at,
                    "title": "Reversao devolvida:",
                    "title_detail": reversal_result_label(attempt.result),
                    "badge": reversal_result_sentiment_label(attempt.result, task.tabulation),
                    "subtitle": f"Motivo da reversao: {reversal_tabulation_label(task.tabulation)}",
                    "user": attempt.user.name if attempt.user else "Sistema",
                    "body": f"Retorno do reversor: {attempt.note}" if attempt.note else None,
                }
            )
    return sorted(entries, key=lambda item: item["created_at"] or datetime.min, reverse=True)[:limit]


def workflow_transition_label(action):
    labels = {
        "stage_seed": "Entrada no fluxo",
        "move": "Movimento de etapa",
        "auto_move": "Movimento automatico",
        "return": "Retorno de etapa",
        "complete_temporary": "Fluxo temporario concluido",
        "return_from_temporary": "Retorno ao fluxo principal",
        "reopen_temporary": "Fluxo temporario reaberto",
        "support_assign": "Apoio atribuido",
        "support_complete": "Apoio concluido",
        "support_return": "Apoio devolvido",
        "support_return_resolution": "Devolucao do apoio resolvida",
        "seller_execution_complete": "Tarefa do vendedor concluida",
        "scheduled_task_complete": "Tarefa programada concluida",
        "support_executor_update": "Responsavel pela etapa",
        "unlock_executor_update": "Responsavel pelo desbloqueio",
    }
    return labels.get(action, action or "-")


def lead_workflow_from_activity_form(lead, form):
    all_lead_workflows = lead_workflows_for_detail(lead)
    operational_lead_workflows, _ = operational_lead_workflows_for_detail(lead, all_lead_workflows)
    allowed_ids = {lead_workflow.id for lead_workflow in operational_lead_workflows}

    lead_workflow_id = parse_int(form.get("lead_workflow_id"), 0)
    if lead_workflow_id:
        lead_workflow = db.session.get(LeadWorkflow, lead_workflow_id)
        if not lead_workflow or lead_workflow.lead_id != lead.id or lead_workflow.id not in allowed_ids:
            raise ValueError("Fluxo inválido para este cliente.")
        return lead_workflow

    lead_workflow = operational_lead_workflows[0] if operational_lead_workflows else primary_lead_workflow_for_lead(lead)
    if not lead_workflow:
        raise ValueError("Nenhum fluxo ativo encontrado para registrar a atuação.")
    return lead_workflow


def activity_details(lead_workflow):
    if not lead_workflow:
        return {"source": "lead_detail"}
    return {
        "source": "lead_detail",
        "lead_workflow_id": lead_workflow.id,
        "workflow_id": lead_workflow.workflow_id,
        "workflow": lead_workflow.workflow.name if lead_workflow.workflow else None,
        "stage_id": lead_workflow.current_stage_id,
        "stage": lead_workflow.current_stage.name if lead_workflow.current_stage else None,
    }


def sync_legacy_activity_from_change(lead, field_name, user):
    lead_workflow = current_operational_lead_workflow_for_lead(lead)
    body = clean_text(getattr(lead, field_name, None))
    existing = LeadActivity.query.filter_by(
        lead_id=lead.id,
        source="legacy_field",
        source_field=field_name,
    ).first()
    now = datetime.now()

    if not body:
        if existing and not existing.deleted_at:
            existing.deleted_at = now
        if lead_workflow:
            lead_workflow.last_action_at = now
        return existing

    if existing:
        existing.body = body
        existing.user_id = user.id if user else existing.user_id
        existing.deleted_at = None
        existing.updated_at = now
        if lead_workflow:
            existing.lead_workflow_id = lead_workflow.id
            existing.workflow_id = lead_workflow.workflow_id
            existing.stage_id = lead_workflow.current_stage_id
    else:
        existing = LeadActivity(
            lead_id=lead.id,
            user_id=user.id if user else None,
            lead_workflow_id=lead_workflow.id if lead_workflow else None,
            workflow_id=lead_workflow.workflow_id if lead_workflow else None,
            stage_id=lead_workflow.current_stage_id if lead_workflow else None,
            body=body,
            source="legacy_field",
            source_field=field_name,
            created_at=now,
            updated_at=now,
        )
        db.session.add(existing)

    if lead_workflow:
        lead_workflow.last_action_at = now
    return existing


def get_visible_teams(user, state="active"):
    teams_query = visible_leads_query(user, state=state).with_entities(Lead.equipe).distinct()
    return sorted(row[0] for row in teams_query.all() if row[0])


def get_visible_consultores(user, equipe=None, state="active"):
    query = visible_leads_query(user, state=state)
    equipes = filter_values(equipe)
    if equipes:
        query = query.filter(Lead.equipe.in_(equipes))
    consultores_query = query.with_entities(Lead.consultor).distinct()
    return sorted(row[0] for row in consultores_query.all() if row[0])


def get_consultores_for_create(user, equipe=None, franchise_id=None):
    if user.can_view_all:
        if not equipe:
            return []
        return get_active_operator_names(equipe, user=user, franchise_id=franchise_id or franchise_id_for_user(user))
    if user.role == Role.SUPERVISOR:
        return get_active_operator_names(user.team, user=user, franchise_id=franchise_id or franchise_id_for_user(user))
    return [lead_assignment_name(user)]


def safe_next_url(value):
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def form_next_url(default_endpoint, **values):
    return safe_next_url(request.form.get("next")) or url_for(default_endpoint, **values)


def public_url_for(app, endpoint, **values):
    public_base_url = app.config.get("PUBLIC_BASE_URL")
    if public_base_url:
        return f"{public_base_url}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def can_view_lead(user, lead):
    if lead.deleted_at:
        return False
    return can_access_lead_scope(user, lead)


def can_access_lead_scope(user, lead):
    if not in_same_company_scope(user, lead):
        return False
    if user.can_view_all:
        return True
    if user.role == Role.SUPERVISOR:
        return lead.equipe == user.team
    if getattr(user, "can_act_as_support", False) and support_assignment_exists_for_user(user, lead):
        return True
    if getattr(user, "can_work_reversal", False) and reversal_assignment_exists_for_user(user, lead):
        return True
    return lead.consultor.lower() == lead_assignment_name(user).lower()


def can_transfer_lead(user, lead):
    if not getattr(user, "can_transfer_leads", False) or lead.deleted_at:
        return False
    if not in_same_company_scope(user, lead):
        return False
    if user.can_view_all:
        return True
    return user.role == Role.SUPERVISOR and lead.equipe == user.team


def get_transfer_team_options(user, lead):
    if not can_transfer_lead(user, lead):
        return []

    if user.can_view_all:
        team_names = get_active_team_names(user=user, franchise_id=lead.franchise_id)
    else:
        team_names = [user.team] if user.team else []

    options = []
    seen = set()
    for team_name in team_names:
        if not team_name or team_name in seen:
            continue
        options.append({"value": team_name, "label": team_name, "active": True})
        seen.add(team_name)

    if lead.equipe and lead.equipe not in seen:
        options.insert(0, {"value": lead.equipe, "label": lead.equipe, "active": False})

    return options


def get_transfer_consultor_options(user, lead, equipe=None):
    if not can_transfer_lead(user, lead):
        return []

    selected_team = clean_text(equipe)
    query = scoped_company_query(
        User.query.filter(
            User.active.is_(True),
            User.role == Role.OPERADOR,
        ),
        User,
        user,
        include_unscoped=True,
    )
    if lead.franchise_id:
        query = query.filter(User.franchise_id == lead.franchise_id)
    if selected_team:
        query = query.filter(User.team == selected_team)
    elif not user.can_view_all:
        query = query.filter(User.team == user.team)

    options = []
    seen = set()
    for operator in query.order_by(User.team.asc(), User.name.asc()).all():
        value = lead_assignment_name(operator)
        team = operator.team
        key = (team, value)
        if not value or key in seen:
            continue
        options.append(
            {
                "value": value,
                "label": value,
                "team": team,
                "active": True,
            }
        )
        seen.add(key)

    current_key = (lead.equipe, lead.consultor)
    if lead.consultor and (not selected_team or selected_team == lead.equipe) and current_key not in seen:
        options.insert(
            0,
            {
                "value": lead.consultor,
                "label": lead.consultor,
                "team": lead.equipe,
                "active": False,
            },
        )

    return options


def get_transfer_consultores_by_team(user, lead):
    grouped = {option["value"]: [] for option in get_transfer_team_options(user, lead)}
    for option in get_transfer_consultor_options(user, lead):
        team = option.get("team") or ""
        grouped.setdefault(team, []).append(option)
    return grouped


def transfer_lead_consultor(lead, form, user):
    if "consultor" not in form:
        return None

    new_consultor = clean_text(form.get("consultor"))
    new_team = clean_text(form.get("equipe")) or lead.equipe
    if not new_consultor:
        raise ValueError("Informe o consultor responsavel pelo cliente.")
    if not can_transfer_lead(user, lead):
        raise ValueError("Seu usuário não tem permissão para transferir este cliente.")

    if new_consultor == lead.consultor and new_team == lead.equipe:
        return None

    allowed_teams = {option["value"] for option in get_transfer_team_options(user, lead) if option["active"]}
    if new_team not in allowed_teams:
        raise ValueError("Essa equipe nao esta disponivel para receber este cliente.")

    options = get_transfer_consultor_options(user, lead, equipe=new_team)
    allowed_options = {
        (option.get("team"), option["value"]): option
        for option in options
        if option["active"]
    }
    target_option = allowed_options.get((new_team, new_consultor))
    if not target_option:
        raise ValueError("Esse operador não está disponível para receber este cliente.")

    old_consultor = lead.consultor
    old_team = lead.equipe
    new_team = target_option.get("team") or new_team
    lead.consultor = new_consultor
    team_change = None
    if new_team and new_team != lead.equipe:
        lead.equipe = new_team
        team_change = {
            "old_value": old_team,
            "new_value": new_team,
        }

    return {
        "old_value": old_consultor,
        "new_value": new_consultor,
        "team_change": team_change,
        "details": {
            "from": old_consultor,
            "to": new_consultor,
            "from_team": old_team,
            "to_team": lead.equipe,
            "source": "lead_detail",
        },
    }


def editable_fields_for(user):
    fields = set()
    if user.can_edit_core:
        fields.update({"nome_cliente", "data_venda", "concessao"})
    if can_edit_protected_lead_cpf(user):
        fields.add("cpf")
    if user.can_edit_contact:
        fields.add("contato")
    return fields


def can_edit_protected_lead_cpf(user):
    return bool(
        user
        and user.role == Role.ADMIN
        and user.can_edit_core
        and user_can_view_all_franchises(user)
    )


def add_field_change(changes, field_name, old_value, new_value):
    old_serialized = serialize_audit_value(old_value)
    new_serialized = serialize_audit_value(new_value)
    if old_serialized == new_serialized:
        return False

    changes.append(
        {
            "field_name": field_name,
            "old_value": old_serialized,
            "new_value": new_serialized,
        }
    )
    return True


def serialize_audit_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def update_allowed_fields(lead, form, allowed_fields):
    changes = []

    if "equipe" in allowed_fields and "equipe" in form:
        value = clean_text(form.get("equipe")) or lead.equipe
        if add_field_change(changes, "equipe", lead.equipe, value):
            lead.equipe = value
    if "consultor" in allowed_fields and "consultor" in form:
        value = clean_text(form.get("consultor")) or lead.consultor
        if add_field_change(changes, "consultor", lead.consultor, value):
            lead.consultor = value
    if "nome_cliente" in allowed_fields and "nome_cliente" in form:
        value = clean_text(form.get("nome_cliente")) or lead.nome_cliente
        if add_field_change(changes, "nome_cliente", lead.nome_cliente, value):
            lead.nome_cliente = value
    if "cpf" in allowed_fields and "cpf" in form:
        cpf = normalize_cpf(form.get("cpf"))
        if not cpf:
            raise ValueError("Informe um CPF válido.")
        company_id = lead.company_id or company_id_for_user(request_user_or_none())
        franchise_id = lead.franchise_id or franchise_id_for_user(request_user_or_none())
        if cpf != lead.cpf and lead_exists_in_contract_scope(
            cpf,
            company_id,
            franchise_id,
            current_id=lead.id,
        ):
            raise ValueError("Já existe outro cliente com esse CPF nesta franquia.")
        if add_field_change(changes, "cpf", lead.cpf, cpf):
            lead.cpf = cpf
            lead.customer = get_or_create_customer_for_contract(
                company_id,
                cpf,
                nome=lead.nome_cliente,
                contato=lead.contato,
            )
    if "data_venda" in allowed_fields and "data_venda" in form:
        value = parse_date(form.get("data_venda"))
        if add_field_change(changes, "data_venda", lead.data_venda, value):
            lead.data_venda = value
    if "concessao" in allowed_fields and "concessao" in form:
        value = parse_date(form.get("concessao"))
        if add_field_change(changes, "concessao", lead.concessao, value):
            lead.concessao = value
    if "contato" in allowed_fields and "contato" in form:
        value = normalize_numeric_text(form.get("contato"))
        if add_field_change(changes, "contato", lead.contato, value):
            lead.contato = value
        lead.contato_manual = True
    if "status" in allowed_fields and "status" in form:
        value = selected_option("status", form.get("status"))
        if add_field_change(changes, "status", lead.status, value):
            lead.status = value
        lead.status_manual = bool(lead.status)
    for field in ATUACAO_FIELDS:
        if field in allowed_fields and field in form:
            value = clean_text(form.get(field))
            if add_field_change(changes, field, getattr(lead, field), value):
                setattr(lead, field, value)
            setattr(lead, f"{field}_manual", bool(value))

    return changes


def log_audit(
    lead,
    user,
    action,
    field_name=None,
    old_value=None,
    new_value=None,
    details=None,
    import_batch=None,
):
    if lead is None and user and action in {
        "workflow_config_create", "workflow_config_update", "workflow_config_delete",
        "workflow_stage_create", "workflow_stage_update", "workflow_stage_delete",
        "workflow_checklist_create", "workflow_checklist_update", "workflow_checklist_delete",
    }:
        record_current_task_configuration(user)
    company_id = lead.company_id if lead and lead.company_id else None
    franchise_id = lead.franchise_id if lead and lead.franchise_id else None
    if import_batch:
        company_id = company_id or import_batch.company_id
        franchise_id = franchise_id or import_batch.franchise_id
    if user:
        company_id = company_id or user.company_id
        franchise_id = franchise_id or user.franchise_id
    audit = AuditLog(
        company_id=company_id,
        franchise_id=franchise_id,
        lead_id=lead.id if lead else None,
        user_id=user.id if user else None,
        action=action,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        lead_cpf=lead.cpf if lead else None,
        lead_name=lead.nome_cliente if lead else None,
        lead_equipe=lead.equipe if lead else None,
        lead_consultor=lead.consultor if lead else None,
        details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
        created_at=datetime.now(),
    )
    if import_batch:
        audit.import_batch = import_batch
    db.session.add(audit)
    return audit


def latest_field_audits(lead):
    audits = (
        AuditLog.query.filter(
            AuditLog.lead_id == lead.id,
            AuditLog.action.in_(("update", "lead_transfer")),
            AuditLog.field_name.isnot(None),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )
    latest = {}
    for audit in audits:
        latest.setdefault(audit.field_name, audit)
    return latest


def recent_lead_audit_events(lead, limit=25):
    return (
        AuditLog.query.filter(AuditLog.lead_id == lead.id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


def latest_activity_audits_for_leads(leads):
    lead_ids = [lead.id for lead in leads]
    if not lead_ids:
        return {}

    audits = (
        AuditLog.query.filter(
            AuditLog.lead_id.in_(lead_ids),
            or_(
                AuditLog.action.in_(OPERATIONAL_AUDIT_ACTIONS),
                (
                    (AuditLog.action == "update")
                    & AuditLog.field_name.in_(LAST_ACTIVITY_FIELDS)
                ),
            ),
        )
        .options(joinedload(AuditLog.user))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )
    latest = {}
    for audit in audits:
        latest.setdefault(audit.lead_id, audit)
    return latest


def activity_alerts_for_leads(leads, latest_activity_audits):
    now = datetime.now()
    alerts = {}
    for lead in leads:
        audit = latest_activity_audits.get(lead.id)
        reference_at = audit.created_at if audit else lead.created_at
        if not reference_at:
            reference_at = now

        elapsed = now - reference_at
        alerts[lead.id] = {
            "is_stale": False,
            "days": max(elapsed.days, 0),
            "reference_at": reference_at,
            "audit": audit,
        }
    return alerts


def audit_action_label(action):
    labels = {
        "create": "Cliente criado",
        "delete": "Cliente excluido",
        "import": "Importacao concluida",
        "import_error": "Erro na importacao",
        "import_create": "Criado por importacao",
        "lead_bulk_deactivate": "Cliente inativado",
        "lead_bulk_reactivate": "Cliente reativado",
        "lead_bulk_export": "Exportacao em massa",
        "lead_transfer": "Cliente transferido",
        "lead_maintenance_import": "Manutencao importada",
        "lead_maintenance_import_error": "Erro na manutencao",
        "activity_create": "Atuacao registrada",
        "workflow_checklist_update": "Tarefas da etapa atualizadas",
        "workflow_move": "Etapa avancada",
        "workflow_auto_move": "Etapa automatica",
        "unlock_executor_update": "Responsavel pelo desbloqueio",
        "support_executor_update": "Responsavel pela etapa",
        "support_assign": "Apoio atribuido",
        "support_assign_missing": "Apoio sem responsavel",
        "support_complete": "Apoio concluido",
        "support_return": "Apoio devolvido",
        "support_return_resolution": "Devolucao do apoio resolvida",
        "seller_execution_complete": "Tarefa do vendedor concluida",
        "scheduled_task_complete": "Tarefa programada concluida",
        "reversal_request": "Reversao solicitada",
        "reversal_assign": "Reversao atribuida",
        "reversal_overdue": "Reversao em atraso",
        "reversal_complete": "Reversao devolvida",
        "reversal_return": "Reversao devolvida",
        "reversal_batch_distribute": "Sorteio de reversao",
        "reversal_cancel": "Reversao cancelada",
        "status_undo": "Status desfeito",
        "update": "Campo editado",
        "user_create": "Usuario criado",
        "user_login": "Login realizado",
        "user_password_change": "Senha alterada",
        "user_update": "Usuario atualizado",
        "user_reactivate": "Usuario reativado",
        "user_deactivate": "Usuario desativado",
        "user_delete": "Usuario excluido",
        "google_access_request": "Solicitacao Google",
        "google_admin_auto_create": "Admin Google criado",
        "google_access_approve": "Acesso Google aprovado",
        "google_access_reject": "Acesso Google recusado",
        "config_option_create": "Opcao criada",
        "config_option_update": "Opcao atualizada",
        "config_option_deactivate": "Opcao desativada",
        "config_option_delete": "Opcao excluida",
        "team_create": "Equipe criada",
        "team_update": "Equipe atualizada",
        "team_deactivate": "Equipe desativada",
        "team_delete": "Equipe excluida",
        "franchise_create": "Franquia criada",
        "franchise_update": "Franquia atualizada",
        "franchise_deactivate": "Franquia desativada",
        "franchise_delete": "Franquia excluida",
        "role_profile_create": "Cargo criado",
        "role_profile_update": "Cargo atualizado",
        "role_profile_deactivate": "Cargo desativado",
        "role_profile_delete": "Cargo excluido",
        "workflow_config_create": "Fluxo criado",
        "workflow_config_update": "Fluxo atualizado",
        "workflow_config_delete": "Fluxo excluido",
        "workflow_stage_create": "Etapa criada",
        "workflow_stage_update": "Etapa atualizada",
        "workflow_stage_delete": "Etapa excluida",
        "workflow_checklist_create": "Tarefa criada",
        "workflow_checklist_update": "Tarefa atualizada",
        "workflow_checklist_delete": "Tarefa excluida",
    }
    return labels.get(action, action)


def audit_field_label(field_name):
    labels = {
        "equipe": "Equipe",
        "consultor": "Consultor",
        "nome_cliente": "Nome cliente",
        "cpf": "CPF",
        "data_venda": "Data venda",
        "concessao": "Concessao",
        "contato": "Contato",
        "status": "Status",
        "posicao": "Campo legado de posição",
        "timeline": "Timeline",
        "support": "Apoio comercial",
        "workflow_stage": "Etapa do fluxo",
        "unlock_executor": "Responsavel pelo desbloqueio",
        "support_executor": "Responsavel pela etapa",
    }
    if field_name in ATUACAO_FIELDS:
        return f"Atuacao {ATUACAO_FIELDS.index(field_name) + 1}"
    return labels.get(field_name, field_name or "-")


def parse_selected_lead_ids(values):
    lead_ids = []
    seen = set()
    for value in values:
        lead_id = parse_int(value, 0)
        if lead_id and lead_id not in seen:
            lead_ids.append(lead_id)
            seen.add(lead_id)
    return lead_ids


def selected_leads_for_bulk(lead_ids, user):
    if not lead_ids:
        return []

    lead_by_id = {
        lead.id: lead
        for lead in Lead.query.filter(Lead.id.in_(lead_ids)).all()
    }
    leads = []
    for lead_id in lead_ids:
        lead = lead_by_id.get(lead_id)
        if lead and can_access_lead_scope(user, lead):
            leads.append(lead)
    return leads


def bulk_action_message(result):
    return (
        f"Acao em massa concluida: {result.get('changed', 0)} alterados, "
        f"{result.get('unchanged', 0)} sem mudanca, {result.get('skipped', 0)} ignorados."
    )


def apply_bulk_status_update(lead_ids, status_value, user):
    if not getattr(user, "can_bulk_edit_status", False):
        raise ValueError("Seu usuario nao tem permissao para alterar status em massa.")

    target_status = selected_option("status", status_value)
    if not target_status:
        raise ValueError("Escolha o status que sera aplicado.")

    leads = selected_leads_for_bulk(lead_ids, user)
    result = {"changed": 0, "unchanged": 0, "skipped": len(lead_ids) - len(leads)}
    target_key = normalize_option_key(target_status)
    terminal_action = None
    if target_key == "concluido":
        terminal_action = "concluido"
    elif target_key in {"recusado", "perdido", "perda"}:
        terminal_action = "recusado"

    for lead in leads:
        if lead.deleted_at:
            result["skipped"] += 1
            continue

        try:
            if terminal_action:
                if terminal_action == "concluido" and not can_user_conclude_lead_contract(user, lead):
                    result["skipped"] += 1
                    continue
                if terminal_action == "recusado" and not getattr(user, "can_reject_contract", False):
                    result["skipped"] += 1
                    continue
                changed = set_lead_contract_status(
                    lead,
                    user,
                    terminal_action,
                    reason="Acao em massa" if terminal_action == "recusado" else None,
                )
                result["changed" if changed else "unchanged"] += 1
                continue

            old_value = serialize_audit_value(lead.status)
            new_value = serialize_audit_value(target_status)
            if old_value == new_value:
                result["unchanged"] += 1
                continue

            lead.status = target_status
            lead.status_manual = bool(lead.status)
            log_audit(
                lead,
                user,
                "update",
                field_name="status",
                old_value=old_value,
                new_value=new_value,
                details={"source": "bulk_action"},
            )
            result["changed"] += 1
        except ValueError:
            result["skipped"] += 1

    return result


def apply_bulk_transfer(lead_ids, form, user):
    if not getattr(user, "can_bulk_transfer_leads", False):
        raise ValueError("Seu usuario nao tem permissao para transferir clientes em massa.")

    target_team = clean_text(form.get("target_equipe"))
    target_consultor = clean_text(form.get("target_consultor"))
    if not target_team or not target_consultor:
        raise ValueError("Escolha a equipe e o consultor de destino.")

    leads = selected_leads_for_bulk(lead_ids, user)
    result = {"changed": 0, "unchanged": 0, "skipped": len(lead_ids) - len(leads)}
    transfer_form = {"equipe": target_team, "consultor": target_consultor}

    for lead in leads:
        try:
            transfer_change = transfer_lead_consultor(lead, transfer_form, user)
        except ValueError:
            result["skipped"] += 1
            continue

        if not transfer_change:
            result["unchanged"] += 1
            continue

        details = {**transfer_change["details"], "source": "bulk_action"}
        log_audit(
            lead,
            user,
            "lead_transfer",
            field_name="consultor",
            old_value=transfer_change["old_value"],
            new_value=transfer_change["new_value"],
            details=details,
        )
        team_change = transfer_change.get("team_change")
        if team_change:
            log_audit(
                lead,
                user,
                "lead_transfer",
                field_name="equipe",
                old_value=team_change["old_value"],
                new_value=team_change["new_value"],
                details=details,
            )
        result["changed"] += 1

    return result


def apply_bulk_delete(lead_ids, user):
    if not getattr(user, "can_bulk_delete_leads", False):
        raise ValueError("Seu usuario nao tem permissao para excluir clientes em massa.")

    leads = selected_leads_for_bulk(lead_ids, user)
    result = {"changed": 0, "unchanged": 0, "skipped": len(lead_ids) - len(leads)}
    now = datetime.now()

    for lead in leads:
        if lead.deleted_at:
            result["unchanged"] += 1
            continue

        lead.deleted_at = now
        lead.deleted_by_id = user.id
        lead.deleted_reason = "Exclusao em massa"
        log_audit(
            lead,
            user,
            "delete",
            details={"reason": lead.deleted_reason, "source": "bulk_action"},
        )
        result["changed"] += 1

    return result


def export_bulk_leads_csv(leads):
    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "ID",
            "Equipe",
            "Consultor",
            "Cliente",
            "CPF",
            "Data Venda",
            "Concessao",
            "Contato",
            "Status",
        ]
    )
    for lead in leads:
        writer.writerow(
            [
                lead.id,
                lead.equipe,
                lead.consultor,
                lead.nome_cliente,
                lead.cpf,
                lead.data_venda.strftime("%d/%m/%Y") if lead.data_venda else "",
                lead.concessao.strftime("%d/%m/%Y") if lead.concessao else "",
                lead.contato or "",
                lead.status or "",
            ]
        )

    filename = f"clientes_selecionados_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def normalize_maintenance_action(value):
    normalized = normalize_option_key(value)
    if normalized in {"inactivate", "inativar", "inativo", "arquivar", "arquivado", "remover", "excluir"}:
        return "inactivate"
    if normalized in {"reactivate", "reativar", "ativar", "ativo", "restaurar"}:
        return "reactivate"
    return ""


def read_maintenance_dataframe(file):
    sheets = pd.read_excel(file, sheet_name=None)
    best_df = None
    best_score = -1

    for df in sheets.values():
        df = canonicalize_columns(df)
        normalized_columns = {normalize_header(column) for column in df.columns}
        score = sum(1 for column in ("cpf", "acao", "motivo") if column in normalized_columns)
        if {"cpf", "acao"}.issubset(normalized_columns) and score > best_score:
            best_df = df
            best_score = score

    if best_df is None:
        raise ValueError("Nenhuma aba com CPF e Ação foi encontrada.")

    return best_df


def maintenance_actions_from_dataframe(df):
    actions = []
    for _, row in df.iterrows():
        actions.append(
            {
                "cpf": normalize_cpf(row_value(row, "cpf")),
                "lead_id": None,
                "action": normalize_maintenance_action(row_value(row, "acao")),
                "reason": clean_text(row_value(row, "motivo")),
            }
        )
    return actions


def apply_lead_maintenance_actions(actions, user, import_batch=None, source="selection"):
    result = {
        "changed": 0,
        "unchanged": 0,
        "skipped": 0,
        "missing": 0,
        "invalid": 0,
        "duplicates": 0,
    }
    seen = set()

    for item in actions:
        lead_id = item.get("lead_id")
        cpf = item.get("cpf")
        action = item.get("action")
        reason = clean_text(item.get("reason"))
        identifier = f"id:{lead_id}" if lead_id else f"cpf:{cpf}" if cpf else ""

        if not identifier or action not in {"inactivate", "reactivate"}:
            result["invalid"] += 1
            result["skipped"] += 1
            continue
        if not user_can_perform_bulk_action(user, action):
            result["skipped"] += 1
            continue
        if identifier in seen:
            result["duplicates"] += 1
            result["skipped"] += 1
            continue
        seen.add(identifier)

        try:
            lead = (
                db.session.get(Lead, lead_id)
                if lead_id
                else visible_lead_by_cpf_for_maintenance(user, cpf, action)
            )
        except ValueError:
            result["invalid"] += 1
            result["skipped"] += 1
            continue
        if not lead:
            result["missing"] += 1
            result["skipped"] += 1
            continue
        if not can_access_lead_scope(user, lead):
            result["skipped"] += 1
            continue

        if action == "inactivate":
            if lead.deleted_at:
                result["unchanged"] += 1
                continue
            lead.deleted_at = datetime.now()
            lead.deleted_by_id = user.id
            lead.deleted_reason = reason or "Inativação em massa"
            log_audit(
                lead,
                user,
                "lead_bulk_deactivate",
                import_batch=import_batch,
                details={"source": source, "reason": lead.deleted_reason},
            )
            result["changed"] += 1
            continue

        if not lead.deleted_at:
            result["unchanged"] += 1
            continue
        previous = {
            "deleted_at": serialize_audit_value(lead.deleted_at),
            "deleted_by_id": lead.deleted_by_id,
            "deleted_reason": lead.deleted_reason,
        }
        lead.deleted_at = None
        lead.deleted_by_id = None
        lead.deleted_reason = None
        log_audit(
            lead,
            user,
            "lead_bulk_reactivate",
            import_batch=import_batch,
            details={"source": source, "reason": reason, "previous": previous},
        )
        result["changed"] += 1

    return result


def read_crm_dataframe(file):
    sheets = pd.read_excel(file, sheet_name=None)
    best_df = None
    best_score = -1

    for df in sheets.values():
        df = canonicalize_columns(df)
        score = sum(1 for column in df.columns if column in all_known_columns())
        if {"Equipe", "Consultor", "Nome Cliente", "CPF"}.issubset(df.columns) and score > best_score:
            best_df = df
            best_score = score

    if best_df is None:
        raise ValueError("Nenhuma aba com Equipe, Consultor, Nome Cliente e CPF foi encontrada.")

    return best_df


def canonicalize_columns(df):
    rename_map = {}
    for column in df.columns:
        normalized = normalize_header(column)
        if normalized in CANONICAL_COLUMNS:
            rename_map[column] = CANONICAL_COLUMNS[normalized]
    return df.rename(columns=rename_map)


def normalize_header(value):
    text_value = "" if value is None else str(value)
    without_accents = unicodedata.normalize("NFKD", text_value)
    ascii_text = "".join(char for char in without_accents if not unicodedata.combining(char))
    normalized = re.sub(r"\s+", " ", ascii_text).strip().lower()
    atuacao_match = re.fullmatch(r"atuacao\s*0*([1-9]|1[0-2])", normalized)
    if atuacao_match:
        return f"atuacao {int(atuacao_match.group(1))}"
    return normalized


def all_known_columns():
    return set(CANONICAL_COLUMNS.values())


def validate_columns(df):
    missing = [column for column in BASE_REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Colunas obrigatórias ausentes: {', '.join(missing)}")


def import_leads_from_dataframe(df, user=None, import_batch=None, commit=True, franchise_id=None, company_id=None):
    created = 0
    duplicates = 0
    skipped = 0
    payloads_by_cpf = {}
    company_id = company_id or company_id_for_user(user)
    franchise_id = franchise_id or franchise_id_for_user(user)

    for _, row in df.iterrows():
        cpf = normalize_cpf(row_value(row, "cpf"))
        if not cpf:
            skipped += 1
            continue

        payload = row_to_payload(row, cpf)
        if cpf in payloads_by_cpf:
            duplicates += 1
            merge_upload_payload(payloads_by_cpf[cpf], payload)
            continue

        payloads_by_cpf[cpf] = payload

    for cpf, payload in payloads_by_cpf.items():
        if not payload["equipe"] or not payload["consultor"] or not payload["nome_cliente"]:
            skipped += 1
            continue

        if lead_exists_in_contract_scope(cpf, company_id, franchise_id):
            duplicates += 1
            continue

        payload["company_id"] = company_id
        payload["franchise_id"] = franchise_id
        customer = get_or_create_customer_for_contract(
            payload["company_id"],
            cpf,
            nome=payload["nome_cliente"],
            contato=payload["contato"],
        )
        lead = Lead(**payload)
        lead.customer = customer
        ensure_lead_new_status(lead)
        db.session.add(lead)
        db.session.flush()
        ensure_lead_default_workflows(lead)
        sync_sale_entry_workflow_for_lead(lead, user=user)
        sync_lead_workflow_due_dates_for_lead(lead)
        ensure_legacy_lead_activities_for_lead(lead, fallback_user=user)
        if lead_has_any_activity(lead):
            ensure_lead_in_progress_status(lead, user, source="import_activity")
        log_audit(
            lead,
            user,
            "import_create",
            import_batch=import_batch,
            details={"filename": import_batch.filename if import_batch else None},
        )
        created += 1

    if commit:
        db.session.commit()
    return {"created": created, "duplicates": duplicates, "skipped": skipped}


def row_to_payload(row, cpf):
    payload = {
        "equipe": clean_text(row_value(row, "equipe")) or "",
        "consultor": clean_text(row_value(row, "consultor")) or "",
        "nome_cliente": clean_text(row_value(row, "nome cliente")) or "",
        "cpf": cpf,
        "data_venda": parse_date(row_value(row, "data venda")),
        "concessao": parse_date(row_value(row, "concessao")),
        "contato": normalize_numeric_text(row_value(row, "contato")),
        "status": clean_text(row_value(row, "status")),
    }
    for index, field in enumerate(ATUACAO_FIELDS, start=1):
        payload[field] = clean_text(row_value(row, f"atuacao {index}"))
    return payload


def row_value(row, normalized_column):
    for column, value in row.items():
        if normalize_header(column) == normalized_column:
            return value
    return None


def merge_upload_payload(target, incoming):
    for field in ("equipe", "consultor", "nome_cliente", "data_venda", "concessao"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]

    for field in ("contato", "status"):
        if incoming.get(field):
            target[field] = incoming[field]

    for field in ATUACAO_FIELDS:
        if incoming.get(field):
            add_atuacao_to_payload(target, field, incoming[field])


def add_atuacao_to_payload(payload, preferred_field, value):
    normalized_value = normalize_atuacao_value(value)
    existing_values = {
        normalize_atuacao_value(payload.get(field))
        for field in ATUACAO_FIELDS
        if payload.get(field)
    }
    if normalized_value in existing_values:
        return

    if not payload.get(preferred_field):
        payload[preferred_field] = value
        return

    for field in ATUACAO_FIELDS:
        if not payload.get(field):
            payload[field] = value
            return


def normalize_atuacao_value(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def apply_upload_update(lead, payload):
    lead.equipe = payload["equipe"]
    lead.consultor = payload["consultor"]
    lead.nome_cliente = payload["nome_cliente"]
    lead.data_venda = payload["data_venda"]
    lead.concessao = payload["concessao"]

    if not lead.contato_manual and payload["contato"]:
        lead.contato = payload["contato"]
    if not lead.status_manual and payload["status"]:
        lead.status = payload["status"]

    for field in ATUACAO_FIELDS:
        if not getattr(lead, f"{field}_manual") and payload.get(field):
            setattr(lead, field, payload[field])


def get_active_field_options(field_name):
    return (
        scoped_company_query(
            FieldOption.query.filter_by(field_name=field_name, active=True),
            FieldOption,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(FieldOption.sort_order.asc(), FieldOption.value.asc())
        .all()
    )


def get_active_options(field_name):
    options = get_active_field_options(field_name)
    return [option.value for option in options] or DEFAULT_OPTIONS[field_name]


def get_option_style_map(field_name):
    options = (
        scoped_company_query(
            FieldOption.query.filter_by(field_name=field_name),
            FieldOption,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(FieldOption.sort_order.asc(), FieldOption.value.asc())
        .all()
    )
    if not options:
        return {
            value: {"style": f"--chip-color: {default_option_color(field_name, value)};"}
            for value in DEFAULT_OPTIONS[field_name]
        }
    return {
        option.value: {"style": f"--chip-color: {safe_option_color(option)};"}
        for option in options
    }


def safe_option_color(option):
    try:
        return normalize_hex_color(option.color_hex) or default_option_color(option.field_name, option.value)
    except ValueError:
        return default_option_color(option.field_name, option.value)


def field_option_exists(field_name, value, current_id=None):
    query = scoped_company_query(
        FieldOption.query.filter(
            FieldOption.field_name == field_name,
            func.lower(FieldOption.value) == value.lower(),
        ),
        FieldOption,
        request_user_or_none(),
        include_unscoped=True,
    )
    if current_id:
        query = query.filter(FieldOption.id != current_id)
    return db.session.query(query.exists()).scalar()


def field_option_is_reserved(field_name, value):
    return field_name == "posicao" and normalize_option_key(value) == "reversao"


def field_option_snapshot(option):
    return {
        "id": option.id,
        "company_id": option.company_id,
        "field_name": option.field_name,
        "value": option.value,
        "color_hex": option.color_hex,
        "sort_order": option.sort_order,
        "active": option.active,
    }


def field_option_usage_count(option):
    lead_query = scoped_company_query(
        Lead.query,
        Lead,
        request_user_or_none(),
        include_unscoped=True,
    )
    if option.field_name == "status":
        return lead_query.filter(Lead.status == option.value).count()
    if option.field_name == "posicao":
        return lead_query.filter(Lead.posicao == option.value).count()
    return 0


def normalize_hex_color(value):
    value = clean_text(value)
    if not value:
        return None
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.lower()
    raise ValueError("Cor invalida. Use o seletor de cor do painel.")


def get_company_franchises(user=None, include_inactive=False):
    query = scoped_company_query(
        Franchise.query,
        Franchise,
        user or request_user_or_none(),
        include_unscoped=True,
    )
    if not include_inactive:
        query = query.filter(Franchise.active.is_(True))
    return query.order_by(Franchise.active.desc(), Franchise.name.asc()).all()


def validate_franchise_id_for_form(value, user=None, fallback_id=None):
    franchise_id = parse_int(value, 0)
    if not franchise_id:
        return fallback_id
    franchise = db.session.get(Franchise, franchise_id)
    if not franchise or not franchise.active or not in_same_company_scope(user or request_user_or_none(), franchise):
        raise ValueError("Franquia invalida.")
    return franchise.id


def franchise_exists(name, current_id=None):
    query = scoped_company_query(
        Franchise.query.filter(func.lower(Franchise.name) == name.lower()),
        Franchise,
        request_user_or_none(),
        include_unscoped=True,
    )
    if current_id:
        query = query.filter(Franchise.id != current_id)
    return db.session.query(query.exists()).scalar()


def franchise_snapshot(franchise):
    return {
        "id": franchise.id,
        "company_id": franchise.company_id,
        "name": franchise.name,
        "document": franchise.document,
        "email": franchise.email,
        "phone": franchise.phone,
        "active": franchise.active,
    }


def franchise_usage_counts(franchise):
    user_query = scoped_company_query(User.query, User, request_user_or_none(), include_unscoped=True)
    lead_query = scoped_company_query(Lead.query, Lead, request_user_or_none(), include_unscoped=True)
    team_query = scoped_company_query(Team.query, Team, request_user_or_none(), include_unscoped=True)
    return {
        "users": user_query.filter(User.franchise_id == franchise.id).count(),
        "leads": lead_query.filter(Lead.franchise_id == franchise.id).count(),
        "teams": team_query.filter(Team.franchise_id == franchise.id).count(),
    }


def franchise_teams(franchise):
    if not franchise:
        return []
    return (
        scoped_company_query(
            Team.query.filter(Team.franchise_id == franchise.id),
            Team,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(Team.active.desc(), Team.name.asc())
        .all()
    )


def franchise_users(franchise):
    if not franchise:
        return []
    return (
        scoped_company_query(
            User.query.filter(User.franchise_id == franchise.id),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(User.active.desc(), User.role.asc(), User.name.asc())
        .all()
    )


def franchise_available_teams(franchise):
    if not franchise:
        return []
    return (
        scoped_company_query(
            Team.query.filter(
                Team.active.is_(True),
                or_(Team.franchise_id.is_(None), Team.franchise_id != franchise.id),
            ),
            Team,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(Team.name.asc())
        .all()
    )


def franchise_available_users(franchise):
    if not franchise:
        return []
    current_user_id = getattr(request_user_or_none(), "id", 0)
    return (
        scoped_company_query(
            User.query.filter(
                User.active.is_(True),
                User.id != current_user_id,
                or_(User.franchise_id.is_(None), User.franchise_id != franchise.id),
            ),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(User.team.asc(), User.name.asc())
        .all()
    )


def team_exists(name, current_id=None, franchise_id=None):
    query = scoped_company_query(
        Team.query.filter(func.lower(Team.name) == name.lower()),
        Team,
        request_user_or_none(),
        include_unscoped=True,
    )
    if franchise_id:
        query = query.filter(Team.franchise_id == franchise_id)
    if current_id:
        query = query.filter(Team.id != current_id)
    return db.session.query(query.exists()).scalar()


def team_snapshot(team):
    return {
        "id": team.id,
        "company_id": team.company_id,
        "franchise_id": team.franchise_id,
        "franchise": team.franchise.name if team.franchise else None,
        "name": team.name,
        "description": team.description,
        "active": team.active,
    }


def reversal_team_config_for_team(team):
    if not team:
        return None
    config = None
    if team.id:
        config = ReversalTeamConfig.query.filter_by(team_id=team.id).first()
    if not config:
        query = ReversalTeamConfig.query.filter(
            ReversalTeamConfig.team_name == team.name,
            ReversalTeamConfig.company_id == team.company_id,
        )
        if team.franchise_id:
            query = query.filter(ReversalTeamConfig.franchise_id == team.franchise_id)
        else:
            query = query.filter(ReversalTeamConfig.franchise_id.is_(None))
        config = query.order_by(ReversalTeamConfig.id.asc()).first()
    return config


def get_or_create_reversal_team_config(team):
    config = reversal_team_config_for_team(team)
    created = False
    if not config:
        config = ReversalTeamConfig(
            company_id=team.company_id,
            franchise_id=team.franchise_id,
            team_id=team.id,
            team_name=team.name,
            enabled=True,
            distribution_mode=ReversalDistributionMode.MANUAL,
        )
        db.session.add(config)
        db.session.flush()
        created = True
    else:
        config.company_id = team.company_id
        config.franchise_id = team.franchise_id
        config.team_id = team.id
        config.team_name = team.name
        if config.distribution_mode not in ReversalDistributionMode.ALL:
            config.distribution_mode = ReversalDistributionMode.MANUAL
    return config, created


def reversal_team_config_snapshot(config):
    if not config:
        return {}
    return {
        "id": config.id,
        "company_id": config.company_id,
        "franchise_id": config.franchise_id,
        "team_id": config.team_id,
        "team_name": config.team_name,
        "enabled": config.enabled,
        "distribution_mode": config.distribution_mode,
    }


def team_usage_counts(team):
    user_query = scoped_company_query(User.query, User, request_user_or_none(), include_unscoped=True)
    lead_query = scoped_company_query(Lead.query, Lead, request_user_or_none(), include_unscoped=True)
    return {
        "users": user_query.filter(User.team == team.name).count(),
        "leads": lead_query.filter(Lead.equipe == team.name).count(),
    }


def team_members(team):
    if not team:
        return []
    query = scoped_company_query(
        User.query.filter(User.team == team.name),
        User,
        request_user_or_none(),
        include_unscoped=True,
    )
    if team.franchise_id:
        query = query.filter(User.franchise_id == team.franchise_id)
    return query.order_by(User.active.desc(), User.role.asc(), User.name.asc()).all()


def team_available_users(team):
    if not team:
        return []
    query = scoped_company_query(
        User.query.filter(
            User.active.is_(True),
            User.id != getattr(request_user_or_none(), "id", 0),
            or_(User.team.is_(None), User.team != team.name),
        ),
        User,
        request_user_or_none(),
        include_unscoped=True,
    )
    if team.franchise_id:
        query = query.filter(User.franchise_id == team.franchise_id)
    return query.order_by(User.team.asc(), User.name.asc()).all()


def role_profile_exists(name, current_id=None):
    query = scoped_company_query(
        RoleProfile.query.filter(func.lower(RoleProfile.name) == name.lower()),
        RoleProfile,
        request_user_or_none(),
        include_unscoped=True,
    )
    if current_id:
        query = query.filter(RoleProfile.id != current_id)
    return db.session.query(query.exists()).scalar()


def build_role_profile_from_form(profile, form):
    name = clean_text(form.get("name"))
    if not name:
        raise ValueError("Informe o nome do cargo.")
    base_role = form.get("base_role") if form.get("base_role") in Role.ALL else Role.OPERADOR
    profile.name = name
    profile.description = clean_text(form.get("description"))
    profile.base_role = base_role
    profile.sort_order = parse_int(form.get("sort_order"), profile.sort_order or 0)
    profile.active = form.get("active") == "on"
    for field in PERMISSION_FIELDS:
        setattr(profile, field, form.get(field) == "on")
    return profile


def role_profile_snapshot(profile):
    snapshot = {
        "id": profile.id,
        "company_id": profile.company_id,
        "name": profile.name,
        "description": profile.description,
        "base_role": profile.base_role,
        "sort_order": profile.sort_order,
        "active": profile.active,
    }
    for field in PERMISSION_FIELDS:
        snapshot[field] = bool(getattr(profile, field, False))
    return snapshot


def get_role_profile_user_counts():
    rows = (
        scoped_company_query(
            db.session.query(User.role_profile_id, func.count(User.id)),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .filter(User.role_profile_id.isnot(None))
        .group_by(User.role_profile_id)
        .all()
    )
    return {profile_id: count for profile_id, count in rows}


def role_profile_users(profile):
    if not profile:
        return []
    return (
        scoped_company_query(
            User.query.filter(User.role_profile_id == profile.id),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(User.active.desc(), User.role.asc(), User.name.asc())
        .all()
    )


def role_profile_available_users(profile):
    if not profile:
        return []
    current_user_id = getattr(request_user_or_none(), "id", 0)
    return (
        scoped_company_query(
            User.query.filter(
                User.active.is_(True),
                User.id != current_user_id,
                or_(User.role_profile_id.is_(None), User.role_profile_id != profile.id),
            ),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .order_by(User.team.asc(), User.name.asc())
        .all()
    )


def enabled_permission_count(subject):
    visible_fields = tuple(
        dict.fromkeys(
            field
            for group in PERMISSION_GROUPS
            for field in group["fields"]
        )
    )
    return sum(1 for field in visible_fields if bool(getattr(subject, field, False)))


def user_usage_counts(user):
    return {
        "audit_logs": AuditLog.query.filter(AuditLog.user_id == user.id).count(),
        "import_batches": ImportBatch.query.filter(ImportBatch.user_id == user.id).count(),
        "lead_activities": LeadActivity.query.filter(LeadActivity.user_id == user.id).count(),
        "workflow_transitions": WorkflowTransition.query.filter(WorkflowTransition.user_id == user.id).count(),
        "deleted_leads": Lead.query.filter(Lead.deleted_by_id == user.id).count(),
        "approved_requests": GoogleAccessRequest.query.filter(GoogleAccessRequest.approved_by_id == user.id).count(),
        "rejected_requests": GoogleAccessRequest.query.filter(GoogleAccessRequest.rejected_by_id == user.id).count(),
    }


def get_active_team_names(user=None, franchise_id=None):
    actor = user if user is not None else request_user_or_none()
    configured_query = scoped_company_query(
        db.session.query(Team.name).filter(Team.active.is_(True)),
        Team,
        actor,
        include_unscoped=True,
    )
    user_team_query = scoped_company_query(
        db.session.query(User.team).filter(User.active.is_(True)),
        User,
        actor,
        include_unscoped=True,
    )
    if franchise_id:
        configured_query = configured_query.filter(Team.franchise_id == franchise_id)
        user_team_query = user_team_query.filter(User.franchise_id == franchise_id)
    configured_teams = [
        row[0]
        for row in configured_query.distinct().all()
        if row[0]
    ]
    user_teams = [
        row[0]
        for row in user_team_query.distinct().all()
        if row[0]
    ]
    return sorted(set(configured_teams + user_teams))


def get_known_teams():
    configured_teams = [
        row[0]
        for row in scoped_company_query(
            db.session.query(Team.name).filter(Team.active.is_(True)),
            Team,
            request_user_or_none(),
            include_unscoped=True,
        )
        .distinct()
        .all()
        if row[0]
    ]
    lead_teams = [
        row[0]
        for row in scoped_company_query(
            db.session.query(Lead.equipe).filter(Lead.deleted_at.is_(None)),
            Lead,
            request_user_or_none(),
            include_unscoped=True,
        )
        .distinct()
        .all()
        if row[0]
    ]
    user_teams = [
        row[0]
        for row in scoped_company_query(
            db.session.query(User.team),
            User,
            request_user_or_none(),
            include_unscoped=True,
        )
        .distinct()
        .all()
        if row[0]
    ]
    return sorted(set(configured_teams + lead_teams + user_teams))


def get_active_operator_names(equipe=None, user=None, franchise_id=None):
    actor = user if user is not None else request_user_or_none()
    query = scoped_company_query(
        User.query.filter(
            User.active.is_(True),
            User.role == Role.OPERADOR,
        ),
        User,
        actor,
        include_unscoped=True,
    )
    if franchise_id:
        query = query.filter(User.franchise_id == franchise_id)
    if equipe:
        query = query.filter(User.team == equipe)

    names = []
    seen = set()
    for operator in query.order_by(User.name.asc()).all():
        name = lead_assignment_name(operator)
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def get_known_consultores(equipe=None):
    lead_query = scoped_company_query(
        db.session.query(Lead.consultor).filter(Lead.deleted_at.is_(None)),
        Lead,
        request_user_or_none(),
        include_unscoped=True,
    )
    user_query = scoped_company_query(
        db.session.query(User.name).filter(User.active.is_(True)),
        User,
        request_user_or_none(),
        include_unscoped=True,
    )
    user_lead_query = scoped_company_query(
        db.session.query(User.lead_name).filter(User.active.is_(True)),
        User,
        request_user_or_none(),
        include_unscoped=True,
    )
    if equipe:
        lead_query = lead_query.filter(Lead.equipe == equipe)
        user_query = user_query.filter(User.team == equipe)
        user_lead_query = user_lead_query.filter(User.team == equipe)

    lead_names = [row[0] for row in lead_query.distinct().all() if row[0]]
    user_names = [row[0] for row in user_query.distinct().all() if row[0]]
    user_lead_names = [row[0] for row in user_lead_query.distinct().all() if row[0]]
    return sorted(set(lead_names + user_names + user_lead_names))


def get_lead_counts_by_consultor():
    rows = (
        scoped_company_query(
            db.session.query(Lead.consultor, func.count(Lead.id)).filter(Lead.deleted_at.is_(None)),
            Lead,
            request_user_or_none(),
            include_unscoped=True,
        )
        .group_by(Lead.consultor)
        .all()
    )
    return {consultor: count for consultor, count in rows if consultor}


def unique_username(value):
    base = re.sub(r"[^a-zA-Z0-9_.-]", "", (value or "user").split("@")[0].lower()) or "user"
    username = base
    suffix = 2
    while User.query.filter_by(username=username).first():
        username = f"{base}{suffix}"
        suffix += 1
    return username


def clean_text(value):
    if value is None or pd.isna(value):
        return None

    text_value = str(value).strip()
    return text_value if text_value else None


def validate_unique_user_email(email, user_id=None):
    normalized_email = clean_text(email)
    if not normalized_email:
        return None

    query = User.query.filter(func.lower(User.email) == normalized_email.lower())
    if user_id:
        query = query.filter(User.id != user_id)
    if query.first():
        raise ValueError("Esse e-mail ja esta em uso por outro usuario.")
    return normalized_email


def normalize_numeric_text(value):
    text_value = clean_text(value)
    if not text_value:
        return None
    if re.fullmatch(r"\d+\.0", text_value):
        return text_value[:-2]
    return text_value


def normalize_cpf(value):
    text_value = normalize_numeric_text(value)
    if not text_value:
        return None

    digits = re.sub(r"\D", "", text_value)
    if not digits:
        return None

    return digits.zfill(11) if len(digits) <= 11 else digits


def parse_date(value):
    if value is None or pd.isna(value):
        return None

    date = pd.to_datetime(value, errors="coerce")
    if pd.isna(date):
        return None

    return date.date()


app = create_app()


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        migrate_sqlite_schema()
        ensure_default_company_structure()
        seed_default_options()
        retire_legacy_reversal_position()
        seed_default_teams()
        seed_default_reversal_configs()
        seed_default_role_profiles()
        if User.query.count() == 0:
            seed_default_users(app)
        seed_default_workflows()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=env_truthy("FLASK_DEBUG") and not is_production_env(),
    )

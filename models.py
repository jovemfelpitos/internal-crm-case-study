from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class Role:
    ADMIN = "ADMIN"
    SUPERVISOR = "SUPERVISOR"
    OPERADOR = "OPERADOR"

    # Alias para bancos antigos criados na primeira versao.
    CONSULTOR = OPERADOR

    ALL = (ADMIN, SUPERVISOR, OPERADOR)


PERMISSION_FIELDS = (
    "can_view_all",
    "can_view_all_franchises",
    "can_upload",
    "can_create_leads",
    "can_manage_users",
    "can_edit_core",
    "can_edit_contact",
    "can_edit_status",
    "can_edit_posicao",
    "can_edit_atuacoes",
    "can_bulk_manage_leads",
    "can_bulk_export_leads",
    "can_bulk_transfer_leads",
    "can_bulk_edit_status",
    "can_bulk_edit_posicao",
    "can_bulk_inactivate_leads",
    "can_bulk_reactivate_leads",
    "can_bulk_delete_leads",
    "can_export_leads",
    "can_transfer_leads",
    "can_move_workflows",
    "can_move_workflows_backwards",
    "can_delete_leads",
    "can_deactivate_leads",
    "can_reactivate_leads",
    "can_conclude_contract",
    "can_reject_contract",
    "can_undo_contract_status",
    "can_act_as_support",
    "can_request_support",
    "can_request_reversal",
    "can_work_reversal",
    "can_manage_reversals",
    "can_view_audit",
    "can_manage_settings",
    "can_manage_workflows",
    "can_manage_roles",
    "can_manage_teams",
)


ATUACAO_FIELDS = tuple(f"atuacao_{index}" for index in range(1, 13))


class ReversalDistributionMode:
    AUTOMATIC = "automatic"
    MANUAL = "manual"

    ALL = (AUTOMATIC, MANUAL)


class ReversalStatus:
    QUEUED = "queued"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    OVERDUE = "overdue"
    COMPLETED = "completed"
    FAILED = "failed"
    RETURNED = "returned"
    CANCELED = "canceled"

    ACTIVE = (QUEUED, ASSIGNED, IN_PROGRESS, OVERDUE)
    TERMINAL = (COMPLETED, FAILED, RETURNED, CANCELED)
    ALL = ACTIVE + TERMINAL


class ReversalPriority:
    STANDARD = "standard"
    ATTENTION = "attention"
    URGENT = "urgent"

    ALL = (STANDARD, ATTENTION, URGENT)


class ReversalResult:
    SEM_CONTATO = "sem_contato"
    FIDELIZADO = "fidelizado"
    NAO_ATENDEU = "nao_atendeu"
    PEDIU_RETORNO = "pediu_retorno"
    BLOQUEOU = "bloqueou"
    CLIENTE_INSEGURO = "cliente_inseguro"
    NAO_QUIS_CONTRATAR = "nao_quis_contratar"
    NEGOCIANDO = "negociando"
    VENDA_FECHADA = "venda_fechada"
    NEGOCIANDO_TERCEIRO = "negociando_terceiro"
    CLIENTE_VENDIDO_PERDIDO = "cliente_vendido_perdido"
    OUTRO = "outro"

    ACTIVE = (
        SEM_CONTATO,
        BLOQUEOU,
        NAO_QUIS_CONTRATAR,
        NEGOCIANDO,
        VENDA_FECHADA,
        NEGOCIANDO_TERCEIRO,
        OUTRO,
    )
    LEGACY = (
        FIDELIZADO,
        NAO_ATENDEU,
        PEDIU_RETORNO,
        CLIENTE_INSEGURO,
        CLIENTE_VENDIDO_PERDIDO,
    )
    CRITICAL = (NEGOCIANDO_TERCEIRO, CLIENTE_VENDIDO_PERDIDO)
    ALL = ACTIVE + LEGACY


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    document = db.Column(db.String(32), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class Franchise(db.Model):
    __tablename__ = "franchises"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    document = db.Column(db.String(32), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_franchise_company_name"),
    )


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    google_sub = db.Column(db.String(255), unique=True, nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    lead_name = db.Column(db.String(120), nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), nullable=False, index=True, default=Role.OPERADOR)
    role_profile_id = db.Column(db.Integer, db.ForeignKey("role_profiles.id"), nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    team = db.Column(db.String(120), nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime, nullable=True, index=True)
    last_login_method = db.Column(db.String(30), nullable=True)
    last_login_ip = db.Column(db.String(64), nullable=True)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    can_view_all = db.Column(db.Boolean, nullable=False, default=False)
    can_view_all_franchises = db.Column(db.Boolean, nullable=False, default=False)
    can_upload = db.Column(db.Boolean, nullable=False, default=False)
    can_create_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_users = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_core = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_contact = db.Column(db.Boolean, nullable=False, default=True)
    can_edit_status = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_posicao = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_atuacoes = db.Column(db.Boolean, nullable=False, default=True)
    can_bulk_manage_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_export_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_transfer_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_edit_status = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_edit_posicao = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_inactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_reactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_delete_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_export_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_transfer_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_move_workflows = db.Column(db.Boolean, nullable=False, default=True)
    can_move_workflows_backwards = db.Column(db.Boolean, nullable=False, default=False)
    can_delete_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_deactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_reactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_conclude_contract = db.Column(db.Boolean, nullable=False, default=False)
    can_reject_contract = db.Column(db.Boolean, nullable=False, default=False)
    can_undo_contract_status = db.Column(db.Boolean, nullable=False, default=False)
    can_act_as_support = db.Column(db.Boolean, nullable=False, default=False)
    can_request_support = db.Column(db.Boolean, nullable=False, default=False)
    can_request_reversal = db.Column(db.Boolean, nullable=False, default=False)
    can_work_reversal = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_reversals = db.Column(db.Boolean, nullable=False, default=False)
    can_view_audit = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_settings = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_workflows = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_roles = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_teams = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def set_password(self, password, must_change=False):
        self.password_hash = generate_password_hash(password) if password else ""
        self.password_changed_at = datetime.utcnow() if password else None
        self.must_change_password = bool(password and must_change)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def apply_role_defaults(self):
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

        for field, value in defaults.get(self.role, defaults[Role.OPERADOR]).items():
            setattr(self, field, value)

    role_profile = db.relationship("RoleProfile", foreign_keys=[role_profile_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_supervisor(self):
        return self.role == Role.SUPERVISOR

    @property
    def is_operador(self):
        return self.role == Role.OPERADOR


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    cpf = db.Column(db.String(20), nullable=False, index=True)
    nome = db.Column(db.String(180), nullable=True)
    contato = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "cpf", name="uq_customers_company_cpf"),
    )

    def __repr__(self):
        return f"<Customer cpf={self.cpf}>"


class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    equipe = db.Column(db.String(120), nullable=False, index=True)
    consultor = db.Column(db.String(120), nullable=False, index=True)
    nome_cliente = db.Column(db.String(180), nullable=False)
    cpf = db.Column(db.String(20), nullable=False, index=True)
    data_venda = db.Column(db.Date, nullable=True)
    concessao = db.Column(db.Date, nullable=True)
    contato = db.Column(db.String(255), nullable=True)
    posicao = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(120), nullable=True)
    atuacao = db.Column(db.Text, nullable=True)
    atuacao_1 = db.Column(db.Text, nullable=True)
    atuacao_2 = db.Column(db.Text, nullable=True)
    atuacao_3 = db.Column(db.Text, nullable=True)
    atuacao_4 = db.Column(db.Text, nullable=True)
    atuacao_5 = db.Column(db.Text, nullable=True)
    atuacao_6 = db.Column(db.Text, nullable=True)
    atuacao_7 = db.Column(db.Text, nullable=True)
    atuacao_8 = db.Column(db.Text, nullable=True)
    atuacao_9 = db.Column(db.Text, nullable=True)
    atuacao_10 = db.Column(db.Text, nullable=True)
    atuacao_11 = db.Column(db.Text, nullable=True)
    atuacao_12 = db.Column(db.Text, nullable=True)

    contato_manual = db.Column(db.Boolean, nullable=False, default=False)
    posicao_manual = db.Column(db.Boolean, nullable=False, default=False)
    status_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_1_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_2_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_3_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_4_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_5_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_6_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_7_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_8_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_9_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_10_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_11_manual = db.Column(db.Boolean, nullable=False, default=False)
    atuacao_12_manual = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    deleted_reason = db.Column(db.Text, nullable=True)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "franchise_id", "cpf", name="uq_leads_company_franchise_cpf"),
    )

    def __repr__(self):
        return f"<Lead cpf={self.cpf} consultor={self.consultor}>"


class Workflow(db.Model):
    __tablename__ = "workflows"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    auto_move_enabled = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])


class ConfigurationMigration(db.Model):
    __tablename__ = "configuration_migrations"

    key = db.Column(db.String(160), primary_key=True)
    applied_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TaskConfigurationScope(db.Model):
    __tablename__ = "task_configuration_scopes"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True)
    scope_key = db.Column(db.String(80), nullable=False)
    matrix_franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True)
    customization_enabled = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("company_id", "scope_key", name="uq_task_configuration_scope"),
    )


class TaskConfigurationRevision(db.Model):
    __tablename__ = "task_configuration_revisions"

    id = db.Column(db.Integer, primary_key=True)
    scope_id = db.Column(db.Integer, db.ForeignKey("task_configuration_scopes.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(20), nullable=False, default="draft")
    content = db.Column(db.Text, nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    base_revision_id = db.Column(db.Integer, db.ForeignKey("task_configuration_revisions.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    scope = db.relationship("TaskConfigurationScope", foreign_keys=[scope_id])
    __table_args__ = (
        db.UniqueConstraint("scope_id", "number", name="uq_task_configuration_revision_number"),
        db.CheckConstraint("status IN ('baseline', 'published', 'draft')", name="ck_task_configuration_status"),
    )


class ContactAgendaConfiguration(db.Model):
    __tablename__ = "contact_agenda_configurations"

    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), primary_key=True)
    revision_id = db.Column(db.Integer, db.ForeignKey("task_configuration_revisions.id"), nullable=False)
    effective_from = db.Column(db.Date, nullable=True)


class ChecklistStageConfiguration(db.Model):
    __tablename__ = "checklist_stage_configurations"

    id = db.Column(db.Integer, primary_key=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=False)
    stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=False)
    item_ids = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("lead_workflow_id", "stage_id", name="uq_checklist_stage_configuration"),
    )


class WorkflowStage(db.Model):
    __tablename__ = "workflow_stages"

    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False, index=True)
    slug = db.Column(db.String(80), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    max_days = db.Column(db.Integer, nullable=True)
    alert_days_before = db.Column(db.Integer, nullable=True)
    first_action_hours = db.Column(db.Integer, nullable=True)
    requires_support = db.Column(db.Boolean, nullable=False, default=False)
    support_sla_hours = db.Column(db.Integer, nullable=True)
    requires_note = db.Column(db.Boolean, nullable=False, default=False)
    requires_checklist = db.Column(db.Boolean, nullable=False, default=False)
    required_fields = db.Column(db.Text, nullable=True)
    allow_auto_move = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])

    __table_args__ = (
        db.UniqueConstraint("workflow_id", "slug", name="uq_workflow_stage_slug"),
    )


class LeadWorkflow(db.Model):
    __tablename__ = "lead_workflows"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False, index=True)
    current_stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default="em_andamento", index=True)
    entered_stage_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    due_at = db.Column(db.DateTime, nullable=True, index=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    support_requested_at = db.Column(db.DateTime, nullable=True)
    support_assigned_at = db.Column(db.DateTime, nullable=True)
    support_completed_at = db.Column(db.DateTime, nullable=True)
    support_returned_at = db.Column(db.DateTime, nullable=True)
    support_return_reason = db.Column(db.Text, nullable=True)
    support_return_resolution_requested_at = db.Column(db.DateTime, nullable=True)
    support_return_resolution_completed_at = db.Column(db.DateTime, nullable=True)
    support_return_resolution_completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    support_return_resolution_note = db.Column(db.Text, nullable=True)
    support_executor = db.Column(db.String(20), nullable=True)
    support_executor_decided_at = db.Column(db.DateTime, nullable=True)
    support_executor_decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    unlock_executor = db.Column(db.String(20), nullable=True)
    unlock_executor_decided_at = db.Column(db.DateTime, nullable=True)
    unlock_executor_decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    last_action_at = db.Column(db.DateTime, nullable=True, index=True)
    fidelity_started_on = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    lead = db.relationship("Lead", foreign_keys=[lead_id])
    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])
    current_stage = db.relationship("WorkflowStage", foreign_keys=[current_stage_id])
    owner_user = db.relationship("User", foreign_keys=[owner_user_id])
    assigned_user = db.relationship("User", foreign_keys=[assigned_user_id])
    support_executor_decided_by = db.relationship("User", foreign_keys=[support_executor_decided_by_id])
    unlock_executor_decided_by = db.relationship("User", foreign_keys=[unlock_executor_decided_by_id])
    support_return_resolution_completed_by = db.relationship("User", foreign_keys=[support_return_resolution_completed_by_id])

    __table_args__ = (
        db.UniqueConstraint("lead_id", "workflow_id", name="uq_lead_workflow"),
    )


class WorkflowTransition(db.Model):
    __tablename__ = "workflow_transitions"

    id = db.Column(db.Integer, primary_key=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False, index=True)
    from_stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=True, index=True)
    to_stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(40), nullable=False, default="move", index=True)
    note = db.Column(db.Text, nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    lead_workflow = db.relationship("LeadWorkflow", foreign_keys=[lead_workflow_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])
    from_stage = db.relationship("WorkflowStage", foreign_keys=[from_stage_id])
    to_stage = db.relationship("WorkflowStage", foreign_keys=[to_stage_id])
    user = db.relationship("User", foreign_keys=[user_id])


class WorkflowChecklistItem(db.Model):
    __tablename__ = "workflow_checklist_items"

    id = db.Column(db.Integer, primary_key=True)
    stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=False, index=True)
    label = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    required = db.Column(db.Boolean, nullable=False, default=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    stage = db.relationship("WorkflowStage", foreign_keys=[stage_id])

    __table_args__ = (
        db.UniqueConstraint("stage_id", "label", name="uq_workflow_checklist_item"),
    )


class LeadWorkflowChecklistCheck(db.Model):
    __tablename__ = "lead_workflow_checklist_checks"

    id = db.Column(db.Integer, primary_key=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=False, index=True)
    checklist_item_id = db.Column(db.Integer, db.ForeignKey("workflow_checklist_items.id"), nullable=False, index=True)
    checked = db.Column(db.Boolean, nullable=False, default=False, index=True)
    checked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    checked_at = db.Column(db.DateTime, nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)
    configuration_revision_id = db.Column(db.Integer, db.ForeignKey("task_configuration_revisions.id"), nullable=True)
    configuration_snapshot = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    lead_workflow = db.relationship("LeadWorkflow", foreign_keys=[lead_workflow_id])
    checklist_item = db.relationship("WorkflowChecklistItem", foreign_keys=[checklist_item_id])
    checked_by = db.relationship("User", foreign_keys=[checked_by_id])

    __table_args__ = (
        db.UniqueConstraint("lead_workflow_id", "checklist_item_id", name="uq_lead_workflow_check"),
    )


class LeadWorkflowScheduledTask(db.Model):
    __tablename__ = "lead_workflow_scheduled_tasks"

    id = db.Column(db.Integer, primary_key=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=False, index=True)
    stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=False, index=True)
    task_key = db.Column(db.String(120), nullable=False, index=True)
    label = db.Column(db.String(180), nullable=False)
    scheduled_for = db.Column(db.Date, nullable=False, index=True)
    completed_at = db.Column(db.DateTime, nullable=True, index=True)
    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)
    configuration_revision_id = db.Column(db.Integer, db.ForeignKey("task_configuration_revisions.id"), nullable=True)
    configuration_snapshot = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    lead_workflow = db.relationship("LeadWorkflow", foreign_keys=[lead_workflow_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])
    stage = db.relationship("WorkflowStage", foreign_keys=[stage_id])
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])

    __table_args__ = (
        db.UniqueConstraint("lead_workflow_id", "task_key", name="uq_lead_workflow_scheduled_task"),
    )


class LeadActivity(db.Model):
    __tablename__ = "lead_activities"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=True, index=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), nullable=True, index=True)
    stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stages.id"), nullable=True, index=True)
    body = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(40), nullable=False, default="manual", index=True)
    source_field = db.Column(db.String(80), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    lead = db.relationship("Lead", foreign_keys=[lead_id])
    user = db.relationship("User", foreign_keys=[user_id])
    lead_workflow = db.relationship("LeadWorkflow", foreign_keys=[lead_workflow_id])
    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])
    stage = db.relationship("WorkflowStage", foreign_keys=[stage_id])


class FieldOption(db.Model):
    __tablename__ = "field_options"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    field_name = db.Column(db.String(80), nullable=False, index=True)
    value = db.Column(db.String(120), nullable=False)
    color_hex = db.Column(db.String(20), nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        db.UniqueConstraint("field_name", "value", name="uq_field_option"),
    )

    company = db.relationship("Company", foreign_keys=[company_id])


class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "franchise_id", "name", name="uq_teams_company_franchise_name"),
    )


class ReversalSettings(db.Model):
    __tablename__ = "reversal_settings"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    default_distribution_mode = db.Column(
        db.String(20),
        nullable=False,
        default=ReversalDistributionMode.MANUAL,
        index=True,
    )
    min_note_chars = db.Column(db.Integer, nullable=False, default=40)
    sla_30_days_hours = db.Column(db.Integer, nullable=False, default=48)
    sla_20_days_hours = db.Column(db.Integer, nullable=False, default=24)
    sla_10_days_hours = db.Column(db.Integer, nullable=False, default=10)
    standard_days_threshold = db.Column(db.Integer, nullable=False, default=30)
    attention_days_threshold = db.Column(db.Integer, nullable=False, default=20)
    urgent_days_threshold = db.Column(db.Integer, nullable=False, default=10)
    show_reversor_to_owner = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "franchise_id", name="uq_reversal_settings_scope"),
    )


class ReversalTeamConfig(db.Model):
    __tablename__ = "reversal_team_configs"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True, unique=True, index=True)
    team_name = db.Column(db.String(120), nullable=False, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    distribution_mode = db.Column(
        db.String(20),
        nullable=False,
        default=ReversalDistributionMode.MANUAL,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    team = db.relationship("Team", foreign_keys=[team_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "franchise_id", "team_name", name="uq_reversal_team_config_scope_team"),
    )


class ReversalAssignmentBatch(db.Model):
    __tablename__ = "reversal_assignment_batches"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True, index=True)
    team_name = db.Column(db.String(120), nullable=False, index=True)
    mode = db.Column(db.String(20), nullable=False, default=ReversalDistributionMode.MANUAL, index=True)
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    total_tasks = db.Column(db.Integer, nullable=False, default=0)
    total_assigned = db.Column(db.Integer, nullable=False, default=0)
    details = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True, index=True)
    finished_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    team = db.relationship("Team", foreign_keys=[team_id])
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])


class ReversalTask(db.Model):
    __tablename__ = "reversal_tasks"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True, index=True)
    team_name = db.Column(db.String(120), nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    owner_name = db.Column(db.String(120), nullable=True, index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    assignment_batch_id = db.Column(
        db.Integer,
        db.ForeignKey("reversal_assignment_batches.id"),
        nullable=True,
        index=True,
    )
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    status = db.Column(
        db.String(30),
        nullable=False,
        default=ReversalStatus.QUEUED,
        index=True,
    )
    priority_level = db.Column(
        db.String(20),
        nullable=False,
        default=ReversalPriority.STANDARD,
        index=True,
    )
    distribution_mode = db.Column(
        db.String(20),
        nullable=False,
        default=ReversalDistributionMode.MANUAL,
        index=True,
    )
    tabulation = db.Column(db.String(80), nullable=True, index=True)
    days_to_unlock = db.Column(db.Integer, nullable=True, index=True)
    sla_hours = db.Column(db.Integer, nullable=True)
    due_at = db.Column(db.DateTime, nullable=True, index=True)
    final_result = db.Column(db.String(60), nullable=True, index=True)
    final_note = db.Column(db.Text, nullable=True)
    owner_notified_at = db.Column(db.DateTime, nullable=True, index=True)
    supervisor_notified_at = db.Column(db.DateTime, nullable=True, index=True)
    queued_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    assigned_at = db.Column(db.DateTime, nullable=True, index=True)
    first_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    completed_at = db.Column(db.DateTime, nullable=True, index=True)
    canceled_at = db.Column(db.DateTime, nullable=True, index=True)
    canceled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cancel_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    team = db.relationship("Team", foreign_keys=[team_id])
    owner_user = db.relationship("User", foreign_keys=[owner_user_id])
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    assignment_batch = db.relationship("ReversalAssignmentBatch", foreign_keys=[assignment_batch_id])
    assigned_user = db.relationship("User", foreign_keys=[assigned_user_id])
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    canceled_by = db.relationship("User", foreign_keys=[canceled_by_id])

    __table_args__ = (
        db.Index("ix_reversal_tasks_scope_status", "company_id", "franchise_id", "status"),
        db.Index("ix_reversal_tasks_team_status", "company_id", "franchise_id", "team_name", "status"),
        db.Index("ix_reversal_tasks_assigned_status", "assigned_user_id", "status"),
    )


class ReversalAttempt(db.Model):
    __tablename__ = "reversal_attempts"

    id = db.Column(db.Integer, primary_key=True)
    reversal_task_id = db.Column(db.Integer, db.ForeignKey("reversal_tasks.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    result = db.Column(db.String(60), nullable=False, index=True)
    note = db.Column(db.Text, nullable=False)
    note_char_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    reversal_task = db.relationship("ReversalTask", foreign_keys=[reversal_task_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        db.Index("ix_reversal_attempts_task_created", "reversal_task_id", "created_at"),
        db.Index("ix_reversal_attempts_scope_result", "company_id", "franchise_id", "result"),
    )


class ReversalNotification(db.Model):
    __tablename__ = "reversal_notifications"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    reversal_task_id = db.Column(db.Integer, db.ForeignKey("reversal_tasks.id"), nullable=False, index=True)
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    notification_type = db.Column(db.String(60), nullable=False, index=True)
    severity = db.Column(db.String(20), nullable=False, default="info", index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    event_key = db.Column(db.String(220), nullable=False, unique=True, index=True)
    read_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    reversal_task = db.relationship("ReversalTask", foreign_keys=[reversal_task_id])
    recipient_user = db.relationship("User", foreign_keys=[recipient_user_id])

    __table_args__ = (
        db.Index("ix_reversal_notifications_recipient_read", "recipient_user_id", "read_at"),
        db.Index("ix_reversal_notifications_scope_created", "company_id", "franchise_id", "created_at"),
    )


class SupportNotification(db.Model):
    __tablename__ = "support_notifications"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    lead_workflow_id = db.Column(db.Integer, db.ForeignKey("lead_workflows.id"), nullable=False, index=True)
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    notification_type = db.Column(db.String(60), nullable=False, index=True)
    severity = db.Column(db.String(20), nullable=False, default="info", index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    event_key = db.Column(db.String(220), nullable=False, unique=True, index=True)
    read_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    lead_workflow = db.relationship("LeadWorkflow", foreign_keys=[lead_workflow_id])
    recipient_user = db.relationship("User", foreign_keys=[recipient_user_id])

    __table_args__ = (
        db.Index("ix_support_notifications_recipient_read", "recipient_user_id", "read_at"),
        db.Index("ix_support_notifications_scope_created", "company_id", "franchise_id", "created_at"),
    )


class RoleProfile(db.Model):
    __tablename__ = "role_profiles"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=True)
    base_role = db.Column(db.String(20), nullable=False, default=Role.OPERADOR, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)

    can_view_all = db.Column(db.Boolean, nullable=False, default=False)
    can_view_all_franchises = db.Column(db.Boolean, nullable=False, default=False)
    can_upload = db.Column(db.Boolean, nullable=False, default=False)
    can_create_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_users = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_core = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_contact = db.Column(db.Boolean, nullable=False, default=True)
    can_edit_status = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_posicao = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_atuacoes = db.Column(db.Boolean, nullable=False, default=True)
    can_bulk_manage_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_export_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_transfer_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_edit_status = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_edit_posicao = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_inactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_reactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_bulk_delete_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_export_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_transfer_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_move_workflows = db.Column(db.Boolean, nullable=False, default=True)
    can_move_workflows_backwards = db.Column(db.Boolean, nullable=False, default=False)
    can_delete_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_deactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_reactivate_leads = db.Column(db.Boolean, nullable=False, default=False)
    can_conclude_contract = db.Column(db.Boolean, nullable=False, default=False)
    can_reject_contract = db.Column(db.Boolean, nullable=False, default=False)
    can_undo_contract_status = db.Column(db.Boolean, nullable=False, default=False)
    can_act_as_support = db.Column(db.Boolean, nullable=False, default=False)
    can_request_support = db.Column(db.Boolean, nullable=False, default=False)
    can_request_reversal = db.Column(db.Boolean, nullable=False, default=False)
    can_work_reversal = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_reversals = db.Column(db.Boolean, nullable=False, default=False)
    can_view_audit = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_settings = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_workflows = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_roles = db.Column(db.Boolean, nullable=False, default=False)
    can_manage_teams = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    company = db.relationship("Company", foreign_keys=[company_id])


class GoogleAccessRequest(db.Model):
    __tablename__ = "google_access_requests"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    google_sub = db.Column(db.String(255), nullable=True, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    picture = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(30), nullable=False, default="PENDING", index=True)
    requested_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    last_attempt_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejected_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    rejected_by = db.relationship("User", foreign_keys=[rejected_by_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])


class ImportBatch(db.Model):
    __tablename__ = "import_batches"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_count = db.Column(db.Integer, nullable=False, default=0)
    duplicate_count = db.Column(db.Integer, nullable=False, default=0)
    skipped_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(30), nullable=False, default="success", index=True)
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])
    user = db.relationship("User", foreign_keys=[user_id])


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True, index=True)
    franchise_id = db.Column(db.Integer, db.ForeignKey("franchises.id"), nullable=True, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    import_batch_id = db.Column(
        db.Integer,
        db.ForeignKey("import_batches.id"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(40), nullable=False, index=True)
    field_name = db.Column(db.String(80), nullable=True, index=True)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    lead_cpf = db.Column(db.String(20), nullable=True, index=True)
    lead_name = db.Column(db.String(180), nullable=True)
    lead_equipe = db.Column(db.String(120), nullable=True)
    lead_consultor = db.Column(db.String(120), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)

    lead = db.relationship("Lead", foreign_keys=[lead_id])
    user = db.relationship("User", foreign_keys=[user_id])
    import_batch = db.relationship("ImportBatch", foreign_keys=[import_batch_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    franchise = db.relationship("Franchise", foreign_keys=[franchise_id])

"""Versioned task configuration foundations; publication UI is intentionally separate."""

import hashlib
import json
from functools import wraps
from types import SimpleNamespace

from flask import abort, g
from sqlalchemy import event, func, select
from sqlalchemy.orm.attributes import flag_modified

from models import (AuditLog, Company, ContactAgendaConfiguration, Franchise, LeadWorkflow, LeadWorkflowChecklistCheck,
                    LeadWorkflowScheduledTask, Role, TaskConfigurationRevision,
                    TaskConfigurationScope, Workflow, WorkflowChecklistItem, WorkflowStage, db)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def company_scope(company_id):
    return TaskConfigurationScope.query.filter_by(company_id=company_id, scope_key="company").first()


def matrix_can_configure(actor, company_id):
    scope = company_scope(company_id)
    franchise = db.session.get(Franchise, actor.franchise_id) if actor and actor.franchise_id else None
    company = db.session.get(Company, company_id)
    return bool(actor and actor.active and actor.can_manage_workflows and actor.company_id == company_id
                and company and company.active and scope and franchise and franchise.active
                and franchise.company_id == company_id and franchise.id == scope.matrix_franchise_id)


def matrix_configuration_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not matrix_can_configure(g.user, g.user.company_id):
            abort(403, description="O padrao de tarefas e fluxos e administrado pela matriz.")
        return view(*args, **kwargs)
    return wrapped


def audit(scope, actor, action, details):
    db.session.add(AuditLog(company_id=scope.company_id, franchise_id=scope.franchise_id,
                            user_id=actor.id if actor else None, action=action, details=encode(details)))


def current_revision(scope):
    if not scope:
        return None
    return (TaskConfigurationRevision.query.filter_by(scope_id=scope.id)
            .filter(TaskConfigurationRevision.status.in_(("baseline", "published")))
            .order_by(TaskConfigurationRevision.number.desc()).first())


def effective_revision(company_id, franchise_id):
    franchise = db.session.get(Franchise, franchise_id) if franchise_id else None
    if not franchise or franchise.company_id != company_id:
        raise ValueError("Franquia invalida para a empresa da configuracao.")
    override = TaskConfigurationScope.query.filter_by(company_id=company_id, scope_key=f"franchise:{franchise_id}").first()
    if override and override.customization_enabled:
        revision = current_revision(override)
        if revision:
            return revision
    return current_revision(company_scope(company_id))


def assert_scope_editor(actor, scope):
    if matrix_can_configure(actor, scope.company_id):
        return
    franchise = db.session.get(Franchise, scope.franchise_id) if scope.franchise_id else None
    company = db.session.get(Company, scope.company_id)
    if not (actor and actor.active and actor.can_manage_workflows and company and company.active
            and actor.company_id == scope.company_id and actor.franchise_id == scope.franchise_id
            and scope.franchise_id and scope.customization_enabled and franchise and franchise.active
            and franchise.company_id == scope.company_id):
        raise ValueError("Sem autorizacao para configurar este escopo.")


def authorize_franchise_customization(actor, franchise_id, enabled):
    franchise = db.session.get(Franchise, franchise_id)
    if (not franchise or not matrix_can_configure(actor, franchise.company_id)
            or actor.role != Role.ADMIN or type(enabled) is not bool):
        raise ValueError("Somente ADMIN da matriz pode autorizar excecoes de franquia.")
    base = company_scope(franchise.company_id)
    if not franchise.active or franchise.id == base.matrix_franchise_id:
        raise ValueError("Selecione uma franquia ativa diferente da matriz.")
    scope = TaskConfigurationScope.query.filter_by(company_id=franchise.company_id, scope_key=f"franchise:{franchise.id}").first()
    if not scope:
        scope = TaskConfigurationScope(company_id=franchise.company_id, franchise_id=franchise.id,
                                       scope_key=f"franchise:{franchise.id}", customization_enabled=False)
        db.session.add(scope)
        db.session.flush()
    before = scope.customization_enabled
    scope.customization_enabled = enabled
    if before != enabled:
        audit(scope, actor, "task_configuration_scope_authorized", {"before": before, "enabled": enabled})
    return scope


def append_revision(scope, content, status, actor=None, base_revision=None):
    payload = encode(content)
    number = (db.session.query(func.max(TaskConfigurationRevision.number)).filter_by(scope_id=scope.id).scalar() or 0) + 1
    revision = TaskConfigurationRevision(scope_id=scope.id, number=number, status=status,
                                         content=payload, fingerprint=hashlib.sha256(payload.encode()).hexdigest(),
                                         base_revision_id=base_revision.id if base_revision else None,
                                         created_by_id=actor.id if actor else None)
    db.session.add(revision)
    db.session.flush()
    audit(scope, actor, "task_configuration_revision_created",
          {"revision_id": revision.id, "number": number, "status": status, "fingerprint": revision.fingerprint})
    return revision


def create_draft(actor, scope_id):
    scope = db.session.get(TaskConfigurationScope, scope_id)
    if not scope:
        raise ValueError("Escopo nao encontrado.")
    assert_scope_editor(actor, scope)
    base = current_revision(scope) or current_revision(company_scope(scope.company_id))
    if not base:
        raise ValueError("Configure primeiro o padrao da matriz.")
    return append_revision(scope, json.loads(base.content), "draft", actor, base)


def configuration_inventory(company_id, contact_rules, inss_tasks, unlock_total_days):
    workflows = Workflow.query.filter_by(company_id=company_id).order_by(Workflow.id).all()
    stages = WorkflowStage.query.join(Workflow).filter(Workflow.company_id == company_id).order_by(WorkflowStage.id).all()
    items = (WorkflowChecklistItem.query.join(WorkflowStage).join(Workflow)
             .filter(Workflow.company_id == company_id).order_by(WorkflowChecklistItem.id).all())
    stage_fields = ("id", "workflow_id", "slug", "name", "sort_order", "active", "max_days",
                    "first_action_hours", "requires_support", "support_sla_hours", "requires_note",
                    "requires_checklist", "required_fields", "allow_auto_move", "alert_days_before")
    return {
        "schema_version": 1,
        "workflows": [{key: getattr(w, key) for key in ("id", "slug", "name", "active", "sort_order", "auto_move_enabled")} for w in workflows],
        "stages": [{key: getattr(stage, key) for key in stage_fields} for stage in stages],
        "checklists": [{"key": f"checklist:{item.id}", **{key: getattr(item, key) for key in
                        ("id", "stage_id", "label", "description", "sort_order", "required", "active")}} for item in items],
        "contact_rules": contact_rules, "inss_tasks": [list(row) for row in inss_tasks],
        "unlock_total_days": unlock_total_days, "note_required": True,
        "reversal_configuration": "existing_reversal_settings_and_team_configs",
    }


def record_company_configuration(company_id, matrix_franchise_id, content, actor=None):
    scope = company_scope(company_id)
    if not scope:
        matrix = db.session.get(Franchise, matrix_franchise_id)
        if not matrix or matrix.company_id != company_id:
            raise ValueError("Matriz invalida para a empresa.")
        scope = TaskConfigurationScope(company_id=company_id, scope_key="company", matrix_franchise_id=matrix.id)
        db.session.add(scope)
        db.session.flush()
    if actor:
        assert_scope_editor(actor, scope)
    previous = current_revision(scope)
    if previous:
        # Legacy stage editing and startup must retain the published automatic rules.
        previous_content = json.loads(previous.content)
        for key in ("schedule", "conditional_task_active"):
            if key in previous_content:
                content[key] = previous_content[key]
        dependencies = {item["key"]: item.get("depends_on") for item in previous_content["checklists"] if "depends_on" in item}
        for item in content["checklists"]:
            if item["key"] in dependencies:
                item["depends_on"] = dependencies[item["key"]]
    fingerprint = hashlib.sha256(encode(content).encode()).hexdigest()
    if previous and previous.fingerprint == fingerprint:
        return previous
    return append_revision(scope, content, "published" if previous else "baseline", actor, previous)


@event.listens_for(TaskConfigurationRevision, "before_update")
@event.listens_for(TaskConfigurationRevision, "before_delete")
def protect_published_revision(mapper, connection, revision):
    old_status = connection.execute(select(TaskConfigurationRevision.status).where(TaskConfigurationRevision.id == revision.id)).scalar()
    if old_status in {"baseline", "published"}:
        raise ValueError("Versoes publicadas sao imutaveis; crie uma nova revisao.")


def task_snapshot(task):
    try:
        return json.loads(task.configuration_snapshot or "{}")
    except (ValueError, TypeError):
        return {}


def stamp_task(task, legacy=False, revision=None):
    if task.configuration_snapshot:
        return False
    workflow = task.lead_workflow or db.session.get(LeadWorkflow, task.lead_workflow_id)
    lead = workflow.lead
    revision = revision or (effective_revision(lead.company_id, lead.franchise_id) if lead.company_id and lead.franchise_id else None)
    if not revision:
        return False
    if isinstance(task, LeadWorkflowScheduledTask):
        snapshot = {"key": task.task_key, "label": task.label, "scheduled_for": task.scheduled_for.isoformat(),
                    "stage_id": task.stage_id, "required": True, "kind": "scheduled"}
    else:
        item = task.checklist_item or db.session.get(WorkflowChecklistItem, task.checklist_item_id)
        snapshot = {"key": f"checklist:{item.id}", "label": item.label, "description": item.description,
                    "stage_id": item.stage_id, "required": item.required, "sort_order": item.sort_order,
                    "due_at": workflow.due_at.isoformat() if workflow.due_at else None, "kind": "checklist"}
        definitions = json.loads(revision.content)["checklists"]
        definition = next((row for row in definitions if row.get("id") == item.id), {})
        dependency = next((row for row in definitions if row["key"] == definition.get("depends_on")), {})
        snapshot["depends_on_id"] = dependency.get("id")
    snapshot.update(note_required=True, origin="legacy_baseline" if legacy else "generated", schema_version=1)
    task.configuration_revision_id = revision.id
    task.configuration_snapshot = encode(snapshot)
    # Adding provenance must not change the last operational modification time.
    if legacy and task.id and task.updated_at:
        flag_modified(task, "updated_at")
    return True


def add_configured_task(task, revision=None):
    with db.session.no_autoflush:
        stamp_task(task, revision=revision)
    db.session.add(task)
    return task


def preserve_existing_tasks():
    count = 0
    for model in (LeadWorkflowScheduledTask, LeadWorkflowChecklistCheck):
        for task in model.query.filter(model.configuration_snapshot.is_(None)).all():
            count += bool(stamp_task(task, legacy=True))
    return count


def checklist_item_view(check, item):
    snapshot = task_snapshot(check) if check else {}
    if not snapshot:
        return item
    return SimpleNamespace(id=item.id, stage_id=item.stage_id, stage=item.stage,
                           label=snapshot["label"], required=snapshot["required"], active=True,
                           description=snapshot.get("description"), sort_order=snapshot.get("sort_order", 0),
                           depends_on_id=snapshot.get("depends_on_id"))


def contact_agenda_revision(tracking):
    """Pin existing agendas; publication alone never replans an existing contract."""
    plan = db.session.get(ContactAgendaConfiguration, tracking.id)
    if plan:
        return db.session.get(TaskConfigurationRevision, plan.revision_id), plan.effective_from
    task = (LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id)
            .filter(LeadWorkflowScheduledTask.task_key.like("unlock-contact:%"),
                    LeadWorkflowScheduledTask.configuration_revision_id.is_not(None))
            .order_by(LeadWorkflowScheduledTask.id).first())
    lead = tracking.lead
    revision = (db.session.get(TaskConfigurationRevision, task.configuration_revision_id) if task else
                effective_revision(lead.company_id, lead.franchise_id))
    if revision:
        db.session.add(ContactAgendaConfiguration(lead_workflow_id=tracking.id, revision_id=revision.id))
        db.session.flush()
    return revision, None

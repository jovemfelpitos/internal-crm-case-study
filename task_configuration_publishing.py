"""Publication and explicit, audited contact replanning. Callers own the transaction."""

import hashlib
import json
from datetime import datetime

from sqlalchemy import text, update

from models import (ContactAgendaConfiguration, Lead, LeadWorkflow, LeadWorkflowScheduledTask,
                    Role, TaskConfigurationRevision, WorkflowChecklistItem, WorkflowStage, db)
from task_configuration import (add_configured_task, audit, current_revision,
                                effective_revision, encode, matrix_can_configure)
from task_configuration_editor import (DraftConflict, editable_draft, editor_scope,
                                       validate_dependencies)
from task_schedule import preview_replan, schedule_configuration


def can_publish(actor):
    return bool(actor and actor.role == Role.ADMIN and matrix_can_configure(actor, actor.company_id))


def require_publisher(actor):
    if not can_publish(actor):
        raise PermissionError("Somente ADMIN da matriz pode publicar ou aplicar replanejamento.")


def lock_scope(scope):
    # Serializes edits/publications on SQLite, without changing operational timestamps.
    db.session.execute(text("UPDATE task_configuration_scopes SET id=id WHERE id=:id"), {"id": scope.id})
    db.session.expire_all()


def publication_errors(content, base_content, seller_tasks):
    errors = []
    try:
        validate_dependencies(content["checklists"])
    except ValueError as exc:
        errors.append(str(exc))
    stages = {s["id"]: s for s in content["stages"]}
    flows = {w["id"]: w for w in content["workflows"]}
    items = {item["key"]: item for item in content["checklists"]}
    for item in base_content["checklists"]:
        stage = stages[item["stage_id"]]
        flow = flows[stage["workflow_id"]]
        if stage["active"] and flow["active"] and flow["slug"] != "30-dias" and item["active"] and item["required"]:
            candidate = items.get(item["key"], {})
            if not candidate.get("active") or not candidate.get("required"):
                errors.append(f'Tarefa essencial: mantenha "{item["label"]}" ativa e obrigatoria.')
    rules = schedule_configuration(content)
    for rule in rules["inss"]:
        if not rule["active"]:
            errors.append(f'INSS essencial: reative "{rule["label"]}" antes de publicar.')
    if not any(rule["active"] for rule in rules["contacts"].values()):
        errors.append("Ative ao menos uma janela de contatos antes de publicar.")
    for task in seller_tasks.values():
        if not content.get("conditional_task_active", {}).get(task["key"], True):
            errors.append(f'Execucao essencial: reative "{task["label"]}" antes de publicar.')
    return errors


def publish_draft(actor, revision_id, fingerprint, seller_tasks, capture_checklist, pin_agenda):
    require_publisher(actor)
    scope = editor_scope(actor)
    lock_scope(scope)
    require_publisher(actor)
    draft = editable_draft(actor, revision_id, fingerprint)
    base = current_revision(scope)
    if not base or draft.base_revision_id != base.id:
        raise DraftConflict("A versao em uso mudou. Concilie o rascunho antes de publicar.")
    content = json.loads(draft.content)
    errors = publication_errors(content, json.loads(base.content), seller_tasks)
    if errors:
        raise ValueError("Publicacao bloqueada: " + " ".join(errors))
    # Snapshot the current concrete stage/agenda BEFORE updating reusable definitions.
    trackings = (LeadWorkflow.query.join(Lead).filter(Lead.company_id == scope.company_id,
                 Lead.deleted_at.is_(None), LeadWorkflow.status.notin_(("concluido", "recusado", "inativo"))).all())
    for tracking in trackings:
        if tracking.workflow.slug == "30-dias":
            pin_agenda(tracking)
        else:
            capture_checklist(tracking)
    new_keys = {}
    for definition in content["checklists"]:
        item = db.session.get(WorkflowChecklistItem, definition["id"]) if definition.get("id") else None
        if item and (item.stage.workflow.company_id != scope.company_id or item.stage_id != definition["stage_id"]):
            raise PermissionError("Modelo fora do escopo da publicacao.")
        if not item:
            item = WorkflowChecklistItem(stage_id=definition["stage_id"])
            db.session.add(item)
        for name in ("label", "description", "sort_order", "required", "active"):
            setattr(item, name, definition[name])
        db.session.flush()
        new_keys[definition["key"]] = f"checklist:{item.id}"
        definition["id"] = item.id
    for definition in content["checklists"]:
        definition["key"] = new_keys[definition["key"]]
        if definition.get("depends_on"):
            definition["depends_on"] = new_keys[definition["depends_on"]]
    # Publish once, atomically replacing draft UUIDs with concrete checklist IDs.
    payload = encode(content)
    changed = db.session.execute(update(TaskConfigurationRevision).where(TaskConfigurationRevision.id == draft.id,
                       TaskConfigurationRevision.status == "draft", TaskConfigurationRevision.fingerprint == fingerprint)
                       .values(status="published", content=payload,
                               fingerprint=hashlib.sha256(payload.encode()).hexdigest())
                       .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DraftConflict("O rascunho mudou durante a publicacao. Gere uma nova revisao.")
    db.session.expire(draft)
    audit(scope, actor, "task_configuration_published", {"draft_id": draft.id, "revision_id": draft.id,
          "previous_id": base.id, "existing_tasks_preserved": True})
    return draft


def replan_state(actor, revision, tracking, effective_from, today):
    lead = tracking.lead
    if lead.deleted_at or tracking.status in {"concluido", "recusado", "inativo"} or not lead.concessao or not tracking.fidelity_started_on:
        raise ValueError("Contrato sem agenda de fidelizacao ativa ou sem datas validas.")
    if revision.scope.company_id != lead.company_id or lead.company_id != actor.company_id:
        raise PermissionError("Contrato fora do escopo da configuracao.")
    if lead.franchise_id != actor.franchise_id and not actor.can_view_all_franchises:
        raise PermissionError("Sem acesso a contratos desta franquia.")
    tasks = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).order_by(LeadWorkflowScheduledTask.id).all()
    result = preview_replan(json.loads(revision.content), lead.concessao, tracking.fidelity_started_on,
                            tasks, today, effective_from)
    current = effective_revision(lead.company_id, lead.franchise_id)
    result["can_apply"] = bool(can_publish(actor) and current and revision.id == current.id and revision.status != "draft")
    # Bind the confirmation to the exact client, version, dates and all task evidence.
    evidence = [{field: getattr(task, field) for field in
                ("id", "task_key", "label", "active", "scheduled_for", "completed_at", "completed_by_id",
                 "note", "updated_at", "configuration_revision_id", "configuration_snapshot")} for task in tasks]
    plan = db.session.get(ContactAgendaConfiguration, tracking.id)
    payload = {"revision": revision.fingerprint, "revision_id": revision.id, "lead_id": lead.id,
               "franchise_id": lead.franchise_id, "stage_id": tracking.current_stage_id, "status": tracking.status,
               "concession": lead.concessao, "entry": tracking.fidelity_started_on, "today": today,
               "effective_from": effective_from, "tasks": evidence,
               "plan": [plan.revision_id, plan.effective_from] if plan else None}
    result["confirmation"] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return result


def apply_replan(actor, revision, tracking, effective_from, confirmation, today):
    require_publisher(actor)
    lock_scope(editor_scope(actor))
    require_publisher(actor)
    result = replan_state(actor, revision, tracking, effective_from, today)
    if not result["can_apply"]:
        raise ValueError("Publique a configuracao e simule sua versao em uso antes de aplicar.")
    if not confirmation or confirmation != result["confirmation"]:
        raise DraftConflict("A agenda ou a versao mudou desde a previa. Gere uma nova previa antes de aplicar.")
    stages = {stage.slug: stage for stage in WorkflowStage.query.filter_by(workflow_id=tracking.workflow_id, active=True).all()}
    created, deactivated = [], []
    for row in result["rows"]:
        if row["action"] == "deactivate":
            task = db.session.get(LeadWorkflowScheduledTask, row["task_id"])
            task.active = False
            deactivated.append(task.id)
        elif row["action"] == "create":
            stage = stages.get(row["key"].split(":")[1])
            if not stage:
                raise ValueError("Uma janela da agenda nao esta ativa. Revise as etapas antes de aplicar.")
            day = datetime.strptime(row["date"], "%Y-%m-%d").date()
            task = add_configured_task(LeadWorkflowScheduledTask(
                lead_workflow_id=tracking.id, lead_id=tracking.lead_id, workflow_id=tracking.workflow_id,
                stage_id=stage.id, task_key=row["key"], label=f'{row["label"]} - {day:%d/%m/%Y}',
                scheduled_for=day, active=True), revision=revision)
            db.session.flush()
            created.append(task.id)
    plan = db.session.get(ContactAgendaConfiguration, tracking.id)
    if not plan:
        plan = ContactAgendaConfiguration(lead_workflow_id=tracking.id)
        db.session.add(plan)
    plan.revision_id, plan.effective_from = revision.id, effective_from
    audit(revision.scope, actor, "task_configuration_agenda_replanned",
          {"lead_id": tracking.lead_id, "lead_workflow_id": tracking.id, "revision_id": revision.id,
           "effective_from": effective_from.isoformat(), "created": created, "deactivated": deactivated,
           "confirmation": confirmation})
    return result

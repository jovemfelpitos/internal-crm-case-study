"""Draft-only task editing. No operational models are changed by this module."""

import copy
import hashlib
import json
import unicodedata
import uuid

from sqlalchemy import update

from models import TaskConfigurationRevision, db
from task_configuration import (assert_scope_editor, audit, company_scope, create_draft,
                                current_revision, encode, matrix_can_configure)
from task_schedule import schedule_configuration


class DraftConflict(ValueError):
    pass


def latest_draft(scope):
    return (TaskConfigurationRevision.query.filter_by(scope_id=scope.id, status="draft")
            .order_by(TaskConfigurationRevision.number.desc()).first())


def editor_scope(actor):
    scope = company_scope(actor.company_id)
    if not scope or not matrix_can_configure(actor, actor.company_id):
        raise PermissionError("O painel de tarefas esta disponivel para a matriz.")
    return scope


def start_draft(actor, base_fingerprint):
    scope = editor_scope(actor)
    existing = latest_draft(scope)
    if existing:
        return existing
    base = current_revision(scope)
    if not base or base.fingerprint != base_fingerprint:
        raise DraftConflict("A configuracao em uso mudou. Atualize a pagina antes de criar o rascunho.")
    return create_draft(actor, scope.id)


def normalized_label(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value)
                             if not unicodedata.combining(c)).casefold().split())


def validate_dependencies(items):
    by_key = {item["key"]: item for item in items}
    if len(by_key) != len(items):
        raise ValueError("Identificadores de tarefas duplicados.")
    for item in items:
        parent = by_key.get(item.get("depends_on"))
        if item.get("depends_on") and not parent:
            raise ValueError("A tarefa anterior nao existe neste rascunho.")
        if parent and parent["stage_id"] != item["stage_id"]:
            raise ValueError("A dependencia deve pertencer a mesma etapa.")
        if parent and item["active"] and not parent["active"]:
            raise ValueError("Uma tarefa ativa nao pode depender de uma tarefa inativa.")
    for item in items:
        seen = set()
        current = item
        while current:
            if current["key"] in seen:
                raise ValueError("Dependencia circular: uma tarefa nao pode depender de si mesma, direta ou indiretamente.")
            seen.add(current["key"])
            current = by_key.get(current.get("depends_on"))


def editable_draft(actor, revision_id, fingerprint):
    scope = editor_scope(actor)
    revision = db.session.get(TaskConfigurationRevision, revision_id)
    if not revision or revision.scope_id != scope.id:
        raise PermissionError("Rascunho indisponivel neste escopo.")
    assert_scope_editor(actor, revision.scope)
    if revision.status != "draft":
        raise ValueError("A configuracao em uso nao pode ser editada. Crie um rascunho.")
    if revision.fingerprint != fingerprint:
        raise DraftConflict("Outra pessoa alterou este rascunho. Seu formulario foi mantido; consulte a versao atual em outra aba antes de tentar novamente.")
    return revision


def write_draft(actor, revision, fingerprint, content, action, details):
    payload = encode(content)
    new_fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    result = db.session.execute(update(TaskConfigurationRevision).where(
        TaskConfigurationRevision.id == revision.id,
        TaskConfigurationRevision.scope_id == revision.scope_id,
        TaskConfigurationRevision.status == "draft",
        TaskConfigurationRevision.fingerprint == fingerprint,
    ).values(content=payload, fingerprint=new_fingerprint).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise DraftConflict("O rascunho mudou durante a gravacao. Atualize antes de tentar novamente.")
    if new_fingerprint != fingerprint:
        audit(revision.scope, actor, action, {"revision_id": revision.id, **details})
    db.session.expire(revision)
    return revision


def save_schedule_rule(actor, revision, fingerprint, key, values):
    content = json.loads(revision.content)
    schedule = schedule_configuration(content)
    contact = key.startswith("contact:")
    original = schedule["contacts"].get(key.removeprefix("contact:")) if contact else next(
        (item for item in schedule["inss"] if item["key"] == key), None)
    if not original:
        raise ValueError("Regra automatica nao encontrada.")
    allowed = {"label", "description", "active", "contacts_per_week" if contact else "delay_business_days"}
    if set(values) - allowed:
        raise ValueError("Janelas, obrigatoriedade, ordem e dependencias automaticas permanecem protegidas.")
    label, description = values.get("label"), values.get("description", "")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 160:
        raise ValueError("Informe um nome de 1 a 160 caracteres.")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("A orientacao deve ter no maximo 2000 caracteres.")
    if type(values.get("active")) is not bool:
        raise ValueError("Informe se a regra esta ativa.")
    before = copy.deepcopy(original)
    if contact:
        count = values.get("contacts_per_week")
        if "contacts_per_week" not in values or (count is not None and (type(count) is not int or not 1 <= count <= 5)):
            raise ValueError("Escolha de 1 a 5 contatos por semana, ou todos os dias uteis.")
        original["contacts_per_week"] = count
    else:
        delay = values.get("delay_business_days")
        if type(delay) is not int or not 0 <= delay <= 30:
            raise ValueError("O prazo deve ser de 0 a 30 dias uteis apos a liberacao.")
        original["delay_business_days"] = delay
    original.update(label=label.strip(), description=description.strip(), active=values["active"])
    by_key = {item["key"]: item for item in schedule["inss"]}
    for item in schedule["inss"]:
        parent = by_key.get(item["depends_on"])
        if item["active"] and parent and not parent["active"]:
            raise ValueError("Ative primeiro a tarefa anterior do INSS, ou inative as tarefas seguintes.")
    content["schedule"] = schedule
    return write_draft(actor, revision, fingerprint, content, "task_configuration_draft_schedule_saved",
                       {"key": key, "before": before, "after": original})


def deactivate_all_draft_tasks(actor, revision_id, fingerprint, seller_tasks):
    revision = editable_draft(actor, revision_id, fingerprint)
    content = json.loads(revision.content)
    schedule = schedule_configuration(content)
    changed_keys = []
    for item in content["checklists"]:
        if item["active"]:
            changed_keys.append(item["key"])
        item["active"] = False
    for slug, item in schedule["contacts"].items():
        if item["active"]:
            changed_keys.append("contact:"+slug)
        item["active"] = False
    for item in schedule["inss"]:
        if item["active"]:
            changed_keys.append(item["key"])
        item["active"] = False
    conditional = content.setdefault("conditional_task_active", {})
    for item in seller_tasks.values():
        if conditional.get(item["key"], True):
            changed_keys.append(item["key"])
        conditional[item["key"]] = False
    content["schedule"] = schedule
    return write_draft(actor, revision, fingerprint, content, "task_configuration_draft_tasks_disabled",
                       {"disabled_keys": changed_keys, "operational_effect": False})


def save_task(actor, revision_id, fingerprint, key, values):
    revision = editable_draft(actor, revision_id, fingerprint)
    if not isinstance(values, dict):
        raise ValueError("Dados da tarefa invalidos.")
    if isinstance(key, str) and key.startswith(("inss:", "contact:")):
        return save_schedule_rule(actor, revision, fingerprint, key, values)
    if key in {"seller_unlock_execution", "seller_anuencia_execution", "seller_post_sale_execution"}:
        if set(values) != {"active"} or type(values["active"]) is not bool:
            raise ValueError("Esta tarefa permite apenas alterar sua ativacao no rascunho.")
        content = json.loads(revision.content)
        content.setdefault("conditional_task_active", {})[key] = values["active"]
        return write_draft(actor, revision, fingerprint, content, "task_configuration_draft_conditional_saved",
                           {"key": key, "active": values["active"]})
    allowed = {"label", "description", "stage_id", "required", "active", "sort_order", "depends_on"}
    if set(values) - allowed:
        raise ValueError("Esta edicao permite somente os campos do modelo de tarefa.")
    content = json.loads(revision.content)
    items = content["checklists"]
    original = next((item for item in items if item["key"] == key), None) if key else None
    if key and not original:
        raise ValueError("Tarefa nao encontrada ou gerenciada por uma regra automatica.")
    stage_id = values.get("stage_id")
    stage = next((stage for stage in content["stages"] if stage["id"] == stage_id), None)
    workflow = next((w for w in content["workflows"] if stage and w["id"] == stage["workflow_id"]), None)
    if type(stage_id) is not int or not stage or not workflow:
        raise ValueError("Selecione uma etapa valida neste escopo.")
    if original and stage_id != original["stage_id"]:
        raise ValueError("Uma tarefa existente nao pode ser transferida de etapa.")
    if original and (not stage["active"] or not workflow["active"] or workflow["slug"] == "30-dias"):
        raise ValueError("Modelo legado ou gerenciado pela agenda: disponivel apenas para consulta.")
    if not original and (not stage["active"] or not workflow["active"] or workflow["slug"] == "30-dias"):
        raise ValueError("Nesta etapa as tarefas sao geradas pela agenda. Escolha uma etapa ativa com checklist.")
    label = values.get("label")
    description = values.get("description", "")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 160:
        raise ValueError("Informe um nome de 1 a 160 caracteres.")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("A orientacao deve ter no maximo 2000 caracteres.")
    for name in ("required", "active"):
        if type(values.get(name)) is not bool:
            raise ValueError("Obrigatoriedade e ativacao devem ser informadas.")
    if type(values.get("sort_order")) is not int or not 0 <= values["sort_order"] <= 999:
        raise ValueError("A ordem deve ser um numero inteiro entre 0 e 999.")
    dependency = values.get("depends_on") or None
    if dependency is not None and not isinstance(dependency, str):
        raise ValueError("Dependencia invalida.")
    if any(item["key"] != key and item["stage_id"] == stage_id
           and normalized_label(item["label"]) == normalized_label(label) for item in items):
        raise ValueError("Ja existe uma tarefa com este nome nesta etapa.")
    before = copy.deepcopy(original)
    candidate = original or {"key": "draft:" + uuid.uuid4().hex, "id": None, "stage_id": stage_id}
    candidate.update(label=label.strip(), description=description.strip(), required=values["required"],
                     active=values["active"], sort_order=values["sort_order"], depends_on=dependency)
    if not original:
        items.append(candidate)
    validate_dependencies(items)
    return write_draft(actor, revision, fingerprint, content, "task_configuration_draft_task_saved",
                       {"before": before, "after": candidate})


def panel_state(actor, seller_tasks):
    scope = editor_scope(actor)
    active = current_revision(scope)
    draft = latest_draft(scope)
    revision = draft or active
    if not revision:
        raise ValueError("Nenhuma configuracao disponivel. Inicialize os fluxos primeiro.")
    content = json.loads(revision.content)
    base = db.session.get(TaskConfigurationRevision, draft.base_revision_id) if draft else active
    base_content = json.loads(base.content) if base else content
    base_items = {item["key"]: item for item in base_content["checklists"]}
    schedule = schedule_configuration(content)
    base_schedule = schedule_configuration(base_content)
    stages = {stage["id"]: stage for stage in content["stages"]}
    workflows = {workflow["id"]: workflow for workflow in content["workflows"]}
    rows = []
    for item in content["checklists"]:
        stage = stages[item["stage_id"]]
        workflow = workflows[stage["workflow_id"]]
        archived = not stage["active"] or not workflow["active"] or workflow["slug"] == "30-dias"
        previous = base_items.get(item["key"])
        fields = ("label", "description", "required", "active", "sort_order", "depends_on")
        changed = not previous or any((previous.get(f) or None) != (item.get(f) or None) for f in fields)
        rows.append(dict(item, kind="checklist", kind_label="Tarefa de etapa", workflow_id=workflow["id"],
                         workflow=workflow["name"], stage=stage["name"], archived=archived,
                         editable=not archived, changed=bool(draft and changed),
                         change="Nova" if not previous else "Alterada", previous=previous,
                         trigger="Conclusao da tarefa anterior" if item.get("depends_on") else "Entrada na etapa",
                         timing="Prazo da etapa", responsible="Dono do contrato / apoio definido"))
    fidelity = next((w for w in workflows.values() if w["slug"] == "30-dias"), None)
    if fidelity:
        for index, rule in enumerate(schedule["inss"]):
            previous = next(item for item in base_schedule["inss"] if item["key"] == rule["key"])
            rows.append(dict(rule, kind="inss", kind_label="Sequencia INSS", workflow_id=fidelity["id"],
                             workflow=fidelity["name"], stage="Todas as janelas", stage_id=None, required=True,
                             archived=False, editable=True, changed=bool(draft and rule != previous), previous=previous,
                             change="Alterada", sort_order=index, responsible="Dono do contrato",
                             trigger="Entrada na fidelizacao" if index == 0 else "Conclusao da tarefa anterior",
                             timing=f'{rule["delay_business_days"]} dia(s) util(eis) apos liberacao'))
        for slug, rule in sorted(schedule["contacts"].items(), key=lambda pair: -pair[1]["window_ceiling_days"]):
            stage = next((s for s in stages.values() if s["workflow_id"] == fidelity["id"] and s["slug"] == slug), None)
            count = rule["contacts_per_week"]
            previous = base_schedule["contacts"][slug]
            rows.append(dict(rule, key="contact:"+slug, kind="contact", kind_label="Contato recorrente",
                             workflow_id=fidelity["id"], workflow=fidelity["name"], stage=stage["name"] if stage else slug,
                             stage_id=stage["id"] if stage else None, required=True, archived=False, editable=True,
                             changed=bool(draft and rule != previous), previous=previous, change="Alterada",
                             trigger="Agenda de dias uteis", responsible="Dono do contrato",
                             timing=f"{count} contato(s) por semana" if count else "Todos os dias uteis"))
    for (flow_slug, stage_slug), task in seller_tasks.items():
        workflow = next((w for w in workflows.values() if w["slug"] == flow_slug), None)
        stage = next((s for s in stages.values() if workflow and s["workflow_id"] == workflow["id"] and s["slug"] == stage_slug), None)
        if not stage:
            continue
        enabled = content.get("conditional_task_active", {}).get(task["key"], True)
        previous_enabled = base_content.get("conditional_task_active", {}).get(task["key"], True)
        rows.append(dict(key=task["key"], label=task["label"], kind="conditional", kind_label="Escolha do responsavel",
                         workflow_id=workflow["id"], workflow=workflow["name"], stage=stage["name"], stage_id=stage["id"],
                         active=enabled, required=True, archived=False, editable=True,
                         changed=bool(draft and enabled != previous_enabled), change="Alterada",
                         previous={"label": task["label"], "active": previous_enabled, "required": True},
                         trigger="Consultor assume a acao", responsible="Dono do contrato", timing="Prazo da etapa",
                         description="Gerada quando o consultor assume esta acao; nao substitui o checklist da etapa."))
    rows.sort(key=lambda row: (row["archived"], workflows[row["workflow_id"]]["sort_order"],
                              stages.get(row["stage_id"], {}).get("sort_order", -1),
                              row.get("sort_order", 0), row["key"]))
    return {"revision_id": revision.id, "fingerprint": revision.fingerprint, "number": revision.number,
            "is_draft": bool(draft), "published_number": active.number if active else None,
            "base_number": base.number if base else None,
            "automatic_inactive": sum(not row["active"] for row in rows if row["kind"] in {"inss", "contact"}),
            "stale": bool(draft and active and draft.base_revision_id != active.id),
            "rows": rows, "workflows": list(workflows.values()), "stages": list(stages.values())}

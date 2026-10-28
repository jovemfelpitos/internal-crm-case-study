import json
import os
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-task-configuration-only"

import app as crm
import task_configuration as config
from models import (AuditLog, Company, ConfigurationMigration, Franchise, Lead, LeadWorkflow,
                    LeadWorkflowChecklistCheck, LeadWorkflowScheduledTask, Role,
                    TaskConfigurationRevision, TaskConfigurationScope, User, WorkflowChecklistItem, db)


class TaskConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.company, self.matrix = crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.admin = User(username="matrix", name="Matrix", company_id=self.company.id,
                          franchise_id=self.matrix.id, active=True, role=Role.ADMIN)
        self.admin.apply_role_defaults()
        self.branch = Franchise(company_id=self.company.id, name="Branch B", active=True)
        db.session.add_all([self.admin, self.branch])
        db.session.flush()
        self.branch_admin = User(username="branch-admin", name="Branch", company_id=self.company.id,
                                 franchise_id=self.branch.id, active=True, role=Role.ADMIN)
        self.branch_admin.apply_role_defaults()
        self.branch_admin.can_view_all_franchises = True
        db.session.add(self.branch_admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def make_workflow(self, actor=None, cpf="00000000001", slug="dia-do-desbloqueio"):
        actor = actor or self.admin
        lead = Lead(nome_cliente="QA", cpf=cpf, company_id=actor.company_id,
                    franchise_id=actor.franchise_id, consultor=actor.name, equipe="QA",
                    status="Em andamento", concessao=datetime.now().date()-timedelta(days=45))
        db.session.add(lead)
        db.session.flush()
        workflow, stage = crm.workflow_stage_by_slug("principal", slug)
        instance = LeadWorkflow(lead_id=lead.id, workflow_id=workflow.id, current_stage=stage,
                                status="em_andamento", owner_user_id=actor.id,
                                due_at=datetime.now()+timedelta(hours=24))
        db.session.add(instance)
        db.session.flush()
        return instance

    def test_baseline_and_bootstrap_are_idempotent(self):
        first = config.current_revision(config.company_scope(self.company.id))
        self.assertEqual(first.status, "baseline")
        self.assertEqual(json.loads(first.content)["unlock_total_days"], 90)
        revisions = TaskConfigurationRevision.query.count()
        audits = AuditLog.query.filter_by(action="task_configuration_revision_created").count()
        crm.seed_default_workflows()
        crm.migrate_sqlite_schema()
        crm.seed_default_workflows()
        self.assertEqual(TaskConfigurationRevision.query.count(), revisions)
        self.assertEqual(AuditLog.query.filter_by(action="task_configuration_revision_created").count(), audits)

    def test_restart_preserves_admin_rules_and_disabled_defaults(self):
        flow, stage = crm.workflow_stage_by_slug("30-dias", "60-dias")
        flow.name = "Relacionamento personalizado"
        stage.name = "Janela personalizada"
        stage.first_action_hours = None
        stage.requires_note = True
        _, support_stage = crm.workflow_stage_by_slug("principal", "anuencia")
        support_stage.requires_support = False
        support_stage.support_sla_hours = None
        support_stage.active = False
        db.session.commit()
        crm.seed_default_workflows()
        self.assertEqual(flow.name, "Relacionamento personalizado")
        self.assertEqual(stage.name, "Janela personalizada")
        self.assertTrue(stage.requires_note)
        self.assertIsNone(stage.first_action_hours)
        self.assertFalse(support_stage.requires_support)
        self.assertIsNone(support_stage.support_sla_hours)
        self.assertFalse(support_stage.active)

    def test_adopts_already_unified_installation_without_resetting_rules(self):
        db.session.delete(db.session.get(ConfigurationMigration, "unified_fidelity_configuration_v1"))
        flow, stage = crm.workflow_stage_by_slug("30-dias", "60-dias")
        stage.name = "Nome ja personalizado"
        stage.requires_checklist = True
        db.session.commit()
        crm.configure_unified_fidelity()
        db.session.commit()
        self.assertEqual(stage.name, "Nome ja personalizado")
        self.assertTrue(stage.requires_checklist)

    def test_published_revision_cannot_be_edited_or_deleted(self):
        revision = config.current_revision(config.company_scope(self.company.id))
        revision.content = "{}"
        with self.assertRaises(ValueError):
            db.session.flush()
        db.session.rollback()
        revision = config.current_revision(config.company_scope(self.company.id))
        db.session.delete(revision)
        with self.assertRaises(ValueError):
            db.session.flush()
        db.session.rollback()

    def test_franchise_inherits_and_requires_separate_authorization(self):
        base = config.current_revision(config.company_scope(self.company.id))
        self.assertEqual(config.effective_revision(self.company.id, self.branch.id).id, base.id)
        with self.assertRaises(ValueError):
            config.create_draft(self.branch_admin, base.scope_id)
        with self.assertRaises(ValueError):
            config.authorize_franchise_customization(self.branch_admin, self.branch.id, True)
        scope = config.authorize_franchise_customization(self.admin, self.branch.id, True)
        draft = config.create_draft(self.branch_admin, scope.id)
        db.session.commit()
        self.assertEqual(draft.status, "draft")
        self.assertEqual(draft.base_revision_id, base.id)
        self.assertEqual(config.effective_revision(self.company.id, self.branch.id).id, base.id)
        config.authorize_franchise_customization(self.admin, self.branch.id, False)
        with self.assertRaises(ValueError):
            config.create_draft(self.branch_admin, scope.id)

    def test_company_and_other_franchise_are_isolated(self):
        other = Franchise(company_id=self.company.id, name="Branch C", active=True)
        company = Company(name="Another company", slug="other", active=True)
        db.session.add_all([other, company]); db.session.flush()
        foreign = Franchise(company_id=company.id, name="Matriz", active=True)
        db.session.add(foreign); db.session.flush()
        scope = config.authorize_franchise_customization(self.admin, other.id, True)
        with self.assertRaises(ValueError):
            config.create_draft(self.branch_admin, scope.id)
        with self.assertRaises(ValueError):
            config.effective_revision(self.company.id, foreign.id)
        with self.assertRaises(ValueError):
            config.authorize_franchise_customization(self.admin, foreign.id, True)
        self.branch_admin.active = False
        own = config.authorize_franchise_customization(self.admin, self.branch.id, True)
        with self.assertRaises(ValueError):
            config.create_draft(self.branch_admin, own.id)

    def test_old_global_routes_reject_franchise_admin_even_with_view_all(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.branch_admin.id
            session["_csrf_token"] = "qa"
        self.assertEqual(client.get("/admin/workflows").status_code, 403)
        response = client.post("/admin/workflows/create", data={"_csrf_token":"qa", "name":"Unauthorized", "slug":"unauthorized"})
        self.assertEqual(response.status_code, 403)
        with client.session_transaction() as session:
            session["user_id"] = self.admin.id
        self.assertEqual(client.get("/admin/workflows").status_code, 200)

    def test_pending_checklist_keeps_label_requirement_and_deadline(self):
        workflow = self.make_workflow()
        state = crm.workflow_checklist_state(workflow)
        self.assertTrue(state)
        item, due = state[0]["item"], state[0]["due_at"]
        original_label = item.label
        check = state[0]["check"]
        original_revision = check.configuration_revision_id
        definition = db.session.get(WorkflowChecklistItem, item.id)
        definition.label = "Novo nome"
        definition.required = False
        definition.active = False
        workflow.due_at += timedelta(days=5)
        crm.log_audit(None, self.admin, "workflow_checklist_update")
        db.session.commit()
        state = crm.workflow_checklist_state(workflow)
        self.assertEqual(state[0]["item"].label, original_label)
        self.assertTrue(state[0]["item"].required)
        self.assertEqual(state[0]["due_at"], due)
        self.assertEqual(check.configuration_revision_id, original_revision)
        self.assertNotEqual(config.current_revision(config.company_scope(self.company.id)).id, original_revision)
        crm.complete_lead_workflow_checklist_item(workflow, item.id, self.admin, "Conferido com o cliente.")
        db.session.commit()
        history = crm.lead_operational_history(workflow.lead)
        self.assertIn(original_label, [entry["subtitle"] for entry in history if entry["kind"] == "checklist"])

    def test_new_tasks_use_new_revision_while_same_cpf_is_scoped(self):
        first = self.make_workflow()
        state = crm.workflow_checklist_state(first)
        definition = db.session.get(WorkflowChecklistItem, state[0]["item"].id)
        revision_id = state[0]["check"].configuration_revision_id
        definition.label = "Tarefa revisada"
        crm.log_audit(None, self.admin, "workflow_checklist_update")
        db.session.commit()
        second = self.make_workflow(actor=self.branch_admin)
        newer = crm.workflow_checklist_state(second)
        self.assertEqual(newer[0]["item"].label, "Tarefa revisada")
        self.assertNotEqual(newer[0]["check"].configuration_revision_id, revision_id)
        self.assertEqual(crm.workflow_checklist_state(first)[0]["check"].configuration_revision_id, revision_id)

    def test_scheduled_tasks_are_stamped_and_completion_keeps_origin(self):
        principal = self.make_workflow(slug="fidelizacao")
        tracking, _ = crm.ensure_temporary_workflow_for_principal(principal)
        db.session.flush()
        task = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id, task_key="inss:fazer").one()
        snapshot = task.configuration_snapshot
        self.assertTrue(task.configuration_revision_id)
        crm.complete_scheduled_contact_task(tracking, task.id, self.admin, "Executado e confirmado.")
        db.session.commit()
        self.assertEqual(task.configuration_snapshot, snapshot)
        self.assertEqual(json.loads(snapshot)["label"], "Fazer INSS")
        count = LeadWorkflowScheduledTask.query.count()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking)
        db.session.flush()
        self.assertEqual(LeadWorkflowScheduledTask.query.count(), count)

    def test_partial_legacy_checklist_does_not_hide_remaining_tasks(self):
        workflow = self.make_workflow()
        item = crm.checklist_items_for_stage(workflow.current_stage)[0]
        extra = WorkflowChecklistItem(stage_id=workflow.current_stage_id, label="Outra tarefa", required=True, active=True)
        check = LeadWorkflowChecklistCheck(lead_workflow_id=workflow.id, checklist_item_id=item.id,
                                          checked=True, checked_at=datetime.now(), note="Legado")
        db.session.add_all([extra, check]); db.session.commit()
        config.preserve_existing_tasks(); db.session.flush()
        state = crm.workflow_checklist_state(workflow)
        self.assertEqual(len(state), 2)
        self.assertEqual([row["item"].label for row in state if not row["checked"]], ["Outra tarefa"])

    def test_adding_rule_does_not_add_task_to_already_captured_stage(self):
        workflow = self.make_workflow()
        before = crm.workflow_checklist_state(workflow)
        db.session.add(WorkflowChecklistItem(stage_id=workflow.current_stage_id, label="Nova regra", required=True, active=True))
        crm.log_audit(None, self.admin, "workflow_checklist_create")
        db.session.commit()
        self.assertEqual(len(crm.workflow_checklist_state(workflow)), len(before))
        other = self.make_workflow(cpf="00000000002")
        self.assertEqual(len(crm.workflow_checklist_state(other)), len(before)+1)

    def test_backfill_adds_provenance_without_changing_operational_columns(self):
        principal = self.make_workflow(slug="fidelizacao")
        tracking, _ = crm.ensure_temporary_workflow_for_principal(principal)
        db.session.flush()
        task = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).first()
        task.configuration_snapshot = None
        task.configuration_revision_id = None
        task.updated_at = datetime(2026, 1, 1, 10, 30)
        db.session.commit()
        expected = (task.label, task.scheduled_for, task.note, task.completed_at, task.updated_at)
        config.preserve_existing_tasks()
        db.session.commit()
        self.assertEqual((task.label, task.scheduled_for, task.note, task.completed_at, task.updated_at), expected)
        self.assertEqual(json.loads(task.configuration_snapshot)["origin"], "legacy_baseline")
        self.assertEqual(config.preserve_existing_tasks(), 0)


if __name__ == "__main__":
    unittest.main()

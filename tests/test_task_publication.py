import json
import os
import unittest
from datetime import date, datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "qa-publication-only"

import app as crm
import task_configuration as config
import task_configuration_editor as editor
from task_configuration_publishing import publish_draft, replan_state, apply_replan
from task_schedule import schedule_configuration
from models import (AuditLog, ContactAgendaConfiguration, Franchise, Lead, LeadWorkflow,
                    LeadWorkflowScheduledTask, Role, User, TaskConfigurationRevision,
                    WorkflowChecklistItem, db)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        company, matrix = crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.admin = User(username="qa-publisher", name="QA Publisher", role=Role.ADMIN, active=True,
                          company_id=company.id, franchise_id=matrix.id)
        self.admin.apply_role_defaults()
        self.admin.can_view_all_franchises = False
        db.session.add(self.admin); db.session.commit()
        self.base = config.current_revision(config.company_scope(company.id))
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.admin.id
            session["_csrf_token"] = "qa"

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def draft(self):
        draft = editor.start_draft(self.admin, self.base.fingerprint)
        db.session.commit()
        return draft

    def publish(self, draft):
        result = publish_draft(self.admin, draft.id, draft.fingerprint, crm.SELLER_EXECUTION_TASKS,
                               crm.checklist_items_for_workflow, config.contact_agenda_revision)
        db.session.commit()
        return result

    def lead(self, slug="30-dias", stage_slug="60-dias", franchise=None):
        lead = Lead(nome_cliente="QA Contract", cpf="00000000001", equipe="QA", consultor=self.admin.name,
                    company_id=self.admin.company_id, franchise_id=franchise or self.admin.franchise_id,
                    status="Em andamento", concessao=date.today()-timedelta(days=45))
        db.session.add(lead); db.session.flush()
        workflow, stage = crm.workflow_stage_by_slug(slug, stage_slug)
        tracking = LeadWorkflow(lead_id=lead.id, workflow_id=workflow.id, current_stage=stage,
                                owner_user_id=self.admin.id, fidelity_started_on=date.today(), status="em_andamento")
        db.session.add(tracking); db.session.commit()
        return tracking

    def edit_schedule(self, draft, key, **changes):
        rules = schedule_configuration(json.loads(draft.content))
        rule = rules["contacts"][key.split(":")[1]] if key.startswith("contact:") else next(r for r in rules["inss"] if r["key"] == key)
        field = "contacts_per_week" if key.startswith("contact:") else "delay_business_days"
        values = {k: rule[k] for k in ("label", "description", "active", field)}
        editor.save_task(self.admin, draft.id, draft.fingerprint, key, {**values, **changes})
        db.session.commit()

    def post(self, url, **data):
        return self.client.post(url, json=data, headers={"X-CSRF-Token": "qa"})

    def tasks(self, tracking):
        return LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).order_by(LeadWorkflowScheduledTask.id).all()

    def test_saved_inactive_draft_does_not_publish_and_reports_blockers(self):
        draft = self.draft()
        editor.deactivate_all_draft_tasks(self.admin, draft.id, draft.fingerprint, crm.SELLER_EXECUTION_TASKS)
        db.session.commit()
        result = self.post(f"/admin/workflows/task-draft/{draft.id}/save", fingerprint=draft.fingerprint)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json["publication_errors"])
        self.assertTrue(all(not row["active"] for row in result.json["rows"]))
        response = self.post(f"/admin/workflows/task-draft/{draft.id}/publish", fingerprint=draft.fingerprint, confirm=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config.current_revision(draft.scope).id, self.base.id)
        self.assertEqual(draft.status, "draft")

    def test_publication_is_confirmed_and_admin_only(self):
        draft = self.draft()
        url = f"/admin/workflows/task-draft/{draft.id}/publish"
        self.assertEqual(self.client.post(url, json={"fingerprint": draft.fingerprint, "confirm": True}).status_code, 400)
        self.assertEqual(self.post(url, fingerprint=draft.fingerprint).status_code, 400)
        self.admin.role = Role.SUPERVISOR
        db.session.commit()
        self.assertEqual(self.post(url, fingerprint=draft.fingerprint, confirm=True).status_code, 403)
        self.assertEqual(self.post(f"/admin/workflows/task-draft/{draft.id}/save", fingerprint=draft.fingerprint).status_code, 200)

    def test_publication_preserves_contacts_and_uses_new_rules_on_new_contract(self):
        tracking = self.lead()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        before = [(t.id, t.label, t.scheduled_for, t.active, t.configuration_snapshot) for t in self.tasks(tracking)]
        draft = self.draft()
        self.edit_schedule(draft, "contact:60-dias", label="Contato novo", contacts_per_week=5)
        published = self.publish(draft)
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        self.assertEqual(before, [(t.id, t.label, t.scheduled_for, t.active, t.configuration_snapshot) for t in self.tasks(tracking)])
        self.assertEqual(db.session.get(ContactAgendaConfiguration, tracking.id).revision_id, self.base.id)
        branch = Franchise(company_id=self.admin.company_id, name="QA Branch", active=True)
        db.session.add(branch); db.session.commit()
        other = self.lead(franchise=branch.id)
        crm.ensure_scheduled_contact_tasks_for_workflow(other); db.session.commit()
        self.assertTrue(any(t.label.startswith("Contato novo") for t in self.tasks(other)))
        self.assertTrue(all(t.configuration_revision_id == published.id for t in self.tasks(other)))
        fingerprint = published.fingerprint
        crm.seed_default_workflows()
        self.assertEqual(config.current_revision(published.scope).fingerprint, fingerprint)
        self.assertEqual(editor.latest_draft(published.scope), None)
        with self.assertRaises(ValueError):
            published.content = "{}"
            db.session.flush()
        db.session.rollback()

    def test_checklist_existing_snapshot_and_new_dependencies(self):
        old = self.lead("principal", "venda")
        old_items = crm.checklist_items_for_workflow(old)
        db.session.commit()
        draft = self.draft()
        item = next(i for i in json.loads(draft.content)["checklists"] if i["id"] == old_items[0].id)
        values = {k: item.get(k) for k in ("label", "description", "stage_id", "sort_order", "required", "active", "depends_on")}
        values.update(label="Etapa revisada", description="Nova orientacao")
        editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], values)
        editor.save_task(self.admin, draft.id, draft.fingerprint, None,
                         {**values, "label": "Dependente", "sort_order": 99, "depends_on": item["key"]})
        db.session.commit()
        self.publish(draft)
        self.assertEqual([i.label for i in crm.checklist_items_for_workflow(old)], [i.label for i in old_items])
        branch = Franchise(company_id=self.admin.company_id, name="QA new", active=True)
        db.session.add(branch); db.session.commit()
        new = self.lead("principal", "venda", branch.id)
        items = crm.checklist_items_for_workflow(new)
        child = next(i for i in items if i.label == "Dependente")
        parent = next(i for i in items if i.label == "Etapa revisada")
        with self.assertRaisesRegex(ValueError, "anterior"):
            crm.complete_lead_workflow_checklist_item(new, child.id, self.admin, "Anotacao")
        crm.complete_lead_workflow_checklist_item(new, parent.id, self.admin, "Concluida")
        crm.complete_lead_workflow_checklist_item(new, child.id, self.admin, "Concluida depois")
        db.session.commit()
        count = TaskConfigurationRevision.query.count()
        crm.seed_default_workflows()
        self.assertEqual(TaskConfigurationRevision.query.count(), count)

    def test_inss_new_deadline_and_existing_task_unchanged(self):
        tracking = self.lead()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        first = next(t for t in self.tasks(tracking) if t.task_key == crm.FIDELITY_INSS_TASKS[0][0])
        old_label, old_date = first.label, first.scheduled_for
        draft = self.draft()
        key = crm.FIDELITY_INSS_TASKS[1][0]
        self.edit_schedule(draft, key, label="Confirmacao configurada", delay_business_days=2)
        self.publish(draft)
        self.assertEqual((first.label, first.scheduled_for), (old_label, old_date))
        crm.complete_scheduled_contact_task(tracking, first.id, self.admin, "INSS concluido")
        db.session.commit()
        second = next(t for t in self.tasks(tracking) if t.task_key == key)
        self.assertEqual(second.label, "Confirmacao configurada")
        self.assertEqual(second.scheduled_for, crm.business_deadline(date.today(), 2))
        with self.assertRaisesRegex(ValueError, "agendada"):
            crm.complete_scheduled_contact_task(tracking, second.id, self.admin, "Tentativa antecipada")

    def test_replan_preserves_evidence_and_survives_generation(self):
        tracking = self.lead()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        future = [t for t in self.tasks(tracking) if t.task_key.startswith("unlock-contact:") and t.scheduled_for > date.today()]
        future[0].note = "Atuacao iniciada"
        future[1].completed_at = datetime.now()
        future[1].completed_by_id = self.admin.id
        db.session.commit()
        protected = [(t.id, t.label, t.note, t.completed_at, t.configuration_snapshot) for t in future[:2]]
        draft = self.draft()
        self.edit_schedule(draft, "contact:60-dias", active=False)
        self.edit_schedule(draft, "contact:20-dias", contacts_per_week=1)
        revision = self.publish(draft)
        tomorrow = date.today()+timedelta(days=1)
        preview = replan_state(self.admin, revision, tracking, tomorrow, date.today())
        self.assertGreater(preview["counts"]["deactivate"], 0)
        apply_replan(self.admin, revision, tracking, tomorrow, preview["confirmation"], date.today())
        db.session.commit()
        inactive = {t.id for t in self.tasks(tracking) if not t.active}
        self.assertTrue(inactive)
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        db.session.expire_all()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking, today=tomorrow+timedelta(days=20)); db.session.commit()
        self.assertEqual(inactive, {t.id for t in self.tasks(tracking) if not t.active})
        self.assertEqual(protected, [(t.id, t.label, t.note, t.completed_at, t.configuration_snapshot) for t in future[:2]])
        self.assertEqual(AuditLog.query.filter_by(action="task_configuration_agenda_replanned").count(), 1)

    def test_replan_rejects_stale_evidence_draft_and_duplicate_submission(self):
        tracking = self.lead()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        draft = self.draft()
        tomorrow = date.today()+timedelta(days=1)
        with self.assertRaises(ValueError):
            apply_replan(self.admin, draft, tracking, tomorrow, "anything", date.today())
        db.session.rollback()
        published = self.publish(draft)
        preview = replan_state(self.admin, published, tracking, tomorrow, date.today())
        self.tasks(tracking)[0].note = "Mudou depois da previa"
        db.session.commit()
        with self.assertRaises(editor.DraftConflict):
            apply_replan(self.admin, published, tracking, tomorrow, preview["confirmation"], date.today())
        db.session.rollback()
        preview = replan_state(self.admin, published, tracking, tomorrow, date.today())
        apply_replan(self.admin, published, tracking, tomorrow, preview["confirmation"], date.today()); db.session.commit()
        with self.assertRaises(editor.DraftConflict):
            apply_replan(self.admin, published, tracking, tomorrow, preview["confirmation"], date.today())
        db.session.rollback()

    def test_apply_route_is_scoped_and_audited(self):
        tracking = self.lead()
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking); db.session.commit()
        data = {"revision_id": self.base.id, "fingerprint": self.base.fingerprint, "lead_id": tracking.lead_id,
                "effective_from": (date.today()+timedelta(days=1)).isoformat()}
        preview = self.post("/admin/workflows/task-agenda/preview", **data)
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertTrue(preview.json["can_apply"])
        result = self.post("/admin/workflows/task-agenda/apply", **data, confirm=True, confirmation=preview.json["confirmation"])
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json["applied"])
        branch = Franchise(company_id=self.admin.company_id, name="Other", active=True)
        db.session.add(branch); db.session.commit()
        foreign = self.lead(franchise=branch.id)
        data["lead_id"] = foreign.lead_id
        self.assertEqual(self.post("/admin/workflows/task-agenda/apply", **data, confirm=True, confirmation=preview.json["confirmation"]).status_code, 403)

    def test_publication_rejects_stale_base_and_fingerprint(self):
        draft = self.draft()
        with self.assertRaises(editor.DraftConflict):
            publish_draft(self.admin, draft.id, "old", crm.SELLER_EXECUTION_TASKS,
                          crm.checklist_items_for_workflow, config.contact_agenda_revision)
        db.session.rollback()
        config.append_revision(draft.scope, json.loads(self.base.content), "published", self.admin, self.base)
        db.session.commit()
        with self.assertRaises(editor.DraftConflict):
            self.publish(draft)
        db.session.rollback()
        self.assertEqual(draft.status, "draft")


if __name__ == "__main__":
    unittest.main()

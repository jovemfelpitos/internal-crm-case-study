import os
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-unified-fidelity-only"

import app as crm
from models import db, Lead, User, Role, Franchise, LeadActivity, LeadWorkflow, Workflow, WorkflowTransition, LeadWorkflowScheduledTask, LeadWorkflowChecklistCheck


class UnifiedFidelityTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.user = User(username="qa-owner", name="QA Owner", role=Role.ADMIN,
                         company_id=crm.default_company().id, franchise_id=Franchise.query.first().id,
                         team="QA", active=True, must_change_password=False)
        self.user.apply_role_defaults()
        db.session.add(self.user)
        db.session.commit()
        self.counter = 0

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def client_at(self, days, principal_slug="fidelizacao"):
        self.counter += 1
        today = datetime.now().date()
        lead = Lead(nome_cliente=f"QA {self.counter}", cpf=f"{self.counter:011d}",
                    consultor=self.user.name, equipe="QA", company_id=self.user.company_id,
                    franchise_id=self.user.franchise_id, data_venda=today,
                    concessao=today - timedelta(days=90-days), status="Em andamento")
        db.session.add(lead)
        db.session.flush()
        workflow, stage = crm.workflow_stage_by_slug("principal", principal_slug)
        principal = LeadWorkflow(lead_id=lead.id, workflow_id=workflow.id, current_stage=stage,
                                 status="em_andamento", owner_user_id=self.user.id)
        db.session.add(principal)
        db.session.flush()
        tracking, _ = crm.ensure_temporary_workflow_for_principal(principal)
        db.session.flush()
        return lead, principal, tracking

    def tasks(self, tracking):
        db.session.flush()
        return LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).all()

    def test_windows_and_day_zero(self):
        for days, slug in [(90,"90-dias"),(61,"90-dias"),(60,"60-dias"),(31,"60-dias"),
                           (30,"30-dias"),(21,"30-dias"),(20,"20-dias"),(11,"20-dias"),
                           (10,"10-dias"),(1,"10-dias"),(0,"dia-do-desbloqueio")]:
            self.assertEqual(crm.expected_unlock_stage(days)["slug"], slug)
        lead, principal, tracking = self.client_at(1)
        self.assertFalse(crm.sync_unlock_day_principal_stage_for_lead(lead))
        lead.concessao -= timedelta(days=1)
        self.assertTrue(crm.sync_unlock_day_principal_stage_for_lead(lead))
        self.assertEqual(principal.current_stage.slug, "dia-do-desbloqueio")
        self.assertFalse(crm.unlock_completed_for_lead(lead))
        self.assertIsNone(principal.support_completed_at)

    def test_45_days_sequence_and_no_retroactive_contacts(self):
        lead, principal, tracking = self.client_at(45)
        self.assertEqual(tracking.current_stage.slug, "60-dias")
        initial = self.tasks(tracking)
        self.assertEqual([t.task_key for t in initial if t.task_key.startswith("inss:")], ["inss:fazer"])
        self.assertTrue(all(t.scheduled_for >= datetime.now().date() for t in initial))
        for key, _, _ in crm.FIDELITY_INSS_TASKS:
            task = next(t for t in self.tasks(tracking) if t.task_key == key)
            crm.complete_scheduled_contact_task(tracking, task.id, self.user, "Procedimento confirmado com o cliente.")
            self.assertEqual(tracking.current_stage.slug, "60-dias")
        self.assertEqual(WorkflowTransition.query.filter_by(action="scheduled_task_complete", lead_id=lead.id).count(), 3)
        with self.assertRaises(ValueError):
            crm.complete_scheduled_contact_task(tracking, task.id, self.user, "Duplicado")

    def test_agenda_stable_weekdays_frequency_and_future_lock(self):
        _, _, tracking = self.client_at(90)
        initial = {t.id: (t.scheduled_for,t.active) for t in self.tasks(tracking)}
        crm.ensure_scheduled_contact_tasks_for_workflow(tracking, today=datetime.now().date()+timedelta(days=8))
        self.assertEqual(initial, {t.id:(t.scheduled_for,t.active) for t in self.tasks(tracking)})
        contacts = [t for t in self.tasks(tracking) if t.task_key.startswith("unlock-contact:")]
        self.assertTrue(all(t.scheduled_for.weekday()<5 for t in contacts))
        counts = {}
        for task in contacts:
            key = (task.stage.slug, task.scheduled_for.isocalendar()[:2])
            counts[key] = counts.get(key,0)+1
        for (slug,_), count in counts.items():
            maximum = crm.UNLOCK_CONTACT_TASK_RULES[slug]["contacts_per_week"] or 5
            self.assertLessEqual(count, maximum)
        future = next(t for t in contacts if t.scheduled_for > datetime.now().date())
        with self.assertRaises(ValueError):
            crm.complete_scheduled_contact_task(tracking, future.id, self.user, "Antecipado")
        with self.assertRaises(ValueError):
            crm.move_lead_workflow_to_next_stage(tracking, self.user)
        with self.assertRaises(ValueError):
            crm.move_lead_workflow_to_stage(tracking, crm.workflow_stage_by_slug("30-dias","10-dias")[1], self.user)

    def test_pending_survives_window_and_unlock_day(self):
        lead, principal, tracking = self.client_at(21)
        task = next(t for t in self.tasks(tracking) if t.task_key == "inss:fazer")
        task.scheduled_for = datetime.now().date()-timedelta(days=3)
        original_deadline = task.scheduled_for
        lead.concessao -= timedelta(days=2)
        crm.sync_unlock_workflow_stage(tracking)
        self.assertEqual(tracking.current_stage.slug,"20-dias")
        self.assertIn(task, crm.scheduled_contact_tasks_for_workflow(tracking))
        self.assertEqual(task.scheduled_for,original_deadline)
        lead.concessao -= timedelta(days=19)
        crm.sync_unlock_day_principal_stage_for_lead(lead)
        self.assertIn(task, crm.scheduled_contact_tasks_for_workflow(principal))
        crm.complete_scheduled_contact_task(principal,task.id,self.user,"INSS confirmado.")
        self.assertIn("Confirmar INSS", [t.label for t in crm.scheduled_contact_tasks_for_workflow(principal)])
        self.assertEqual(principal.current_stage.slug,"dia-do-desbloqueio")

    def test_custom_task_permissions_notes_and_history(self):
        lead, _, tracking = self.client_at(45)
        task = crm.create_fidelity_task(tracking,self.user,"Confirmacao adicional",datetime.now().date().isoformat())
        with self.assertRaises(ValueError):
            crm.complete_scheduled_contact_task(tracking,task.id,self.user,"")
        crm.complete_scheduled_contact_task(tracking,task.id,self.user,"Confirmado com o cliente.")
        self.assertEqual(WorkflowTransition.query.filter_by(lead_id=lead.id,action="scheduled_task_create").count(),1)
        self.assertEqual(WorkflowTransition.query.filter_by(lead_id=lead.id,action="scheduled_task_complete").count(),1)
        self.user.can_move_workflows=False
        with self.assertRaises(ValueError):
            crm.create_fidelity_task(tracking,self.user,"Nao autorizado",datetime.now().date().isoformat())

    def test_same_cpf_other_franchise_cannot_complete_task(self):
        lead, _, tracking = self.client_at(45)
        other = Franchise(company_id=self.user.company_id,name="Outra franquia")
        db.session.add(other)
        db.session.flush()
        lead2, _, tracking2 = self.client_at(45)
        lead2.franchise_id=other.id
        lead2.cpf=lead.cpf
        task = self.tasks(tracking2)[0]
        with self.assertRaises(ValueError):
            crm.complete_scheduled_contact_task(tracking,task.id,self.user,"Tentativa entre contratos")
        self.user.role=Role.OPERADOR
        self.user.can_view_all=False
        self.user.can_view_all_franchises=False
        self.assertFalse(crm.can_create_fidelity_task(self.user,tracking2))

    def test_migration_idempotent_and_legacy_completion_preserved(self):
        lead, principal, tracking = self.client_at(45)
        # Simulate a pre-unification client: no new tasks and a checked INSS item.
        LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).delete()
        legacy, _ = crm.ensure_lead_workflow_for_slug(lead,"fidelizacao","confirmacao-inss-1")
        item = crm.checklist_items_for_stage(crm.workflow_stage_by_slug("fidelizacao","fazer-inss")[1])[0]
        check = LeadWorkflowChecklistCheck(lead_workflow_id=legacy.id,checklist_item_id=item.id,checked=True,
                                          checked_at=datetime.now()-timedelta(days=4),checked_by_id=self.user.id,note="Ja feito")
        db.session.add(check)
        principal.current_stage=crm.workflow_stage_by_slug("principal","30-dias-desbloqueio")[1]
        db.session.flush()
        crm.migrate_unified_fidelity()
        before = (LeadWorkflowScheduledTask.query.count(),WorkflowTransition.query.count())
        crm.migrate_unified_fidelity()
        self.assertEqual(before,(LeadWorkflowScheduledTask.query.count(),WorkflowTransition.query.count()))
        self.assertEqual(principal.current_stage.slug,"fidelizacao")
        self.assertEqual(legacy.status,"inativo")
        restored = next(t for t in self.tasks(tracking) if t.task_key=="inss:fazer")
        self.assertEqual(restored.completed_at,check.checked_at)
        self.assertEqual(restored.note,"Ja feito")
        self.assertEqual(LeadWorkflowChecklistCheck.query.count(),1)

    def test_templates_and_board_routes(self):
        lead, _, tracking = self.client_at(45)
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.user.id
        for path in ("/workflows?workflow=principal","/workflows?workflow=fidelizacao",f"/leads/{lead.id}"):
            response=client.get(path)
            self.assertEqual(response.status_code,200,(path,response.location))
            if "workflow=fidelizacao" in path:
                self.assertIn(b"Preparacao inicial",response.data)
        response=client.get(f"/leads/{lead.id}")
        self.assertIn(b"Adicionar tarefa",response.data)

    def test_task_post_validation_and_stage_post_protection(self):
        lead, _, tracking = self.client_at(45)
        db.session.commit()
        client=self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"]=self.user.id
            session["_csrf_token"]="qa-csrf"
        url=f"/lead-workflows/{tracking.id}/progress"
        form={"_csrf_token":"qa-csrf", "workflow_action":"create_fidelity_task",
              "task_label":"Confirmacao extra QA", "task_due_date":"2020-01-01"}
        response=client.post(url,data=form,follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertIn(b'data-modal-auto-open="1"',response.data)
        self.assertIn(b'value="Confirmacao extra QA"',response.data)
        self.assertEqual(LeadWorkflowScheduledTask.query.filter(LeadWorkflowScheduledTask.task_key.like("custom:%")).count(),0)
        form["task_due_date"]=datetime.now().date().isoformat()
        response=client.post(url,data=form,follow_redirects=True)
        self.assertEqual(response.status_code,200)
        task=LeadWorkflowScheduledTask.query.filter(LeadWorkflowScheduledTask.task_key.like("custom:%")).one()
        response=client.post(url,data={"_csrf_token":"qa-csrf","workflow_action":"complete_scheduled_task",
                                     "scheduled_task_id":task.id,"task_note":"Confirmacao feita."},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        db.session.refresh(task)
        self.assertIsNotNone(task.completed_at)
        target=crm.workflow_stage_by_slug("30-dias","10-dias")[1]
        response=client.post(f"/lead-workflows/{tracking.id}/stage",data={"_csrf_token":"qa-csrf","stage_id":target.id},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        db.session.refresh(tracking)
        self.assertEqual(tracking.current_stage.slug,"60-dias")

    def test_legacy_history_keeps_original_stage_and_terminal_stays_terminal(self):
        lead, principal, tracking = self.client_at(45)
        lead.atuacao_1="Registro antigo preservado"
        activity=LeadActivity(lead_id=lead.id,lead_workflow_id=principal.id,workflow_id=principal.workflow_id,
                              stage_id=principal.current_stage_id,body=lead.atuacao_1,source="legacy_field",source_field="atuacao_1")
        db.session.add(activity)
        db.session.flush()
        original=(activity.lead_workflow_id,activity.workflow_id,activity.stage_id,activity.created_at,activity.body)
        crm.ensure_legacy_lead_activities_for_lead(lead)
        self.assertEqual(original,(activity.lead_workflow_id,activity.workflow_id,activity.stage_id,activity.created_at,activity.body))
        principal.status="recusado"
        tracking.status="recusado"
        lead.concessao-=timedelta(days=50)
        self.assertFalse(crm.sync_unlock_day_principal_stage_for_lead(lead))
        crm.migrate_unified_fidelity()
        self.assertEqual(principal.status,"recusado")
        self.assertEqual(tracking.status,"recusado")

    def test_existing_unlock_stage_closes_parallel_tracking_without_losing_tasks(self):
        lead, principal, tracking = self.client_at(15)
        principal.current_stage=crm.workflow_stage_by_slug("principal","dia-do-desbloqueio")[1]
        db.session.flush()
        self.assertTrue(crm.sync_unlock_day_principal_stage_for_lead(lead))
        self.assertEqual(tracking.status,"concluido")
        self.assertEqual(principal.current_stage.slug,"dia-do-desbloqueio")
        self.assertTrue(crm.scheduled_contact_tasks_for_workflow(principal))


if __name__ == "__main__":
    unittest.main()

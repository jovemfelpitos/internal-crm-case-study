import json
import os
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "qa-integrated-disposable-only"

import app as crm
import task_configuration as config
import task_configuration_editor as editor
from qa_integrated_fixture import IntegratedFixture, NOTE
from models import (AuditLog, LeadWorkflowChecklistCheck, LeadWorkflowScheduledTask, ReversalAttempt,
                    ReversalDistributionMode, ReversalNotification, ReversalResult, ReversalStatus,
                    ReversalTask, SupportNotification, WorkflowTransition, db)


class IntegratedOperationsTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.qa = IntegratedFixture()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def post_progress(self, tracking, user, action, **fields):
        client = self.qa.client(self.app, user)
        result = client.post(f"/lead-workflows/{tracking.id}/progress", follow_redirects=True,
                             data={"_csrf_token": "qa-integrated", "workflow_action": action, **fields})
        self.assertEqual(result.status_code, 200)
        db.session.refresh(tracking)
        return result

    def request(self, lead, owner=None, reason=None):
        task = crm.create_reversal_task_for_lead(lead, owner or self.qa.owner,
                   tabulation=reason or crm.REVERSAL_TABULATION_TEST_FIDELITY, note=NOTE)
        db.session.commit()
        return task

    def test_published_task_blocks_sale_until_each_task_is_completed_with_note(self):
        base = config.current_revision(config.company_scope(self.qa.company.id))
        draft = editor.start_draft(self.qa.admin, base.fingerprint)
        stage = crm.workflow_stage_by_slug("principal", "venda")[1]
        editor.save_task(self.qa.admin, draft.id, draft.fingerprint, None,
                         {"label": "QA Validar documentos", "description": "Conferir documento antes de seguir",
                          "stage_id": stage.id, "sort_order": 99, "required": True, "active": True, "depends_on": None})
        db.session.commit()
        from task_configuration_publishing import publish_draft
        publish_draft(self.qa.admin, draft.id, draft.fingerprint, crm.SELLER_EXECUTION_TASKS,
                      crm.checklist_items_for_workflow, config.contact_agenda_revision)
        db.session.commit()
        lead, principal, _ = self.qa.contract(stage="venda")
        items = crm.checklist_items_for_workflow(principal)
        extra = next(item for item in items if item.label == "QA Validar documentos")
        self.post_progress(principal, self.qa.owner, "complete_checklist_item", checklist_item_id=extra.id, task_note="")
        self.assertFalse(LeadWorkflowChecklistCheck.query.filter_by(lead_workflow_id=principal.id, checklist_item_id=extra.id).one().checked)
        for item in items:
            if item.id != extra.id:
                self.post_progress(principal, self.qa.owner, "complete_checklist_item", checklist_item_id=item.id, task_note=NOTE)
        self.assertEqual(principal.current_stage.slug, "venda")
        self.post_progress(principal, self.qa.owner, "advance")
        self.assertEqual(principal.current_stage.slug, "venda")
        self.post_progress(principal, self.qa.owner, "complete_checklist_item", checklist_item_id=extra.id, task_note=NOTE)
        self.post_progress(principal, self.qa.owner, "advance")
        self.assertEqual(principal.current_stage.slug, "fidelizacao")
        history = crm.lead_operational_history(lead)
        self.assertEqual(sum(event["title"] == "Tarefa concluida" and event["subtitle"] == extra.label for event in history), 1)
        self.assertTrue(any(event["title"] == "Mudanca de etapa" for event in history))

    def test_support_return_resolution_resends_notifies_and_does_not_complete_next_stage(self):
        lead, principal, _ = self.qa.contract(stage="dia-do-desbloqueio", days=0)
        crm.set_unlock_executor_for_workflow(principal, self.qa.owner, "support")
        db.session.commit()
        self.assertEqual(principal.assigned_user_id, self.qa.support.id)
        crm.return_support_task(principal, self.qa.support, "Documento ilegivel; enviar nova imagem")
        db.session.commit()
        self.assertTrue(crm.support_return_resolution_pending(principal))
        self.assertIn(principal.id, {task.id for task in crm.visible_support_tasks_query(self.qa.support).all()})
        result = self.post_progress(principal, self.qa.owner, "resolve_support_return", task_note="")
        self.assertTrue(crm.support_return_resolution_pending(principal))
        self.assertTrue(b"corrigido" in result.data, "Devolucao vazia deve pedir anotacao da correcao")
        self.post_progress(principal, self.qa.owner, "resolve_support_return", task_note="Nova imagem do documento enviada pelo cliente")
        self.assertFalse(crm.support_return_resolution_pending(principal))
        self.assertIsNone(principal.support_returned_at)
        self.assertEqual(SupportNotification.query.filter_by(lead_workflow_id=principal.id, notification_type="support_resent").count(), 1)
        crm.complete_support_task(principal, self.qa.support, note=NOTE); db.session.commit()
        self.post_progress(principal, self.qa.owner, "advance")
        db.session.refresh(principal)
        self.assertEqual(principal.current_stage.slug, "anuencia")
        self.assertIsNone(principal.support_completed_at)
        self.assertFalse(any(c.checked for c in LeadWorkflowChecklistCheck.query.filter_by(lead_workflow_id=principal.id).all()
                             if c.checklist_item.stage.slug in {"anuencia", "pos-venda"}))
        history = crm.lead_operational_history(lead)
        self.assertEqual(sum(event["title"] == "Retorno apoio: concluido" for event in history), 1)
        self.assertEqual(sum(event["title"] == "Retorno apoio: devolvido" for event in history), 1)

    def test_support_missing_and_foreign_support_cannot_resolve_others(self):
        _, principal, _ = self.qa.contract(stage="anuencia", days=0)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "support"); db.session.commit()
        self.assertEqual(principal.assigned_user_id, self.qa.support.id)
        with self.assertRaises(ValueError):
            crm.return_support_task(principal, self.qa.branch_support, "Tentativa de outra franquia")
        crm.return_support_task(principal, self.qa.support, "Falta confirmacao"); db.session.commit()
        self.qa.support.active = False; db.session.commit()
        with self.assertRaisesRegex(ValueError, "apoio ativo"):
            crm.complete_support_return_resolution(principal, self.qa.owner, NOTE)
        db.session.rollback()
        self.assertTrue(crm.support_return_resolution_pending(principal))
        self.assertIsNone(principal.support_return_resolution_completed_at)

    def test_seller_execution_is_independent_for_unlock_anuencia_and_post_sale(self):
        _, principal, _ = self.qa.contract(stage="dia-do-desbloqueio", days=0)
        crm.set_unlock_executor_for_workflow(principal, self.qa.owner, "seller"); db.session.commit()
        state = crm.seller_execution_task_state(principal)
        self.assertEqual(state["key"], "seller_unlock_execution")
        self.post_progress(principal, self.qa.owner, "complete_seller_execution_task", seller_task_key=state["key"], task_note=NOTE)
        self.assertEqual(principal.current_stage.slug, "dia-do-desbloqueio")
        for item in crm.checklist_items_for_workflow(principal):
            if principal.current_stage.slug != "dia-do-desbloqueio":
                break
            self.post_progress(principal, self.qa.owner, "complete_checklist_item", checklist_item_id=item.id, task_note=NOTE)
        self.post_progress(principal, self.qa.owner, "advance")
        self.assertEqual(principal.current_stage.slug, "anuencia")
        self.assertIsNone(principal.support_completed_at)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "seller"); db.session.commit()
        state = crm.seller_execution_task_state(principal)
        self.assertEqual(state["key"], "seller_anuencia_execution")
        self.assertFalse(state["checked"])
        self.post_progress(principal, self.qa.owner, "complete_seller_execution_task", seller_task_key=state["key"], task_note=NOTE)
        for item in crm.checklist_items_for_workflow(principal):
            self.post_progress(principal, self.qa.owner, "complete_checklist_item", checklist_item_id=item.id, task_note=NOTE)
        self.post_progress(principal, self.qa.owner, "advance")
        db.session.refresh(principal)
        self.assertEqual(principal.current_stage.slug, "pos-venda")
        self.assertIsNone(principal.support_completed_at)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "seller"); db.session.commit()
        state = crm.seller_execution_task_state(principal)
        self.assertEqual(state["key"], "seller_post_sale_execution")
        self.assertFalse(state["checked"])

    def test_support_and_reversal_coexist_with_fidelity_and_separate_histories(self):
        lead, principal, tracking = self.qa.contract()
        tasks = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id).all()
        first = next(task for task in tasks if task.task_key == "inss:fazer")
        crm.set_unlock_executor_for_workflow(principal, self.qa.owner, "support")
        task = self.request(lead)
        self.assertNotEqual(task.assigned_user_id, self.qa.owner.id)
        self.assertEqual(principal.current_stage.slug, "fidelizacao")
        self.assertEqual(tracking.current_stage.slug, "60-dias")
        self.assertEqual(lead.consultor, self.qa.owner.name)
        self.assertTrue(crm.lead_access_is_reversal_only(task.assigned_user, lead))
        crm.complete_reversal_task(task, task.assigned_user, ReversalResult.SEM_CONTATO, NOTE)
        db.session.commit()
        self.assertEqual(task.status, ReversalStatus.RETURNED)
        self.assertIsNone(first.completed_at)
        self.assertEqual(principal.current_stage.slug, "fidelizacao")
        self.assertEqual(tracking.current_stage.slug, "60-dias")
        self.assertTrue(any(event["title"].startswith("Reversao devolvida") for event in crm.reversal_history_for_lead(lead)))
        self.assertFalse(any(event["title"].startswith("Reversao devolvida") for event in crm.lead_operational_history(lead)))
        self.assertIsNotNone(self.request(lead))

    def test_reversor_cannot_complete_owner_tasks_or_change_contact(self):
        lead, _, tracking = self.qa.contract()
        task = self.request(lead)
        assignee = task.assigned_user
        inss = LeadWorkflowScheduledTask.query.filter_by(lead_workflow_id=tracking.id, task_key="inss:fazer").one()
        result = self.post_progress(tracking, assignee, "complete_scheduled_task", scheduled_task_id=inss.id, task_note=NOTE)
        db.session.refresh(inss)
        self.assertIsNone(inss.completed_at)
        self.assertIn(b"somente para executar", result.data)
        page = self.qa.client(self.app, assignee).get(f"/leads/{lead.id}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'workflow_action" value="complete_scheduled_task', page.data)
        self.assertNotIn(b"Registrar atuacao", page.data)
        original = lead.contato
        response = self.qa.client(self.app, assignee).post(f"/leads/{lead.id}/update", follow_redirects=True,
                   data={"_csrf_token": "qa-integrated", "contato": "11999999999"})
        self.assertEqual(response.status_code, 200)
        db.session.refresh(lead)
        self.assertEqual(lead.contato, original)

    def test_reversal_all_result_meanings_are_saved_and_notified(self):
        expected = {
            crm.REVERSAL_TABULATION_LOST_CONTACT: ["critical", "critical", "critical", "positive", "positive", "critical", "negative"],
            crm.REVERSAL_TABULATION_TEST_FIDELITY: ["positive", "positive", "alert", "negative", "critical", "critical", "negative"],
        }
        results = [ReversalResult.SEM_CONTATO, ReversalResult.BLOQUEOU, ReversalResult.NAO_QUIS_CONTRATAR,
                   ReversalResult.NEGOCIANDO, ReversalResult.VENDA_FECHADA, ReversalResult.NEGOCIANDO_TERCEIRO, ReversalResult.OUTRO]
        for reason, meanings in expected.items():
            for result, meaning in zip(results, meanings):
                with self.subTest(reason=reason, result=result):
                    lead, _, _ = self.qa.contract(days=8)
                    task = self.request(lead, reason=reason)
                    with self.assertRaises(ValueError):
                        crm.complete_reversal_task(task, task.assigned_user, result, "Curto")
                    crm.complete_reversal_task(task, task.assigned_user, result, NOTE); db.session.commit()
                    self.assertEqual(task.status, ReversalStatus.RETURNED)
                    attempt = ReversalAttempt.query.filter_by(reversal_task_id=task.id).one()
                    self.assertEqual(attempt.result, result)
                    self.assertEqual(crm.reversal_result_sentiment(result, reason), meaning)
                    self.assertTrue(ReversalNotification.query.filter_by(reversal_task_id=task.id, recipient_user_id=self.qa.owner.id).count())
                    self.assertEqual(AuditLog.query.filter_by(lead_id=lead.id, action="reversal_complete").count(), 1)

    def test_reversal_auto_balances_and_manual_assignment_transfer_is_scoped(self):
        assignments = []
        for _ in range(8):
            lead, _, _ = self.qa.contract()
            assignments.append(self.request(lead).assigned_user_id)
        self.assertEqual(assignments.count(self.qa.reversor.id), 4)
        self.assertEqual(assignments.count(self.qa.reversor2.id), 4)
        lead, _, _ = self.qa.contract(owner=self.qa.branch_owner)
        manual = self.request(lead, owner=self.qa.branch_owner)
        self.assertEqual(manual.status, ReversalStatus.QUEUED)
        self.assertIsNone(manual.assigned_user_id)
        self.assertTrue(crm.can_assign_reversal_task(self.qa.branch_admin, manual))
        self.assertFalse(crm.can_assign_reversal_task(self.qa.owner, manual))
        with self.assertRaises(ValueError):
            crm.assign_reversal_task(manual, self.qa.reversor, self.qa.branch_admin)
        crm.assign_reversal_task(manual, self.qa.branch_reversor, self.qa.branch_admin); db.session.commit()
        self.assertEqual(manual.assigned_user_id, self.qa.branch_reversor.id)
        first = ReversalTask.query.filter_by(assigned_user_id=self.qa.reversor.id).first()
        crm.assign_reversal_task(first, self.qa.reversor2, self.qa.supervisor); db.session.commit()
        self.assertEqual(first.assigned_user_id, self.qa.reversor2.id)

    def test_alarms_overdue_are_idempotent_for_support_and_reversal(self):
        lead, principal, _ = self.qa.contract(stage="anuencia", days=0)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "support")
        principal.support_assigned_at = datetime.now()-timedelta(days=3)
        principal.support_requested_at = principal.support_assigned_at
        principal.entered_stage_at = principal.support_assigned_at
        task = self.request(lead)
        task.due_at = datetime.now()-timedelta(hours=1)
        db.session.commit()
        crm.sync_reversal_overdue_statuses()
        crm.sync_support_overdue_notifications([principal]); db.session.commit()
        counts = (ReversalNotification.query.count(), SupportNotification.query.count())
        crm.sync_reversal_overdue_statuses()
        crm.sync_support_overdue_notifications([principal]); db.session.commit()
        self.assertEqual(task.status, ReversalStatus.OVERDUE)
        self.assertEqual(counts, (ReversalNotification.query.count(), SupportNotification.query.count()))
        self.assertTrue(SupportNotification.query.filter_by(lead_workflow_id=principal.id, notification_type="support_overdue").count())
        self.assertTrue(ReversalNotification.query.filter_by(reversal_task_id=task.id, recipient_user_id=self.qa.supervisor.id).count())

    def test_same_cpf_and_team_name_are_isolated_in_tasks_support_and_reversals(self):
        lead, principal, _ = self.qa.contract(stage="anuencia", days=0, cpf="80000000999")
        other, foreign, _ = self.qa.contract(stage="anuencia", days=0, cpf=lead.cpf, owner=self.qa.branch_owner)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "support")
        crm.set_support_executor_for_workflow(foreign, self.qa.branch_owner, "support"); db.session.commit()
        self.assertEqual(principal.assigned_user_id, self.qa.support.id)
        self.assertEqual(foreign.assigned_user_id, self.qa.branch_support.id)
        self.assertFalse(crm.can_view_lead(self.qa.branch_admin, lead))
        self.assertFalse(crm.can_view_lead(self.qa.owner, other))
        self.assertTrue(crm.can_view_lead(self.qa.admin, other))
        task = self.request(lead)
        second = self.request(other, owner=self.qa.branch_owner)
        self.assertNotEqual(task.lead_id, second.lead_id)
        self.assertIsNone(second.assigned_user_id)
        self.assertEqual(task.owner_user_id, self.qa.owner.id)
        self.assertEqual(second.owner_user_id, self.qa.branch_owner.id)
        response = self.qa.client(self.app, self.qa.branch_admin).get(f"/leads/{lead.id}")
        self.assertIn(response.status_code, (302, 403))

    def test_refused_contract_closes_active_reversal_and_cannot_receive_another(self):
        lead, principal, tracking = self.qa.contract()
        task = self.request(lead)
        crm.set_lead_contract_status(lead, self.qa.admin, "recusado", reason="Cliente recusou a contratacao")
        db.session.commit()
        self.assertEqual(principal.status, "recusado")
        self.assertEqual(tracking.status, "recusado")
        self.assertEqual(task.status, ReversalStatus.CANCELED)
        self.assertIsNone(crm.active_reversal_task_for_lead(lead))
        self.assertFalse(crm.can_work_reversal_task(task.assigned_user, task))
        self.assertFalse(crm.can_request_lead_reversal(self.qa.owner, lead))
        with self.assertRaises(ValueError):
            self.request(lead)
        db.session.rollback()
        self.assertEqual(ReversalAttempt.query.filter_by(reversal_task_id=task.id).count(), 0)

    def test_manual_batch_and_no_eligible_reversor(self):
        tasks = []
        for _ in range(3):
            lead, _, _ = self.qa.contract(owner=self.qa.branch_owner)
            tasks.append(self.request(lead, self.qa.branch_owner))
        batch = crm.distribute_manual_reversal_queue(self.qa.team_name, self.qa.branch_admin, franchise_id=self.qa.branch.id)
        db.session.commit()
        self.assertEqual(batch.total_assigned, 3)
        self.assertTrue(all(task.assigned_user_id == self.qa.branch_reversor.id for task in tasks))
        self.qa.reversor.active = False
        self.qa.reversor2.active = False
        db.session.commit()
        lead, _, _ = self.qa.contract()
        with self.assertRaisesRegex(ValueError, "outro vendedor"):
            self.request(lead)
        db.session.rollback()
        self.assertIsNone(crm.active_reversal_task_for_lead(lead))

    def test_support_resolution_requires_owner_permission_and_rejects_other_seller(self):
        _, principal, _ = self.qa.contract(stage="anuencia", days=0)
        crm.set_support_executor_for_workflow(principal, self.qa.owner, "support")
        crm.return_support_task(principal, self.qa.support, "Corrigir documento"); db.session.commit()
        self.assertTrue(crm.can_resolve_support_return(self.qa.owner, principal))
        self.assertFalse(crm.can_resolve_support_return(self.qa.reversor, principal))
        self.qa.owner.can_move_workflows = False
        self.qa.owner.can_request_support = False
        db.session.commit()
        self.assertFalse(crm.can_resolve_support_return(self.qa.owner, principal))
        self.post_progress(principal, self.qa.owner, "resolve_support_return", task_note=NOTE)
        self.assertTrue(crm.support_return_resolution_pending(principal))


if __name__ == "__main__":
    unittest.main()

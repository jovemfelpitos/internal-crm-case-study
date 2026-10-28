import copy
import json
import os
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "qa-task-schedule-only"

import app as crm
import task_configuration as config
import task_configuration_editor as editor
from task_schedule import (business_deadline, contact_calendar, parse_day, preview_replan,
                           schedule_configuration, simulate_schedule)
from models import (AuditLog, Franchise, Lead, LeadWorkflow, LeadWorkflowScheduledTask, Role,
                    TaskConfigurationRevision, User, WorkflowChecklistItem, db)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        company, matrix = crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.admin = User(username="qa-schedule", name="QA Schedule", role=Role.ADMIN, active=True,
                          company_id=company.id, franchise_id=matrix.id)
        self.admin.apply_role_defaults()
        self.admin.can_view_all_franchises = False
        db.session.add(self.admin); db.session.commit()
        self.base = config.current_revision(config.company_scope(company.id))
        self.content = json.loads(self.base.content)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.admin.id
            session["_csrf_token"] = "qa"

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def draft(self):
        revision = editor.start_draft(self.admin, self.base.fingerprint)
        db.session.commit()
        return revision

    def rule_values(self, key, **changes):
        rules = schedule_configuration(self.content)
        if key.startswith("contact:"):
            rule = rules["contacts"][key.removeprefix("contact:")]
            field = "contacts_per_week"
        else:
            rule = next(item for item in rules["inss"] if item["key"] == key)
            field = "delay_business_days"
        return {**{name: rule[name] for name in ("label", "description", "active", field)}, **changes}

    def preview(self, revision=None, **data):
        revision = revision or self.base
        payload = {"revision_id": revision.id, "fingerprint": revision.fingerprint,
                   "concession": "2026-08-22", "started_on": "2026-10-06", **data}
        return self.client.post('/admin/workflows/task-agenda/preview', json=payload,
                                headers={"X-CSRF-Token": "qa"})

    def test_calendar_matches_previous_algorithm_for_entry_dates(self):
        unlock = date(2026, 11, 20)
        for days_left in (91, 90, 61, 60, 45, 31, 30, 21, 20, 11, 10, 1, 0, -1):
            start = unlock - timedelta(days=days_left)
            expected = set()
            for slug, rule in crm.UNLOCK_CONTACT_TASK_RULES.items():
                weeks = {}
                for day in crm.business_days_between(max(start, unlock-timedelta(days=rule["window_ceiling_days"])),
                                                     unlock-timedelta(days=rule["window_floor_days"])):
                    weeks.setdefault(day.isocalendar()[:2], []).append(day)
                for week in weeks.values():
                    for day in crm.select_evenly_spaced_dates(week, rule["contacts_per_week"]) if rule["contacts_per_week"] else week:
                        expected.add(crm.scheduled_contact_task_key(slug, day))
            actual = contact_calendar(unlock, start, crm.UNLOCK_CONTACT_TASK_RULES)
            self.assertEqual({row["key"] for row in actual}, expected, days_left)
            self.assertEqual(len(actual), len(expected))
            self.assertTrue(all(start <= row["date"] < unlock and row["date"].weekday() < 5 for row in actual))

    def test_frequencies_week_boundaries_and_partial_week(self):
        for count in (1, 2, 3, 4, 5, None):
            rules = schedule_configuration(self.content)["contacts"]
            for rule in rules.values():
                rule["contacts_per_week"] = count
            rows = contact_calendar(date(2027, 1, 12), date(2026, 11, 26), rules)
            buckets = {}
            for row in rows:
                self.assertLess(row["date"].weekday(), 5)
                group = row["stage_slug"], row["date"].isocalendar()[:2]
                buckets[group] = buckets.get(group, 0) + 1
            self.assertTrue(all(total <= (count or 5) for total in buckets.values()))

    def test_inss_sequence_deadlines_and_midwindow_entry(self):
        content = copy.deepcopy(self.content)
        rules = schedule_configuration(content)
        for rule in rules["inss"]:
            rule["delay_business_days"] = 1
        content["schedule"] = rules
        result = simulate_schedule(content, date(2026, 8, 24), date(2026, 10, 9))
        inss = [row for row in result["rows"] if row["kind"] == "inss"]
        self.assertEqual([row["date"] for row in inss], ["2026-10-12", "2026-10-13", "2026-10-14"])
        self.assertTrue(all(row["estimated"] for row in inss))
        self.assertEqual(business_deadline(date(2026, 10, 10), 0), date(2026, 10, 10))
        self.assertEqual(simulate_schedule(content, date(2026, 1, 1), date(2026, 5, 1))["rows"], [])

    def test_rules_are_edited_only_in_draft_with_audit(self):
        draft = self.draft()
        original = self.base.content
        for key, values in (("contact:60-dias", self.rule_values("contact:60-dias", contacts_per_week=4)),
                            ("inss:fazer", self.rule_values("inss:fazer", delay_business_days=2, label="INSS configurado"))):
            editor.save_task(self.admin, draft.id, draft.fingerprint, key, values)
            db.session.commit()
        self.assertEqual(self.base.content, original)
        self.assertEqual(LeadWorkflowScheduledTask.query.count(), 0)
        self.assertEqual(AuditLog.query.filter_by(action="task_configuration_draft_schedule_saved").count(), 2)
        state = editor.panel_state(self.admin, crm.SELLER_EXECUTION_TASKS)
        self.assertTrue(next(r for r in state["rows"] if r["key"] == "inss:fazer")["changed"])
        crm.seed_default_workflows()
        self.assertEqual(schedule_configuration(json.loads(draft.content))["contacts"]["60-dias"]["contacts_per_week"], 4)
        self.assertEqual(config.current_revision(draft.scope).id, self.base.id)

    def test_protected_fields_and_invalid_frequencies(self):
        draft = self.draft()
        for frequency in (0, 6, -1, True, "3", 1.5):
            with self.assertRaises(ValueError):
                editor.save_task(self.admin, draft.id, draft.fingerprint, "contact:10-dias",
                                 self.rule_values("contact:10-dias", contacts_per_week=frequency))
        for changes in ({"window_floor_days": 0}, {"required": False}, {"note_required": False}, {"active": "false"}):
            with self.assertRaises(ValueError):
                editor.save_task(self.admin, draft.id, draft.fingerprint, "contact:30-dias", self.rule_values("contact:30-dias", **changes))
        for delay in (-1, 31, True, "2", 1.5):
            with self.assertRaises(ValueError):
                editor.save_task(self.admin, draft.id, draft.fingerprint, "inss:fazer", self.rule_values("inss:fazer", delay_business_days=delay))
        with self.assertRaises(ValueError):
            editor.save_task(self.admin, draft.id, draft.fingerprint, "inss:fazer", self.rule_values("inss:fazer", active=False))

    def test_all_inactive_only_in_draft_and_reactivation_order(self):
        draft = self.draft()
        actual = [(item.id, item.active) for item in WorkflowChecklistItem.query.all()]
        editor.deactivate_all_draft_tasks(self.admin, draft.id, draft.fingerprint, crm.SELLER_EXECUTION_TASKS)
        db.session.commit()
        self.assertTrue(all(not row["active"] for row in editor.panel_state(self.admin, crm.SELLER_EXECUTION_TASKS)["rows"]))
        self.assertEqual(actual, [(item.id, item.active) for item in WorkflowChecklistItem.query.all()])
        self.assertEqual(self.preview(draft).json["rows"], [])
        self.assertGreater(len(self.preview(self.base).json["rows"]), 0)
        editor.deactivate_all_draft_tasks(self.admin, draft.id, draft.fingerprint, crm.SELLER_EXECUTION_TASKS)
        db.session.commit()
        self.assertEqual(AuditLog.query.filter_by(action="task_configuration_draft_tasks_disabled").count(), 1)
        with self.assertRaises(ValueError):
            editor.save_task(self.admin, draft.id, draft.fingerprint, "inss:retornar", self.rule_values("inss:retornar"))
        editor.save_task(self.admin, draft.id, draft.fingerprint, "inss:fazer", self.rule_values("inss:fazer"))
        db.session.commit()
        self.assertEqual(self.preview(draft).json["counts"]["inss"], 1)

    def test_simulation_no_mutation_and_invalid_inputs(self):
        before = (TaskConfigurationRevision.query.count(), AuditLog.query.count(), LeadWorkflowScheduledTask.query.count())
        self.assertEqual(self.preview().status_code, 200)
        self.assertEqual(before, (TaskConfigurationRevision.query.count(), AuditLog.query.count(), LeadWorkflowScheduledTask.query.count()))
        for value in (None, 8, "", "2026-99-99", "0001-01-01"):
            self.assertEqual(self.preview(concession=value).status_code, 400)
        self.assertEqual(self.preview(fingerprint="stale").status_code, 409)
        self.assertEqual(self.client.post('/admin/workflows/task-agenda/preview', json={}).status_code, 400)
        with self.assertRaises(ValueError):
            parse_day([], "QA")

    def test_replan_preserves_completed_notes_custom_today_and_overdue(self):
        today = date(2026, 10, 6)
        content = copy.deepcopy(self.content)
        rules = schedule_configuration(content)
        for rule in rules["contacts"].values():
            rule["active"] = False
        content["schedule"] = rules
        def task(i, days, **fields):
            return SimpleNamespace(id=i, task_key=f"unlock-contact:10-dias:{(today+timedelta(days=days)).isoformat()}",
                                   scheduled_for=today+timedelta(days=days), label=f"Original {i}",
                                   **{"completed_at": None, "completed_by_id": None, "note": None, "active": True, **fields})
        tasks = [task(1, -3), task(2, 0), task(3, 1, completed_at=datetime(2026, 10, 6)),
                 task(4, 2, note="Registro preservado"), task(5, 3), task(6, 4, active=False), task(7, 5)]
        tasks[-1].task_key = "custom:extra"
        before = copy.deepcopy([vars(t) for t in tasks])
        result = preview_replan(content, date(2026, 7, 18), today, tasks, today, today+timedelta(days=1))
        self.assertEqual([r["task_id"] for r in result["rows"] if r["action"] == "deactivate"], [5])
        self.assertEqual(before, [vars(t) for t in tasks])
        self.assertEqual(result["counts"], {"preserve": 6, "deactivate": 1, "create": 0})
        with self.assertRaises(ValueError):
            preview_replan(content, today, today, tasks, today, today)

    def test_replan_does_not_recreate_inactive_keys(self):
        today = date(2026, 10, 6)
        concession = today-timedelta(days=80)
        desired = contact_calendar(concession+timedelta(days=90), today, crm.UNLOCK_CONTACT_TASK_RULES)
        row = next(r for r in desired if r["date"] > today)
        task = SimpleNamespace(id=1, task_key=row["key"], label="Nao recriar", scheduled_for=row["date"],
                               active=False, note=None, completed_at=None, completed_by_id=None)
        result = preview_replan(self.content, concession, today, [task], today, today+timedelta(days=1))
        self.assertEqual(len([r for r in result["rows"] if r["key"] == task.task_key]), 1)
        self.assertEqual(next(r for r in result["rows"] if r["key"] == task.task_key)["action"], "preserve")

    def test_preview_contract_scope_uses_id_not_cpf(self):
        other = Franchise(company_id=self.admin.company_id, name="Other", active=True)
        db.session.add(other); db.session.flush()
        leads = []
        for franchise in (self.admin.franchise_id, other.id):
            lead = Lead(nome_cliente="Mesmo CPF", cpf="00000000001", company_id=self.admin.company_id,
                        franchise_id=franchise, equipe="QA", consultor=self.admin.name,
                        status="Em andamento", concessao=date.today()-timedelta(days=45))
            db.session.add(lead); db.session.flush()
            workflow, stage = crm.workflow_stage_by_slug("30-dias", "60-dias")
            db.session.add(LeadWorkflow(lead_id=lead.id, workflow_id=workflow.id, current_stage=stage,
                                       fidelity_started_on=date.today(), status="em_andamento"))
            leads.append(lead)
        db.session.commit()
        tomorrow = (date.today()+timedelta(days=1)).isoformat()
        self.assertEqual(self.preview(lead_id=leads[0].id, effective_from=tomorrow).status_code, 200)
        self.assertEqual(self.preview(lead_id=leads[1].id, effective_from=tomorrow).status_code, 403)
        self.assertEqual(self.preview(lead_id=999999, effective_from=tomorrow).status_code, 403)
        self.assertEqual(self.preview(lead_id=leads[0].id, effective_from=date.today().isoformat()).status_code, 400)


if __name__ == '__main__':
    unittest.main()

import copy
import json
import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-task-editor-only"

import app as crm
import task_configuration as config
import task_configuration_editor as editor
from models import (AuditLog, Company, Franchise, Role, TaskConfigurationRevision,
                    User, WorkflowChecklistItem, db)


class TaskEditorTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        company, matrix = crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.admin = User(username="qa-editor", name="QA Admin", role=Role.ADMIN, active=True,
                          company_id=company.id, franchise_id=matrix.id)
        self.admin.apply_role_defaults()
        db.session.add(self.admin)
        db.session.commit()
        self.client = self.app.test_client()
        self.login(self.admin)
        self.scope = config.company_scope(company.id)
        self.base = config.current_revision(self.scope)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = user.id
            session["_csrf_token"] = "qa"

    def draft(self):
        draft = editor.start_draft(self.admin, self.base.fingerprint)
        db.session.commit()
        return draft

    def item(self, draft):
        content = json.loads(draft.content)
        active = {s["id"] for s in content["stages"] if s["active"]}
        return next(item for item in content["checklists"] if item["stage_id"] in active)

    def values(self, item, **changes):
        result = {name: item.get(name) for name in ("label", "description", "stage_id", "required", "active", "sort_order", "depends_on")}
        result["description"] = result["description"] or ""
        result.update(changes)
        return result

    def post(self, draft, item, **changes):
        return self.client.post(f"/admin/workflows/task-draft/{draft.id}/task",
                                headers={"X-CSRF-Token": "qa"}, json={"fingerprint": draft.fingerprint,
                                "key": item["key"], "task": self.values(item, **changes)})

    def test_get_has_no_side_effects_and_keeps_legacy_editor(self):
        count = TaskConfigurationRevision.query.count()
        page = self.client.get("/admin/workflows")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"tc-editor", page.data)
        self.assertEqual(TaskConfigurationRevision.query.count(), count)
        self.assertEqual(self.client.get("/admin/workflows?view=stages").status_code, 200)

    def test_create_is_idempotent_and_requires_csrf(self):
        url = "/admin/workflows/task-draft"
        payload = {"fingerprint": self.base.fingerprint}
        self.assertEqual(self.client.post(url, json=payload).status_code, 400)
        first = self.client.post(url, json=payload, headers={"X-CSRF-Token": "qa"})
        second = self.client.post(url, json=payload, headers={"X-CSRF-Token": "qa"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["revision_id"], second.json["revision_id"])
        self.assertTrue(first.json["is_draft"])

    def test_edit_only_changes_draft_and_audit(self):
        draft = self.draft()
        item = self.item(draft)
        actual = db.session.get(WorkflowChecklistItem, item["id"])
        label = actual.label
        base_content = self.base.content
        response = self.post(draft, item, label="Nova orientacao de tarefa")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(actual.label, label)
        self.assertEqual(self.base.content, base_content)
        self.assertEqual(config.current_revision(self.scope).id, self.base.id)
        self.assertEqual(AuditLog.query.filter_by(action="task_configuration_draft_task_saved").count(), 1)
        self.assertTrue(next(row for row in response.json["rows"] if row["key"] == item["key"])["changed"])
        crm.seed_default_workflows()
        self.assertIn("Nova orientacao", db.session.get(TaskConfigurationRevision, draft.id).content)

    def test_stale_edit_does_not_overwrite(self):
        draft = self.draft()
        item = self.item(draft)
        fingerprint = draft.fingerprint
        self.assertEqual(self.post(draft, item, label="Primeira edicao").status_code, 200)
        with self.assertRaises(editor.DraftConflict):
            editor.save_task(self.admin, draft.id, fingerprint, item["key"], self.values(item, label="Edicao antiga"))
        self.assertIn("Primeira edicao", draft.content)
        self.assertNotIn("Edicao antiga", draft.content)

    def test_cross_franchise_and_permission_checks(self):
        draft = self.draft()
        item = self.item(draft)
        branch = Franchise(company_id=self.admin.company_id, name="Branch", active=True)
        db.session.add(branch); db.session.flush()
        user = User(username="branch", name="Branch Admin", role=Role.ADMIN, active=True,
                    company_id=self.admin.company_id, franchise_id=branch.id)
        user.apply_role_defaults()
        user.can_view_all_franchises = True
        db.session.add(user); db.session.commit()
        self.login(user)
        self.assertEqual(self.client.get("/admin/workflows").status_code, 403)
        self.assertEqual(self.post(draft, item, label="Ataque").status_code, 403)
        self.login(self.admin)
        self.admin.can_manage_workflows = False
        db.session.commit()
        self.assertEqual(self.post(draft, item).status_code, 302)
        with self.assertRaises(PermissionError):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], self.values(item))

    def test_foreign_scope_revision_is_not_accessible(self):
        other_company = Company(name="Other", slug="other-editor", active=True)
        db.session.add(other_company); db.session.flush()
        other_matrix = Franchise(company_id=other_company.id, name="Matriz", active=True)
        db.session.add(other_matrix); db.session.flush()
        foreign = config.record_company_configuration(other_company.id, other_matrix.id, json.loads(self.base.content))
        db.session.commit()
        item = self.item(foreign)
        self.assertEqual(self.post(foreign, item).status_code, 403)

    def test_validation_and_published_protection(self):
        draft = self.draft()
        item = self.item(draft)
        old = draft.content
        for changes in ({"label": " "}, {"sort_order": -1}, {"sort_order": 1.5}, {"required": "false"},
                        {"stage_id": 999999}, {"description": "x" * 2001}, {"depends_on": [1]}):
            response = self.post(draft, item, **changes)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(draft.content, old)
        self.assertEqual(self.post(self.base, item, label="Mutacao publicada").status_code, 400)
        values = self.values(item)
        values["note_required"] = False
        with self.assertRaises(ValueError):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], values)

    def test_dependencies_reject_cycles_missing_and_cross_stage(self):
        draft = self.draft()
        item = self.item(draft)
        with self.assertRaisesRegex(ValueError, "circular"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], self.values(item, depends_on=item["key"]))
        with self.assertRaisesRegex(ValueError, "nao existe"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], self.values(item, depends_on="unknown"))
        rows = [dict(item, key="a", depends_on="b"), dict(item, key="b", depends_on="a")]
        with self.assertRaisesRegex(ValueError, "circular"):
            editor.validate_dependencies(rows)
        rows[1]["depends_on"] = None
        rows[1]["stage_id"] += 1
        with self.assertRaisesRegex(ValueError, "mesma etapa"):
            editor.validate_dependencies(rows)
        rows[1]["stage_id"] = item["stage_id"]
        rows[1]["active"] = False
        with self.assertRaisesRegex(ValueError, "inativa"):
            editor.validate_dependencies(rows)

    def test_new_task_and_dependency_are_draft_only(self):
        draft = self.draft()
        item = self.item(draft)
        count = WorkflowChecklistItem.query.count()
        editor.save_task(self.admin, draft.id, draft.fingerprint, None,
                         self.values(item, label="Confirmar documento adicional", depends_on=item["key"]))
        db.session.commit()
        self.assertEqual(WorkflowChecklistItem.query.count(), count)
        new = next(row for row in json.loads(draft.content)["checklists"] if row["key"].startswith("draft:"))
        self.assertEqual(new["depends_on"], item["key"])
        with self.assertRaisesRegex(ValueError, "inativa"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], self.values(item, active=False))

    def test_duplicate_names_and_moving_stage_are_rejected(self):
        draft = self.draft()
        item = self.item(draft)
        with self.assertRaisesRegex(ValueError, "nome"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, None, self.values(item, label="  "+item["label"].upper()+"  "))
        other = next(s for s in json.loads(draft.content)["stages"] if s["id"] != item["stage_id"])
        with self.assertRaisesRegex(ValueError, "transferida"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, item["key"], self.values(item, stage_id=other["id"]))

    def test_stale_base_is_visible_and_automatic_rules_are_consultation(self):
        self.draft()
        content = copy.deepcopy(json.loads(self.base.content))
        content["checklists"][0]["label"] = "Alteracao pelo painel antigo"
        config.record_company_configuration(self.admin.company_id, self.admin.franchise_id, content, self.admin)
        db.session.commit()
        state = editor.panel_state(self.admin, crm.SELLER_EXECUTION_TASKS)
        self.assertTrue(state["stale"])
        self.assertEqual({r["kind"] for r in state["rows"]}, {"checklist", "inss", "contact", "conditional"})
        self.assertTrue(all(r["editable"] for r in state["rows"] if r["kind"] == "conditional"))
        self.assertTrue(all(r["editable"] for r in state["rows"] if r["kind"] in {"inss", "contact"}))
        self.assertEqual([r["key"] for r in state["rows"] if r["kind"] == "inss"], [row[0] for row in crm.FIDELITY_INSS_TASKS])

    def test_legacy_and_automatic_stage_edits_are_rejected(self):
        draft = self.draft()
        state = editor.panel_state(self.admin, crm.SELLER_EXECUTION_TASKS)
        legacy = next(row for row in state["rows"] if row["kind"] == "checklist" and row["archived"])
        self.assertEqual(self.post(draft, legacy, label="Alterar legado").status_code, 400)
        item = self.item(draft)
        automatic_ids = {w["id"] for w in state["workflows"] if w["slug"] == "30-dias"}
        stage = next(s for s in state["stages"] if s["workflow_id"] in automatic_ids)
        with self.assertRaisesRegex(ValueError, "agenda"):
            editor.save_task(self.admin, draft.id, draft.fingerprint, None, self.values(item, label="Tarefa indevida", stage_id=stage["id"]))


if __name__ == "__main__":
    unittest.main()

"""Synthetic users/contracts for integrated QA; caller must provide a disposable DB."""

from datetime import datetime, timedelta

import app as crm
from models import (Franchise, Lead, LeadWorkflow, ReversalDistributionMode, ReversalTeamConfig,
                    Role, Team, User, db)


NOTE = "Contato realizado em QA. Procedimento conferido com o cliente e retorno registrado para a equipe."


class IntegratedFixture:
    def __init__(self):
        assert db.engine.url.database == ":memory:", "This fixture only runs in memory"
        db.create_all()
        self.company, self.matrix = crm.ensure_default_company_structure()
        crm.seed_default_options()
        crm.seed_default_workflows()
        self.branch = Franchise(company_id=self.company.id, name="QA Franquia B", active=True)
        db.session.add(self.branch); db.session.flush()
        self.team_name = "QA Integracao"
        self.matrix_team = Team(company_id=self.company.id, franchise_id=self.matrix.id, name=self.team_name, active=True)
        self.branch_team = Team(company_id=self.company.id, franchise_id=self.branch.id, name=self.team_name, active=True)
        db.session.add_all([self.matrix_team, self.branch_team]); db.session.flush()
        self.admin = self.make_user("admin", Role.ADMIN)
        self.owner = self.make_user("dono", Role.OPERADOR)
        self.reversor = self.make_user("reversor", Role.OPERADOR)
        self.reversor2 = self.make_user("reversor2", Role.OPERADOR)
        self.supervisor = self.make_user("supervisor", Role.SUPERVISOR)
        self.support = self.make_user("apoio", Role.OPERADOR, support=True)
        self.branch_admin = self.make_user("admin.b", Role.ADMIN, branch=True)
        self.branch_owner = self.make_user("dono.b", Role.OPERADOR, branch=True)
        self.branch_reversor = self.make_user("reversor.b", Role.OPERADOR, branch=True)
        self.branch_support = self.make_user("apoio.b", Role.OPERADOR, branch=True, support=True)
        crm.seed_default_reversal_configs()
        db.session.commit()
        self.mode(self.matrix_team, ReversalDistributionMode.AUTOMATIC)
        self.mode(self.branch_team, ReversalDistributionMode.MANUAL)
        self.counter = 0

    def make_user(self, key, role, branch=False, support=False):
        user = User(username="qa.s5."+key, name="QA "+key, lead_name="QA "+key,
                    role=role, active=True, must_change_password=False, team=self.team_name,
                    company_id=self.company.id, franchise_id=self.branch.id if branch else self.matrix.id)
        user.apply_role_defaults()
        user.can_view_all_franchises = key == "admin"
        if support:
            user.can_act_as_support = True
            user.can_work_reversal = False
            user.can_request_reversal = False
        db.session.add(user); db.session.flush()
        return user

    def mode(self, team, mode):
        config = ReversalTeamConfig.query.filter_by(team_id=team.id).one()
        config.distribution_mode = mode
        config.enabled = True
        db.session.commit()

    def contract(self, stage="fidelizacao", days=45, owner=None, cpf=None):
        self.counter += 1
        owner = owner or self.owner
        today = datetime.now().date()
        lead = Lead(company_id=self.company.id, franchise_id=owner.franchise_id,
                    cpf=cpf or f"{80000000000+self.counter:011d}", nome_cliente=f"QA S5 Cliente {self.counter:02d}",
                    equipe=self.team_name, consultor=crm.lead_assignment_name(owner), status="Em andamento",
                    data_venda=today, concessao=today+timedelta(days=days-90))
        db.session.add(lead); db.session.flush()
        workflow, target = crm.workflow_stage_by_slug("principal", stage)
        principal = LeadWorkflow(lead_id=lead.id, workflow_id=workflow.id, current_stage=target,
                                 owner_user_id=owner.id, status="em_andamento")
        db.session.add(principal); db.session.flush()
        tracking, _ = crm.ensure_temporary_workflow_for_principal(principal)
        if stage != "fidelizacao":
            crm.checklist_items_for_workflow(principal)
        db.session.commit()
        return lead, principal, tracking

    def client(self, app, user):
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["_csrf_token"] = "qa-integrated"
        return client

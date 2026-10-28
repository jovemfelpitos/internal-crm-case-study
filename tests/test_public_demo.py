"""Public demo setup must stay independent from operational data."""
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "public-demo-test-session-only"

import app as crm
from models import Lead, User, db


class PublicDemoTests(unittest.TestCase):
    def test_initial_password_is_required_and_validated(self):
        with patch.dict(os.environ, {"INITIAL_LOCAL_PASSWORD": ""}):
            with self.assertRaises(RuntimeError):
                crm.seed_local_password()
        with patch.dict(os.environ, {"INITIAL_LOCAL_PASSWORD": "admin123"}):
            with self.assertRaises(ValueError):
                crm.seed_local_password()
        with patch.dict(os.environ, {"INITIAL_LOCAL_PASSWORD": "Test-only-demo-pass-2026"}):
            self.assertEqual(crm.seed_local_password(), "Test-only-demo-pass-2026")

    def test_fresh_install_creates_only_demo_accounts_and_login_works(self):
        with patch.dict(os.environ, {
            "INITIAL_LOCAL_PASSWORD": "Test-only-demo-pass-2026",
            "ADMIN_EMAILS": "", "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "",
        }):
            application = crm.create_app()
            application.config["TESTING"] = True
            with application.app_context():
                result = application.test_cli_runner().invoke(args=["init-db"])
                self.assertEqual(result.exit_code, 0, str(result.exception))
                self.assertEqual(Lead.query.count(), 0)
                users = User.query.all()
                self.assertEqual(len(users), 6)
                self.assertTrue(all(user.email.endswith("@example.invalid") for user in users))
                self.assertTrue(all(user.check_password("Test-only-demo-pass-2026") for user in users))
                self.assertFalse(application.config["GOOGLE_AUTH_AVAILABLE"])

                client = application.test_client()
                self.assertEqual(client.get("/login").status_code, 200)
                with client.session_transaction() as session:
                    token = session["_csrf_token"]
                response = client.post("/login", data={
                    "username": "admin", "password": "Test-only-demo-pass-2026", "_csrf_token": token,
                })
                self.assertEqual(response.status_code, 302)
                self.assertEqual(client.get("/dashboard").status_code, 200)
                self.assertEqual(client.get("/workflows").status_code, 200)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()

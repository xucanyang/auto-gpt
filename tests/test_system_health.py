import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from api.system import build_system_health
from core import db as core_db
from core.db import AccountModel, ProxyModel
from services.chatgpt_core import phone_pool_repository as repo_module
from services.chatgpt_core.phone_pool_repository import PhonePoolRepository


class SystemHealthTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "system_health.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.core_engine_patch = mock.patch.object(core_db, "engine", self.engine)
        self.repo_engine_patch = mock.patch.object(repo_module, "engine", self.engine)
        self.core_engine_patch.start()
        self.repo_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db._ensure_phone_pool_schema()

    def tearDown(self):
        self.repo_engine_patch.stop()
        self.core_engine_patch.stop()
        self._tmpdir.cleanup()

    def test_database_backed_health_resources_include_accounts_proxies_and_phone_pool(self):
        with Session(self.engine) as session:
            session.add(AccountModel(platform="chatgpt", email="ok@example.com", password="pw", status="subscribed"))
            session.add(AccountModel(platform="chatgpt", email="bad@example.com", password="pw", status="invalid"))
            session.add(ProxyModel(url="http://127.0.0.1:18080", region="US", is_active=True))
            session.commit()

        repo = PhonePoolRepository()
        repo.add(phone="+15551230001", api_url="https://relay.example.com/a")

        with Session(self.engine) as session:
            payload = build_system_health(session, include_runtime=False)

        self.assertEqual(payload["summary"]["total"], 3)
        by_key = {item["key"]: item for item in payload["resources"]}
        self.assertEqual(by_key["accounts"]["metrics"]["total"], 2)
        self.assertEqual(by_key["accounts"]["metrics"]["invalid"], 1)
        self.assertEqual(by_key["proxies"]["metrics"]["active"], 1)
        self.assertEqual(by_key["phone_pool"]["metrics"]["available"], 1)
        self.assertEqual(by_key["phone_pool"]["status"], "healthy")

    def test_empty_resource_pools_are_visible_warnings_not_silent_success(self):
        with Session(self.engine) as session:
            payload = build_system_health(session, include_runtime=False)

        by_key = {item["key"]: item for item in payload["resources"]}
        self.assertEqual(by_key["accounts"]["status"], "warning")
        self.assertEqual(by_key["proxies"]["status"], "warning")
        self.assertEqual(by_key["phone_pool"]["status"], "warning")
        self.assertEqual(payload["summary"]["warning"], 3)


if __name__ == "__main__":
    unittest.main()

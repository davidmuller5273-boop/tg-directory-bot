from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from tg_directory_bot.config import Config
from tg_directory_bot.storage import DirectoryStore


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class WebTest(unittest.TestCase):
    def test_login_and_dashboard(self):
        from tg_directory_bot.web import create_app

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(
                bot_token="",
                admin_ids=set(),
                super_admin_ids=set(),
                db_path=Path(temp_dir) / "db.sqlite3",
                categories=("other",),
                blocked_keywords=(),
                admin_username="admin",
                admin_password="testing-password",
                session_secret="x" * 40,
                backups_dir=Path(temp_dir) / "backups",
            )
            with TestClient(create_app(config)) as client:
                page = client.get("/login")
                token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                response = client.post(
                    "/login",
                    data={"username": "admin", "password": "testing-password", "csrf_token": token},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                dashboard = client.get("/")
                self.assertEqual(dashboard.status_code, 200)
                self.assertIn("仪表盘", dashboard.text)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text).group(1)
                for path in (
                    "/entries", "/users", "/support", "/broadcasts", "/reports",
                    "/settings", "/backups", "/audit", "/groups", "/monitor", "/auto-replies",
                    "/lottery", "/admins",
                    "/search-stats",
                ):
                    self.assertEqual(client.get(path).status_code, 200, path)
                add_admin = client.post(
                    "/admins",
                    data={"user_id": "123456", "csrf_token": csrf},
                    follow_redirects=False,
                )
                self.assertEqual(add_admin.status_code, 303)
                self.assertIn("123456", client.get("/admins").text)
                auto_reply = client.post(
                    "/auto-replies",
                    data={
                        "keyword": "余额", "reply_text": "使用 /balance 查询",
                        "match_mode": "contains", "scope": "both", "csrf_token": csrf,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(auto_reply.status_code, 303)
                self.assertIn("余额", client.get("/auto-replies").text)
                moderation_keyword = client.post(
                    "/moderation-keywords",
                    data={"keyword": "违规词", "csrf_token": csrf},
                    follow_redirects=False,
                )
                self.assertEqual(moderation_keyword.status_code, 303)
                settings_page = client.get("/settings")
                self.assertIn("违规词", settings_page.text)
                store = DirectoryStore(config.db_path)
                rich_id = store.add_rich_submission(
                    "v8", "tgcontent://123/456", "原说明", 123, "tester",
                    file_id="photo-file-id", file_type="photo",
                )
                entries_page = client.get("/entries")
                self.assertIn("原说明", entries_page.text)
                rich_update = client.post(
                    f"/entries/{rich_id}/update",
                    data={
                        "title": "v8新版", "url": "tgcontent://123/456",
                        "content_text": "新说明", "category": "other",
                        "description": "后台修改", "csrf_token": csrf,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(rich_update.status_code, 303)
                self.assertEqual(store.get(rich_id).content_text, "新说明")
                store.record_search(
                    "888", 123, "tester", "Test User", 123, "private", "search_command", 1
                )
                search_stats = client.get("/search-stats")
                self.assertIn("关键词搜索量排名", search_stats.text)
                self.assertIn("Test User", search_stats.text)
                for user_id in range(1, 12):
                    store.record_group_activity(
                        -9001, "Web Paging Group", "webpaging", "supergroup",
                        user_id, f"webuser{user_id}", f"Web User {user_id}", messages=user_id,
                    )
                group_page_one = client.get("/groups?chat_id=-9001")
                self.assertEqual(group_page_one.status_code, 200)
                self.assertIn("共 11 人", group_page_one.text)
                self.assertNotIn("Web User 1</strong>", group_page_one.text)
                group_page_two = client.get("/groups?chat_id=-9001&speaker_page=2")
                self.assertEqual(group_page_two.status_code, 200)
                self.assertIn("Web User 1</strong>", group_page_two.text)
                self.assertIn("第 2/2 页", group_page_two.text)
                with patch(
                    "tg_directory_bot.chain.ChainService.okx_p2p_quotes",
                    new=AsyncMock(return_value=[]),
                ):
                    self.assertEqual(client.get("/chain").status_code, 200)
                backup = client.post("/backups", data={"csrf_token": csrf}, follow_redirects=False)
                self.assertEqual(backup.status_code, 303)
                self.assertEqual(len(list(config.backups_dir.glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()

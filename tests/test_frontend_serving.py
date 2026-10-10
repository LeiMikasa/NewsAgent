import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import Settings
from backend.app import create_app


class FrontendServingTest(unittest.TestCase):
    def test_only_public_build_is_served_and_api_routes_stay_separate(self):
        with tempfile.TemporaryDirectory(prefix="newsagent-ui-test-") as root:
            root = Path(root)
            dist = root / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text('<html lang="zh-CN">测试前端</html>', encoding="utf-8")
            (dist / "assets" / "app.js").write_text('console.log("test")', encoding="utf-8")
            (root / ".env").write_text("private-secret", encoding="utf-8")
            with patch("backend.frontend.FRONTEND_DIST", dist), TestClient(create_app(Settings(workdir=root))) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertIn("测试前端", client.get("/").text)
                self.assertEqual(client.get("/").headers["cache-control"], "no-cache")
                self.assertEqual(client.get("/assets/app.js").status_code, 200)
                self.assertEqual(client.get("/api/status").json()["busy"], False)
                self.assertEqual(client.get("/docs").status_code, 200)
                for path in ("/api/unknown", "/.env", "/.agent/backend.sqlite3", "/assets/%2e%2e/%2e%2e/.env"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404, path)
                    self.assertNotIn("private-secret", response.text)

    def test_missing_build_leaves_backend_available(self):
        with tempfile.TemporaryDirectory(prefix="newsagent-ui-missing-") as root:
            with patch("backend.frontend.FRONTEND_DIST", Path(root) / "absent"), TestClient(create_app(Settings(workdir=Path(root)))) as client:
                self.assertEqual(client.get("/").status_code, 503)
                self.assertIn("npm run build", client.get("/").text)
                self.assertEqual(client.get("/api/profile").status_code, 200)


if __name__ == "__main__":
    unittest.main()

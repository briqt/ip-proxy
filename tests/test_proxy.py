"""离线回归：不联网，用假 API 替换 http_json。运行：python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "ip-proxy", "scripts"))
import proxy  # noqa: E402

KEY = "hpx_TESTSECRET_should_never_print"
PLAN = {"id": "plan1", "productId": "residential-lite", "status": "active", "dataGbTotal": 5.0,
        "dataGbRemaining": 4.5, "expiresAt": None, "generate": {"protocols": ["http", "socks5"]}}


class FakeAPI:
    def __init__(self, plans=None, status=200):
        self.plans = [PLAN] if plans is None else plans
        self.status = status
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None, timeout=30, opener=None):
        self.calls.append((method, url, body))
        if self.status != 200:
            return self.status, {"error": "Invalid API key", "code": "unauthorized"}
        if url.endswith("/plans?status=all"):
            return 200, {"plans": self.plans}
        if url.endswith("/generate"):
            c = (body.get("country") or "any").lower()
            return 200, {"lines": [f"gw.example:6969:user-country-{c}:p@ss:w/rd"]}
        raise AssertionError(url)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["IP_PROXY_HOME"] = self.tmp.name
        os.environ["IP_PROXY_NO_UPDATE_CHECK"] = "1"
        self.cfg = os.path.join(self.tmp.name, "config.json")
        self._orig = proxy.http_json

    def tearDown(self):
        proxy.http_json = self._orig
        os.environ.pop("IP_PROXY_HOME", None)
        os.environ.pop("IP_PROXY_NO_UPDATE_CHECK", None)
        self.tmp.cleanup()

    def write_cfg(self, key=KEY, **extra):
        pc = {"api_key": key}
        pc.update(extra)
        with open(self.cfg, "w") as f:
            json.dump({"default_provider": "hproxy", "providers": {"hproxy": pc}}, f)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = proxy.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class ConfigTests(Base):
    def test_init_creates_600_and_never_overwrites(self):
        code, out, _ = self.run_main("config", "init")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), self.cfg)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        with open(self.cfg, "w") as f:
            f.write('{"keep": true}')
        code, _, err = self.run_main("config", "init")
        self.assertEqual(code, 0)
        self.assertIn("未覆盖", err)
        self.assertEqual(Path(self.cfg).read_text(), '{"keep": true}')

    def test_template_has_no_secret_and_matches_schema_keys(self):
        d = os.path.join(ROOT, "skills", "ip-proxy")
        tpl = json.loads(Path(os.path.join(d, "config.json")).read_text(encoding="utf-8"))
        schema = json.loads(Path(os.path.join(d, "config.schema.json")).read_text(encoding="utf-8"))
        for pc in tpl["providers"].values():
            self.assertEqual(pc["api_key"], "")
            self.assertTrue(set(pc) <= set(schema["properties"]["providers"]["additionalProperties"]["properties"]))
        self.assertTrue(set(tpl) <= set(schema["properties"]))

    def test_missing_config_exit2_with_path_and_fix(self):
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn(self.cfg, err)
        self.assertIn("config init", err)

    def test_invalid_json_exit2(self):
        open(self.cfg, "w").write("{bad")
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn("JSON", err)

    def test_empty_key_exit2(self):
        self.write_cfg(key="")
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn("api_key", err)


class ProviderTests(Base):
    def test_plans_compact(self):
        self.write_cfg()
        proxy.http_json = FakeAPI()
        code, out, _ = self.run_main("plans")
        self.assertEqual(code, 0)
        self.assertIn("plan1  residential-lite  active  4.500/5.0GB", out)

    def test_api_error_does_not_leak_key(self):
        self.write_cfg()
        proxy.http_json = FakeAPI(status=401)
        code, out, err = self.run_main("plans")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401 unauthorized", err)
        self.assertNotIn(KEY, out + err)

    def test_multiple_plans_require_choice(self):
        self.write_cfg()
        p2 = dict(PLAN, id="plan2", productId="residential-premium")
        proxy.http_json = FakeAPI(plans=[PLAN, p2])
        code, _, err = self.run_main("run", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 1)
        self.assertIn("--plan", err)

    def test_url_encoding_and_targeting(self):
        self.write_cfg(default_country="sg")
        api = FakeAPI()
        proxy.http_json = api
        prov = proxy.pick_provider(*proxy.load_config())
        url, label = prov.proxy_url(sticky=30)
        self.assertEqual(url, "http://user-country-sg:p%40ss%3Aw%2Frd@gw.example:6969")
        self.assertEqual(api.calls[-1][2], {"count": 1, "protocol": "http", "country": "SG", "stickyMinutes": 30})
        self.assertIn("sticky 30m", label)
        url, _ = prov.proxy_url(country="us", protocol="socks5", plan="planX")
        self.assertTrue(url.startswith("socks5h://"))
        self.assertTrue(api.calls[-1][1].endswith("/plans/planX/generate"))

    def test_run_injects_env_and_passes_exit_code(self):
        self.write_cfg()
        proxy.http_json = FakeAPI()
        child = ("import os,sys;u=os.environ.get('HTTPS_PROXY','');"
                 "sys.exit(7 if u.startswith('http://user-country-any') and os.environ.get('all_proxy')==u and os.environ.get('NODE_USE_ENV_PROXY')=='1' else 3)")
        code, out, err = self.run_main("run", "--", sys.executable, "-c", child)
        self.assertEqual(code, 7)
        self.assertIn("[hproxy any rotating http]", err)
        self.assertNotIn("p%40ss", out + err)

    def test_run_without_command(self):
        self.write_cfg()
        code, _, err = self.run_main("run", "--country", "SG")
        self.assertEqual(code, 2)
        self.assertIn("缺少要运行的命令", err)


class VersionTests(unittest.TestCase):
    def test_version_from_skill_md(self):
        text = Path(os.path.join(ROOT, "skills", "ip-proxy", "SKILL.md")).read_text(encoding="utf-8")
        v = re.search(r'version:\s*"([^"]+)"', text).group(1)
        self.assertEqual(proxy.version(), v)
        log = Path(os.path.join(ROOT, "CHANGELOG.md")).read_text(encoding="utf-8")
        self.assertIn(f"## {v}", log)
        notice = json.loads(Path(ROOT, "notice.json").read_text(encoding="utf-8"))
        self.assertEqual(notice["latest_version"], v)
        self.assertTrue(notice["release_url"].endswith(f"/v{v}"))


if __name__ == "__main__":
    unittest.main()

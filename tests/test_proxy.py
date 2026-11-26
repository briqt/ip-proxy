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
PLAN = {
    "id": "plan1",
    "productId": "residential-lite",
    "status": "active",
    "dataGbTotal": 5.0,
    "dataGbRemaining": 4.5,
    "expiresAt": None,
    "generate": {
        "protocols": ["http", "socks5"],
        "stickyTakesDuration": False,
        "minStickyMinutes": 0,
        "maxStickyMinutes": 10080,
        "targeting": {"country": True, "state": False, "city": False},
    },
}
PLAN_PREMIUM = {
    "id": "plan2",
    "productId": "residential-premium",
    "status": "active",
    "dataGbTotal": 2.0,
    "dataGbRemaining": 2.0,
    "expiresAt": None,
    "generate": {
        "protocols": ["http", "socks5"],
        "stickyTakesDuration": True,
        "minStickyMinutes": 3,
        "maxStickyMinutes": 1440,
        "targeting": {"country": True, "state": True, "city": True, "asn": True, "os": True},
    },
}
COUNTRIES = [
    {"code": "AU", "name": "Australia"},
    {"code": "SG", "name": "Singapore"},
    {"code": "KR", "name": "South Korea"},
    {"code": "US", "name": "United States"},
]


class FakeAPI:
    def __init__(self, plans=None, details=None, countries=None, status=200):
        self.plans = [PLAN] if plans is None else plans
        self.details = details or {}
        self.countries = COUNTRIES if countries is None else countries
        self.status = status
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None, timeout=30, opener=None):
        self.calls.append((method, url, body))
        if self.status != 200:
            return self.status, {"error": "Invalid API key", "code": "unauthorized"}
        if url.endswith("/plans?status=all"):
            return 200, {"plans": self.plans}
        if url.endswith("/geo/countries"):
            return 200, {"countries": self.countries}
        if url.endswith("/generate"):
            c = (body.get("country") or "any").lower()
            return 200, {"lines": [f"gw.example:6969:user-country-{c}:p@ss:w/rd"]}
        m = re.search(r"/plans/([^/?]+)$", url)
        if m and method == "GET":
            pid = m.group(1)
            base = next((dict(p) for p in self.plans if p["id"] == pid), {"id": pid})
            base.update(self.details.get(pid, {}))
            return 200, base
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
        with open(self.cfg, "w", encoding="utf-8") as f:
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
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write('{"keep": true}')
        code, _, err = self.run_main("config", "init")
        self.assertEqual(code, 0)
        self.assertIn("未覆盖", err)
        self.assertEqual(Path(self.cfg).read_text(encoding="utf-8"), '{"keep": true}')

    def test_config_show_redacts_secret_in_text_and_json(self):
        self.write_cfg(plan_id="plan2", default_country="sg", default_sticky=15, default_protocol="socks5")
        code, out, err = self.run_main("config", "show")
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, out + err)
        self.assertIn("api_key=<set>", out)
        self.assertIn("plan_id=plan2", out)
        self.assertIn("default_country=SG", out)
        self.assertIn("default_sticky=15m", out)
        self.assertIn("default_protocol=socks5", out)

        code, jout, jerr = self.run_main("config", "show", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, jout + jerr)
        parsed = json.loads(jout)
        self.assertEqual(parsed["providers"]["hproxy"]["api_key"], "<set>")
        self.assertEqual(parsed["config_path"], self.cfg)

    def test_config_show_allows_empty_key_after_init(self):
        self.run_main("config", "init")
        code, out, _ = self.run_main("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("api_key=<empty>", out)
        self.assertIn("default_sticky=rotating", out)

    def test_invalid_provider_defaults_exit2(self):
        self.write_cfg(default_sticky=True)
        code, _, err = self.run_main("config", "show")
        self.assertEqual(code, 2)
        self.assertIn("default_sticky", err)

        self.write_cfg(default_sticky=-5)
        code, _, _ = self.run_main("plans")
        self.assertEqual(code, 2)

        self.write_cfg(default_protocol="ftp")
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn("default_protocol", err)

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
        Path(self.cfg).write_text("{bad", encoding="utf-8")
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn("JSON", err)

    def test_empty_key_exit2(self):
        self.write_cfg(key="")
        code, _, err = self.run_main("plans")
        self.assertEqual(code, 2)
        self.assertIn("api_key", err)


class ProviderTests(Base):
    def test_plans_compact_and_capabilities(self):
        self.write_cfg(plan_id="plan2")
        proxy.http_json = FakeAPI(plans=[PLAN, PLAN_PREMIUM])
        code, out, _ = self.run_main("plans")
        self.assertEqual(code, 0)
        self.assertIn("plan1  residential-lite  active  4.500/5.0GB  sticky=hold geo=country", out)
        self.assertIn("plan2  residential-premium  active  2.000/2.0GB  default sticky=3-1440m geo=country,state,city,asn,os", out)

    def test_plans_refresh_merges_realtime_usage(self):
        self.write_cfg()
        api = FakeAPI(details={"plan1": {"dataGbRemaining": 4.125, "usageAsOf": "2026-11-26T09:30:00Z"}})
        proxy.http_json = api
        code, out, err = self.run_main("plans", "--refresh")
        self.assertEqual(code, 0)
        self.assertIn("4.125/5.0GB", out)
        self.assertIn("as_of=2026-11-26T09:30:00Z", out)
        self.assertIn("实时余量", err)
        self.assertTrue(any(u.endswith("/plans/plan1") for _, u, _ in api.calls))

    def test_countries_list_and_filter(self):
        self.write_cfg()
        proxy.http_json = FakeAPI()
        code, out, err = self.run_main("countries")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "AU KR SG US")
        self.assertIn("total=4", err)

        # 2-letter code "US" must match only US, not AU ("Australia")
        code, out, _ = self.run_main("countries", "us", "korea")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip().splitlines(), ["KR  South Korea", "US  United States"])

        code, out, err = self.run_main("countries", "ZZ")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("未匹配", err)

        code, jout, _ = self.run_main("countries", "SG", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(jout), [{"code": "SG", "name": "Singapore"}])

    def test_api_error_does_not_leak_key(self):
        self.write_cfg()
        proxy.http_json = FakeAPI(status=401)
        code, out, err = self.run_main("plans")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401 unauthorized", err)
        self.assertNotIn(KEY, out + err)

    def test_multiple_plans_require_choice(self):
        self.write_cfg()
        proxy.http_json = FakeAPI(plans=[PLAN, PLAN_PREMIUM])
        code, _, err = self.run_main("run", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 1)
        self.assertIn("--plan", err)

    def test_url_encoding_targeting_and_overrides(self):
        self.write_cfg(default_country="sg", default_sticky=20, default_protocol="socks5")
        api = FakeAPI()
        proxy.http_json = api
        prov = proxy.pick_provider(*proxy.load_config())

        # Uses config defaults when CLI args omitted
        url, label = prov.proxy_url()
        self.assertEqual(url, "socks5h://user-country-sg:p%40ss%3Aw%2Frd@gw.example:6969")
        self.assertEqual(api.calls[-1][2], {"count": 1, "protocol": "socks5", "country": "SG", "stickyMinutes": 20})
        self.assertEqual(label, "hproxy SG sticky 20m socks5")

        # --country any and --sticky 0 override config defaults back to unrestricted / rotating
        url, label = prov.proxy_url(country="any", sticky=0, protocol="http")
        self.assertTrue(url.startswith("http://user-country-any:"))
        self.assertEqual(api.calls[-1][2], {"count": 1, "protocol": "http"})
        self.assertEqual(label, "hproxy any rotating http")

        # Fine-grained state & city targeting
        url, label = prov.proxy_url(country="us", state="California", city="Los Angeles", sticky=30, protocol="http", plan="planX")
        self.assertEqual(api.calls[-1][2], {
            "count": 1, "protocol": "http", "country": "US",
            "state": "California", "city": "Los Angeles", "stickyMinutes": 30,
        })
        self.assertEqual(label, "hproxy US/California/Los Angeles sticky 30m http")
        self.assertTrue(api.calls[-1][1].endswith("/plans/planX/generate"))

    def test_city_without_country_rejected(self):
        self.write_cfg()
        code, _, err = self.run_main("run", "--city", "Tokyo", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("--country", err)

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
    def test_version_and_description_from_skill_md(self):
        text = Path(os.path.join(ROOT, "skills", "ip-proxy", "SKILL.md")).read_text(encoding="utf-8")
        v = re.search(r'version:\s*"([^"]+)"', text).group(1)
        self.assertEqual(proxy.version(), v)
        fm = text.split("---", 2)[1]
        self.assertNotIn("（", fm)
        self.assertNotIn("(", fm)
        log = Path(os.path.join(ROOT, "CHANGELOG.md")).read_text(encoding="utf-8")
        self.assertIn(f"## {v}", log)
        notice = json.loads(Path(ROOT, "notice.json").read_text(encoding="utf-8"))
        self.assertEqual(notice["latest_version"], v)
        self.assertTrue(notice["release_url"].endswith(f"/v{v}"))


if __name__ == "__main__":
    unittest.main()

"""更新检查：临时缓存目录、可控时钟、假 fetch，不联网。"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "ip-proxy", "scripts"))
import proxy  # noqa: E402
import update_check as uc  # noqa: E402

H = uc.HOUR_MS
D = uc.DAY_MS


def manifest(ver="0.2.0", nid="", notice=""):
    return {"schema_version": 1, "latest_version": ver,
            "release_url": f"https://github.com/briqt/ip-proxy/releases/tag/v{ver.lstrip('v')}",
            "notice_id": nid, "notice": notice}


def fetcher(obj=None, raw=None, exc=None):
    calls = []

    def f(url, timeout):
        calls.append(url)
        if exc:
            raise exc
        return raw if raw is not None else json.dumps(obj).encode()
    f.calls = calls
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, now, fetch, current="0.1.0", **kw):
        return uc.check_update(self.dir, current, now=now, fetch=fetch, emit=self.out.append, **kw)


class VersionLogic(unittest.TestCase):
    def test_semver_compare(self):
        self.assertTrue(uc.newer("0.10.0", "0.9.0"))
        self.assertTrue(uc.newer("v1.0.0", "0.9.9"))
        self.assertFalse(uc.newer("0.1.0", "0.1.0"))
        self.assertFalse(uc.newer("0.0.9", "0.1.0"))
        self.assertFalse(uc.newer("1.0.0-rc.1", "0.1.0"))
        self.assertFalse(uc.newer("garbage", "0.1.0"))

    def test_manifest_validation(self):
        self.assertEqual(uc.validate_manifest(manifest("v0.2.0"))["latest_version"], "0.2.0")
        bad = [dict(manifest(), schema_version=2), dict(manifest(), release_url="https://evil.example/x"),
               dict(manifest(), release_url="http://github.com/briqt/ip-proxy/releases/tag/v1"),
               dict(manifest(), notice_id="a", notice=""), dict(manifest(), notice="x" * 1001, notice_id="n")]
        for m in bad:
            with self.assertRaises(ValueError):
                uc.validate_manifest(m)
        self.assertEqual(uc.validate_manifest(manifest(nid="n1", notice="a\x1b[31mb\x07"))["notice"], "ab ")


class Flow(Base):
    def test_notify_once_then_cooldown_then_again(self):
        f = fetcher(manifest("0.2.0"))
        r = self.run_check(0, f)
        self.assertEqual((r["status"], r["update_available"]), ("checked", True))
        self.assertEqual(len(self.out), 1)
        self.assertIn("npx skills add briqt/ip-proxy -g -y", self.out[0])
        self.run_check(10 * 60_000, f)                      # 缓存内、冷却内：不联网、不重复
        self.assertEqual((len(f.calls), len(self.out)), (1, 1))
        self.run_check(D + 1, f)                            # 冷却后再提醒
        self.assertEqual(len(self.out), 2)

    def test_new_version_or_notice_bypasses_cooldown_and_retract_stops(self):
        self.run_check(0, fetcher(manifest("0.2.0")))
        self.run_check(2 * H, fetcher(manifest("0.3.0", "n1", "hello")))
        self.assertEqual(len(self.out), 3)
        self.assertIn("0.3.0", self.out[1])
        self.assertIn("hello", self.out[2])
        self.run_check(4 * H + 2 * D, fetcher(manifest("0.1.0")))  # 公告撤回、版本不新
        self.assertEqual(len(self.out), 3)

    def test_same_or_older_version_silent(self):
        for v in ("0.1.0", "0.0.9"):
            self.out.clear()
            uc.check_update(self.dir, "0.1.0", force=True, now=0, fetch=fetcher(manifest(v)), emit=self.out.append)
            self.assertEqual(self.out, [])

    def test_failure_backoff_keeps_old_manifest(self):
        self.run_check(0, fetcher(manifest("0.2.0")))
        r = self.run_check(2 * H, fetcher(exc=RuntimeError("offline")))
        self.assertEqual(r["status"], "unavailable")
        self.assertTrue(r["update_available"])
        st = json.loads((self.dir / "update_check.json").read_text())
        self.assertEqual(st["next_check_at"], 2 * H + uc.RETRY_MS)
        self.assertEqual(st["last_success_at"], 0)

    def test_invalid_or_oversized_manifest(self):
        for f in (fetcher(raw=b"not json"), fetcher(raw=b"{" + b" " * uc.MAX_BYTES + b"}"), fetcher({"schema_version": 9})):
            r = uc.check_update(self.dir, "0.1.0", force=True, now=0, fetch=f, emit=self.out.append)
            self.assertEqual(r["status"], "unavailable")
        self.assertEqual(self.out, [])

    def test_stale_cache_hides_tips(self):
        self.run_check(0, fetcher(manifest("0.2.0", "n1", "old")))
        self.out.clear()
        self.run_check(8 * D, fetcher(exc=RuntimeError("offline")))
        self.assertEqual(self.out, [])

    def test_corrupt_cache_and_future_clock(self):
        (self.dir / "update_check.json").write_text("{broken")
        self.assertEqual(self.run_check(0, fetcher(manifest()))["status"], "checked")
        st = json.loads((self.dir / "update_check.json").read_text())
        st["next_check_at"] = 10 ** 15
        (self.dir / "update_check.json").write_text(json.dumps(st))
        f = fetcher(manifest())
        self.run_check(H, f)
        self.assertEqual(len(f.calls), 1)

    def test_lock_conflict_skips(self):
        (self.dir / "update_check.lock").write_text(json.dumps({"pid": os.getpid(), "token": "x"}))
        f = fetcher(manifest())
        self.assertEqual(self.run_check(0, f)["status"], "busy")
        self.assertEqual(f.calls, [])

    def test_unwritable_dir_does_not_raise(self):
        r = uc.check_update(Path("/dev/null/nope"), "0.1.0", now=0, fetch=fetcher(manifest()), emit=self.out.append)
        self.assertEqual(r["status"], "unavailable")


class CliIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["IP_PROXY_HOME"] = self.tmp.name
        os.environ.pop(uc.DISABLE_ENV, None)
        self._fetch = uc._default_fetch
        self.calls = []

        def fake(url, timeout):
            self.calls.append(url)
            return json.dumps(manifest("9.0.0")).encode()
        uc._default_fetch = fake

    def tearDown(self):
        uc._default_fetch = self._fetch
        os.environ.pop("IP_PROXY_HOME", None)
        os.environ.pop(uc.DISABLE_ENV, None)
        self.tmp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = proxy.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_tip_goes_to_stderr_and_business_exit_code_kept(self):
        code, out, err = self.main("plans")          # 无配置 → 业务错误 2
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("[ip-proxy 更新] 9.0.0", err)

    def test_disable_paths_do_not_fetch(self):
        self.main("--no-update-check", "plans")
        os.environ[uc.DISABLE_ENV] = "1"
        self.main("plans")
        os.environ.pop(uc.DISABLE_ENV)
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            json.dump({"disable_update_check": True, "providers": {"hproxy": {"api_key": ""}}}, f)
        self.main("plans")
        self.main("--version")
        self.main("--help")
        self.assertEqual(self.calls, [])

    def test_check_update_command(self):
        code, out, _ = self.main("check-update")
        self.assertEqual(code, 0)
        self.assertIn("latest=9.0.0 update_available=true status=checked", out)


if __name__ == "__main__":
    unittest.main()

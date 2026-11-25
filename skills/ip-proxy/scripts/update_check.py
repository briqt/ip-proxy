"""可选的更新与公告检查：读分发仓库根目录的 notice.json，提示只写 stderr，失败不影响主命令。"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

ENDPOINT = "https://raw.githubusercontent.com/briqt/ip-proxy/main/notice.json"
UPGRADE_COMMAND = "npx skills add briqt/ip-proxy -g -y"
DISABLE_ENV = "IP_PROXY_NO_UPDATE_CHECK"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
RETRY_MS = 5 * 60_000
MAX_AGE_MS = 7 * DAY_MS
MAX_BYTES = 64 * 1024
SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[\da-zA-Z-]+(?:\.[\da-zA-Z-]+)*)?$")
RELEASE_PATH_RE = re.compile(r"^/briqt/ip-proxy/(?:releases/tag|tree)/[A-Za-z0-9._-]+$")
NOTICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{0,100}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")
TIME_FIELDS = ("last_success_at", "next_check_at", "last_notified_version_at", "last_notified_notice_at")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect disallowed", headers, fp)


def stable_version(value: Any):
    if not isinstance(value, str) or len(value) > 100:
        return None
    m = SEMVER_RE.match(value)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def newer(remote: Any, current: Any) -> bool:
    a, b = stable_version(remote), stable_version(current)
    return a is not None and b is not None and a > b


def validate_manifest(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not stable_version(value.get("latest_version")):
        raise ValueError("invalid manifest schema or version")
    url = value.get("release_url")
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError("invalid release url")
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or p.netloc != "github.com" or p.username or p.password or p.query or p.fragment
            or not RELEASE_PATH_RE.match(p.path)):
        raise ValueError("release url outside distribution repository")
    nid, notice = value.get("notice_id"), value.get("notice")
    if (not isinstance(nid, str) or not NOTICE_ID_RE.match(nid) or not isinstance(notice, str)
            or len(notice) > 1000 or bool(nid) != bool(notice)):
        raise ValueError("invalid notice")
    return {"schema_version": 1,
            "latest_version": value["latest_version"].lstrip("v").split("+")[0],
            "release_url": urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", "")),
            "notice_id": nid,
            "notice": CTRL_RE.sub(" ", ANSI_RE.sub("", notice))}


def _default_fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ip-proxy-skill"})
    # 只连固定分发端点，不跟随重定向；沿用本进程自身的网络环境（run 注入的代理只在子进程里）
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > MAX_BYTES:
                raise ValueError("manifest too large")
            return r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"update endpoint returned HTTP {e.code}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise RuntimeError("update check timed out or failed") from e


def fetch_manifest(timeout: float = 2.0, fetch: Callable[[str, float], bytes] | None = None) -> dict:
    raw = (fetch or _default_fetch)(ENDPOINT, timeout)
    if len(raw) > MAX_BYTES:
        raise ValueError("manifest too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError("manifest is not valid JSON") from e
    return validate_manifest(data)


def _ts(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def load_state(file: Path, now: int) -> dict:
    try:
        state = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    for k in TIME_FIELDS:
        if k in state and not _ts(state[k]):
            state.pop(k)
    # 时钟回拨：缓存时间明显在未来时整体重置，不永久抑制检查
    if state.get("next_check_at", 0) > now + HOUR_MS or any(state.get(k, 0) > now for k in TIME_FIELDS if k != "next_check_at"):
        return {}
    if "manifest" in state:
        try:
            state["manifest"] = validate_manifest(state["manifest"])
        except ValueError:
            state.pop("manifest")
    return state


def save_state(file: Path, state: dict) -> None:
    tmp = file.parent / f".{file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, file)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def evaluate(state: dict, current: str, now: int) -> list:
    """纯逻辑：根据有效缓存返回待输出的提示，不联网。"""
    m, ok = state.get("manifest"), state.get("last_success_at")
    if not isinstance(m, dict) or not _ts(ok) or now - ok > MAX_AGE_MS:
        return []
    tips = []
    latest = m["latest_version"]
    if newer(latest, current) and (state.get("last_notified_version") != latest
                                   or not _ts(state.get("last_notified_version_at"))
                                   or now - state["last_notified_version_at"] >= DAY_MS):
        tips.append({"kind": "version", "id": latest,
                     "text": f"[ip-proxy 更新] {latest} 可用（当前 {current}）：{m['release_url']}\n升级：{UPGRADE_COMMAND}"})
    nid = m.get("notice_id", "")
    if nid and not newer(current, latest) and (state.get("last_notified_notice") != nid
                                               or not _ts(state.get("last_notified_notice_at"))
                                               or now - state["last_notified_notice_at"] >= DAY_MS):
        tips.append({"kind": "notice", "id": nid, "text": f"[ip-proxy 公告] {m['notice']}"})
    return tips


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def acquire_lock(directory: Path):
    """短时非阻塞锁；拿不到返回 None。持有者进程已退出的陈旧锁会被回收。"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / "update_check.lock"
    token = uuid.uuid4().hex

    def take():
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "token": token}).encode())
        finally:
            os.close(fd)

        def release():
            try:
                cur = json.loads(lock.read_text(encoding="utf-8"))
                if isinstance(cur, dict) and cur.get("token") == token:
                    lock.unlink()
            except (OSError, ValueError):
                pass
        return release

    try:
        return take()
    except FileExistsError:
        pass
    reap = directory / "update_check.lock.reap"
    try:
        reap.mkdir()
    except OSError:
        return None
    try:
        try:
            owner = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = owner.get("pid") if isinstance(owner, dict) else None
        if not isinstance(pid, int) or _alive(pid):
            return None
        try:
            lock.unlink()
        except OSError:
            return None
        try:
            return take()
        except FileExistsError:
            return None
    finally:
        try:
            reap.rmdir()
        except OSError:
            pass


def disabled_by_env(env=None) -> bool:
    return (os.environ if env is None else env).get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def check_update(directory: Path, current: str, *, force: bool = False, now: int | None = None,
                 timeout: float = 2.0, fetch=None, emit=None) -> dict:
    """尽力而为：任何异常都只反映在返回值里，不抛出。"""
    now = int(time.time() * 1000) if now is None else now
    emit = emit or (lambda t: print(t, file=sys.stderr))
    release = None
    try:
        release = acquire_lock(directory)
        if release is None:
            return {"status": "busy", "current_version": current}
        cache = directory / "update_check.json"
        state = load_state(cache, now)
        status, err = "cached", None
        if force or "next_check_at" not in state or now >= state["next_check_at"]:
            try:
                state["manifest"] = fetch_manifest(timeout, fetch)
                state["last_success_at"] = now
                state["next_check_at"] = now + HOUR_MS
                status = "checked"
            except Exception as e:  # 网络、清单无效等
                state["next_check_at"] = now + RETRY_MS
                status, err = "unavailable", str(e) or "update check failed"
        tips = evaluate(state, current, now)
        for t in tips:
            emit(t["text"])
            state[f"last_notified_{t['kind']}"] = t["id"]
            state[f"last_notified_{t['kind']}_at"] = now
        save_state(cache, state)
        m = state.get("manifest") if isinstance(state.get("manifest"), dict) else None
        fresh = bool(m and _ts(state.get("last_success_at")) and now - state["last_success_at"] <= MAX_AGE_MS)
        out = {"status": status, "current_version": current,
               "latest_version": m["latest_version"] if m else None,
               "release_url": m["release_url"] if m else None,
               "update_available": bool(fresh and newer(m["latest_version"], current))}
        if err:
            out["error"] = err
        return out
    except Exception:
        return {"status": "unavailable", "current_version": current, "error": "update cache or lock unavailable"}
    finally:
        if release is not None:
            release()

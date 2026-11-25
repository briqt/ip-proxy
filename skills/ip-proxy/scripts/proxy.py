#!/usr/bin/env python3
"""ip-proxy：用已购的 IP 代理服务给单条命令换出口 IP。

命令：
  config init                    从模板创建用户配置（已存在则不覆盖）
  plans                          列出套餐、状态与剩余流量
  check [选线参数]               经代理请求一次出口信息，报出口 IP、国家、网络与耗时
  run [选线参数] -- <命令...>    只给这一个子进程注入代理环境变量，退出码透传
  check-update                   立即检查新版本与公告

命令结束后会尽力检查更新（最多约 2 秒，结果缓存 1 小时，提示只写 stderr）；
--no-update-check、环境变量 IP_PROXY_NO_UPDATE_CHECK=1 或配置 disable_update_check: true 可关闭。

选线参数：--country XX（两位国家码，省略则不限国家）、--sticky N（分钟，省略为轮换）、
--protocol http|socks5（默认 http）、--provider 名称、--plan ID。

配置：--config 路径 > $IP_PROXY_HOME/config.json > ~/.config/agent-skills/ip-proxy/config.json。
密钥只从配置读取，不接受命令行传入，也不出现在任何输出中。
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import update_check

SKILL = "ip-proxy"
HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TEMPLATE = os.path.join(SKILL_DIR, "config.json")
CHECK_URL = "https://ipinfo.io/json"
UA = "ip-proxy-skill"


class Fail(Exception):
    """预期内的错误：消息给 agent 看，退出码 1（配置类为 2）。"""

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def version():
    try:
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'^\s+version:\s*"?([^"\s]+)"?', head, re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


# ---------- 配置 ----------

def runtime_dir():
    home = os.environ.get("IP_PROXY_HOME")
    if home:
        return home
    return os.path.join(os.path.expanduser("~"), ".config", "agent-skills", SKILL)


def config_path(explicit=None):
    return explicit or os.path.join(runtime_dir(), "config.json")


def init_hint(path):
    return f"运行 python3 {os.path.join(HERE, 'proxy.py')} config init 创建，再填写 {path} 中的 api_key"


def load_config(explicit=None):
    path = config_path(explicit)
    if not os.path.exists(path):
        raise Fail(f"配置不存在：{path}\n修复：{init_hint(path)}", 2)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise Fail(f"配置无法读取：{path}（{type(e).__name__}: {e}）\n修复：改正该文件的 JSON 格式", 2)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("providers"), dict) or not cfg["providers"]:
        raise Fail(f"配置缺少 providers：{path}\n修复：参照模板 {TEMPLATE} 补齐", 2)
    if not isinstance(cfg.get("disable_update_check", False), bool):
        raise Fail(f"{path} 中 disable_update_check 须为 true 或 false", 2)
    return path, cfg


def update_check_disabled(args):
    if args.no_update_check or update_check.disabled_by_env():
        return True
    try:
        with open(config_path(args.config), encoding="utf-8") as f:
            cfg = json.load(f)
        return isinstance(cfg, dict) and cfg.get("disable_update_check") is True
    except (OSError, ValueError):
        return False


def pick_provider(path, cfg, name=None):
    name = name or cfg.get("default_provider")
    provs = cfg["providers"]
    if not name:
        if len(provs) == 1:
            name = next(iter(provs))
        else:
            raise Fail(f"未指定服务商，可用：{', '.join(provs)}\n修复：加 --provider <名称>，或在 {path} 设 default_provider", 2)
    if name not in provs:
        raise Fail(f"配置里没有服务商 {name}，可用：{', '.join(provs) or '无'}\n修复：在 {path} 的 providers 下补充", 2)
    if name not in PROVIDERS:
        raise Fail(f"脚本不支持服务商 {name}，支持：{', '.join(PROVIDERS)}", 2)
    pc = provs[name]
    if not isinstance(pc, dict) or not pc.get("api_key"):
        raise Fail(f"{path} 中 providers.{name}.api_key 为空\n修复：填写该字段（不要放在命令行或环境变量里）", 2)
    return PROVIDERS[name](name, pc)


def cmd_config_init(args):
    path = config_path(args.config)
    if os.path.exists(path):
        print(path)
        print("配置已存在，未覆盖", file=sys.stderr)
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    shutil.copyfile(TEMPLATE, tmp)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    print(path)
    print("已创建（权限 600）。需填写：providers.<名称>.api_key；可选 plan_id、default_country", file=sys.stderr)
    return 0


# ---------- HTTP ----------

def http_json(url, method="GET", headers=None, body=None, timeout=30, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "null")
        except ValueError:
            payload = None
        return e.code, payload


# ---------- 服务商 ----------

class HProxy:
    """HProxy（hproxy.com）。文档：https://hproxy.com/llms-full.txt"""

    DEFAULT_BASE = "https://hproxy.com/api/v1"

    def __init__(self, name, pc):
        self.name = name
        self.key = pc["api_key"]
        self.base = (pc.get("api_base") or self.DEFAULT_BASE).rstrip("/")
        self.plan_id = pc.get("plan_id") or ""
        self.default_country = pc.get("default_country") or ""

    def _call(self, path, method="GET", body=None):
        code, j = http_json(self.base + path, method, {"X-API-Key": self.key}, body)
        if code >= 400:
            err = (j or {}).get("code") or "error"
            msg = (j or {}).get("error") or ""
            hint = {401: "api_key 无效或已吊销：到服务商后台重建密钥后更新配置",
                    403: "密钥缺少所需权限（scope）：到后台给密钥加权限",
                    404: "套餐不存在或不属于该账户：去掉配置里的 plan_id 让脚本自动选择，或用 plans 查正确 ID",
                    429: "请求过快：稍后重试"}.get(code, "")
            raise Fail(f"{self.name} API {method} {path} → HTTP {code} {err} {msg}".rstrip()
                       + (f"\n修复：{hint}" if hint else ""))
        return j

    def plans(self):
        out = []
        for p in (self._call("/plans?status=all") or {}).get("plans", []):
            g = p.get("generate") or {}
            out.append({"id": p.get("id"), "product": p.get("productId") or p.get("product"),
                        "status": p.get("status"), "gb_remaining": p.get("dataGbRemaining"),
                        "gb_total": p.get("dataGbTotal"), "expires": p.get("expiresAt"),
                        "usage_as_of": p.get("usageAsOf"), "lines": bool(g),
                        "protocols": g.get("protocols"), "max_sticky_min": g.get("maxStickyMinutes")})
        return out

    def _plan(self):
        if self.plan_id:
            return self.plan_id
        usable = [p for p in self.plans() if p["status"] == "active" and p["lines"]]
        if len(usable) == 1:
            return usable[0]["id"]
        if not usable:
            raise Fail(f"{self.name} 没有可生成线路的 active 套餐\n修复：运行 plans 查看状态；流量用尽需到服务商充值")
        ids = ", ".join(f"{p['id']}({p['product']})" for p in usable)
        raise Fail(f"{self.name} 有多个可用套餐：{ids}\n修复：加 --plan <ID>，或在配置里设 plan_id")

    def proxy_url(self, country=None, sticky=None, protocol="http", plan=None):
        body = {"count": 1, "protocol": protocol}
        country = country or self.default_country
        if country:
            body["country"] = country.upper()
        if sticky:
            body["stickyMinutes"] = int(sticky)
        pid = plan or self._plan()
        lines = (self._call(f"/plans/{pid}/generate", "POST", body) or {}).get("lines") or []
        if not lines:
            raise Fail(f"{self.name} 未返回线路（plan {pid}）")
        host, port, user, pw = lines[0].split(":", 3)
        scheme = "socks5h" if protocol == "socks5" else "http"
        q = urllib.parse.quote
        label = f"{self.name} {country.upper() if country else 'any'} {'sticky ' + str(sticky) + 'm' if sticky else 'rotating'} {protocol}"
        return f"{scheme}://{q(user, safe='')}:{q(pw, safe='')}@{host}:{port}", label


PROVIDERS = {"hproxy": HProxy}


# ---------- 命令 ----------

def provider_from(args):
    path, cfg = load_config(args.config)
    return pick_provider(path, cfg, args.provider)


def cmd_plans(args):
    prov = provider_from(args)
    rows = prov.plans()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("无套餐")
        return 1
    for p in rows:
        rem = p["gb_remaining"]
        rem = f"{rem:.3f}" if isinstance(rem, (int, float)) else "?"
        extra = f" expires={p['expires']}" if p["expires"] else ""
        print(f"{p['id']}  {p['product']}  {p['status']}  {rem}/{p['gb_total']}GB{extra}")
    print(f"[{prov.name}] 列表中的剩余流量约每30分钟同步一次", file=sys.stderr)
    return 0


def proxy_env(url):
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[k] = url
        env[k.lower()] = url
    env["NODE_USE_ENV_PROXY"] = "1"  # 让较新 Node 的内置 fetch / http(s) 也读上面的变量
    return env


def cmd_check(args):
    prov = provider_from(args)
    url, label = prov.proxy_url(args.country, args.sticky, args.protocol, args.plan)
    print(f"[{label}]", file=sys.stderr)
    if args.protocol == "socks5":
        if not shutil.which("curl"):
            raise Fail("socks5 检查需要 curl，本机未找到")
        t = time.time()
        r = subprocess.run(["curl", "-s", "--max-time", str(args.timeout), CHECK_URL],
                           env=proxy_env(url), capture_output=True, text=True)
        ms = int((time.time() - t) * 1000)
        if r.returncode != 0:
            raise Fail(f"经代理请求失败：curl 退出码 {r.returncode}，耗时 {ms}ms\n修复：换 --country 或稍后重试")
        info = json.loads(r.stdout or "{}")
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
        t = time.time()
        try:
            code, info = http_json(CHECK_URL, timeout=args.timeout, opener=op)
        except Exception as e:  # 代理握手失败、超时等
            ms = int((time.time() - t) * 1000)
            raise Fail(f"经代理请求失败：{type(e).__name__}: {getattr(e, 'reason', e)}，耗时 {ms}ms\n"
                       "修复：502 多为该国家暂无可用 IP，换 --country；407 为线路认证失败，检查套餐状态")
        ms = int((time.time() - t) * 1000)
        if code != 200:
            raise Fail(f"检查地址返回 HTTP {code}，耗时 {ms}ms")
    info = info or {}
    print(f"ok exit={info.get('ip')} country={info.get('country')} org={info.get('org')!r} ms={ms}")
    return 0


def cmd_run(args):
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise Fail("缺少要运行的命令\n修复：proxy.py run [--country XX] -- <命令> [参数...]", 2)
    prov = provider_from(args)
    url, label = prov.proxy_url(args.country, args.sticky, args.protocol, args.plan)
    print(f"[{label}]", file=sys.stderr)
    try:
        return subprocess.call(cmd, env=proxy_env(url))
    except FileNotFoundError:
        raise Fail(f"找不到命令：{cmd[0]}")


def cmd_check_update(args):
    if update_check_disabled(args):
        print(f"current={version()} status=disabled")
        return 0
    r = update_check.check_update(Path(runtime_dir()), version(), force=True)
    line = f"current={r['current_version']} latest={r.get('latest_version')} update_available={str(r.get('update_available', False)).lower()} status={r['status']}"
    print(line)
    if r.get("error"):
        print(f"错误：{r['error']}", file=sys.stderr)
    return 0 if r["status"] == "checked" else 1


def build_parser():
    ap = argparse.ArgumentParser(
        prog="proxy.py", description="用已购的 IP 代理服务给单条命令换出口 IP",
        epilog="更新检查：业务命令结束后尽力检查（约 2 秒，成功缓存 1 小时，提示只写 stderr）。"
               f"关闭：--no-update-check、环境变量 {update_check.DISABLE_ENV}=1 或配置 \"disable_update_check\": true。")
    ap.add_argument("--version", action="version", version=version())
    ap.add_argument("--config", help="配置文件路径（默认 ~/.config/agent-skills/ip-proxy/config.json）")
    ap.add_argument("--provider", help="服务商名（默认取配置的 default_provider）")
    ap.add_argument("--no-update-check", action="store_true", help="本次不检查更新")
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("config", help="配置管理")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("init", help="从模板创建用户配置，已存在则不覆盖")

    p = sub.add_parser("plans", help="列出套餐与剩余流量")
    p.add_argument("--json", action="store_true", help="输出完整 JSON")

    def line_opts(sp):
        sp.add_argument("--country", help="两位国家码，如 SG、US；省略则不限国家（或用配置的 default_country）")
        sp.add_argument("--sticky", type=int, metavar="MIN", help="固定出口 IP 的分钟数；省略则每个新连接可换 IP")
        sp.add_argument("--protocol", choices=["http", "socks5"], default="http")
        sp.add_argument("--plan", help="套餐 ID（默认取配置的 plan_id，否则自动选唯一可用套餐）")

    ck = sub.add_parser("check", help="经代理请求一次出口信息")
    line_opts(ck)
    ck.add_argument("--timeout", type=int, default=25, help="秒，默认 25")

    r = sub.add_parser("run", help="只给这一个子进程注入代理环境变量，退出码透传")
    line_opts(r)
    r.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后为要运行的命令")

    sub.add_parser("check-update", help="立即检查新版本与公告")
    return ap


def dispatch(args):
    try:
        if args.command == "config":
            return cmd_config_init(args)
        return {"plans": cmd_plans, "check": cmd_check, "run": cmd_run}[args.command](args)
    except Fail as e:
        print(f"错误：{e}", file=sys.stderr)
        return e.code
    except urllib.error.URLError as e:
        print(f"错误：无法连接服务商 API：{e.reason}\n修复：检查本机网络后重试", file=sys.stderr)
        return 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "check-update":
        return cmd_check_update(args)
    code = dispatch(args)
    try:
        if not update_check_disabled(args):
            update_check.check_update(Path(runtime_dir()), version())
    except Exception:
        pass  # 可选检查，不影响业务结果
    return code


if __name__ == "__main__":
    sys.exit(main())

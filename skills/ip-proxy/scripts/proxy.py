#!/usr/bin/env python3
"""ip-proxy：用已购的 IP 代理服务给单条命令换出口 IP。

命令：
  config init                    从模板创建用户配置（已存在则不覆盖）
  config show [--json]           查看当前生效配置（api_key 自动脱敏）
  plans [--refresh] [--json]     列出套餐、状态、余量及粘性/定位能力；--refresh 实时刷新余量
  countries [筛选词...] [--json] 列出套餐池支持的国家，可按两位国家码或英文名筛选
  check [选线参数]               经代理请求一次出口信息，报出口 IP、国家、网络与耗时
  run [选线参数] -- <命令...>    只给这一个子进程注入代理环境变量，退出码透传
  check-update                   立即检查新版本与公告

命令结束后会尽力检查更新（最多约 2 秒，结果缓存 1 小时，提示只写 stderr）；
--no-update-check、环境变量 IP_PROXY_NO_UPDATE_CHECK=1 或配置 disable_update_check: true 可关闭。

选线参数：--country XX（两位国家码，传 any 单次不限国家）、--state 州名、--city 城市名、
--sticky N（分钟，传 0 单次切回轮换）、--protocol http|socks5、--provider 名称、--plan ID。

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
SECRET_HINTS = ("key", "secret", "token", "pass")
GEO_KEYS = ("country", "state", "city", "isp", "asn", "os")


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


def validate_provider_defaults(path, name, pc):
    sticky = pc.get("default_sticky", 0)
    if sticky in (None, ""):
        sticky = 0
    elif isinstance(sticky, bool) or not isinstance(sticky, int) or sticky < 0:
        raise Fail(f"{path} 中 providers.{name}.default_sticky 须为非负整数（0 表示轮换）", 2)
    proto = pc.get("default_protocol") or "http"
    if proto not in ("http", "socks5"):
        raise Fail(f"{path} 中 providers.{name}.default_protocol 须为 http 或 socks5", 2)
    return sticky, proto


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
    validate_provider_defaults(path, name, pc)
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
    print("已创建（权限 600）。需填写：providers.<名称>.api_key；可选 plan_id、default_country、default_sticky、default_protocol",
          file=sys.stderr)
    return 0


def cmd_config_show(args):
    path, cfg = load_config(args.config)
    provs = cfg["providers"]
    default_prov = cfg.get("default_provider") or (next(iter(provs)) if len(provs) == 1 else "")
    safe_provs = {}
    for name, pc in provs.items():
        if not isinstance(pc, dict):
            raise Fail(f"{path} 中 providers.{name} 须为对象", 2)
        validate_provider_defaults(path, name, pc)
        item = {}
        for k, v in pc.items():
            if k == "api_key" or any(s in k.lower() for s in SECRET_HINTS):
                item[k] = "<set>" if v else "<empty>"
            else:
                item[k] = v
        if "api_key" not in item:
            item["api_key"] = "<empty>"
        safe_provs[name] = item
    data = {
        "config_path": path,
        "default_provider": default_prov,
        "disable_update_check": bool(cfg.get("disable_update_check", False)),
        "providers": safe_provs,
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    print(f"path={path} default_provider={default_prov or 'none'} disable_update_check={str(data['disable_update_check']).lower()}")
    for name, pc in safe_provs.items():
        sticky, proto = validate_provider_defaults(path, name, provs[name])
        sticky_s = f"{sticky}m" if sticky else "rotating"
        country_s = (pc.get("default_country") or "").strip().upper() or "any"
        plan_s = pc.get("plan_id") or "auto"
        base_s = pc.get("api_base") or PROVIDERS[name].DEFAULT_BASE if name in PROVIDERS else (pc.get("api_base") or "")
        mark = "*" if name == default_prov else ""
        print(f"{name}{mark}  api_key={pc['api_key']}  plan_id={plan_s}  default_country={country_s}"
              f"  default_sticky={sticky_s}  default_protocol={proto}  api_base={base_s}")
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

def _sticky_summary(p):
    takes = p.get("sticky_takes_duration")
    lo, hi = p.get("min_sticky_min"), p.get("max_sticky_min")
    if takes is False:
        return "hold"
    if takes is True and hi:
        return f"{lo or 1}-{hi}m"
    if hi:
        return f"<={hi}m"
    return ""


class HProxy:
    """HProxy（hproxy.com）。文档：https://hproxy.com/llms-full.txt"""

    DEFAULT_BASE = "https://hproxy.com/api/v1"

    def __init__(self, name, pc):
        self.name = name
        self.key = pc["api_key"]
        self.base = (pc.get("api_base") or self.DEFAULT_BASE).rstrip("/")
        self.plan_id = pc.get("plan_id") or ""
        self.default_country = (pc.get("default_country") or "").strip()
        sticky = pc.get("default_sticky", 0)
        self.default_sticky = 0 if sticky in (None, "") else int(sticky)
        self.default_protocol = pc.get("default_protocol") or "http"

    def _call(self, path, method="GET", body=None):
        code, j = http_json(self.base + path, method, {"X-API-Key": self.key}, body)
        if code >= 400:
            err = (j or {}).get("code") or "error"
            msg = (j or {}).get("error") or ""
            hint = {400: "请求参数超出该套餐支持范围：运行 plans 查看套餐支持的 sticky 分钟数与 geo 定位维度",
                    401: "api_key 无效或已吊销：到服务商后台重建密钥后更新配置",
                    403: "密钥缺少所需权限（scope）：到后台检查密钥是否开启 proxies 权限",
                    404: "套餐不存在或不属于该账户：去掉配置里的 plan_id 让脚本自动选择，或用 plans 查正确 ID",
                    429: "请求过快：稍后重试"}.get(code, "")
            raise Fail(f"{self.name} API {method} {path} → HTTP {code} {err} {msg}".rstrip()
                       + (f"\n修复：{hint}" if hint else ""))
        return j

    def _format_plan(self, p):
        g = p.get("generate") or {}
        t = g.get("targeting") or {}
        pid = p.get("id")
        return {
            "id": pid,
            "product": p.get("productId") or p.get("product"),
            "status": p.get("status"),
            "default": bool(self.plan_id and pid == self.plan_id),
            "gb_remaining": p.get("dataGbRemaining"),
            "gb_total": p.get("dataGbTotal"),
            "expires": p.get("expiresAt"),
            "usage_as_of": p.get("usageAsOf"),
            "lines": bool(g),
            "protocols": g.get("protocols"),
            "sticky_takes_duration": g.get("stickyTakesDuration"),
            "min_sticky_min": g.get("minStickyMinutes"),
            "max_sticky_min": g.get("maxStickyMinutes"),
            "targeting": [k for k in GEO_KEYS if t.get(k)],
        }

    def plans(self, refresh=False):
        raw_list = (self._call("/plans?status=all") or {}).get("plans", [])
        out = []
        for p in raw_list:
            merged = dict(p)
            if refresh and p.get("id"):
                detail = self._call(f"/plans/{urllib.parse.quote(str(p['id']), safe='')}") or {}
                for k, v in detail.items():
                    if v is not None or k in ("expiresAt", "label"):
                        merged[k] = v
            out.append(self._format_plan(merged))
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

    def countries(self, plan=None):
        pid = plan or self._plan()
        enc = urllib.parse.quote(str(pid), safe="")
        rows = (self._call(f"/plans/{enc}/geo/countries") or {}).get("countries") or []
        out = [{"code": str(r.get("code") or "").upper(), "name": str(r.get("name") or "")}
               for r in rows if r.get("code")]
        out.sort(key=lambda r: r["code"])
        return pid, out

    def resolve_line_opts(self, country=None, state=None, city=None, sticky=None, protocol=None):
        if country is None:
            resolved_country = self.default_country.upper() if self.default_country else ""
        elif country.strip().lower() in ("", "any", "*"):
            resolved_country = ""
        else:
            resolved_country = country.strip().upper()

        resolved_state = (state or "").strip()
        resolved_city = (city or "").strip()
        if (resolved_state or resolved_city) and not resolved_country:
            raise Fail("--state / --city 需配合 --country（或配置 default_country）指定所属国家", 2)

        if sticky is None:
            resolved_sticky = self.default_sticky
        elif sticky < 0:
            raise Fail("--sticky 不能为负数（传 0 表示轮换）", 2)
        else:
            resolved_sticky = int(sticky)

        resolved_proto = protocol or self.default_protocol or "http"
        return resolved_country, resolved_state, resolved_city, resolved_sticky, resolved_proto

    def proxy_url(self, country=None, sticky=None, protocol=None, plan=None, state=None, city=None):
        c, st, ct, sk, proto = self.resolve_line_opts(country, state, city, sticky, protocol)
        body = {"count": 1, "protocol": proto}
        if c:
            body["country"] = c
        if st:
            body["state"] = st
        if ct:
            body["city"] = ct
        if sk > 0:
            body["stickyMinutes"] = sk
        pid = plan or self._plan()
        enc = urllib.parse.quote(str(pid), safe="")
        lines = (self._call(f"/plans/{enc}/generate", "POST", body) or {}).get("lines") or []
        if not lines:
            raise Fail(f"{self.name} 未返回线路（plan {pid}）")
        host, port, user, pw = lines[0].split(":", 3)
        scheme = "socks5h" if proto == "socks5" else "http"
        q = urllib.parse.quote
        geo_parts = [x for x in (c, st, ct) if x]
        geo_label = "/".join(geo_parts) if geo_parts else "any"
        mode_label = f"sticky {sk}m" if sk > 0 else "rotating"
        label = f"{self.name} {geo_label} {mode_label} {proto}"
        return f"{scheme}://{q(user, safe='')}:{q(pw, safe='')}@{host}:{port}", label


PROVIDERS = {"hproxy": HProxy}


# ---------- 命令 ----------

def provider_from(args):
    path, cfg = load_config(args.config)
    return pick_provider(path, cfg, args.provider)


def cmd_plans(args):
    prov = provider_from(args)
    rows = prov.plans(refresh=args.refresh)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("无套餐")
        return 1
    for p in rows:
        rem = p["gb_remaining"]
        rem = f"{rem:.3f}" if isinstance(rem, (int, float)) else "?"
        tags = []
        if p.get("default"):
            tags.append("default")
        sk = _sticky_summary(p)
        if sk:
            tags.append(f"sticky={sk}")
        if p.get("targeting"):
            tags.append(f"geo={','.join(p['targeting'])}")
        if p.get("expires"):
            tags.append(f"expires={p['expires']}")
        if args.refresh and p.get("usage_as_of"):
            tags.append(f"as_of={p['usage_as_of']}")
        suffix = f"  {' '.join(tags)}" if tags else ""
        print(f"{p['id']}  {p['product']}  {p['status']}  {rem}/{p['gb_total']}GB{suffix}")
    if args.refresh:
        print(f"[{prov.name}] 已从网关刷新实时余量（每套餐约每分钟更新一次）", file=sys.stderr)
    else:
        print(f"[{prov.name}] 列表中的剩余流量约每30分钟同步一次；实时刷新加 --refresh", file=sys.stderr)
    return 0


def _match_country(row, queries):
    code = row["code"]
    name_l = row["name"].lower()
    for raw in queries:
        q = raw.strip()
        if not q:
            continue
        if code == q.upper():
            return True
        if len(q) >= 3 and q.lower() in name_l:
            return True
        if not q.isascii() or not q.isalpha():
            if q.lower() in name_l:
                return True
    return False


def cmd_countries(args):
    prov = provider_from(args)
    pid, rows = prov.countries(plan=args.plan)
    queries = [q for q in (args.query or []) if q.strip()]
    matched = [r for r in rows if _match_country(r, queries)] if queries else rows
    if args.json:
        print(json.dumps(matched, ensure_ascii=False))
        return 0 if (matched or not queries) else 1
    if queries:
        if not matched:
            print(f"[{prov.name} plan={pid}] 未匹配到可用国家：{', '.join(queries)}", file=sys.stderr)
            return 1
        print(f"[{prov.name} plan={pid} matched={len(matched)}/{len(rows)}]", file=sys.stderr)
        for r in matched:
            print(f"{r['code']}  {r['name']}")
        return 0
    print(f"[{prov.name} plan={pid} total={len(rows)}]", file=sys.stderr)
    if rows:
        print(" ".join(r["code"] for r in rows))
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
    _, _, _, _, proto = prov.resolve_line_opts(args.country, args.state, args.city, args.sticky, args.protocol)
    url, label = prov.proxy_url(args.country, args.sticky, args.protocol, args.plan, args.state, args.city)
    print(f"[{label}]", file=sys.stderr)
    if proto == "socks5":
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
                       "修复：502/465 多为该地区暂无可用 IP，放宽 --city/--state 或换 --country；407 为线路认证失败，检查套餐状态")
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
    url, label = prov.proxy_url(args.country, args.sticky, args.protocol, args.plan, args.state, args.city)
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

    c = sub.add_parser("config", help="配置初始化与脱敏查看")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("init", help="从模板创建用户配置，已存在则不覆盖")
    cs = csub.add_parser("show", help="查看当前生效配置（api_key 自动脱敏）")
    cs.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("plans", help="列出套餐、状态、余量及支持的粘性/定位能力")
    p.add_argument("--refresh", action="store_true", help="逐套餐从网关刷新实时剩余流量")
    p.add_argument("--json", action="store_true", help="输出完整 JSON")

    ct = sub.add_parser("countries", help="列出套餐池支持的国家，可按两位国家码或英文名筛选")
    ct.add_argument("--plan", help="套餐 ID（默认取配置的 plan_id，否则自动选唯一可用套餐）")
    ct.add_argument("--json", action="store_true", help="输出完整 JSON")
    ct.add_argument("query", nargs="*", help="可选筛选词：两位国家码精确匹配（如 SG US），3 字符以上匹配英文国名")

    def line_opts(sp):
        sp.add_argument("--country", help="两位国家码（如 SG、US）；传 any 单次忽略配置的 default_country")
        sp.add_argument("--state", help="州/省名（如 California），需配合 --country 且套餐支持 state 定位")
        sp.add_argument("--city", help="城市名（如 Seattle），需配合 --country 且套餐支持 city 定位")
        sp.add_argument("--sticky", type=int, default=None, metavar="MIN",
                        help="固定出口 IP 的分钟数；传 0 单次忽略配置的 default_sticky 切回轮换")
        sp.add_argument("--protocol", choices=["http", "socks5"], default=None,
                        help="代理协议（默认取配置的 default_protocol，否则 http）")
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
            return {"init": cmd_config_init, "show": cmd_config_show}[args.action](args)
        return {"plans": cmd_plans, "countries": cmd_countries, "check": cmd_check, "run": cmd_run}[args.command](args)
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

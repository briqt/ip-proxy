---
name: ip-proxy
description: >-
  为单条命令或脚本临时切换代理出口 IP，支持按地区选线、轮换或固定会话，以及查询代理配置、套餐余量与可用地区。
  仅在用户明确要求走代理、换出口 IP 或查看代理套餐时使用；遇到限流、封禁或地区限制而用户未提代理时不自行启用。
metadata:
  version: "0.2.0"
---

# IP 代理

给单条命令换出口 IP 的工具箱：服务商清单、原始文档、已确认的用法，加一个只用 Python 标准库的对接脚本。

## 运行要求与配置

- Python 3.8+（只用标准库）；`--protocol socks5` 的 `check` 还需要 `curl`。
- 配置只读 `~/.config/agent-skills/ip-proxy/config.json`（`--config` 可指定别处，`IP_PROXY_HOME` 可把整个运行时目录指到临时位置）。字段说明见同目录 `config.schema.json`；skill 目录里的 `config.json` 只是模板。
- 查看当前配置用 `python3 scripts/proxy.py config show`（自动脱敏 `api_key`），不要直接读取或输出配置文件原文。没有配置时用 `python3 scripts/proxy.py config init` 生成空模板（权限 600），**由用户自己**填入密钥。密钥不写进命令行、环境变量、日志或对话。

## 用法

`scripts/` 相对本 skill 目录。完整选项看 `--help`。

```bash
python3 scripts/proxy.py config show                     # 查看生效配置（api_key 脱敏）
python3 scripts/proxy.py plans                           # 套餐、状态、余量及支持的粘性/定位能力
python3 scripts/proxy.py plans --refresh                 # 从网关实时刷新各套餐剩余流量
python3 scripts/proxy.py countries [SG US korea ...]     # 列出当前套餐支持的国家，或按国家码/英文名筛选
python3 scripts/proxy.py check --country SG              # 经代理取一次出口 IP / 国家 / 网络 / 耗时
python3 scripts/proxy.py run --country SG -- <命令...>   # 只给这个子进程设代理，退出码透传
python3 scripts/proxy.py run --country US --city Seattle --sticky 30 -- python3 job.py
```

- 默认值与单次覆盖：有多个可用套餐时，在配置设 `plan_id` 或命令加 `--plan <ID>`。配置设了 `default_country` 或 `default_sticky` 后，命令传 `--country any` 可单次放开国家限制，传 `--sticky 0` 可单次切回轮换。
- `run` 给子进程设置 `HTTP(S)_PROXY`、`ALL_PROXY`（含小写）和 `NODE_USE_ENV_PROXY=1`。各客户端是否跟随（用本地假代理逐个验证过）：

  | 客户端 | 是否走代理 |
  |---|---|
  | curl；Python urllib、requests、httpx | 是 |
  | Python aiohttp | 默认否；`ClientSession(trust_env=True)` 才走 |
  | Node 内置 `fetch`、`http`/`https` | 靠 `NODE_USE_ENV_PROXY=1`，较新的 Node 才认（24.15 验证有效），旧版本照样直连 |
  | Node axios | 是；但不解码 URL 里转义的密码，有 `NODE_USE_ENV_PROXY` 时改由 Node 处理才正常 |
  | Node node-fetch、got；浏览器 | 否，要在代码里显式配置代理 |

- **不跟随的程序不报错，而是悄悄直连**，请求从本机真实 IP 发出。大任务前用同一运行时、同一个库，经 `run` 请求一次 `https://ipinfo.io/ip`，确认出口已变。
- `--protocol socks5` 只适合支持 SOCKS 认证的程序（如 curl）；Python urllib 不支持，requests 需另装 SOCKS 扩展，Chromium 系浏览器不发送 SOCKS5 账号密码。默认的 `http` 适用面最广。
- 代理 URL 含密码，只存在于子进程环境里，不打印。子进程自己打印环境变量或代理 URL 时同样会泄露，别这样做。
- stderr 首行 `[服务商 地区 轮换/固定 协议]` 标明本次用的线路；`check` 成功时 stdout 一行 `ok exit=… country=… org=… ms=…`，失败退出码非 0。

## 用之前

1. **先看套餐能力与目标地区。** 用 `plans` 看手头套餐支持的定位粒度（`geo=`）和粘性规则（`sticky=`），用 `countries` 核对国家码，再用 `check` 实测目标地区。各国 IP 池随时变化，某地区暂无空闲 IP 时通常报 502 或 465，放宽 `--city`/`--state`、换 `--country` 或传 `--country any`。
2. **估流量。** 按实际传输的字节计费。大任务先用 `单次响应大小 × 次数` 估总量，再用 `plans`（或 `plans --refresh`）核对余量。
3. **轮换还是固定 IP，看任务。** 默认轮换：每个新连接可能换一个出口，适合分散请求。登录态、购物车、多步表单这类要求同一身份的场景用 `--sticky N`（分钟）。同一 `run` 进程内的固定会话保持同一出口；需要多个并行身份就分别起多个 `run`。HTTP 长连接（keep-alive）复用期间出口不变。
4. **频率照旧克制。** 代理换的是出口，不是对方的容忍度：按目标站点能承受的速度请求，出现断连、403、429 时放慢，不要靠加线程或加 IP 硬冲。
5. **目标站点可能拒绝海外或住宅 IP。** 这属于站点策略，不是代理故障：换国家或换数据源。

## 服务商

| 服务商 | 接入范围 | 原始文档（以此为准） | 认证 |
|---|---|---|---|
| HProxy（`hproxy`） | 住宅等按流量计费的网关线路（`plans` / `countries` / 生成线路）；未接入下单、独享 IP、Scraper API | 给 agent 读：<https://hproxy.com/llms-full.txt>（全文）、<https://hproxy.com/llms.txt>（索引）；网页：<https://hproxy.com/docs>；OpenAPI：`https://hproxy.com/api/v1/openapi.json` | `X-API-Key: hpx_…`，API 根 `https://hproxy.com/api/v1` |

### HProxy

- **套餐与生成模型**：每种住宅产品对应一个套餐、共用一组底层凭据，套餐本身不绑定国家，全球共用同一流量池。`POST /plans/{id}/generate` 把国家/城市与会话写进用户名后返回 `host:port:user:pass` 线路，可反复免费生成，不换底层密码；流量用尽（`used_up`）后续费原套餐自动恢复。
- **住宅档位差异**（由 `plans` 输出的 `sticky=` 与 `geo=` 直接反映）：
  - `residential-premium`：粘性按分钟精确计时（`sticky=3-1440m`，传低于下限的值如 `1` 会报 400）；定位支持 `country`、`state`、`city` 等细粒度参数。
  - `residential-lite`：仅支持国家级定位（`geo=country`，传 `--state`/`--city` 会报 400）；粘性池不设分钟时长（`sticky=hold`，`--sticky` 传任意正整数即开启粘性，只要持续复用该线路就保持同一出口，住宅设备离线仍可能提前切换）。
- **余量与区域查询**：`plans` 列表读取约 30 分钟同步一次的账单缓存；刚跑完任务要核对实时扣减时用 `plans --refresh`（调 `GET /plans/{id}` 从网关实时读表，每套餐约每分钟可刷新一次；网关自身的流量上限实时生效）。`countries` 调 `GET /plans/{id}/geo/countries` 返回该套餐所在池支持的国家清单。
- **密钥权限（scope）**：日常使用只需 `proxies` 权限即可完成 `plans`、`countries` 与生成线路；钱包（`/wallet`）、下单（`/orders`）及产品目录级州/城市枚举（`/products/{id}/geo/*`）需要 `buy` 权限，缺权限返回 `403 missing_scope`。本脚本不调用任何扣费接口。
- **API 与网关状态码**：API 错误看 `code` 字段，429 按 `Retry-After` 退避。线路网关本身返回：407 认证错（重新生成线路）；465 该定位暂无可用 IP（放宽定位）；466 套餐流量用尽；568 固定会话异常（重新生成换会话）；其他 5xx 退避重试。

## 更新

```bash
npx skills add briqt/ip-proxy -g -y
python3 scripts/proxy.py --version     # 核对已装版本
```

脚本命令结束后会尽力检查新版本与公告（读本仓库根目录的 `notice.json`，最多约 2 秒），提示只写 stderr、不改变 stdout 与退出码。提示只是告知，有没有升级授权看用户；`check-update` 立即检查，关闭方式见 `--help`。

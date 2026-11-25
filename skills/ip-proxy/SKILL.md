---
name: ip-proxy
description: >-
  经已购代理服务（如 HProxy）的出口 IP 发出网络请求：给任意命令或脚本临时套上代理，可选国家、轮换或固定 IP，
  并查询套餐余量与出口可用性。用户明确要求走代理、换出口 IP 或使用 IP 池时使用；
  遇到限流、封禁或地区限制而用户没提代理时不自行启用，先说明情况由用户决定。
metadata:
  version: "0.1.0"
---

# IP 代理

给单条命令换出口 IP 的工具箱：服务商清单、原始文档、已确认的用法，加一个只用 Python 标准库的对接脚本。

## 运行要求与配置

- Python 3.8+（只用标准库）；`--protocol socks5` 的 `check` 还需要 `curl`。
- 配置只读 `~/.config/agent-skills/ip-proxy/config.json`（`--config` 可指定别处，`IP_PROXY_HOME` 可把整个运行时目录指到临时位置）。字段说明见同目录 `config.schema.json`；skill 目录里的 `config.json` 只是模板。
- 没有配置时：`python3 scripts/proxy.py config init` 生成空模板（权限 600），**由用户自己**把密钥填进去。密钥不写进命令行、环境变量、日志或对话。

## 用法

`scripts/` 相对本 skill 目录。完整选项看 `--help`。

```bash
python3 scripts/proxy.py plans                          # 套餐、状态、剩余流量
python3 scripts/proxy.py check --country SG             # 经代理取一次出口 IP / 国家 / 网络 / 耗时
python3 scripts/proxy.py run --country SG -- <命令...>   # 只给这个子进程设代理，退出码透传
python3 scripts/proxy.py run --country US --sticky 30 -- python3 job.py
```

- `run` 给子进程设置 `HTTP(S)_PROXY`、`ALL_PROXY`（含小写）和 `NODE_USE_ENV_PROXY=1`。各客户端是否跟随（用本地假代理逐个验证过）：

  | 客户端 | 是否走代理 |
  |---|---|
  | curl；Python urllib、requests、httpx | 是 |
  | Python aiohttp | 默认否；`ClientSession(trust_env=True)` 才走 |
  | Node 内置 `fetch`、`http`/`https` | 靠 `NODE_USE_ENV_PROXY=1`，较新的 Node 才认（24.15 验证有效），旧版本照样直连 |
  | Node axios | 是；但不解码 URL 里转义的密码，有 `NODE_USE_ENV_PROXY` 时改由 Node 处理才正常 |
  | Node node-fetch、got；浏览器 | 否，要在代码里显式配置代理 |

- **不跟随的程序不报错，而是悄悄直连**，请求从本机真实 IP 发出。大任务前用同一运行时、同一个库，经 `run` 请求一次 `https://ipinfo.io/ip`，确认出口已变。
- `--protocol socks5` 只适合支持 SOCKS 的程序（如 curl）；Python urllib 不支持，requests 需另装 SOCKS 扩展。默认的 http 适用面最广。
- 代理 URL 含密码，只存在于子进程环境里，不打印。子进程自己打印环境变量或代理 URL 时同样会泄露，别这样做。
- stderr 首行 `[服务商 国家 轮换/固定 协议]` 标明本次用的线路；`check` 成功时 stdout 一行 `ok exit=… country=… org=… ms=…`，失败退出码非 0。

## 用之前

1. **先 `check` 目标国家。** 各国 IP 池随时变化，某国没有可用 IP 时通常表现为 502。换个国家，或者不加 `--country`。
2. **估流量。** 按实际传输的字节计费。大任务先用 `单次响应大小 × 次数` 估总量，再用 `plans` 看余量够不够。
3. **轮换还是固定 IP，看任务。** 默认轮换：每个新连接可能换一个出口，适合分散请求。登录态、购物车、多步表单这类要求同一身份的场景用 `--sticky N`（分钟）。同一 `run` 进程内的固定会话保持同一出口；需要多个并行身份就分别起多个 `run`。HTTP 长连接（keep-alive）复用期间出口不变。
4. **频率照旧克制。** 代理换的是出口，不是对方的容忍度：按目标站点能承受的速度请求，出现断连、403、429 时放慢，不要靠加线程或加 IP 硬冲。
5. **目标站点可能拒绝海外或住宅 IP。** 这属于站点策略，不是代理故障：换国家或换数据源。

## 服务商

| 服务商 | 接入范围 | 原始文档（以此为准） | 认证 |
|---|---|---|---|
| HProxy（`hproxy`） | 住宅等按流量计费的网关线路（`plans` / 生成线路）；未接入下单、独享 IP、Scraper API | 给 agent 读：<https://hproxy.com/llms-full.txt>（全文）、<https://hproxy.com/llms.txt>（索引）；网页：<https://hproxy.com/docs>；OpenAPI 见文档 “OpenAPI spec” 一节 | `X-API-Key: hpx_…`，API 根 `https://hproxy.com/api/v1` |

### HProxy

- **模型**：一个账户的每种住宅产品只有一个套餐和一组账号密码。`POST /plans/{id}/generate` 把国家、会话写进用户名后返回 `host:port:user:pass` 线路，可以反复生成、不另计费，也不会生成新的凭据。流量用完（`used_up`）后充值，原套餐自动恢复。
- **密钥权限**：密钥按 scope 授权，缺权限时返回 `403 missing_scope`。读套餐、生成线路都不动钱包余额；下单（`POST /orders`）才扣钱，本脚本不调用。下单类密钥可以在后台设每日消费上限。
- **余量显示**：`plans` 列表里的余量约 30 分钟同步一次，刚跑完的任务可能还没扣。
- **API 错误**：判断用 `code` 字段，不用 message 文案；429 按 `Retry-After` 退避。
- **网关状态码**（线路本身返回）：407 认证错（重新生成线路）；465 该定位暂无 IP（放宽定位）；466 套餐流量用尽；568 固定会话异常（换会话，即重新生成）；其他 5xx 退避重试。各码在各产品上的含义以原始文档 “Gateway status codes” 为准。

## 更新

```bash
npx skills add briqt/ip-proxy -g -y
python3 scripts/proxy.py --version     # 核对已装版本
```

脚本命令结束后会尽力检查新版本与公告（读本仓库根目录的 `notice.json`，最多约 2 秒），提示只写 stderr、不改变 stdout 与退出码。提示只是告知，有没有升级授权看用户；`check-update` 立即检查，关闭方式见 `--help`。

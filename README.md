# ip-proxy

给 agent 用的 IP 代理工具箱：可用的代理服务商、原始文档地址、已确认的用法，以及一个只用 Python 标准库的对接脚本，用来给单条命令换出口 IP，或查询当前代理配置、套餐余量与可用地区。只有在用户明确要求使用 IP 代理或查看代理套餐时才触发。

```bash
npx skills add briqt/ip-proxy -g -y
```

## 首次配置

```bash
python3 ~/.agents/skills/ip-proxy/scripts/proxy.py config init   # 生成 ~/.config/agent-skills/ip-proxy/config.json（权限 600）
```

在生成的文件里填写 `providers.hproxy.api_key`。密钥只放在这个文件里；之后可用 `config show` 脱敏查看当前配置。

## 用法

```bash
python3 scripts/proxy.py config show
python3 scripts/proxy.py plans
python3 scripts/proxy.py plans --refresh
python3 scripts/proxy.py countries SG US
python3 scripts/proxy.py check --country SG
python3 scripts/proxy.py run --country SG -- curl -s https://ipinfo.io/json
```

`scripts/` 相对已安装的 skill 目录。细节见 [SKILL.md](skills/ip-proxy/SKILL.md)。

## 更新

```bash
npx skills add briqt/ip-proxy -g -y
```

脚本运行后会读本仓库的 [notice.json](notice.json) 提示新版本（只写 stderr，约 1 小时联网一次）；`--no-update-check`、`IP_PROXY_NO_UPDATE_CHECK=1` 或配置 `"disable_update_check": true` 可关闭。

## 设计取舍

- **只在用户明确要求时启用**：代理按流量计费，且换出口可能改变目标站点对请求的处理；遇到限流或封禁时由用户决定要不要用。
- **不提供打印代理 URL 或密钥的命令**：代理 URL 含密码、`config.json` 含 `api_key`，打印出来就会进入 agent 的上下文和日志；`run` 只把代理放进子进程环境变量，`config show` 对密钥做脱敏输出。
- **不实现下单、充值**：花钱的操作留给用户在服务商后台完成，日常只需给密钥授予最小的 `proxies` 权限。
- **注入 `NODE_USE_ENV_PROXY=1`**：Node 内置 `fetch` / `http(s)` 默认忽略代理变量并静默直连，axios 会原样发送 URL 里转义过的密码；设上它后，较新的 Node 由内置实现统一处理。其余不跟随变量的库只能在代码里配置，所以 SKILL.md 要求大任务前用同一个库核对出口。
- **动态能力靠脚本现查，SKILL.md 不写个人账户实测**：套餐粘性范围、定位粒度、实时余量与可用国家由 `plans` / `countries` / `check` 现查，静态机制差异写在 SKILL.md 的服务商小节。

## 仓库结构

```
README.md          本文件
CHANGELOG.md       发版记录
notice.json        更新检查读取的最新版本与公告
tests/             离线回归（不联网）
skills/ip-proxy/   安装时被整体复制的单元
├── SKILL.md       产品本体：用法、最佳实践、服务商表与平台要点
├── config.json    配置模板（不含密钥）
├── config.schema.json
└── scripts/
    ├── proxy.py
    └── update_check.py
```

## 许可

MIT

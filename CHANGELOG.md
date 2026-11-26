# Changelog

## 0.2.0 — 2026-11-26

- 查询命令扩展：新增 `config show`（脱敏输出当前配置，不回显 `api_key`）、`plans --refresh`（调单套餐接口从网关实时刷新剩余流量，默认列表同步展示套餐是否为默认、支持的粘性规则 `sticky=` 与定位维度 `geo=`）、`countries`（查询套餐池可用国家，支持按两位国家码或英文国名筛选）。
- 配置与选线增强：配置新增可选的 `default_sticky`、`default_protocol`；`check` 与 `run` 新增 `--state`、`--city` 细粒度选线，并支持 `--country any`、`--sticky 0` 单次覆盖配置默认值。
- 文档改进：精简 `SKILL.md` 的 `description`，补充 HProxy 各住宅产品档位的粘性与定位差异、实时余量刷新机制及 `proxies` 最小权限说明。

## 0.1.0 — 2026-11-25

首个发布。

- 接入 HProxy 按流量计费的网关线路：`plans` 查套餐与流量，`check` 测出口，`run` 只给单个子进程注入代理环境变量（含 `NODE_USE_ENV_PROXY=1`），代理密码不输出。SKILL.md 列出常见客户端是否跟随这些变量（本地假代理验证），不跟随的会静默直连。
- 配置只读 `~/.config/agent-skills/ip-proxy/config.json`（`--config`、`IP_PROXY_HOME` 可改），密钥不进命令行与输出。
- 更新检查：命令结束后读仓库根目录的 `notice.json`（约 2 秒预算，成功缓存 1 小时、失败 5 分钟后重试，同一提示 24 小时一次），提示只写 stderr；`check-update` 立即检查；`--no-update-check`、`IP_PROXY_NO_UPDATE_CHECK=1` 或配置 `disable_update_check` 关闭。

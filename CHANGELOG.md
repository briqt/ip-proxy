# Changelog

## 0.1.0 — 2026-11-25

首个发布。

- 接入 HProxy 按流量计费的网关线路：`plans` 查套餐与流量，`check` 测出口，`run` 只给单个子进程注入代理环境变量（含 `NODE_USE_ENV_PROXY=1`），代理密码不输出。SKILL.md 列出常见客户端是否跟随这些变量（本地假代理验证），不跟随的会静默直连。
- 配置只读 `~/.config/agent-skills/ip-proxy/config.json`（`--config`、`IP_PROXY_HOME` 可改），密钥不进命令行与输出。
- 更新检查：命令结束后读仓库根目录的 `notice.json`（约 2 秒预算，成功缓存 1 小时、失败 5 分钟后重试，同一提示 24 小时一次），提示只写 stderr；`check-update` 立即检查；`--no-update-check`、`IP_PROXY_NO_UPDATE_CHECK=1` 或配置 `disable_update_check` 关闭。

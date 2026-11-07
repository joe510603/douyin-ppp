# Douyin PPP — LaunchAgent 部署指南

> macOS 开机自启守护方案，参考你 autoengine-monitor 的成熟模式。

## 架构

```
   launchd (用户登录时启动)
       │
       ▼
   ~/Library/LaunchAgents/com.douyin-ppp.app.plist
       │  ProgramArguments: /Users/jack/bin/start-douyin.sh start
       ▼
   start-douyin.sh  (守护进程，自身不退出)
       │
       ├── node server.js  (签名服务，端口 3010)
       │
       └── python -u app.py  (主程序，端口 9527)
              │
              └─→ http://localhost:9527  (NiceGUI Web UI)
```

## 安装路径

| 文件 | 路径 | 说明 |
|---|---|---|
| 项目根 | `/Users/jack/github/douyin-ppp-main/` | 源代码 |
| Python 3.12 | `/Users/jack/.python-standalone/python/` | standalone tarball，无 brew |
| 项目 venv | `/Users/jack/github/douyin-ppp-main/.venv/` | pip 装在 venv 里 |
| 启动 wrapper | `/Users/jack/bin/start-douyin.sh` | 守护循环 |
| LaunchAgent | `~/Library/LaunchAgents/com.douyin-ppp.app.plist` | launchd 加载 |
| 日志 | `~/Library/logs/douyin-ppp/{app,signer}.log` | 各服务日志 |
| 数据 | `/Users/jack/github/douyin-ppp-main/data/{db,exports,logs,browser_data,xhs_browser_data}` | 持久化 |
| PID | `/Users/jack/github/douyin-ppp-main/data/{app,signer}.pid` | 当前 PID |

## 一次性安装步骤（已在本机完成）

```bash
# 1) Python 3.12.3（standalone，无 brew）
mkdir -p /Users/jack/.python-standalone
curl -fsSL -o /tmp/python.tar.gz \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20240415/cpython-3.12.3+20240415-aarch64-apple-darwin-install_only.tar.gz"
tar -xzf /tmp/python.tar.gz -C /Users/jack/.python-standalone

# 2) 项目 venv + Python 依赖
cd /Users/jack/github/douyin-ppp-main
/Users/jack/.python-standalone/python/bin/python3 -m venv .venv
.venv/bin/pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple/
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/

# 3) Playwright Chromium
.venv/bin/playwright install chromium    # 或加 PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright

# 4) Node 签名服务依赖（macOS 自带 node 22+）
cd websdk
npm install --registry=https://registry.npmmirror.com
cd ..

# 5) 加载 LaunchAgent（关键：用 gui/$UID，不是普通 launchctl load）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.douyin-ppp.app.plist
launchctl kickstart -k gui/$(id -u)/com.douyin-ppp.app   # 立即启动（不一定要重启）
```

## 日常运维

```bash
# 查看状态
/Users/jack/bin/start-douyin.sh status

# 看 Web UI
open http://localhost:9527

# 看日志
tail -f ~/Library/logs/douyin-ppp/app.log
tail -f ~/Library/logs/douyin-ppp/signer.log

# 重启（先 stop 再 kickstart）
launchctl kickstart -k gui/$(id -u)/com.douyin-ppp.app

# 临时停掉（开机自启也会失效，下次登录才恢复）
launchctl bootout gui/$(id -u)/com.douyin-ppp.app

# 完全卸载
launchctl bootout gui/$(id -u)/com.douyin-ppp.app
rm ~/Library/LaunchAgents/com.douyin-ppp.app.plist
```

## 关键设计决策（重要）

### 1. 为什么 wrapper 不 `exec python`？

autoengine-monitor 是单进程，直接 `exec python -u app.py` 就好。但 douyin-ppp 有
**两个**进程（Python 主程序 + Node 签名服务），两者都得常驻。所以 wrapper 必须：

- 启动两个子进程（subshell 包住 + nohup + `< /dev/null` 防止信号传递）
- **自己保持前台不退出**（否则 launchd 会清掉整个 session）
- 子进程死了 → 拉起来（while true 守护循环）
- 收到 SIGTERM → 优雅关闭两个子进程再退出

### 2. 为什么用 `gui/$UID` 而不是普通 `launchctl load`？

这是给用户的 Aqua session 启动的（不是系统服务）。Playwright headful Chromium
需要 WindowServer 连接；用 `gui/$UID` 才能 attach 到当前 GUI session，
否则 Chrome 启动会失败（参考 autoengine-monitor 的 M.4b 教训）。

### 3. 为什么 `RunAtLoad` 而**不** `KeepAlive`？

单实例监控 + Playwright + WebSocket + 自定义签名的复杂进程，**任何一个子进程挂掉都
不应该被 launchd 自动重启**（可能死循环 + 启动 throttle 卡死）。

我们的 wrapper 守护两个子进程就够了 —— 一个挂了 wrapper 拉起来，整个 LaunchAgent
级别不重启。

## 故障排查

### 服务没起来

```bash
launchctl print gui/$(id -u)/com.douyin-ppp.app | head -30
# 看 state / last exit code / runs

tail -50 /tmp/douyin-ppp-launchd.err.log
tail -50 /Users/jack/Library/logs/douyin-ppp/app.log
```

### 端口被占（502 错误）

```bash
lsof -iTCP:9527 -sTCP:LISTEN
lsof -iTCP:3010 -sTCP:LISTEN
# 手动 kill 占用进程，再 kickstart
```

### 签名服务挂了

```bash
tail -50 ~/Library/logs/douyin-ppp/signer.log
# 大概率是 jsdom 缺文件，重装：
cd /Users/jack/github/douyin-ppp-main/websdk
rm -rf node_modules package-lock.json
npm install --registry=https://registry.npmmirror.com
```

### Cookie / Playwright 问题

Web UI 里"配置管理"页面有 Playwright 一键登录按钮；如果首次失败，
删除 browser profile 重新登录：

```bash
rm -rf /Users/jack/github/douyin-ppp-main/data/browser_data/*
rm -rf /Users/jack/github/douyin-ppp-main/data/xhs_browser_data/*
launchctl kickstart -k gui/$(id -u)/com.douyin-ppp.app
```

## 数据备份

```bash
# 停服务（保证 SQLite 一致）
launchctl bootout gui/$(id -u)/com.douyin-ppp.app

# 打包
tar czf douyin-ppp-$(date +%Y%m%d).tar.gz \
    /Users/jack/github/douyin-ppp-main/data/

# 恢复
tar xzf douyin-ppp-YYYYMMDD.tar.gz -C /

# 重启
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.douyin-ppp.app.plist
```
# Douyin PPP — Docker 部署指南

> 把 12,200 行 Python + Node 双服务打包成两个容器，docker-compose 一键编排。

## 架构

```
┌────────────────────────────────────────────────────────────┐
│                     docker host                            │
│                                                            │
│   ┌──────────────┐    HTTP signature     ┌──────────────┐  │
│   │   app:9527   │ ───────────────────►  │ signer:3010  │  │
│   │ (NiceGUI +   │    http://signer:     │ (Node.js +   │  │
│   │  Playwright) │         3010          │  jsdom +     │  │
│   │              │                       │  webmssdk)   │  │
│   └──────┬───────┘                       └──────────────┘  │
│          │                                                 │
│          ▼                                                 │
│   ./data/ (volume)                                         │
│   ├── db/        SQLite 数据库                              │
│   ├── browser_data/   Playwright 抖音 profile              │
│   ├── xhs_browser_data/ Playwright 小红书 profile          │
│   ├── exports/   Excel 导出                                │
│   └── logs/      日志                                      │
└────────────────────────────────────────────────────────────┘
```

两个容器通过 docker bridge 网络 `douyin-ppp-net` 互联，
app 容器通过 DNS 服务名 `signer` 解析到签名服务，
所以 `SIGNER_URL=http://signer:3010` 是关键的环境变量。

## 前置要求

- Docker Engine ≥ 20.10
- Docker Compose ≥ 2.0（或 docker-compose v1.x 也兼容）
- 宿主机空闲端口：`9527`（Web UI）、`3010`（签名服务，仅调试用可选）

## 一键启动

```bash
cd /path/to/douyin-ppp   # 项目根目录

# 1. 构建并后台启动
docker compose build      # 首次约 3-5 分钟（要装 Playwright Chromium）
docker compose up -d      # 后台启动

# 2. 看日志
docker compose logs -f app
docker compose logs -f signing

# 3. 验证
docker compose ps                                     # 两个服务 health: healthy
curl http://localhost:9527/                           # 应返回 HTML
docker exec douyin-ppp-signer wget -qO- --post-data=hc http://127.0.0.1:3010/signature   # 返回签名

# 4. 打开浏览器
open http://localhost:9527                           # macOS
xdg-open http://localhost:9527                       # Linux
```

## 关键配置

| 位置 | 说明 |
|---|---|
| `./data/` | **挂载到容器**，SQLite / 浏览器数据 / 导出 / 日志 |
| `deploy/config.yaml` | **打进镜像**的默认配置，建议生产用挂载覆盖 |
| 环境变量 `DOUYIN_PPP_*` | 覆盖任意配置字段（Pydantic Settings 标准约定） |

### 常用 env var

```yaml
# docker-compose.yml 里用 ${VAR:-default} 形式读 .env
TZ: Asia/Shanghai              # 时区
PIP_INDEX_URL: https://pypi.tuna.tsinghua.edu.cn/simple   # pip 镜像
```

### 自定义 config.yaml

把项目根目录的 `config.example.yaml` 复制成 `my-config.yaml`，改完挂载进去：

```yaml
# docker-compose.yml volumes:
- ./my-config.yaml:/app/config.yaml:ro
```

## 数据备份与迁移

```bash
# 备份
tar czf douyin-ppp-backup-$(date +%Y%m%d).tar.gz ./data/

# 迁移到新机器：scp 整个项目 + 备份 → 在新机器执行 docker compose up -d
```

**注意备份 sqlite db 时建议先停容器**，否则可能有写入竞争：

```bash
docker compose stop app          # 停主程序（签名服务可保留）
tar czf ...   ./data/
docker compose start app
```

## 故障排查

### 1. 容器启动失败

```bash
docker compose logs --tail=100 app
```

常见原因：
- 端口被占用：改 `docker-compose.yml` 里 `ports: "9527:9527"` → `"9528:9527"`
- 磁盘空间不足：`docker system df` / `docker system prune -a`

### 2. 签名服务连不上

```bash
# 验证 signer 容器健康
docker compose ps signing      # 应该 STATUS = healthy
docker exec douyin-ppp-signer wget -qO- --post-data=hc http://127.0.0.1:3010/signature

# 验证 app 容器能解析 signer DNS
docker exec douyin-ppp-app getent hosts signer     # 应该返回 signer 容器 IP
```

### 3. Playwright 启动失败

```bash
docker exec -it douyin-ppp-app bash
python -c "from playwright.sync_api import sync_playwright; \
  with sync_playwright() as p: print(p.chromium.executable_path)"
```

应该输出 `/ms-playwright/chromium-*/chrome-linux/chrome` 路径。

### 4. 重置 Playwright 登录态

```bash
docker compose down
rm -rf ./data/browser_data ./data/xhs_browser_data
docker compose up -d
# 然后在 Web UI 重新扫码登录
```

### 6. 升级应用代码

```bash
git pull
docker compose build
docker compose up -d
```

数据卷 `./data` **不会被覆盖**，升级安全。

## 文件清单

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 编排两个服务 |
| `deploy/Dockerfile.app` | Python 主程序镜像（playwright + python3.10） |
| `deploy/Dockerfile.signer` | Node.js 签名服务镜像（~80MB alpine） |
| `deploy/entrypoint.app.sh` | 容器入口：等签名服务 → 启动 NiceGUI |
| `deploy/config.yaml` | 容器内默认配置（建议 mount 覆盖） |
| `.dockerignore` | 排除构建上下文里不需要的文件 |
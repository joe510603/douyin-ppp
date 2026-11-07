# ============================================================================
# Douyin PPP — Python 主程序镜像（含 Playwright Chromium）
# 多阶段：先在 builder 装依赖，再拷到 slim runtime 镜像以减小最终体积
# 注意：未使用 # syntax= directive，避免去 docker.io 拉 frontend 镜像（国内网络不稳）
# ============================================================================

# ---- Stage 1: builder ----
# 用 full Python 镜像装 wheel/编译依赖（很多 wheel 在 slim 下编译失败）
FROM python:3.10-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 仅先 copy requirements，利用 Docker layer cache
COPY requirements.txt ./
# 默认走清华源；可通过 build arg PIP_INDEX_URL 覆盖（如海外构建用官方源）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt -i ${PIP_INDEX_URL}


# ---- Stage 2: runtime ----
# Playwright 官方镜像：python 3.10 + bookworm + chromium 浏览器 + 系统依赖
# 比自己用 slim 一个个装系统库要稳得多
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 强制 print/log 直接刷到 stdout（NiceGUI 日志即时可见）
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 仅装运行时需要的额外系统工具（curl 用于 healthcheck，tini 用于 PID 1 信号处理）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 拷 wheel 装好的依赖到 runtime（注意：playwright 基础镜像自带 python，
# 所以 --prefix=/install 出来的 site-packages 要对齐到它默认的 sys.path）
COPY --from=builder /install /usr/local

# 拷贝应用代码
COPY app.py ./
COPY config.example.yaml ./config.example.yaml
# 在容器里也放一份默认 config.yaml（用户可挂载覆盖）
COPY deploy/config.yaml ./config.yaml
COPY src/ ./src/
COPY proto/ ./proto/

# 运行时数据目录（SQLite / 浏览器数据 / 导出 / 日志）
# 生产环境应挂载匿名卷或具名卷以保留数据
RUN mkdir -p /app/data/db /app/data/exports /app/data/logs \
    /app/data/browser_data /app/data/xhs_browser_data \
    /app/logs

# Playwright 浏览器在 mcr.microsoft.com/playwright 镜像里已预装，跳过 playwright install
# 但显式声明 PLAYWRIGHT_BROWSERS_PATH 让代码可读
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 暴露主应用端口
EXPOSE 9527

# 健康检查：HTTP GET / 期望 200（NiceGUI 始终返回 200）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:9527/ >/dev/null || exit 1

# tini 作为 PID 1，正确转发 SIGTERM/SIGINT 给 Python 进程
ENTRYPOINT ["/usr/bin/tini", "--"]

# 用 entrypoint.sh 启动（支持 docker-compose 注入额外 env / 自定义配置）
CMD ["/bin/bash", "/app/deploy/entrypoint.app.sh"]
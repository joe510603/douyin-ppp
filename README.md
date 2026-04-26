# Douyin PPP — 抖音数据采集工具

[![Version](https://img.shields.io/badge/version-2.0.4-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> 直播弹幕实时采集 | 视频评论批量抓取 | 小红书笔记评论采集 | Web 可视化

## 功能概览

### 1. 抖音直播弹幕
- 多账号监控：配置任意数量抖音账号，开播后自动采集
- 实时弹幕：WebSocket + Protobuf 协议，弹幕/礼物/进场/点赞全采集
- 用户 ID 提取：每条弹幕记录发送者 user_id（支持 `id` / `idStr` / `shortId`）
- 断线重连：指数退避自动重连，后台 7×24 运行

### 2. 抖音视频评论
- 关键词模式：搜索关键词，批量抓取视频评论
- 账号模式：指定账号，抓取该账号所有视频评论
- 自动去重：搜索结果和评论内容双重去重

### 3. 小红书笔记评论（Playwright 拦截）
- 通过 Playwright headful 浏览器拦截真实签名请求
- 绕过 xhshow 算法的设备绑定限制
- 支持搜索笔记 + 抓取评论

### 4. 数据分析
- LLM 情感分析 / 意图分类 / 竞品识别（需配置 LLM API）
- 词云生成
- Excel 导出

---

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+（签名服务）

### 安装

```bash
# 克隆项目
git clone <repo-url> && cd douyin-ppp

# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium

# 初始化配置
cp config.example.yaml config.yaml
```

### 启动

#### Linux / OpenCloudOS / CentOS / Ubuntu

```bash
# 一键启动（签名服务 + 主程序）
./start.sh

# 查看服务状态
./status.sh

# 停止服务
./stop.sh

# 重启服务
./restart.sh

# 环境检测（首次使用）
./env_check.sh
```

#### macOS

```bash
# 一键启动（签名服务 + 主程序）
./start.sh

# 其他命令同 Linux
./status.sh
./stop.sh
./restart.sh
```

#### 手动启动（各平台通用）

```bash
# 终端 1：签名服务
cd websdk && npm install && npm start

# 终端 2：主服务
python3 app.py

# 浏览器访问 http://localhost:9527
```

---

## 项目结构

```
douyin-ppp/
├── app.py                      # NiceGUI 应用入口
├── config.example.yaml          # 配置模板（复制为 config.yaml）
├── requirements.txt             # Python 依赖
│
├── src/
│   ├── config.py               # 配置加载模块
│   ├── collector/             # 数据采集器
│   │   ├── live_collector.py  #   抖音直播弹幕（WebSocket + Protobuf）
│   │   ├── video_comment_collector.py  # 抖音视频评论（搜索 API）
│   │   └── xhs_comment_collector.py    # 小红书评论（Playwright 拦截）
│   ├── detector/
│   │   └── live_detector.py   # 开播检测
│   ├── processor/
│   │   └── comment_processor.py  # 数据处理（LLM 分析等）
│   ├── storage/
│   │   ├── db_storage.py      # SQLite 数据库
│   │   └── excel_storage.py  # Excel 导出
│   ├── models/
│   │   └── comment.py         # 数据模型（LiveComment / VideoComment）
│   ├── task/
│   │   └── task_manager.py    # 抓取任务管理
│   ├── utils/
│   │   ├── cookie_manager.py  # 抖音 Cookie 管理（Playwright）
│   │   ├── xhs_cookie_manager.py  # 小红书 Cookie 管理
│   │   ├── xhs_signer.py      # 小红书签名算法（xhshow）
│   │   ├── signer.py          # 抖音签名算法
│   │   ├── signer_client.py   # Node.js 签名服务客户端
│   │   ├── llm_client.py      # LLM API 客户端
│   │   ├── logger.py          # 日志工具
│   │   └── retry.py           # 重试机制
│   └── web/                   # NiceGUI Web 界面
│       ├── dashboard.py        #   仪表盘
│       ├── monitor_page.py    #   直播监控管理
│       ├── danmaku_page.py    #   实时弹幕大屏
│       ├── data_page.py        #   数据浏览与导出
│       ├── video_scrape_page.py  # 视频评论抓取
│       ├── config_page.py     #   配置管理
│       ├── log_page.py        #   日志查看
│       └── wordcloud_page.py  #   词云生成
│
├── proto/                      # 抖音 WebSocket 协议
│   ├── dy.proto               #   Protobuf 消息定义
│   ├── dy_pb2.py             #   编译后的 Python 绑定
│   ├── douyin.js             #   JS 签名脚本
│   └── parser.py              #   Protobuf 消息解析器
│
├── websdk/                    # Node.js 签名服务
│
├── data/                      # 运行时数据（自动创建）
│   ├── db/douyin_ppp.db      #   SQLite 数据库
│   ├── exports/              #   Excel 导出文件
│   ├── browser_data/         #   抖音浏览器登录数据
│   ├── xhs_browser_data/     #   小红书浏览器登录数据
│   └── logs/                 #   日志文件
│
└── tests/
    └── test_core.py          # 核心测试
```

---

## 配置说明

配置文件 `config.yaml` 主要字段：

```yaml
app:
  port: 9527          # Web 服务端口

douyin:
  cookie: ""          # 抖音 Cookie（Web UI 可自动获取）

douyin_video:
  cookie: ""          # 抖音视频评论 Cookie

xhs:
  cookie: ""          # 小红书 Cookie（Web UI 可自动获取）

monitors:
  - name: "账号名"    # 显示名称
    sec_user_id: ""  # 抖音 sec_user_id

llm:
  enabled: false
  api_key: ""        # OpenAI / DeepSeek API Key
  provider: "openai"
  model: "gpt-4o-mini"

storage:
  db_path: "data/db/douyin_ppp.db"
  export_dir: "data/exports"
```

> **敏感信息**：请勿将 `config.yaml` 提交到代码仓库（已加入 `.gitignore`）。

---

## 核心数据模型

### 直播弹幕（live_comments 表）

| 字段 | 说明 |
|------|------|
| id | 主键 |
| message_type | 消息类型（WebcastChatMessage/GiftMessage/MemberMessage/LikeMessage） |
| content | 弹幕/评论内容 |
| **user_id** | 发送者用户 ID（长整数，可用于用户去重识别） |
| user_nickname | 发送者昵称 |
| user_avatar | 用户头像 URL |
| room_id | 直播间 ID |
| anchor_id | 主播 sec_user_id |
| create_time | 消息时间 |
| collected_at | 采集入库时间 |

### 视频评论（video_comments 表）

| 字段 | 说明 |
|------|------|
| id | 主键 |
| source | 来源（douyin / xiaohongshu） |
| task_id | 关联抓取任务 ID |
| video_id | 视频/笔记 ID |
| content | 评论内容 |
| user_id | 评论者用户 ID |
| user_nickname | 评论者昵称 |
| like_count / reply_count | 点赞数 / 回复数 |

---

## 技术栈

| 组件 | 技术 |
|------|------|
| Web UI | [NiceGUI](https://nicegui.io/)（FastAPI + Vue3） |
| 抖音直播协议 | WebSocket + Protobuf（逆向） |
| 抖音搜索签名 | Node.js 签名服务 / Playwright 拦截 |
| 小红书签名 | Playwright 浏览器拦截 |
| 数据存储 | SQLite（实时）+ Excel（导出） |
| 任务调度 | APScheduler + asyncio |

---

## 参考项目

- [zhonghangAlex/DySpider](https://github.com/zhonghangAlex/DySpider) — 直播弹幕 WebSocket + Protobuf 核心方案
- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) — 抖音/小红书 API 封装参考
- [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) — 最新签名算法参考

---

## ⚠️ 免责声明

本项目仅供学习研究交流使用。请遵守抖音、小红书平台的使用条款和相关法律法规，不得用于商业用途或非法行为。

**License**: MIT

# 🎯 Douyin PPP — 抖音评论抓取工具

> **本地运行，开箱即用** | 直播弹幕实时抓取 | 视频评论采集 | Excel 导出

搜集抖音直播间真实用户关注的话题，作为内容创作和直播优化的数据依据。

## ✨ 功能特性

### 直播评论抓取（已实现）
- **多账号监控**：支持配置任意数量的抖音账号，开播后自动连接采集
- **实时弹幕**：WebSocket + Protobuf 协议，毫秒级实时获取弹幕/评论/礼物/进场等数据
- **后台运行**：本地服务方式，支持 7×24 小时持续运行
- **智能去重**：自动去重，避免重复采集
- **断线重连**：WebSocket 断开后自动指数退避重连

### Web 图形化界面（NiceGUI）
- **仪表盘**：系统状态概览、在线直播间数、今日采集量
- **监控管理**：添加/编辑/删除监控账号，一键启停
- **实时弹幕大屏**：实时滚动展示弹幕流
- **数据浏览**：表格查看已采集评论，支持筛选搜索
- **一键导出**：导出为格式化的 Excel 文件
- **配置管理**：Cookie/Token 配置、参数调整
- **日志查看**：运行日志和错误告警

### 视频评论抓取（规划中）
- 关键词模式：按关键词搜索热门视频 → 批量抓取评论
- 指定账号模式：输入账号 → 抓取该账号视频评论

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+ (用于签名服务)

### 本地运行

```bash
# 1. 克隆项目
git clone <repo-url>
cd douyin-ppp

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器（用于自动获取 Cookie）
playwright install chromium

# 4. 初始化配置文件
cp config.example.yaml config.yaml

# 5. 启动签名服务（新终端窗口）
cd websdk && npm install && npm start

# 6. 启动主服务（新终端窗口）
python3 app.py

# 7. 打开浏览器访问 http://localhost:9527
```

## 📁 项目结构

```
douyin-ppp/
├── app.py                    # NiceGUI 应用入口
├── config.example.yaml       # 配置文件模板（复制为 config.yaml 使用）
├── requirements.txt          # Python 依赖
├── proto/                    # Protobuf 定义 & JS 签名脚本
├── websdk/                   # 签名服务（Node.js）
├── src/
│   ├── web/                  # Web UI 页面模块
│   │   ├── dashboard.py      #   仪表盘首页
│   │   ├── monitor_page.py   #   监控管理页
│   │   ├── danmaku_page.py   #   实时弹幕大屏
│   │   ├── data_page.py      #   数据浏览与导出
│   │   ├── config_page.py    #   配置管理页
│   │   └── log_page.py       #   日志查看页
│   ├── collector/            # 采集器
│   │   └── live_collector.py #   直播间弹幕采集器
│   ├── detector/              # 检测器
│   │   └── live_detector.py  #   开播状态检测器
│   ├── processor/             # 数据处理
│   │   └── comment_processor.py
│   ├── storage/               # 存储
│   │   ├── db_storage.py     #   SQLite 存储
│   │   └── excel_storage.py  #   Excel 导出
│   ├── models/                # 数据模型
│   │   └── comment.py
│   └── utils/                 # 工具
│       ├── logger.py         #   日志
│       ├── retry.py          #   重试机制
│       ├── signer.py         #   签名算法
│       └── cookie_manager.py #   Cookie 管理
├── data/                      # 数据目录（运行时生成）
│   ├── db/                   #   SQLite 数据库
│   ├── exports/              #   Excel 导出文件
│   ├── browser_data/         #   浏览器登录数据
│   └── logs/                 #   日志文件
└── tests/                     # 测试
```

## 🔧 使用指南

### 1. 配置 Cookie

首次使用需要在 Web UI 的「配置管理」页面填写抖音 Cookie：

**方式一：自动获取（推荐）**

1. 进入「配置管理」页面
2. 点击「自动获取 Cookie」按钮
3. 在弹出的浏览器窗口中登录抖音
4. 登录成功后程序自动获取并保存 Cookie

**方式二：手动获取**

1. 用浏览器登录 [抖音网页版](https://www.douyin.com/)
2. 按 F12 打开开发者工具 → Application → Cookies → 复制 `ttwid` 值
3. 粘贴到 Web UI 的 Cookie 输入框并保存

### 2. 添加监控账号

1. 进入「监控管理」页面
2. 点击「添加账号」，填写：
   - 账号昵称（自定义）
   - sec_user_id（从抖音主页 URL 中获取）
3. 保存后系统会自动开始检测开播状态

### 3. 查看实时弹幕

1. 当检测到目标账号开播后，自动开始采集
2. 进入「实时弹幕大屏」查看滚动弹幕流
3. 或进入「数据浏览」页面以表格形式查看

### 4. 导出数据

在「数据浏览」页面点击「导出 Excel」按钮，文件保存在 `data/exports/` 目录。

## 🛠 技术栈

| 组件 | 技术 |
|------|------|
| Web UI | [NiceGUI](https://nicegui.io/) (FastAPI + Vue3) |
| 弹幕协议 | WebSocket + Protobuf (逆向) |
| 签名算法 | JS 逆向 (MiniRacer / PyExecJS) |
| 数据存储 | SQLite (实时) + Excel (导出) |
| 任务调度 | APScheduler + asyncio |
| 部署 | 本地 Python3 |

## 📋 参考

核心实现参考以下开源项目：

- [zhonghangAlex/DySpider](https://github.com/zhonghangAlex/DySpider) — 直播弹幕 WebSocket+Protobuf 核心方案
- [Evil0ctal/Douyin_Tiktok_Scraper](https://github.com/Evil0ctal/Douyin_Tiktok_Scraper_PyPI) — 视频评论 API 封装
- [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) — 最新签名算法参考

## ⚠️ 免责声明

本项目仅供学习研究交流使用。请遵守抖音平台的使用条款和相关法律法规。
不得将本工具用于任何商业用途或非法行为。

## License

MIT

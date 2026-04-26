# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.4] - 2026-04-26

### Fixed
- **browser_data 目录膨胀**：Playwright 持久化上下文累积到 1.1GB，导致内存占用过高、电脑卡顿。新增自动清理机制或启动时检测大小（需手动清理 `data/browser_data/` 目录）
- **msg_queue 无界增长**：`live_collector.py` 中 `Queue()` 未设置 maxsize，高吞吐时内存持续膨胀。已添加 `maxsize=5000` 限制，队列满时记录日志而非静默丢弃
- **buffer_comment 刷写任务静默丢失**：数据库缓冲区溢出时 fire-and-forget 的 task 未被跟踪，异常时数据永久丢失。已添加 `_flush_tasks` 列表跟踪任务，失败时降级为同步 daemon 线程写入
- **检测循环 14 个账号集中请求**：14 个账号每 60s 同时发起检测请求，造成瞬时网络/服务端压力。已实现错峰检测，每账号间隔 `interval/账号数` 秒依次检测
- **在线人数回调协程丢失**：`on_viewer_count_update` 创建的 asyncio.Task 未被引用，协程可能被垃圾回收。已改为 `self._viewer_task` 引用保存

### Changed
- **检测错峰**：将 `stagger_delay` 参数加入 `batch_detect()` 方法，检测线程和应用主线程均支持错峰配置
- **buffer_comment 超限写入降级**：事件循环不存在时，改用 daemon 线程异步写入，避免 RuntimeError 时数据丢失

---

## [2.0.3] - 2026-04-17

### Fixed
- **user_id 全为 111111**：抖音对部分直播间用户的 `shortId` 字段填充匿名占位符 `111111`，已修正为跳过该占位符并回退到 `idStr` / `id`
- **视频评论抓取失败**：Playwright 拦截遇到验证码时直接失败，已增加 HTTP 降级请求 + Cookie 失效检测
- **任务列表 async 警告**：`_initial_load` 协程未 await 导致 `RuntimeWarning`，已修复
- **stop.sh 杀进程不彻底**：增加按端口兜底杀进程（兼容无 PID 文件场景）
- **端口占用误判**：macOS `lsof` 会残留已关闭连接，改为优先 `ss` / `netstat` + `LISTEN` 过滤

### Changed
- `video_comment_collector`：增强 `x-whale-throughput-abort-data` 强制登录检测，提示用户刷新 Cookie
- `status.sh`：无 PID 文件时支持按端口检测进程

---

## [2.0.2] - 2026-04-14

### Added
- **跨平台启动脚本**：新增 `start.sh` / `stop.sh` / `restart.sh` / `status.sh`，一键启动签名服务 + 主程序，兼容 macOS / Linux（Ubuntu/CentOS/OpenCloudOS）
- **环境检测脚本**：`env_check.sh` 一键检测 Node.js / Python / pip / Python 包 / 端口占用

### Changed
- **跨平台兼容**：`start.sh` / `status.sh` / `start_with_signer.sh` 支持 `lsof` / `ss` / `netstat` 三种端口检测方式
- **启动脚本**：签名服务启动后验证端口监听，失败自动回滚
- **README.md**：更新启动说明，区分 Linux/macOS 一键启动方式

---

## [2.0.1] - 2026-04-14

### Fixed
- **在线人数显示为0**：抖音服务端改用 `WebcastRoomUserSeqMessage` 推送人数，`RoomStatsMessage` 数值归零。新增 `RoomUserSeqMessage` 处理器，从 `total` 字段取实时在线人数
- **在线人数异常大（如154人）**：`RoomUserSeqMessage.totalUser` 是累计入场人数（非当前在线），已修正为只用 `total` 字段
- **ttwid 解析警告刷屏**：ttwid 格式从 `1|时间戳|签名` 变更为 base64，解析逻辑已兼容
- **_fetch_webcast_detail Protobuf 解析失败**：接口在无数据时返回 JSON 而非 protobuf，已增加 gzip 解压 + JSON 降级处理
- **collector 崩溃后未清理**：`collector._task.done()` 检测 + state.collectors 主动清理逻辑
- **只取更大的人数值**：避免 `RoomStatsMessage` 的小值覆盖 `RoomUserSeqMessage` 的真实人数

### Changed
- 服务管理页面：新增 /service 页面，监控签名服务和主服务状态，支持一键重启
- Dashboard 在线人数刷新策略：`≥ current` 策略，避免波动覆盖

---

## [2.0.0] - 2026-04-13

### Added
- **NiceGUI Web UI**：完整的 Web 可视化界面（仪表盘、弹幕大屏、数据管理、配置页、日志查看、词云）
- **抖音直播弹幕采集**：WebSocket + Protobuf 协议，弹幕/礼物/进场/点赞全采集，自动重连
- **抖音视频评论采集**：关键词模式和账号模式，支持自动去重
- **小红书笔记评论采集**：Playwright 浏览器拦截真实签名请求，支持笔记搜索和评论抓取
- **LLM 情感分析**：集成 LLM API 进行意图分类和竞品识别
- **词云生成**：弹幕数据词云可视化
- **任务管理模块**：`src/task/task_manager.py` 统一管理抓取任务
- **签名前端服务**：`signer_manager.py` 自动启动/管理 Node.js 签名服务

### Changed
- 项目架构重构：从命令行工具升级为 Web 服务 + NiceGUI 可视化界面
- 采集器模块化：`src/collector/` 下分 `live_collector`、`video_comment_collector`、`xhs_comment_collector`
- 数据库支持 SQLite 和 Excel 双导出
- 配置系统支持 `config.yaml` 本地覆盖

### Fixed
- **asyncio 事件循环阻塞**：`live_collector.py` 中 `queue.Queue.get()` 改为 `await asyncio.to_thread()`，解决 Web UI 无响应问题
- **检测结果状态不更新**：账号下播后 `monitor_states` 状态未同步更新，导致 UI 一直显示"直播中"
- **httpx.AsyncClient 跨事件循环阻塞**：检测线程改为创建独立的 `LiveDetector()` 实例
- **webcast_detail API 返回空**：添加 `a_bogus` 签名和 `msToken` 参数
- **Cookie 失效时 room_id 获取失败**：强制在 Cookie 失效时触发 Playwright 浏览器检测
- **Dashboard timer 异常阻塞事件循环**：所有 `ui.timer()` 回调包装 try-except

---

## [1.0.0] - 2026-04-11

### Added
- 基础抖音直播间弹幕采集（命令行版本）
- Node.js 签名服务
- 初步的 Protobuf 协议支持

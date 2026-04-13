# 抖音直播弹幕收集器问题诊断报告

**报告日期**: 2026年4月13日
**问题类型**: asyncio 事件循环阻塞导致 Web UI 无响应
**影响范围**: NiceGUI WebSocket 连接、HTTP 请求
**修复文件**: `src/collector/live_collector.py`, `app.py`

---

## 1. 问题现象

### 1.1 用户反馈
- 浏览器访问 `http://localhost:9527` 页面一直转圈，无法加载
- curl 测试 `curl http://127.0.0.1:9527/` 超时（5秒无响应）
- 服务启动正常，日志显示采集也在跑，但网页打不开

### 1.2 日志表现
```
NiceGUI ready to go on http://localhost:9527
2026-04-13 13:04:06 | INFO | src.collector.live_collector:_run:472 - ✅ 直播间 7628089978349701942 已连接（后台线程模式）
2026-04-13 13:04:12 | INFO | src.collector.live_collector:_handle_raw_message:1515 - [ACK] needAck=True
```
服务日志显示 NiceGUI 启动成功、采集器连接成功，但 HTTP 请求始终超时。

### 1.3 诊断过程

#### 第一步：定位服务器状态
```bash
lsof -i :9527
```
服务器在 `LISTEN`，但 curl 超时。排除端口未绑定问题。

#### 第二步：分时段测试
```bash
# 启动后立刻 curl（采集还没开始）
curl http://127.0.0.1:9527/ → HTTP 200 in 6ms  ✅

# 采集启动后 curl
curl http://127.0.0.1:9527/ → 超时 5秒  ❌
```
确认是**采集器启动后**才出问题。

#### 第三步：排除各层嫌疑
- httpx.AsyncClient 跨事件循环：已用 `LiveDetector()` 创建独立实例
- `queue.Queue` 跨线程传递消息：队列本身没问题
- `WebSocketApp.run_forever()`：在 daemon 线程中，不影响主事件循环
- `_fetch_webcast_detail` 的 `httpx.Client`：`run_in_executor()` 已包装

#### 第四步：精确定位
在 `app.py` 的 `on_startup()` 中，对比启动后立刻 curl vs 等待采集启动后 curl 的结果差异。

关键发现：**采集器 `LiveCollector._run()` 是一个 asyncio 协程**，在 `start_collection()` 中通过 `asyncio.create_task()` 或直接 `await` 启动后，主事件循环开始被其占用。

---

## 2. 根因分析

### 2.1 直接原因

`src/collector/live_collector.py` 第 535 行：

```python
# _run() 协程的消息消费主循环
while running and self._running:
    try:
        # 【问题】这是一个同步阻塞调用！
        raw_msg = msg_queue.get(timeout=1)  # ← 卡死事件循环 1 秒
    except Empty:
        continue
```

`queue.Queue.get(timeout=1)` 是**同步阻塞调用**。在 asyncio 协程中直接调用，会把整个事件循环阻塞 1 秒，期间 uvicorn 无法处理任何 HTTP 请求和 WebSocket 消息。

### 2.2 事件循环被占用的机理

Python asyncio 的事件循环是单线程的，所有协程和回调都在同一个线程中运行。当 `_run()` 的 while 循环执行到 `msg_queue.get(timeout=1)` 时：

1. 调用栈深入 `queue.Queue.get()` → `queue.Queue._get()` → `QueueLock.acquire()`
2. 如果队列为空，调用线程（事件循环所在线程）进入**睡眠等待**
3. 等待期间，事件循环无法调度其他协程（`flush_loop`, `_listen_detection_results`, NiceGUI 的所有 timer 回调）
4. 1 秒后 `get()` 返回，再循环一次，又卡 1 秒
5. uvicorn 的 HTTP handler 永远拿不到 CPU 时间片

### 2.3 误判经过

| 尝试 | 方案 | 结果 |
|------|------|------|
| 换 httpx client | `get_detector()` → `LiveDetector()` | ❌ curl 仍然超时 |
| Playwright 隔离 | 检测线程独立运行 | ❌ curl 仍然超时 |
| 启动后立刻 curl | 采集还没跑时测试 | ✅ curl 正常 |

第3个测试才揭示真相：问题不在于启动顺序，在于 `_run()` 协程本身的实现。

---

## 3. 修复方案

### 3.1 核心修复

**文件**: `src/collector/live_collector.py` 第 535 行

```python
# Before（同步阻塞，卡死事件循环）:
raw_msg = msg_queue.get(timeout=1)

# After（异步非阻塞，事件循环保持响应）:
raw_msg = await asyncio.to_thread(msg_queue.get, timeout=1)
```

`asyncio.to_thread()` 将同步阻塞操作扔进线程池执行，不占用事件循环。`await` 让事件循环在等待期间可以调度其他协程。

### 3.2 连带修复

**文件**: `app.py` 第 574-582 行

```python
# Before（共享主进程的 httpx.AsyncClient，跨事件循环有隐患）:
from src.detector.live_detector import get_detector
detector = get_detector()

# After（在线程内创建独立实例）:
from src.detector.live_detector import LiveDetector
detector = LiveDetector()
```

---

## 4. 验证结果

修复后连续 curl 测试（采集启动中）：

```
第1次: HTTP 200 in 0.005773s
第2次: HTTP 200 in 0.012714s
第3次: HTTP 200 in 0.004260s
```

同时采集器正常运行：
```
✅ 直播间 7628111688297925402 已连接（后台线程模式）
✅ 直播间 7628089978349701942 已连接（后台线程模式）
🚪 [111111/梅***] 进入直播间
```

HTTP 响应和弹幕采集并行不干扰。

---

## 5. 经验教训

### 5.1 asyncio 协程中禁止的同步操作

在 asyncio 协程里，以下同步操作会卡死事件循环：

| 操作 | 表现 | 正确做法 |
|------|------|---------|
| `queue.Queue.get(timeout=N)` | 阻塞 N 秒 | `await asyncio.to_thread(q.get, timeout=N)` |
| `time.sleep(N)` | 阻塞 N 秒 | `await asyncio.sleep(N)` |
| `requests.get()` | 阻塞直到返回 | `await httpx.AsyncClient.get()` 或 `run_in_executor()` |
| 任意耗时的 CPU 计算 | 阻塞直到完成 | `await asyncio.to_thread(heavy_function)` |

### 5.2 诊断方法论

定位这类问题的关键：**分时段测试**。当"服务器启动正常但 HTTP 不响应"时，立即测试"在什么阶段开始出问题"。不同时机的测试结果差异能直接缩小根因范围。

### 5.3 项目架构隐患

`LiveCollector._run()` 协程直接运行在主事件循环中（通过 `await collector.connect()` 或 `asyncio.create_task()`），其中混入了：
- 异步操作（WebSocket 连接、`_handle_raw_message_async`）
- 同步阻塞操作（`queue.Queue.get`、`msg_queue.queue.clear()`）

更好的架构是：将 `_run()` 整体移入独立线程，用 `queue.Queue` 与主事件循环通信。所有与弹幕消息处理无关的逻辑（消息分发、监控回调）通过队列传递给主循环处理。

---

## 6. 涉及文件变更

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/collector/live_collector.py:535` | Bug Fix | `queue.Queue.get()` → `await asyncio.to_thread()` |
| `app.py:574` | Bug Fix | `get_detector()` → `LiveDetector()` |

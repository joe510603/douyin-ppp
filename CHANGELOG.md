# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.16] - 2026-11-11

### Fixed
- **话术角度选错：证据被截断成「最后一条弹幕」**（根因修复，影响 31% 的线索）。
  实时链路 `upsert_lead` 在同一个人再次命中时，会把 `evidence` **替换成最新那一条弹幕**，
  而 `evidence_count` 却在累加——结果库里存的是「最后一条命中的弹幕」，
  不是「客户问得最重的那个」。
  实测 262 条线索里 82 条这样：客户先问「全款多少钱提回家」又发了句「N90」，
  存下来只剩「N90」，话术就回「看到你关注小米汽车」，完全没回应重点。
  修法：重跑 `scripts/backfill_lead_evidence.py --force --top 3`，
  按「购买类信号优先 + 分数」重排证据。回填后 **22 条线索的话术角度变了**
  （model→price 5 条、model→config 4 条…），**20 条线索的等级/分数变了**
- **回填脚本自身三个缺陷**（不修的话回填反而更差）：
  - 不去重——用户重复发同一句很常见，「qq?」能占满 3 个名额，把真正问价格的那条挤掉
  - 「联系方式」信号权重 45 最高，会霸占证据首位；
    但它**不映射任何模板**（评论区不能出现微信/电话），霸着首位等于话术角度直接废掉。
    改成：有「购买类」信号的弹幕优先，纯联系方式的排后面
  - `evidence_count` 被覆盖成 `len(top)`（最多 3），丢掉了「触发弹幕条数」的信息，
    改成按实际命中信号的条数（去重后）计算
- **裸 `max/pro/ultra` 被误判成版本询问**（规则层，17 条里 9 条误判）：
  「展车有pro吗」「N70 Max能讲解一下吗」「su7max现在什么价位了」
  都是**点名那台车**，不是问选哪个版本。
  早先用裸 `max|pro|ultra` 匹配，客户白拿 16 分，话术还套成「几个版本怎么选」，答非所问。
  改成只认明确问版本的词（什么版本/标准版/有什么区别…），
  裸后缀单独出现走「车型关注」；只有**既点名后缀又在比较**（「su7 max和pro哪个好」）才算版本询问
- **车型说错**：客户问「N70 Max」，话术回「SU7 Max选哪个版本」——
  话术库用子串匹配车名，「N70 Max」里的 max 先被匹配到。
  改用规则引擎的 `extract_model`（它分得清 su7max 和 N70 Max），子串匹配降级为兜底
- **信号路径漏了售后语境守卫**（本次改动引入的回归）：
  关键词匹配路径有「售后语境不选价格角度」的守卫，
  但新增的信号路径（`_pick_by_signals`）没有，
  「修车有优惠吗」命中「金融补贴」信号后直接套 price 模板，回「给你报个落地价」，答非所问。
  两条路径的守卫现在一致

### Changed
- **话术角度改由规则引擎的信号决定**（原来两套词表各匹配各的会错位）：
  客户同时问了价格和到店地址，模板顺序固定，选出来的角度不一定是他主要关心的那个。
  现在用信号（按规则引擎的权重排序）选角度，两边天然同一套口径。
  通用兜底从 38 条降到 **2 条**
- 新增 `version` 模板（版本选择角度）、`price` 模板变体 3→8、`config` 变体 +1、
  `car_alias` 补 `ULTRA`
- 变体/称呼选择改用 4 字节哈希取模（单字节 `% N` 在小样本下偏差明显）
- `RuleScorer` 新增 `trim_as_version` 参数（默认 False）。
  True = v2.0.15 之前「裸后缀也算版本询问」的旧口径，给重打分脚本做新旧对比用
- `scripts/rescore_leads.py` 新增 `--recompute`：
  按**当前证据 + 当前规则**重算并回写（证据回填后用这个）；
  原来的模式只对比「规则口径差异」，证据变了它察觉不到
- `scripts/backfill_lead_evidence.py` 支持去重 + 分档排序

### Data
- 证据回填 262 条，重算 20 条等级/分数
- 飞书表定向对齐 87 条（意向等级/意向分/判定依据/弹幕原文）。
  **只 PATCH 这四个字段**，「处理状态」「触达状态」一律不碰——
  那是双向字段，另一台机器也在写这张表，
  用本地默认值覆盖会把对方的真实触达进度抹掉
- 修正 `84668218565`：飞书显示「低意向/24」，
  实际问的是「yu7标准版首付8万加购置税保险一共首付多少」「yu7标准版下定到提车多久时间」
  → 改为「高意向/72」

### Testing
- 测试 215 → 222

---

## [2.0.15] - 2026-11-10

### Added
- **`scripts/rescore_leads.py`**：按新规则重打分线索。
  只更新**新旧规则结果不同**的那些——
  库里有些线索新旧分数不一致是因为「实时链路」和「回溯扫描」判定口径本来不同
  （单条弹幕打分 vs 多条取最高），不是规则变了。
  脚本用两个打分器对比（`exclude_aftersale` 开/关）来精确锁定受规则变更影响的线索。
  实测 240 条里只有 1 条受影响（问修车多少钱那条），已重打分并同步到飞书表

### Changed
- `RuleScorer` 新增 `exclude_aftersale` 参数（默认 True）。
  关掉它等于回到 v2.0.14 之前的口径，给重打分脚本做对比用

---

## [2.0.14] - 2026-11-09

### Fixed
- **抖音软风控没被识别，账号被限流了**（真 bug）：抖音的软风控**不弹验证码、不提示「操作频繁」**，
  就是把评论静默吞掉——点发送、没报错、回查评论区也没有。
  原来的风控检测只看页面上的验证码/提示词，识别不了这种情况，
  结果一条条继续发，越撞越狠。
  现在「发送无回执 + 回查不到」会计数，连续达到 `silent_block_threshold`（默认 2）
  就判定账号被限流并**立即停手**
- **风控冷却**：命中风控后 `risk_cooldown_minutes`（默认 120）分钟内拒绝启动触达，
  避免继续撞风控把号搞废。冷却依据是 `outreach_runs.note` 里的风控标记
- **干跑记录会挡住真发**：去重 `recent_touch_days` 把 `dry_run` 也算成「已触达」，
  干跑验证完的人就永远真发不出去了，「先干跑看看再真发」这个流程直接走不通。
  现在只认 `status='sent'`（真发出去了），失败记录也不算（可以重试）

### Added
- **`scripts/outreach_console.py` 触达控制台**：在终端实时看到每一步
  （搜索 → 定位 → 挑作品 → 生成话术 → 发送 → 回查），
  带模式/限速/窗口/冷却状态横幅。默认干跑，`--real` 才真发
- **触达日志实时输出**：之前只存在内存里，任务跑完才打印；
  现在边跑边打到控制台和服务日志
- **`scripts/find_reset_touches.py`**：扫飞书全表变更历史，找出
  「曾经留言过、但被重置回待触达」的线索（跨机重复评论的来源）

### Changed
- **`outreach.hide_window`（默认开）**：浏览器窗口摆到屏幕外，看不见但不挡你干活。
  ⚠️ 别直接开 headless——抖音对无头浏览器的检测更严，风控风险明显更高
- 新增配置：`hide_window` / `silent_block_threshold` / `risk_cooldown_minutes`

### Testing
- 测试 208 → 210

---

## [2.0.13] - 2026-11-09

### Fixed
- **干跑会污染线索状态**（真 bug）：干跑时 `_set_status` 仍会写本地 `outreach_status`
  （searching → found），只是不写飞书。后果有两个：
  - 这条线索就卡在队列外了——回拉不会把它改回 pending
    （本地相对上次同步的值变了，走「跳过」分支），下一轮真跑时这些线索再也不会被处理
  - 多机共用一张飞书表时更危险：干跑会把**另一台机器**写的真实触达状态盖掉
  现在干跑**完全不改状态**（本地也不改）。日志、UI 实时事件、
  `outreach_records` 里的干跑记录都照常保留——干跑该有的留痕一样不少

### Testing
- 测试 196 → 199（新增 `tests/test_outreach_worker.py`）
- 扫码登录后实测干跑 3 条：1 条走完「搜人 → 挑作品 → 生成话术」、
  1 条用户无公开作品（跳过）、1 条抖音号搜不到且昵称 fallback 也没搜到（失败）；
  干跑前后 `leads.outreach_status` 分布逐项一致，飞书端未被写入

---

## [2.0.12] - 2026-11-09

### Fixed
- **触达状态可以由另一台机器写入，回拉机制要认它**（重要认知修正）：
  另一台机器也在跑触达，用的是**同一张飞书表**——它把「已留言/失败/已跳过/已定位」
  写回飞书，本地通过回拉同步过来，所以本地 `outreach_records` 里查不到这些执行记录是
  **正常的**（执行发生在另一台机器上）。
  因此「本地查不到触达记录」**不能**作为状态可疑的依据——
  已撤掉基于该假设的回拉告警（跨机场景下会持续误报）。

### Added
- **`scripts/reset_outreach_status.py`**：批量把触达状态重置回「待触达」，
  飞书和本地一起改（只改一边会被下一轮回拉同步带回来）。
  字段 ID 动态查，不硬编码；默认只预览，加 `--apply` 才写。
  多机同时触达时，两端状态可能需要手动对齐时用它

### Notes
- **`+record-batch-update` 用中文字段名会静默失败**：返回 `ok`、rev 也涨，
  但值根本没变、变更历史里也不留痕迹。必须用字段 ID（`fldXXXX`）。
  本项目的写入路径没踩这个坑（HTTP 后端走 v1 `PATCH records/{id}` 接受中文名，
  lark-cli 后端走 `+record-upsert` 中文名有效），但手工改表时会踩
- **查「谁改了什么」用 `lark-cli base +record-history-list`**：返回 operator +
  before/after + 时间线。注意程序用用户 token 写入时，operator 也显示为用户本人，
  所以不能只看 operator，要看状态变化序列是否符合某个程序状态机

---

## [2.0.11] - 2026-11-09

### Added
- **抖音自动触达**：在抖音真实界面搜索线索的抖音号 → 定位主页 → 挑最近的作品 → 按弹幕内容和车型生成一条像真人写的留言 → 发出 → 回查确认 → 回写状态
  - 复用 `douyin-touch` 项目的实战经验（响应监听抓数据、首页搜索框搜人、发完回查、风控 DOM 检测）
  - 线索中心新增「抖音触达队列」页签：话术预览、开始触达、运行日志、触达记录
  - 配置页新增触达参数区块（干跑开关、限速、回查、称呼变体、浏览器 profile）
  - `outreach_records` 表补明细字段（record_id / sec_uid / aweme_id / template_id / verified），新增 `outreach_runs` 表

### Fixed
- **话术会编造客户没问过的事**：模板选择原来用「判定依据」（"命中信号：价格询问"），
  客户只说了「su7」也会回「看到你问SU7的价格」。改为用**弹幕原文**（`evidence` 字段），
  并新增「车型关注」角度——具体但不撒谎
- **话术截断超长 1 字**：`_truncate` 在标点处截断后补「～」，
  `cut[:i] + "～"` 实际是 limit+1 字

### Changed
- **话术重复率降低 3.7 倍**：模板变体（同一角度多种说法）× 称呼变体（老板/这位朋友/你好）组合，
  202 条待触达从 21 种话术提升到 77 种，最高重复从 18 次降到 8 次。
  变体用**抖音号做种子**确定性选择——同一个人永远同一句话（可复盘），不同人自然分散
- **触达跑在专用线程**：Playwright sync API 不能跨线程用，而主进程是 asyncio 架构。
  整个触达流程在专用线程跑，主页面照常响应，不用把 1000 多行操作逻辑重写成 async
- **不再重复造轮子**：触达直接用本项目的 `leads` 表、`outreach_records` 表和
  `notify/bitable.py` 写入器（部分更新语义，不会覆盖销售改过的「处理状态」），
  不再另建独立 SQLite 和飞书模块
- **干跑默认开启**：`outreach.dry_run` 默认 `true`，确认话术没问题再关。
  真发前还有二次确认弹窗
- 删除旧的 `src/outreach/operator.py`（直接 goto `/search?q` 实测搜不到人，
  被 `douyin_api.py` 取代）

### Changed (config)
- `outreach` 新增：`dry_run` / `stop_on_risk` / `dedupe_days` / `rest_every` /
  `rest_min` / `rest_max` / `recent_days` / `intent_levels` / `stores` /
  `profile_dir` / `headless` / `templates_file` / `max_chars` / `salutation` /
  `salutation_pool` / `allow_contact_words` / `verify_after_post` / `verify_scrolls`
- 移除 `outreach.message_template`（改用 templates.yaml 的话术库）

### Testing
- 测试 177 → 196（新增 `tests/test_outreach_copywriter.py`）
- UI 用 Playwright 实测：话术预览表渲染、干跑开关、配置项齐全

---

## [2.0.10] - 2026-11-07

### Fixed
- **销售在飞书表里改的状态被后台同步覆盖**（真 bug）：`处理状态` / `触达状态` 是双向字段——销售在飞书表里标「已跟进」「已忽略」，本地运营在网页线索中心也能改。但每次同步都无条件把本地默认值写回飞书，导致销售刚标完「已跟进」，后台下一轮同步就抹回「待处理」。现在改成两条防线：
  - **推送守卫**：新增 `leads.bitable_synced_status` 记录「上次同步进飞书时的状态」，本地状态相对它没变过就不写状态字段
  - **定时回拉**：新增 `pull_bitable_status()`，每 5 分钟（`notify.bitable_pull_interval_minutes`）把飞书端销售改过的状态拉回本地；只在本地状态相对上次同步没变过时才采纳，不会覆盖本地刚做的修改
- **网页「同步到多维表格」绕过状态守卫**：`_do_sync_bitable` 直接调 `writer.sync_leads()`，绕过了统一同步路径。改为复用 `sync_leads_to_bitable()`，与实时链路行为一致
- **配置里德阳万达体验店重复 2 条**：14 个 monitor 实为 13 个唯一门店（`sec_user_id` 完全相同），会重复起采集任务。已删除重复项

### Added
- **飞书表新增「门店」列**：14 个门店的线索混在一张表里，销售没法按门店筛选、也没法把线索分给对应门店的人。门店名做归一化处理——配置里同一个门店有多种写法（`小米汽车｜世茂广场店` / `小米汽车 | 成都龙湖滨江体验店` / `小米汽车 ｜ 机场路店`，混用全角竖线、半角竖线、顿号还夹空格），统一收敛成短名（「世茂广场店」「成都龙湖滨江体验店」）
- **本地 `leads` 表新增 `store` 字段**：落库时自动归一化，线索中心支持按门店筛选
- **`scripts/backfill_store_field.py`**：回填飞书表门店列 + 初始化本地同步状态标记（幂等）
- **线索中心新增**：门店列、门店筛选器、「拉取飞书状态」按钮
- **配置项**：`notify.bitable_pull_interval_minutes`（状态回拉间隔，默认 5 分钟）

### Changed
- 多维表格字段从 12 个增加到 13 个（新增「门店」）
- 测试从 154 个增加到 171 个（新增 `test_bitable_status_sync.py`、`test_bitable_pull.py`、`test_lead_store.py`）

---

## [2.0.9] - 2026-11-07

### Changed
- **飞书表列顺序调整**：发现日期作为主字段排第一列，只显示日期（yyyy/MM/dd，不要时分秒）。飞书主字段恒在最左列且 API 不允许改主字段，只能重建表迁移

---

## [2.0.8] - 2026-11-07

### Added
- **意向弹幕自动上飞书多维表格**：实时筛查命中即自动写入，不再依赖群推送是否开启
- **`LarkCliBitableWriter`**：认证与 token 续期全交给 lark-cli，适合 24h 后台自跑

### Fixed
- **测试污染真实飞书表**：生产配置里 `bitable_enabled=true` 时，pytest 造的假线索真写进了销售的飞书表。新增 `tests/conftest.py` 全局隔离

---

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

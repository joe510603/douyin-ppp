# DouYin_Spider 参考项目知识库

> **目的**：遇到抖音直播间相关无法解决的问题时，来这里找思路
> **参考来源**：本地 `/Users/limingrui/CodeBuddy/DouYin_Spider-1` | GitHub `https://github.com/cv-cat/DouYin_Spider`
> **项目作者**：cv-cat | Star: 1.3k | Fork: 325

---

## 一、项目架构总览

```
DouYin_Spider/
├── dy_live/server.py          # 🎯 直播间 WebSocket 监听（核心参考）
├── dy_apis/douyin_api.py     # API 方法（1500+ 行）
├── builder/
│   ├── auth.py               # 认证处理
│   ├── params.py             # URL 参数构建
│   └── proto.py              # Protobuf 请求构建
├── utils/
│   ├── dy_util.py            # JS 签名调用接口
│   └── cookie_util.py        # Playwright Cookie 提取
├── static/
│   ├── Live_pb2.py           # 直播间 Protobuf 定义
│   ├── dy_live_sign.js       # 直播间签名逻辑
│   └── dy_ab.js              # a_bogus 签名逻辑
```

---

## 二、直播间 WebSocket 连接流程（最关键参考）

### 完整连接流程（`dy_live/server.py` 的 `DouyinLive` 类）

```
Step 1: 获取房间信息
  → DouyinAPI.get_live_info()  # 访问 live.douyin.com/{room_id}
  → 从页面 <script nonce> 中提取 roomId, user_id, ttwid

Step 2: 获取直播详情
  → DouyinAPI.get_webcast_detail()  # 获取 cursor 和 internal_ext

Step 3: 构建 WebSocket URL
  → ~20+ 个参数（signature, cursor, internal_ext 等）
  → Origin: https://live.douyin.com

Step 4: 连接 WebSocket
  → 心跳：独立线程每 5 秒发送 PushFrame (payloadType="hb")
  → 收到 needAck=True 时发送 ACK
```

### WebSocket URL 参数（参考 `dy_live_sign.js`）

```javascript
// 签名原始字符串格式
live_id=1,aid=6383,version_code=180800,webcast_sdk_version=1.0.15,
room_id={room_id},user_unique_id={user_unique_id},
{其他参数...}
// → MD5 哈希 → X-Bogus 签名
```

### 直播间消息解析流程

```python
# server.py on_message() 中
frame = Live_pb2.PushFrame()
frame.ParseFromString(message)
origin_bytes = gzip.decompress(frame.payload)  # 🔑 gzip 解压

response = Live_pb2.LiveResponse()
response.ParseFromString(origin_bytes)

for item in response.messagesList:
    method = item.method
    payload = item.payload

    if method == 'WebcastChatMessage':
        msg = Live_pb2.ChatMessage()
        msg.ParseFromString(payload)
        # 提取: msg.user.sec_uid, msg.user.nickname, msg.content

    elif method == 'WebcastGiftMessage':
        msg = Live_pb2.GiftMessage()
        msg.ParseFromString(payload)

    elif method == 'WebcastMemberMessage':
        # 用户进入直播间

    elif method == 'WebcastLikeMessage':
        # 点赞（带 count 和 total）

    elif method == 'WebcastSocialMessage':
        # 关注行为 (action==1)

    elif method == 'WebcastRoomStatsMessage':
        # 房间统计
        msg.displayLong  # 长在线人数
        msg.displayMiddle # 中在线人数
        msg.displayShort  # 短在线人数
        msg.total         # 总观看人数
```

---

## 三、Room ID 提取技术（重点）

### 方法：`get_live_info()` 从 HTML 提取

```python
def get_live_info(auth_, live_id):
    url = "https://live.douyin.com/" + live_id
    res = requests.get(url, headers=headers, cookies=auth_.cookie)

    # 从响应 Cookie 中获取 ttwid
    ttwid = res.cookies.get_dict()['ttwid']

    soup = BeautifulSoup(res.text, 'html.parser')
    scripts = soup.select('script[nonce]')

    for script in scripts:
        if script.string and 'roomId' in script.string:
            # 从 script 标签的 JSON 中提取
            room_id = re.findall(r'"roomId":"(\d+)"', script.string)[0]
            user_id = re.findall(r'"user_unique_id":"(\d+)"', script.string)[0]
            sec_uid = re.findall(r'"sec_uid":"(.*?)"', script.string)[0]
            room_status = re.findall(r'"roomInfo":\{"room":\{.*?"status":(.*?),', script.string)[0]
            room_title = re.findall(r'"roomInfo":\{"room":\{.*?"title":"(.*?)"', script.string)[0]
```

### 🔑 关键洞察

- **Room ID 不在 URL 路径中**，必须请求 HTML 页面
- 关键数据嵌入在 `<script nonce>` 标签的 JSON 中
- 同时提取：`roomId`, `user_unique_id`, `sec_uid`, `room_status`, `room_title`
- `live.douyin.com/{short_id}` → 重定向到带真实 room_id 的页面

---

## 四、Cookie 双套策略

```
DY_COOKIES     (www.douyin.com)  → 用于数据爬取
DY_LIVE_COOKIES (live.douyin.com) → 用于直播间监控
```

### `DouyinAuth` 类（`builder/auth.py`）

```python
class DouyinAuth:
    def prepare_auth(self, cookieStr, web_protect_="", keys_=""):
        # 解析 cookie 字符串为字典
        # 提取 msToken（没有则生成 107 位随机字符串）
        # 如有 web_protect：提取 ticket, ts_sign, client_cert
        # 如有 keys：提取 ec_privateKey 用于 RSA 签名
```

### 关键认证头

| 字段 | 用途 |
|------|------|
| `bd-ticket-guard-client-data` | Base64 编码的票据数据 + RSA 签名 |
| `bd-ticket-guard-ree-public-key` | RSA 公钥 |
| `x-secsdk-csdk-csrf-token` | CSRF token |
| `a_bogus` | URL 参数签名（JS 生成） |

---

## 五、签名生成技术

### 三种 JS 签名（`dy_util.py`）

1. **`generate_a_bogus(query, data)`** — `dy_ab.js`
   - 传入 URL query string 和可选 POST data
   - 通过 `execjs` 执行返回 `a_bogus` 参数

2. **`generate_signature(room_id, user_unique_id)`** — `dy_live_sign.js`
   - 原始字符串格式：
     ```
     live_id=1,aid=6383,version_code=180800,
     webcast_sdk_version=1.0.15,room_id={room_id},
     user_unique_id={user_unique_id}...
     ```
   - MD5 哈希 → 传给 JS 生成 X-Bogus

3. **`generate_req_sign(e, priK)`** — 私信请求签名

### IM WebSocket 的 Access Key 生成

```python
accessKey = f'{fpId + appKey + deviceId}f8a69f1719916z'
accessKey = hashlib.md5(accessKey.encode()).hexdigest()
# appKey = 'e1bd35ec9db7b8d846de66ed140b1ad9'
```

---

## 六、Protobuf 定义

### 直播间 Protobuf（`Live_pb2.py`）

```
PushFrame          # 封包：logId, payload, payloadType, encoding
  ├── payload      # gzip 压缩的数据
  └── encoding     # 编码方式

LiveResponse       # 服务器响应
  ├── cursor       # 游标
  ├── messagesList # 消息列表
  ├── heartbeatDuration  # 心跳间隔
  └── needAck      # 是否需要 ACK

Message            # 单条消息包装
  ├── method       # 消息类型字符串（如 "WebcastChatMessage"）
  ├── payload      # 消息体 Protobuf 二进制
  ├── msgId        # 消息 ID
  └── offset       # 偏移量

ChatMessage        # 弹幕
GiftMessage       # 礼物
MemberMessage     # 进房
LikeMessage       # 点赞
SocialMessage     # 关注
RoomStatsMessage  # 房间统计
```

### 私信 Protobuf（`Request_pb2.py`）

```
Request            # 命令式请求
  ├── cmd          # 命令 ID（100=发消息, 609=创建会话）
  ├── sequence_id  # 序列号
  ├── sdk_version # SDK 版本
  ├── token       # 认证 token
  ├── auth_type   # 认证类型
  ├── headers      # 头信息 map
  └── body        # 请求体
```

---

## 七、私信 WebSocket（`dy_apis/douyin_recv_msg.py`）

```
URL: wss://frontier-im.douyin.com/ws/v2
认证: appKey + deviceId + MD5(AccessKey)
协议: PushFrame 二进制协议
```

---

## 八、调试要点

| 问题 | 参考来源 | 可能原因 |
|------|---------|---------|
| WebSocket 连接被拒 | `dy_live/server.py` 连接参数 | 签名无效 / ttwid 缺失 / Origin 错误 |
| Room ID 获取不到 | `get_live_info()` HTML 解析 | 页面结构变化 / Cookie 过期 |
| 心跳断开 | 独立 ping 线程机制 | 心跳间隔不对 / needAck 未处理 |
| 消息解析失败 | `Live_pb2.py` | Protobuf 定义版本不匹配 / 压缩方式错误 |
| 弹幕获取为空 | `WebcastChatMessage` 解析 | method 名不匹配 / payload 解析错误 |

---

## 九、文件对应参考索引

| 我们的文件 | 参考文件 | 关键内容 |
|-----------|---------|---------|
| `live_collector.py` | `dy_live/server.py` | WebSocket 连接 + 消息解析 |
| `live_detector.py` | `douyin_api.py` (get_live_info) | Room ID HTML 提取 |
| `signer_manager.py` | `utils/dy_util.py` | JS 签名调用 |
| `cookie_manager.py` | `utils/cookie_util.py` | Playwright Cookie 提取 |
| `proto/dy_pb2.py` | `static/Live_pb2.py` | Protobuf 定义 |
| `auth.py` | `builder/auth.py` | 认证处理 |

---

## 十、我们的项目 vs 参考项目

| 维度 | 我们的项目 | 参考项目 |
|------|-----------|---------|
| WebSocket Server | `wss://webcast5-ws-web-lf.douyin.com` | `wss://webcast100-ws-web-hl.douyin.com` |
| 心跳机制 | websockets 库 ping_interval | 独立线程 PushFrame |
| Room ID | API + Cookie live_debug_info | HTML script 标签提取 |
| 签名 | Node.js 签名服务 | JS (execjs) 本地执行 |
| Protobuf | `proto/dy_pb2.py` | `static/Live_pb2.py` |
| Cookie | ttwid 优先 | ttwid + msToken 双套 |

> ⚠️ **参考项目使用不同的服务器域名**，可能有不同的稳定性表现

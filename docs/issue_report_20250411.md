# 抖音直播弹幕收集器问题诊断报告

**报告日期**: 2026年4月11日  
**问题类型**: Cookie 失效导致验证码拦截  
**影响范围**: 开播检测、room_id 获取

---

## 1. 问题现象

### 1.1 用户反馈
- 开播检测无法正常工作
- 无法自动获取 room_id
- 需要手动填写 room_id 才能采集弹幕

### 1.2 日志表现
```
[浏览器] httpx 返回页面太短(6284B)，可能被验证码拦截
[浏览器] Playwright 访问直播链接页面失败
```

---

## 2. 问题根因

### 2.1 抖音反爬策略升级
抖音对 `live.douyin.com/{web_rid}` 端点增加了验证码保护：
- **无 Cookie 请求**: 返回验证码页面（约 6KB）
- **过期 Cookie 请求**: 同样返回验证码页面
- **有效 Cookie 请求**: 返回完整页面（约 1MB+）

### 2.2 原代码缺陷
`src/detector/live_detector.py` 第 569-626 行的 httpx 请求：
- ❌ 未携带 Cookie
- ❌ 未添加 X-Bogus 签名
- ❌ 被抖音识别为爬虫流量

---

## 3. 解决方案

### 3.1 立即修复（已实施）

#### 3.1.1 更新 Cookie
- 手动在浏览器中访问 `https://live.douyin.com/566264616512`
- 完成滑动验证码验证
- 从浏览器开发者工具复制完整 Cookie
- 更新到 `config.yaml` 的 `douyin.cookie` 字段

#### 3.1.2 代码修改
修改 `src/detector/live_detector.py` 第 569-626 行：
- ✅ 添加 Cookie 到请求头
- ✅ 调用签名服务生成 X-Bogus
- ✅ 构建带签名的完整 URL

```python
# 修改后代码片段
_headers2 = {
    "User-Agent": self.WEB_UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://live.douyin.com/",
}

# 添加 Cookie 到请求头
if cookie:
    _headers2["Cookie"] = cookie

# 尝试带签名的请求
from ..utils.signer import generate_a_bogus
params_str = f"web_rid={found_web_rid}"
x_bogus = generate_a_bogus(params_str, self.WEB_UA)
if x_bogus:
    base_url = f"{base_url}?web_rid={found_web_rid}&X-Bogus={x_bogus}"
```

### 3.2 验证结果

| 测试项 | 修改前 | 修改后 |
|--------|--------|--------|
| 页面大小 | 6KB（验证码） | 1,091,798B（完整页面） |
| room_id 提取 | ❌ 失败 | ✅ 成功 |
| 检测流程 | ❌ 中断 | ✅ 正常 |

---

## 4. 后续建议

### 4.1 Cookie 维护
- **有效期**: 手动验证后的 Cookie 通常可用几小时到几天
- **更新方式**: 当检测到验证码页面时，需重新手动验证
- **自动化限制**: 抖音验证码为滑动拼图，难以完全自动化

### 4.2 监控建议
建议添加 Cookie 健康检查：
```python
# 定期检测 Cookie 是否仍有效
def check_cookie_health():
    test_url = "https://live.douyin.com/566264616512"
    response = httpx.get(test_url, headers={"Cookie": cookie})
    if len(response.text) < 50000:
        alert("Cookie 已失效，需要重新验证")
```

### 4.3 备选方案
如 Cookie 频繁失效，可考虑：
1. **Playwright 常驻**: 保持浏览器实例运行，模拟真实用户
2. **代理池**: 使用多个 IP 轮换降低风控概率
3. **降低频率**: 减少检测频率，避免触发风控

---

## 5. 相关文件

| 文件 | 修改内容 |
|------|----------|
| `config.yaml` | 更新 `douyin.cookie` 为过验证后的值 |
| `src/detector/live_detector.py` | 第 569-626 行添加 Cookie 和签名支持 |

---

## 6. 附录

### 6.1 测试命令
```bash
# 验证 Cookie 是否有效
cd /Users/limingrui/CodeBuddy/douyin-ppp
python3 -c "
import httpx
import yaml

with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
cookie = config['douyin']['cookie']

headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Cookie': cookie,
}

with httpx.Client(timeout=15) as client:
    resp = client.get('https://live.douyin.com/566264616512', headers=headers)
    print(f'页面大小: {len(resp.text)} bytes')
    if len(resp.text) > 50000:
        print('✅ Cookie 有效')
    else:
        print('❌ Cookie 已失效，需要重新验证')
"
```

### 6.2 服务启动命令
```bash
# 签名服务（端口 3010）
cd websdk && npm start

# 主服务（端口 9527）
cd /Users/limingrui/CodeBuddy/douyin-ppp
python3 app.py

# 或使用 nohup 后台运行
nohup python3 app.py > data/logs/app.log 2>&1 &
```

---

**报告生成时间**: 2026-04-11 20:45  
**报告状态**: ✅ 已解决

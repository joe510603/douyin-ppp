#!/usr/bin/env python3
"""
测试脚本 - 验证弹幕采集器升级是否成功

测试内容:
1. Protobuf 解析功能
2. 签名服务连接
3. WebSocket 连接
"""

import asyncio
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from src.utils.logger import get_logger

log = get_logger("test")


async def test_protobuf():
    """测试 Protobuf 解析"""
    log.info("=" * 50)
    log.info("测试 1: Protobuf 解析")
    log.info("=" * 50)
    
    try:
        from proto import dy_pb2 as pb
        log.info("✅ Protobuf 模块导入成功")
        
        # 测试 PushFrame 解析
        push_frame = pb.PushFrame()
        push_frame.seqId = 123
        push_frame.logId = 456
        log.info(f"✅ PushFrame 创建成功: seqId={push_frame.seqId}")
        
        # 测试 ChatMessage 解析
        chat_msg = pb.ChatMessage()
        chat_msg.content = "测试弹幕"
        log.info(f"✅ ChatMessage 创建成功: content={chat_msg.content}")
        
        return True
        
    except ImportError as e:
        log.error(f"❌ Protobuf 模块导入失败: {e}")
        log.error("请运行: cd proto && protoc --python_out=. dy.proto")
        return False
    except Exception as e:
        log.error(f"❌ Protobuf 测试失败: {e}")
        return False


async def test_signer_client():
    """测试签名服务客户端"""
    log.info("=" * 50)
    log.info("测试 2: 签名服务连接")
    log.info("=" * 50)
    
    try:
        from src.utils.signer_client import SignerClient
        
        client = SignerClient()
        log.info("✅ 签名客户端创建成功")
        
        # 测试获取签名
        test_stub = "test_x_ms_stub_123"
        log.info(f"发送测试请求: {test_stub}")
        
        signature = await client.get_signature(test_stub)
        
        if signature:
            log.info(f"✅ 签名服务响应成功: {signature[:30]}...")
            await client.close()
            return True
        else:
            log.error("❌ 签名服务返回空")
            log.error("请检查签名服务是否启动: cd websdk && node server.js")
            await client.close()
            return False
            
    except Exception as e:
        log.error(f"❌ 签名服务测试失败: {e}")
        log.error("请确保签名服务已启动: cd websdk && node server.js")
        return False


async def test_websocket_url():
    """测试 WebSocket URL 构建"""
    log.info("=" * 50)
    log.info("测试 3: WebSocket URL 构建")
    log.info("=" * 50)
    
    try:
        from src.utils.signer_client import get_signer_client
        import hashlib
        
        # 模拟参数
        params = {
            "live_id": "1",
            "aid": "6383",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "room_id": "123456789",
            "user_unique_id": "7123456789012345678",
            "device_platform": "web",
            "identity": "audience"
        }
        
        # 构建 X-Ms-Stub
        sig_params = ",".join([f"{k}={v}" for k, v in params.items()])
        x_ms_stub = hashlib.md5(sig_params.encode()).hexdigest()
        
        log.info(f"X-Ms-Stub: {x_ms_stub}")
        
        # 获取签名
        client = get_signer_client()
        signature = await client.get_signature(x_ms_stub)
        
        if signature:
            log.info(f"✅ 签名获取成功: {signature}")
            
            # 构建 WebSocket URL
            ws_url = (
                f"wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?"
                f"room_id={params['room_id']}"
                f"&compress=gzip"
                f"&version_code={params['version_code']}"
                f"&webcast_sdk_version={params['webcast_sdk_version']}"
                f"&signature={signature}"
            )
            
            log.info(f"✅ WebSocket URL 构建成功")
            log.info(f"URL (前100字符): {ws_url[:100]}...")
            
            return True
        else:
            log.error("❌ 签名获取失败")
            return False
            
    except Exception as e:
        log.error(f"❌ WebSocket URL 构建测试失败: {e}")
        return False


async def main():
    """运行所有测试"""
    log.info("\n" + "🚀 " * 25)
    log.info("开始测试弹幕采集器升级")
    log.info("🚀 " * 25 + "\n")
    
    results = []
    
    # 测试 1: Protobuf
    results.append(await test_protobuf())
    print()
    
    # 测试 2: 签名服务
    results.append(await test_signer_client())
    print()
    
    # 测试 3: WebSocket URL
    results.append(await test_websocket_url())
    print()
    
    # 汇总结果
    log.info("=" * 50)
    log.info("测试结果汇总")
    log.info("=" * 50)
    
    passed = sum(results)
    total = len(results)
    
    log.info(f"✅ 通过: {passed}/{total}")
    log.info(f"❌ 失败: {total - passed}/{total}")
    
    if passed == total:
        log.info("\n🎉 所有测试通过! 升级成功!")
        log.info("\n下一步:")
        log.info("  1. 启动签名服务: cd websdk && node server.js")
        log.info("  2. 启动主程序: python app.py")
        log.info("  3. 访问: http://localhost:9527")
    else:
        log.error("\n❌ 部分测试未通过,请检查上述错误信息")
        log.error("\n常见问题:")
        log.error("  - Protobuf 未编译: cd proto && protoc --python_out=. dy.proto")
        log.error("  - 签名服务未启动: cd websdk && node server.js")
        log.error("  - Node.js 依赖未安装: cd websdk && npm install")
    
    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
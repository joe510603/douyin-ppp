"""
抖音 Cookie 自动提取工具
用法: python -m src.utils.cookie_extractor
"""

from playwright.sync_api import sync_playwright
import time
import json
from pathlib import Path
import sys

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.resolve()))
from src.config import get_config, save_config, Settings

def extract_cookie(auto_mode: bool = False, wait_time: int = 60):
    """
    使用 Playwright 打开抖音，等待用户登录后自动提取 Cookie

    Args:
        auto_mode: 是否自动模式（不等待用户输入）
        wait_time: 自动模式下的等待时间（秒）
    """
    print("=" * 60)
    print("🍪 抖音 Cookie 提取工具")
    print("=" * 60)
    print()
    print("步骤:")
    print("1. 浏览器将自动打开抖音页面")
    print("2. 请手动扫码或密码登录")

    if auto_mode:
        print(f"3. 等待 {wait_time} 秒让你完成登录...")
        print("4. 自动提取并保存 Cookie")
    else:
        print("3. 登录成功后，按回车键提取 Cookie")
        print("4. Cookie 将自动保存到 config.yaml")
        print()
        input("按回车键开始...")
    print()

    try:
        with sync_playwright() as p:
            # 启动浏览器（有头模式，方便用户操作）
            browser = p.chromium.launch(
                headless=False,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--window-size=1400,900'
                ]
            )

            context = browser.new_context(
                viewport={'width': 1400, 'height': 900},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )

            page = context.new_page()

            # 打开抖音
            print("🌐 正在打开抖音...")
            page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(3000)  # 等待3秒让页面继续加载

            print()
            print("📱 请扫码或密码登录抖音")

            if auto_mode:
                print(f"⏳ 等待 {wait_time} 秒让你完成登录...")
                print("   (如果需要更多时间，请修改 wait_time 参数)")
                import time
                time.sleep(wait_time)
            else:
                print("⏳ 登录成功后，请回到这里按回车键...")
                print()
                input("登录完成后按回车键继续...")

            # 等待页面加载完成
            print("🔄 正在获取 Cookie...")
            page.wait_for_timeout(3000)

            # 获取所有 Cookie
            cookies = context.cookies()

            # 筛选关键 Cookie
            essential_cookies = ['ttwid', '__ac_nonce', '__ac_signature', 'sessionid']
            cookie_dict = {}

            for cookie in cookies:
                name = cookie['name']
                if name in essential_cookies or 'tt' in name.lower():
                    cookie_dict[name] = cookie['value']
                    print(f"  ✓ 获取到: {name}")

            # 构建 Cookie 字符串
            cookie_str = '; '.join([f"{k}={v}" for k, v in cookie_dict.items()])

            print()
            print("=" * 60)
            print("📋 提取到的 Cookie:")
            print("=" * 60)
            print(cookie_str[:100] + "..." if len(cookie_str) > 100 else cookie_str)
            print()

            # 保存到配置文件
            config = get_config()
            config.douyin.cookie = cookie_str
            save_config(config)

            print("✅ Cookie 已保存到 config.yaml")
            print()

            # 验证 Cookie
            print("🔍 正在验证 Cookie...")
            try:
                page.goto('https://live.douyin.com/', wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"⚠️ 验证页面加载超时，但 Cookie 已保存: {e}")

            # 检查是否已登录（通过检查页面元素）
            try:
                # 如果有用户头像或用户名，说明登录成功
                avatar = page.locator('[data-e2e="user-avatar"]').first
                if avatar.is_visible(timeout=5000):
                    print("✅ Cookie 验证成功！已登录状态")
                else:
                    print("⚠️ 可能未登录，请检查")
            except:
                print("⚠️ 无法验证登录状态，但 Cookie 已保存")

            browser.close()

            print()
            print("📝 提示:")
            print("- 重启 app.py 以使用新 Cookie")
            print("- Cookie 有效期通常为几天到几周")
            print("- 失效后需要重新运行此工具")
            print()

            return cookie_str

    except ImportError:
        print("❌ 错误: Playwright 未安装")
        print("请运行: pip install playwright && playwright install chromium")
        return None
    except Exception as e:
        print(f"❌ 错误: {e}")
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='抖音 Cookie 提取工具')
    parser.add_argument('--auto', '-a', action='store_true',
                        help='自动模式（不等待用户输入，等待60秒）')
    parser.add_argument('--wait', '-w', type=int, default=60,
                        help='自动模式下的等待时间（秒），默认60秒')
    args = parser.parse_args()

    # 检测是否在交互式环境
    is_interactive = sys.stdin.isatty()

    if not is_interactive and not args.auto:
        print("⚠️ 检测到非交互式环境，自动启用自动模式（等待60秒）")
        args.auto = True

    cookie = extract_cookie(auto_mode=args.auto, wait_time=args.wait)
    if cookie:
        print("🎉 Cookie 提取完成！请重启 app.py")
        sys.exit(0)
    else:
        print("❌ Cookie 提取失败")
        sys.exit(1)

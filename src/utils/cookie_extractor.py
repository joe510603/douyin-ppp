"""
抖音 Cookie 提取工具（支持滑块验证码自动处理）
用法: python -m src.utils.cookie_extractor

功能:
  1. Playwright 可见浏览器打开抖音
  2. 自动检测并处理滑块验证码
  3. 用户手动登录
  4. 提取完整 cookies（同时保存到 douyin.cookie 和 douyin_video.cookie）
  5. 验证 cookie 有效性
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.resolve()))

from src.config import get_config, save_config


# ============================================================
# 滑块验证码处理（参考 MediaCrawler slider_util.py）
# ============================================================

def get_track_simple(distance: int) -> list[int]:
    """生成滑动轨迹（带加速减速）"""
    track: list[int] = []
    current = 0
    mid = distance * 4 / 5
    t = 0.2
    v = 1

    while current < distance:
        if current < mid:
            a = 4
        else:
            a = -3
        v0 = v
        v = v0 + a * t
        move = v0 * t + 1 / 2 * a * t * t
        current += move
        track.append(round(move))
    return track


def slide_image_match(gap_url: str, bg_url: str) -> Optional[int]:
    """
    使用图像识别找到滑块位置
    Returns: 滑块需要移动的距离（像素）
    """
    try:
        import httpx

        def download_img(url: str) -> Optional[np.ndarray]:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            }
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            nparr = np.frombuffer(resp.content, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            return img

        bg_img = download_img(bg_url)
        gap_img = download_img(gap_url)

        if bg_img is None or gap_img is None:
            return None

        # 缩放到标准尺寸
        bg_img = cv2.resize(bg_img, (340, 212))
        gap_img = cv2.resize(gap_img, (68, 68))

        # 转灰度
        bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        gap_gray = cv2.cvtColor(gap_img, cv2.COLOR_BGR2GRAY)

        # 边缘检测
        bg_edge = cv2.Canny(bg_gray, 100, 200)
        gap_edge = cv2.Canny(gap_gray, 100, 200)

        # 模板匹配
        res = cv2.matchTemplate(bg_edge, gap_edge, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > 0.7:
            return max_loc[0]
    except Exception as e:
        print(f"    图像识别失败: {e}")
    return None


async def try_solve_slider(page) -> bool:
    """
    尝试自动解决滑块验证码
    Returns: True=解决成功, False=没有滑块或解决失败
    """
    try:
        # 检查滑块验证码元素
        bg_selector = "#captcha-verify-image"
        gap_selector = 'xpath=//*[@id="captcha_container"]/div/div[2]/img[2]'

        # 等待滑块出现（最多等30秒）
        try:
            await page.wait_for_selector(bg_selector, state="visible", timeout=30000)
        except Exception:
            # 没有滑块，返回
            return False

        print("    检测到滑块验证码，正在自动处理...")

        for attempt in range(10):
            try:
                # 获取背景图和滑块图
                bg_ele = await page.wait_for_selector(bg_selector, timeout=5000)
                gap_ele = await page.wait_for_selector(gap_selector, timeout=5000)

                bg_src = str(await bg_ele.get_property("src"))
                gap_src = str(await gap_ele.get_property("src"))

                distance = slide_image_match(gap_src, bg_src)
                if distance is None:
                    print(f"    第{attempt+1}次：图像识别失败，等待重试...")
                    await asyncio.sleep(2)
                    continue

                # 获取滑动轨迹
                tracks = get_track_simple(distance)
                # 修正最后一步
                new_last = tracks[-1] - (sum(tracks) - distance)
                tracks[-1] = new_last

                # 获取滑块元素
                slider_ele = await page.query_selector(gap_selector)
                box = await slider_ele.bounding_box()

                start_x = box["x"] + box["width"] / 2
                start_y = box["y"] + box["height"] / 2

                await page.mouse.move(start_x, start_y)
                await slider_ele.hover()
                await page.mouse.down()

                x = start_x
                for track in tracks:
                    await page.mouse.move(x + track, start_y, steps=3)
                    x += track

                await page.mouse.up()
                await asyncio.sleep(1)

                # 检查是否需要刷新
                page_content = await page.content()
                if "操作过慢" in page_content or "重新操作" in page_content:
                    print(f"    第{attempt+1}次：滑块验证失败（操作过慢），重试...")
                    try:
                        refresh = page.locator('a[class*="refresh"]')
                        if await refresh.is_visible(timeout=2000):
                            await refresh.click()
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                    continue

                # 等待滑块消失（最多等5秒）
                try:
                    await page.wait_for_selector(bg_selector, state="hidden", timeout=5000)
                    print("    滑块验证成功!")
                    return True
                except Exception:
                    print(f"    第{attempt+1}次：滑块未消失，继续...")

            except Exception as e:
                print(f"    第{attempt+1}次：滑块处理异常: {e}")
                await asyncio.sleep(1)

        print("    滑块验证自动处理失败，将等待手动处理...")
        return False

    except Exception as e:
        print(f"    滑块检测异常: {e}")
        return False


# ============================================================
# 主提取逻辑（同步包装）
# ============================================================

def _convert_str_cookie(cookie_str: str) -> list[dict]:
    """将 cookie 字符串转换为 Playwright 格式"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })
    return cookies


def _cookies_to_str(cookies: list[dict]) -> str:
    """将 Playwright cookie 列表转换为字符串"""
    return "; ".join([f"{c['name']}={c['value']}" for c in cookies])


def extract_cookie_async(
    auto_mode: bool = False,
    wait_time: int = 60,
    initial_cookie: str = "",
) -> Optional[dict]:
    """
    使用 Playwright 异步提取 Cookie（支持滑块验证码）

    Args:
        auto_mode: 自动模式（不等待用户输入）
        wait_time: 自动模式下的等待时间
        initial_cookie: 初始 cookie（会注入到浏览器）

    Returns:
        {"douyin": str, "douyin_video": str} or None
    """

    async def _run():
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

        print("=" * 60)
        print("🍪 抖音 Cookie 提取工具")
        print("=" * 60)
        print()

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,  # 必须非无头，让用户看到浏览器
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--window-size=1400,900",
                    ],
                )

                context = await browser.new_context(
                    viewport={"width": 1400, "height": 900},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                )

                # 如果有初始 cookie，先注入
                if initial_cookie:
                    cookies = _convert_str_cookie(initial_cookie)
                    await context.add_cookies(cookies)
                    print("✓ 已注入初始 Cookie")

                page = await context.new_page()

                print("🌐 正在打开抖音（可能需要完成验证码）...")
                try:
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
                except PlaywrightTimeoutError:
                    print("⚠️ 页面加载超时，继续...")

                await asyncio.sleep(3)

                # 处理滑块验证码
                await try_solve_slider(page)

                # 检查是否需要登录
                page_content = await page.content()
                is_logged_in = 'id="is-login"' in page_content or "login" not in page_content

                if not is_logged_in:
                    print()
                    print("📱 请手动扫码或密码登录抖音...")
                    print("   （如果出现验证码，请手动完成）")
                    if auto_mode:
                        print(f"⏳ 等待 {wait_time} 秒...")
                        await asyncio.sleep(wait_time)
                    else:
                        print("⏳ 登录成功后，请回到这里按回车键...")
                        input("按回车键继续...")

                    # 登录后再处理滑块
                    await try_solve_slider(page)

                # 等待页面稳定
                await asyncio.sleep(3)

                print()
                print("🔄 正在获取 Cookie...")

                # 获取所有 cookies
                all_cookies = await context.cookies()

                # 打印获取到的 cookie 列表
                print(f"   共获取 {len(all_cookies)} 个 cookie:")
                for c in all_cookies:
                    print(f"   ✓ {c['name']}")

                # 构建完整 cookie 字符串
                cookie_str = _cookies_to_str(all_cookies)

                # 保存到配置
                config = get_config()
                config.douyin.cookie = cookie_str
                config.douyin_video.cookie = cookie_str  # 同时保存到视频专用
                save_config(config)

                print()
                print(f"✅ Cookie 已保存到 douyin.cookie 和 douyin_video.cookie")
                print(f"   长度: {len(cookie_str)} 字符")

                # 验证
                print()
                print("🔍 正在验证 Cookie 有效性...")
                try:
                    await page.goto("https://www.douyin.com/search/test", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)

                    # 检查是否遇到验证码
                    content = await page.content()
                    if "验证码中间页" in content:
                        print("⚠️ Cookie 可能已过期（仍需验证码），建议重新提取")
                    else:
                        print("✅ Cookie 验证通过！")
                except Exception as e:
                    print(f"⚠️ 验证过程出现异常: {e}")

                await browser.close()

                print()
                print("📝 提示:")
                print("- 请重启 app.py 以使用新 Cookie")
                print("- Cookie 有效期通常为几天到几周")
                print("- 失效后重新运行此工具提取新 Cookie")
                print()

                return {"douyin": cookie_str, "douyin_video": cookie_str}

        except ImportError:
            print("❌ 错误: Playwright 未安装")
            print("请运行: pip install playwright && playwright install chromium")
            return None
        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()
            return None

    return asyncio.run(_run())


def extract_cookie(
    auto_mode: bool = False,
    wait_time: int = 60,
    initial_cookie: str = "",
) -> Optional[dict]:
    """同步入口"""
    return extract_cookie_async(auto_mode=auto_mode, wait_time=wait_time, initial_cookie=initial_cookie)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="抖音 Cookie 提取工具")
    parser.add_argument("--auto", "-a", action="store_true", help="自动模式（不等待用户输入）")
    parser.add_argument("--wait", "-w", type=int, default=60, help="自动模式等待时间（秒）")
    args = parser.parse_args()

    is_interactive = sys.stdin.isatty()
    if not is_interactive and not args.auto:
        print("⚠️ 非交互式环境，自动启用等待模式（60秒）")
        args.auto = True

    result = extract_cookie(auto_mode=args.auto, wait_time=args.wait)
    if result:
        print("🎉 Cookie 提取完成！请重启 app.py")
        sys.exit(0)
    else:
        print("❌ Cookie 提取失败")
        sys.exit(1)

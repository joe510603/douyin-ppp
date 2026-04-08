#!/usr/bin/env python3
"""
抖音 Cookie 自动提取工具
用法: python get_cookie.py          # 交互模式
       python get_cookie.py --auto   # 自动模式（等待60秒）
       python get_cookie.py -a -w 90 # 自动模式，等待90秒
"""

import sys
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.utils.cookie_extractor import extract_cookie
import argparse

if __name__ == "__main__":
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
        sys.exit(1)

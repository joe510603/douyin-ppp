"""词云分析页面 — 8大分析功能（左侧菜单 + 右侧内容区）"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from nicegui import ui

from ..config import get_config
from ..storage.db_storage import get_db
from ..utils.logger import get_logger

log = get_logger("wordcloud_page")

# 分析功能定义
ANALYSIS_TYPES = {
    "wordcloud": {"name": "词云图", "icon": "cloud", "need_llm": False, "desc": "可视化展示高频词"},
    "high_freq": {"name": "高频词分析", "icon": "bar_chart", "need_llm": False, "desc": "统计出现最多的词及词频"},
    "sentiment": {"name": "情感分析", "icon": "sentiment_satisfied", "need_llm": True, "desc": "正面/负面/中性分类"},
    "questions": {"name": "问题挖掘", "icon": "help_outline", "need_llm": False, "desc": "提取用户疑问句"},
    "intent": {"name": "意图分类", "icon": "psychology", "need_llm": True, "desc": "购买/体验/疑问/吐槽/互动"},
    "competitor": {"name": "竞品对比", "icon": "compare_arrows", "need_llm": True, "desc": "提取竞品提及"},
    "time_trend": {"name": "时间趋势", "icon": "timeline", "need_llm": False, "desc": "评论量随时间变化"},
    "profile": {"name": "用户画像", "icon": "person_outline", "need_llm": True, "desc": "年龄/肤质/地区分析"},
    "cluster": {"name": "热词聚类", "icon": "category", "need_llm": True, "desc": "主题自动聚类"},
}


def _find_chinese_font() -> Optional[str]:
    """查找系统中可用的中文字体"""
    import platform
    system = platform.system()

    candidates = []
    if system == "Darwin":  # macOS
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    elif system == "Windows":
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]

    for path in candidates:
        if Path(path).exists():
            return path

    return None


def _generate_wordcloud_image(
    texts: list[str],
    width: int = 800,
    height: int = 600,
    max_words: int = 200,
    font_path: Optional[str] = None,
) -> Optional[bytes]:
    """生成词云图片"""
    try:
        import jieba
        from wordcloud import WordCloud
        from PIL import Image

        # 合并文本
        full_text = " ".join(texts)
        if not full_text.strip():
            return None

        # jieba 分词
        words = jieba.lcut(full_text)
        # 过滤短词和停用词
        stopwords = {
            "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
            "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
            "会", "着", "没有", "看", "好", "自己", "这", "他", "她", "它",
            "那", "被", "还", "能", "把", "从", "让", "用", "对", "又",
            "吗", "吧", "啊", "呢", "哦", "哈", "嗯", "呀", "嘛", "啦",
            "所以", "但是", "因为", "如果", "虽然", "这个", "那个", "什么",
            "怎么", "可以", "已经", "可能", "应该", "需要", "知道", "觉得",
        }
        filtered_words = [w for w in words if len(w) >= 2 and w not in stopwords]
        if not filtered_words:
            return None

        text_for_cloud = " ".join(filtered_words)

        # 查找中文字体
        if not font_path:
            font_path = _find_chinese_font()
        if not font_path:
            log.warning("未找到中文字体，词云中文可能显示为方框")

        # 生成词云
        wc_kwargs = {
            "width": width,
            "height": height,
            "max_words": max_words,
            "background_color": "#1a1a2e",
            "colormap": "viridis",
            "prefer_horizontal": 0.7,
            "min_font_size": 10,
        }
        if font_path:
            wc_kwargs["font_path"] = font_path

        wc = WordCloud(**wc_kwargs)
        wc.generate(text_for_cloud)

        # 输出 PNG
        img_buffer = io.BytesIO()
        wc.to_image().save(img_buffer, format="PNG")
        return img_buffer.getvalue()

    except ImportError as e:
        log.error(f"缺少依赖: {e}，请安装 jieba, wordcloud, Pillow")
        return None
    except Exception as e:
        log.error(f"词云生成失败: {e}", exc_info=True)
        return None


def create_wordcloud_page():
    """创建词云分析页面"""
    
    # 获取配置
    config = get_config()
    monitor_names = ["全部账号"] + [m.name for m in config.monitors if m.enabled]
    all_tags = set()
    for m in config.monitors:
        all_tags.update(m.tags)
    tag_options = ["全部标签"] + sorted(all_tags)
    
    # 当前选中的分析类型
    current_analysis = {"type": "wordcloud"}
    
    # 主布局：左侧菜单 + 右侧内容
    with ui.element("div").classes("w-full flex gap-md"):
        
        # ========== 左侧菜单 ==========
        with ui.element("div").classes("w-56 flex-shrink-0"):
            with ui.element("div").classes("app-card q-pa-md"):
                ui.label("分析功能").classes("section-title q-mb-md")
                
                # 菜单项
                menu_items = {}
                for key, info in ANALYSIS_TYPES.items():
                    with ui.element("div").classes(
                        f"menu-item q-pa-sm cursor-pointer rounded transition-all"
                    ) as menu_item:
                        with ui.row().classes("items-center gap-sm"):
                            ui.icon(info["icon"]).classes("text-lg")
                            ui.label(info["name"]).classes("text-subtitle2")
                        
                        # 点击事件
                        def select_analysis(analysis_key):
                            current_analysis["type"] = analysis_key
                            # 更新菜单样式
                            for k, v in menu_items.items():
                                if k == analysis_key:
                                    v.classes(remove="menu-item-inactive", add="menu-item-active bg-primary")
                                else:
                                    v.classes(remove="menu-item-active bg-primary", add="menu-item-inactive")
                            # 刷新内容区
                            content_area.refresh()
                        
                        menu_item.on("click", lambda k=key: select_analysis(k))
                        menu_items[key] = menu_item
                
                # 默认选中第一个
                menu_items["wordcloud"].classes(remove="menu-item-inactive", add="menu-item-active bg-primary")
        
        # ========== 右侧内容区 ==========
        content_container = ui.element("div").classes("flex-grow")
        
        @ui.refreshable
        def content_area():
            with content_container:
                _render_analysis_content(current_analysis["type"], monitor_names, tag_options, config)
        
        content_area()


def _render_analysis_content(analysis_type: str, monitor_names: list, tag_options: list, config):
    """渲染分析内容区"""
    
    info = ANALYSIS_TYPES[analysis_type]
    
    # ========== 筛选条件 ==========
    with ui.element("div").classes("app-card w-full q-pa-md q-mb-md"):
        ui.label(f"{info['name']} - {info['desc']}").classes("section-title q-mb-md")
        
        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            # 数据源
            source_select = ui.select(
                options={"live": "直播弹幕", "video": "视频评论"},
                value="video",
                label="数据源",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            
            # 账号筛选
            monitor_select = ui.select(
                options=monitor_names,
                value="全部账号",
                label="监控账号",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            
            # 标签筛选
            tag_select = ui.select(
                options=tag_options,
                value="全部标签",
                label="标签",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            
            # 日期选择器（点击输入框弹出日历）
            date_from = ui.date_input("开始日期").props("dense outlined").classes("col-6 col-sm-2 app-input")
            date_to = ui.date_input("结束日期").props("dense outlined").classes("col-6 col-sm-2 app-input")
        
        # 分析参数
        if analysis_type == "wordcloud":
            with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
                max_words_input = ui.number("最大词数", value=200, min=50, max=500).props("dense outlined").classes("app-input")
                max_texts_input = ui.number("最大文本数", value=50000, min=1000, max=200000).props("dense outlined").classes("app-input")
        
        # 执行按钮
        ui.button("开始分析", on_click=lambda: _run_analysis(
            analysis_type, source_select, monitor_select, tag_select, 
            date_from, date_to, config,
            max_words_input.value if analysis_type == "wordcloud" else 200,
            max_texts_input.value if analysis_type == "wordcloud" else 50000,
        )).classes("btn-primary q-mt-md").props("unelevated icon=play_arrow")
    
    # ========== 结果展示区 ==========
    result_container = ui.element("div").classes("app-card w-full q-pa-md")
    
    with result_container:
        ui.label("点击「开始分析」查看结果").classes("text-center w-full").style(
            "color: var(--color-text-tertiary); padding: 48px 0;"
        )


async def _run_analysis(
    analysis_type: str,
    source_select, monitor_select, tag_select,
    date_from, date_to, config,
    max_words: int,
    max_texts: int,
):
    """执行分析"""
    
    # 获取数据库连接
    db = get_db()
    if not db or not db._db:
        ui.notify("数据库未初始化", type="negative")
        return
    
    ui.notify("正在获取数据...", type="info")
    
    # 解析筛选条件
    source = source_select.value
    monitor_name = None if monitor_select.value == "全部账号" else monitor_select.value
    tag = None if tag_select.value == "全部标签" else tag_select.value
    
    dt_start = None
    dt_end = None
    # ui.date_input 返回的value是字符串（YYYY-MM-DD格式）
    if date_from.value:
        try:
            dt_start = datetime.strptime(date_from.value, "%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    if date_to.value:
        try:
            dt_end = datetime.strptime(date_to.value, "%Y-%m-%d") + timedelta(days=1)
        except (ValueError, TypeError):
            pass
    
    # 如果选择了标签，找到该标签下的所有账号
    if tag:
        monitor_names_for_tag = [m.name for m in config.monitors if tag in m.tags and m.enabled]
        if monitor_names_for_tag:
            monitor_name = None  # 清空，后面用 IN 查询
        else:
            ui.notify(f"没有找到标签为 {tag} 的监控账号", type="warning")
            return
    
    try:
        # 根据数据源获取文本
        texts = []
        if source == "live":
            texts = await db.get_live_comment_texts(
                monitor_name=monitor_name,
                start_time=dt_start,
                end_time=dt_end,
                limit=max_texts,
            )
        else:
            texts = await db.get_video_comment_texts(
                monitor_name=monitor_name,
                start_time=dt_start,
                end_time=dt_end,
                limit=max_texts,
            )
        
        if not texts:
            ui.notify("没有找到符合条件的评论数据", type="warning")
            return
        
        ui.notify(f"获取到 {len(texts)} 条评论，正在分析...", type="info")
        
        # 执行对应的分析
        if analysis_type == "wordcloud":
            await _analyze_wordcloud(texts, max_words)
        elif analysis_type == "high_freq":
            await _analyze_high_freq(texts)
        elif analysis_type == "questions":
            await _analyze_questions(texts)
        elif analysis_type == "time_trend":
            await _analyze_time_trend(texts)
        else:
            # LLM 分析
            if not config.llm.enabled:
                ui.notify("此功能需要启用 LLM，请在配置页面设置", type="warning")
                return
            ui.notify("LLM 分析功能开发中...", type="info")
    
    except Exception as e:
        log.error(f"分析失败: {e}", exc_info=True)
        ui.notify(f"分析失败: {e}", type="negative")


async def _analyze_wordcloud(texts: list, max_words: int):
    """词云分析"""
    ui.notify("正在生成词云...", type="info")
    
    # 生成词云
    img_bytes = await asyncio.to_thread(
        _generate_wordcloud_image,
        texts,
        800, 600, max_words,
    )
    
    if not img_bytes:
        ui.notify("词云生成失败", type="negative")
        return
    
    # 转为 base64 展示
    b64 = base64.b64encode(img_bytes).decode("ascii")
    
    ui.notify(f"词云生成完成（{len(texts)} 条评论）", type="positive")


async def _analyze_high_freq(texts: list):
    """高频词分析"""
    import jieba
    
    # 分词
    all_words = []
    for text in texts:
        words = jieba.lcut(text)
        all_words.extend([w for w in words if len(w) >= 2])
    
    # 统计词频
    counter = Counter(all_words)
    top_words = counter.most_common(50)
    
    ui.notify(f"高频词分析完成（共 {len(all_words)} 个词）", type="positive")


async def _analyze_questions(texts: list):
    """问题挖掘"""
    # 匹配问号句式
    question_pattern = r'[^。！？\n]*[\？\?][^。！？\n]*'
    questions = []
    
    for text in texts:
        matches = re.findall(question_pattern, text)
        questions.extend([m.strip() for m in matches if len(m.strip()) > 3])
    
    # 统计高频问题
    counter = Counter(questions)
    top_questions = counter.most_common(30)
    
    ui.notify(f"问题挖掘完成（共 {len(questions)} 个问题）", type="positive")


async def _analyze_time_trend(texts_with_time: list):
    """时间趋势分析（需要传入时间戳）"""
    ui.notify("时间趋势分析开发中...", type="info")
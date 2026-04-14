"""全局样式系统 — CSS 变量、动画关键帧、组件样式覆盖"""

from __future__ import annotations

from nicegui import ui


# ============================================================
# 主题色彩定义
# ============================================================

_THEME_COLORS = {
    # 主色调
    "primary": "#6366F1",
    "primary-light": "#818CF8",
    "primary-dark": "#4F46E5",
    # 功能色
    "success": "#22C55E",
    "danger": "#EF4444",
    "warning": "#F59E0B",
    "info": "#3B82F6",
    # 亮色背景
    "bg-base": "#FFFFFF",
    "bg-surface": "#F9FAFB",
    "bg-elevated": "#F3F4F6",
    # 亮色文字
    "text-primary": "#111827",
    "text-secondary": "#6B7280",
    "text-tertiary": "#9CA3AF",
    # 边框
    "border": "#E5E7EB",
    "border-light": "#F3F4F6",
    # 暗色背景
    "dark-bg-base": "#111827",
    "dark-bg-surface": "#1F2937",
    "dark-bg-elevated": "#374151",
    # 暗色文字
    "dark-text-primary": "#F9FAFB",
    "dark-text-secondary": "#D1D5DB",
    "dark-text-tertiary": "#9CA3AF",
    # 暗色边框
    "dark-border": "#374151",
    "dark-border-light": "#4B5563",
}

# 渐变色
_GRADIENTS = {
    "blue": "linear-gradient(135deg, #6366F1 0%, #818CF8 100%)",
    "green": "linear-gradient(135deg, #22C55E 0%, #4ADE80 100%)",
    "purple": "linear-gradient(135deg, #8B5CF6 0%, #A78BFA 100%)",
    "orange": "linear-gradient(135deg, #F59E0B 0%, #FBBF24 100%)",
    "red": "linear-gradient(135deg, #EF4444 0%, #F87171 100%)",
    "indigo": "linear-gradient(135deg, #4F46E5 0%, #6366F1 100%)",
}

# 阴影
_SHADOWS = {
    "sm": "0 1px 2px 0 rgba(0, 0, 0, 0.05)",
    "md": "0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1)",
    "lg": "0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1)",
    "xl": "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)",
    "dark-sm": "0 1px 2px 0 rgba(0, 0, 0, 0.3)",
    "dark-md": "0 4px 6px -1px rgba(0, 0, 0, 0.4), 0 2px 4px -2px rgba(0, 0, 0, 0.3)",
    "dark-lg": "0 10px 15px -3px rgba(0, 0, 0, 0.4), 0 4px 6px -4px rgba(0, 0, 0, 0.3)",
}

# 间距
_SPACING = {
    "xs": "4px",
    "sm": "8px",
    "md": "16px",
    "lg": "24px",
    "xl": "32px",
    "2xl": "48px",
}

# 圆角
_RADIUS = {
    "sm": "6px",
    "md": "10px",
    "lg": "14px",
    "xl": "20px",
    "full": "9999px",
}


def _build_css_variables() -> str:
    """生成 CSS 变量声明"""
    vars_lines = []
    for key, val in _THEME_COLORS.items():
        vars_lines.append(f"  --color-{key}: {val};")
    for key, val in _GRADIENTS.items():
        vars_lines.append(f"  --gradient-{key}: {val};")
    for key, val in _SHADOWS.items():
        vars_lines.append(f"  --shadow-{key}: {val};")
    for key, val in _SPACING.items():
        vars_lines.append(f"  --spacing-{key}: {val};")
    for key, val in _RADIUS.items():
        vars_lines.append(f"  --radius-{key}: {val};")
    return "\n".join(vars_lines)


def _build_dark_overrides() -> str:
    """生成暗色模式变量覆盖"""
    dark_vars = [
        ("--color-bg-base", "var(--color-dark-bg-base)"),
        ("--color-bg-surface", "var(--color-dark-bg-surface)"),
        ("--color-bg-elevated", "var(--color-dark-bg-elevated)"),
        ("--color-text-primary", "var(--color-dark-text-primary)"),
        ("--color-text-secondary", "var(--color-dark-text-secondary)"),
        ("--color-text-tertiary", "var(--color-dark-text-tertiary)"),
        ("--color-border", "var(--color-dark-border)"),
        ("--color-border-light", "var(--color-dark-border-light)"),
        ("--shadow-sm", "var(--shadow-dark-sm)"),
        ("--shadow-md", "var(--shadow-dark-md)"),
        ("--shadow-lg", "var(--shadow-dark-lg)"),
    ]
    return "\n".join(f"  {k}: {v};" for k, v in dark_vars)


def _build_css() -> str:
    """构建全局 CSS 字符串"""

    css = f"""
/* ============================================
   Douyin PPP — 全局样式系统
   ============================================ */

:root {{
{_build_css_variables()}
  /* 过渡 */
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-normal: 250ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 350ms cubic-bezier(0.4, 0, 0.2, 1);

  /* 字体 */
  --font-sans: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
  --font-mono: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
}}

/* 暗色模式覆盖 */
.body--dark {{
{_build_dark_overrides()}
}}


/* ============================================
   全局基础
   ============================================ */

body {{
  font-family: var(--font-sans);
  color: var(--color-text-primary);
  background: var(--color-bg-surface);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}

/* NiceGUI 默认 header 背景修正 */
.q-header {{
  background: var(--color-bg-base) !important;
  border-bottom: 1px solid var(--color-border);
  box-shadow: var(--shadow-sm);
}}

.body--dark .q-header {{
  background: var(--color-dark-bg-base) !important;
  border-bottom-color: var(--color-dark-border);
}}


/* ============================================
   导航栏
   ============================================ */

.app-header {{
  background: var(--color-bg-base);
  border-bottom: 1px solid var(--color-border);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  transition: background var(--transition-normal), border-color var(--transition-normal);
}}

.body--dark .app-header {{
  background: rgba(17, 24, 39, 0.85);
  border-bottom-color: var(--color-dark-border);
}}

.app-header-immersive {{
  background: rgba(17, 24, 39, 0.75) !important;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06) !important;
  backdrop-filter: blur(16px) saturate(180%);
  -webkit-backdrop-filter: blur(16px) saturate(180%);
}}

.nav-brand {{
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--color-primary);
  text-decoration: none;
  transition: opacity var(--transition-fast);
  cursor: pointer;
  user-select: none;
}}

.nav-brand:hover {{
  opacity: 0.8;
}}

.nav-tab {{
  position: relative;
  display: inline-flex;
  align-items: center;
  padding: 8px 16px;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background var(--transition-fast);
  cursor: pointer;
  white-space: nowrap;
}}

.nav-tab:hover {{
  color: var(--color-text-primary);
  background: var(--color-bg-elevated);
}}

.body--dark .nav-tab:hover {{
  background: var(--color-dark-bg-elevated);
}}

.nav-tab-active {{
  color: var(--color-primary) !important;
  font-weight: 600;
}}

.nav-tab-active::after {{
  content: '';
  position: absolute;
  bottom: -1px;
  left: 16px;
  right: 16px;
  height: 2px;
  background: var(--color-primary);
  border-radius: 2px 2px 0 0;
  animation: slideIn var(--transition-normal) ease-out;
}}

.nav-tab-active:hover {{
  background: rgba(99, 102, 241, 0.06);
}}

.body--dark .nav-tab-active:hover {{
  background: rgba(129, 140, 248, 0.1);
}}

/* 主题切换按钮 */
.theme-toggle {{
  width: 40px;
  height: 40px;
  border-radius: var(--radius-full) !important;
  display: flex !important;
  align-items: center;
  justify-content: center;
  transition: background var(--transition-fast), transform var(--transition-fast);
  font-size: 18px;
}}

.theme-toggle:hover {{
  background: var(--color-bg-elevated) !important;
  transform: rotate(15deg);
}}

.body--dark .theme-toggle:hover {{
  background: var(--color-dark-bg-elevated) !important;
}}


/* ============================================
   卡片系统
   ============================================ */

.app-card {{
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  transition: box-shadow var(--transition-normal), transform var(--transition-normal), border-color var(--transition-normal);
  overflow: hidden;
}}

.app-card:hover {{
  box-shadow: var(--shadow-md);
  border-color: var(--color-border);
}}

.body--dark .app-card {{
  background: var(--color-dark-bg-surface);
  border-color: var(--color-dark-border);
}}

.body--dark .app-card:hover {{
  border-color: var(--color-dark-border-light);
  box-shadow: var(--shadow-dark-md);
}}

/* 统计卡片 */
.stat-card {{
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-lg);
  padding: 24px;
  text-align: center;
  position: relative;
  overflow: hidden;
  transition: transform var(--transition-normal), box-shadow var(--transition-normal);
}}

.stat-card::before {{
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}}

.stat-card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}}

.stat-card-blue::before {{ background: var(--gradient-blue); }}
.stat-card-green::before {{ background: var(--gradient-green); }}
.stat-card-purple::before {{ background: var(--gradient-purple); }}
.stat-card-orange::before {{ background: var(--gradient-orange); }}

.body--dark .stat-card {{
  background: var(--color-dark-bg-surface);
  border-color: var(--color-dark-border);
}}

.body--dark .stat-card:hover {{
  box-shadow: var(--shadow-dark-lg);
}}

.stat-value {{
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1.2;
  transition: color var(--transition-fast);
}}

.stat-label {{
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-secondary);
  margin-top: 6px;
}}

/* 监控账号卡片 */
.account-card {{
  position: relative;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: 16px 16px 16px 20px;
  transition: transform var(--transition-fast), box-shadow var(--transition-fast), border-color var(--transition-fast);
}}

.account-card::before {{
  content: '';
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 12px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  transition: background var(--transition-fast);
}}

.account-card-live::before {{ background: var(--color-success); }}
.account-card-idle::before {{ background: var(--color-text-tertiary); }}
.account-card-error::before {{ background: var(--color-danger); }}

.account-card:hover {{
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
  border-color: var(--color-border);
}}

.body--dark .account-card {{
  background: var(--color-dark-bg-surface);
  border-color: var(--color-dark-border);
}}

.body--dark .account-card:hover {{
  border-color: var(--color-dark-border-light);
  box-shadow: var(--shadow-dark-md);
}}


/* ============================================
   弹幕消息流
   ============================================ */

.danmaku-container {{
  background: #0a0a0f;
  border-radius: var(--radius-lg);
  overflow: hidden;
}}

.danmaku-header {{
  background: rgba(0, 0, 0, 0.6);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(8px);
}}

.danmaku-item {{
  background: rgba(255, 255, 255, 0.03);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  margin: 3px 0;
  border-left: 2px solid transparent;
  transition: background var(--transition-fast);
  animation: fadeSlideIn 0.3s ease-out;
}}

.danmaku-item:hover {{
  background: rgba(255, 255, 255, 0.06);
}}

.danmaku-item-gift {{
  background: rgba(255, 193, 7, 0.06);
  border-left-color: #FFC107;
}}

.danmaku-item-member {{
  background: rgba(76, 175, 80, 0.06);
  border-left-color: #4CAF50;
}}

.danmaku-item-chat {{
  border-left-color: rgba(99, 102, 241, 0.4);
}}


/* ============================================
   按钮增强
   ============================================ */

.btn-primary {{
  background: var(--gradient-blue) !important;
  border: none !important;
  border-radius: var(--radius-full) !important;
  font-weight: 600 !important;
  letter-spacing: 0.01em;
  transition: transform var(--transition-fast), box-shadow var(--transition-fast), opacity var(--transition-fast) !important;
}}

.btn-primary:hover {{
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35);
}}

.btn-primary:active {{
  transform: translateY(0);
}}

.btn-secondary {{
  border-radius: var(--radius-full) !important;
  font-weight: 500 !important;
  transition: background var(--transition-fast), border-color var(--transition-fast) !important;
}}

.btn-ghost {{
  border-radius: var(--radius-full) !important;
  font-weight: 500 !important;
  transition: background var(--transition-fast) !important;
}}

.btn-ghost:hover {{
  background: var(--color-bg-elevated) !important;
}}

.body--dark .btn-ghost:hover {{
  background: var(--color-dark-bg-elevated) !important;
}}


/* ============================================
   输入框增强
   ============================================ */

.app-input .q-field__control {{
  border-radius: var(--radius-md) !important;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}}

.app-input .q-field__control:hover {{
  border-color: var(--color-primary) !important;
}}

.app-input .q-field--focused .q-field__control {{
  border-color: var(--color-primary) !important;
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
}}


/* ============================================
   标签/Tag
   ============================================ */

.app-tag {{
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 600;
  border-radius: var(--radius-full);
  line-height: 20px;
}}

.app-tag-success {{
  background: rgba(34, 197, 94, 0.1);
  color: #16a34a;
}}

.app-tag-danger {{
  background: rgba(239, 68, 68, 0.1);
  color: #dc2626;
}}

.app-tag-warning {{
  background: rgba(245, 158, 11, 0.1);
  color: #d97706;
}}

.app-tag-info {{
  background: rgba(59, 130, 246, 0.1);
  color: #2563eb;
}}

.app-tag-default {{
  background: var(--color-bg-elevated);
  color: var(--color-text-secondary);
}}

.body--dark .app-tag-default {{
  background: var(--color-dark-bg-elevated);
}}

.app-tag-primary {{
  background: rgba(99, 102, 241, 0.1);
  color: var(--color-primary);
}}

/* 标签 chip（可删除） */
.app-chip {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px 3px 10px;
  font-size: 12px;
  font-weight: 500;
  border-radius: var(--radius-full);
  background: rgba(99, 102, 241, 0.08);
  color: var(--color-primary);
  border: 1px solid rgba(99, 102, 241, 0.15);
  transition: background var(--transition-fast), border-color var(--transition-fast);
}}

.app-chip:hover {{
  background: rgba(99, 102, 241, 0.14);
  border-color: rgba(99, 102, 241, 0.25);
}}

.app-chip-remove {{
  cursor: pointer;
  font-size: 14px;
  opacity: 0.6;
  transition: opacity var(--transition-fast);
  margin-left: 2px;
}}

.app-chip-remove:hover {{
  opacity: 1;
}}


/* ============================================
   表格增强
   ============================================ */

.app-table {{
  border-radius: var(--radius-md) !important;
  overflow: hidden;
}}

.app-table .q-table__top,
.app-table .q-table__bottom {{
  background: var(--color-bg-surface);
}}

.body--dark .app-table .q-table__top,
.body--dark .app-table .q-table__bottom {{
  background: var(--color-dark-bg-base);
}}

.app-table tr:hover {{
  background: rgba(99, 102, 241, 0.03) !important;
}}

.body--dark .app-table tr:hover {{
  background: rgba(129, 140, 248, 0.05) !important;
}}


/* ============================================
   确认弹窗
   ============================================ */

.confirm-dialog .q-card {{
  border-radius: var(--radius-lg) !important;
  padding: 8px;
  min-width: 360px;
}}

.confirm-dialog-title {{
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
}}

.confirm-dialog-body {{
  font-size: 14px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}}


/* ============================================
   通知增强
   ============================================ */

.q-notification {{
  border-radius: var(--radius-md) !important;
  box-shadow: var(--shadow-lg) !important;
  font-family: var(--font-sans) !important;
}}


/* ============================================
   日志区域
   ============================================ */

.log-display {{
  background: var(--color-dark-bg-base) !important;
  color: var(--color-dark-text-secondary) !important;
  border-radius: var(--radius-md);
  font-family: var(--font-mono) !important;
  font-size: 12px !important;
  line-height: 1.7 !important;
  border: 1px solid var(--color-dark-border);
}}


/* ============================================
   分区标题
   ============================================ */

.section-title {{
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}}

.section-title-icon {{
  font-size: 18px;
}}


/* ============================================
   动画关键帧
   ============================================ */

@keyframes fadeSlideIn {{
  from {{
    opacity: 0;
    transform: translateY(6px);
  }}
  to {{
    opacity: 1;
    transform: translateY(0);
  }}
}}

@keyframes slideIn {{
  from {{
    transform: scaleX(0);
  }}
  to {{
    transform: scaleX(1);
  }}
}}

@keyframes fadeIn {{
  from {{ opacity: 0; }}
  to {{ opacity: 1; }}
}}

@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.6; }}
}}

@keyframes countUp {{
  from {{ opacity: 0; transform: translateY(8px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}


/* ============================================
   页面入场动画
   ============================================ */

.page-enter {{
  animation: fadeSlideIn 0.4s ease-out;
}}

.page-enter-delay-1 {{
  animation: fadeSlideIn 0.4s ease-out 0.1s both;
}}

.page-enter-delay-2 {{
  animation: fadeSlideIn 0.4s ease-out 0.2s both;
}}

.page-enter-delay-3 {{
  animation: fadeSlideIn 0.4s ease-out 0.3s both;
}}


/* ============================================
   滚动条美化
   ============================================ */

::-webkit-scrollbar {{
  width: 6px;
  height: 6px;
}}

::-webkit-scrollbar-track {{
  background: transparent;
}}

::-webkit-scrollbar-thumb {{
  background: var(--color-text-tertiary);
  border-radius: 3px;
}}

::-webkit-scrollbar-thumb:hover {{
  background: var(--color-text-secondary);
}}

.body--dark ::-webkit-scrollbar-thumb {{
  background: var(--color-dark-border-light);
}}


/* ============================================
   状态圆点
   ============================================ */

.status-dot {{
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}}

.status-dot-live {{
  background: var(--color-success);
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
  animation: pulse 2s infinite;
}}

.status-dot-idle {{
  background: var(--color-text-tertiary);
}}

.status-dot-error {{
  background: var(--color-danger);
}}

.status-dot-detecting {{
  background: var(--color-warning);
  animation: pulse 1.5s infinite;
}}


/* ============================================
   布局工具
   ============================================ */

.gap-sm {{ gap: 8px; }}
.gap-md {{ gap: 16px; }}
.gap-lg {{ gap: 24px; }}

.transition-all {{
  transition: all var(--transition-normal);
}}


/* ============================================
   服务管理页面
   ============================================ */

.service-card {{
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-lg);
  min-height: 200px;
}}

.body--dark .service-card {{
  background: var(--color-dark-bg-surface);
  border-color: var(--color-dark-border);
}}

.restart-btn {{
  background: var(--gradient-blue) !important;
  color: white !important;
  border: none !important;
}}

.check-btn {{
  background: var(--color-bg-elevated) !important;
  color: var(--color-text-secondary) !important;
  border: 1px solid var(--color-border-light) !important;
}}

.body--dark .check-btn {{
  background: var(--color-dark-bg-elevated) !important;
  color: var(--color-dark-text-secondary) !important;
  border-color: var(--color-dark-border) !important;
}}
"""

    return css


# 缓存已生成的 CSS
_css_cache: str | None = None


def get_global_css() -> str:
    """获取全局 CSS 字符串（带缓存）"""
    global _css_cache
    if _css_cache is None:
        _css_cache = _build_css()
    return _css_cache


# 标记是否已在当前页面注入
_injected_flag = "_ppp_styles_injected"


def inject_global_styles():
    """在页面上下文中注入全局样式（每个页面调用一次）"""
    from nicegui import app as ng_app
    # 使用 app.storage.general 标记防止重复注入
    css = get_global_css()
    ui.add_head_html(f"<style>{css}</style>")

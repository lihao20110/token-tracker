import os

_STRINGS = {
    "zh": {
        # --- cli.py ---
        "unknown_sort_field": "未知排序字段: {key}，可用: {valid}",
        "no_token_data": "暂无 token 使用数据",
        "no_data": "暂无使用记录，开始使用 Claude Code 或 Codex 后数据会自动出现在这里。",
        "detected": "检测到: {agents}",
        "unknown_cmd": "未知命令: {cmd}",
        "agent_filter_conflict": "--claude 与 --codex 互斥，只能指定其中一个",
        "agent_not_detected": "未检测到 {flag} 的使用数据，请先在该 agent 中使用后再查询",
        "sessions_limit_invalid": "会话条数必须是正整数，收到: {value}",
        "available_cmds": "可用命令: status, daily, weekly, monthly, sessions, sidebar, theme, setup, unsetup, --version",
        # --- sidebar（cli.py / ui/sidebar.py）---
        "sidebar_empty": "窗口期内没有活跃会话",
        "sidebar_waiting_prompt": "等待当前会话的第一条提示词…",
        "sidebar_state_running": "运行中",
        "sidebar_state_attention": "待确认",
        "sidebar_state_waiting": "等输入",
        "sidebar_state_idle": "空闲",
        "sidebar_jump_no_target": "该会话暂无终端定位（其状态栏渲染过一帧后才有）",
        "sidebar_jump_failed": "跳转失败: {err}",
        "sidebar_next": "下一步",
        "sidebar_active_count": "最近活跃会话 {n} 条",
        "sidebar_tz_bj": "北京",
        "sidebar_tz_la": "洛杉矶",
        "sidebar_tz_ldn": "伦敦",
        "sidebar_jump_gone": "目标终端窗格不存在（可能已关闭）",
        "sidebar_update_hint": "Claude Code 版本较旧，无法识别已关闭的会话，建议升级",
        # --- status.py（会话表列名与 tips）---
        "recent_sessions": "最近会话",
        "sessions_tips": "Tips: tt sessions <N> 调数量 · --sort cost|tokens|time|messages · --asc/--desc 改排序",
        "col_time": "时间",
        "col_agent": "Agent",
        "col_project": "项目",
        "col_model": "模型",
        "col_total_tokens": "总Token",
        "col_cost": "等效成本",
        "col_messages": "消息",
        # --- heatmap.py ---（图例 Less / More 不翻译、硬编码英文）
        "daily_peak": "峰值",
        "daily_streak": "连续/最长",
        "active_days": "活跃天数",
        "weekday_grid": "周日,周一,周二,周三,周四,周五,周六",  # 热力图左侧行标签（周日开头）
        "month_short": "1月,2月,3月,4月,5月,6月,7月,8月,9月,10月,11月,12月",  # 热力图月份表头
        "unit_day": "天",   # 连续天数单位（daily streak）
        # --- theme (cli.py) ---
        "theme_current": "当前主题: {name}{src}",
        "theme_src_env": "（来自环境变量 TT_THEME）",
        "theme_src_config": "（来自配置文件）",
        "theme_src_auto": "（自动选择）",
        "theme_unknown": "未知主题: {name}",
        "theme_options": "可选主题: {names}",
        "theme_set_ok": "已切换到主题 {name}",
        "theme_set_statusline": "状态栏已重新生成，重启会话后生效",
        "theme_env_override": "注意：环境变量 TT_THEME 已设置，会覆盖此次切换",
        "theme_usage": "用法: tt theme [show | list | set <主题名> | preview <主题名>]",
        # --- wizard (wizard.py) ---
        "wizard_pick_theme": "选择配色主题",
        "wizard_q_cc_statusline": "接管 Claude Code 状态栏",
        "wizard_q_codex_statusline": "启用 Codex 伪 statusline",
        "theme_recommended": "（推荐）",
        "wizard_done": "配置完成",
        "wizard_summary_lang": "语言",
        "wizard_summary_theme": "主题",
        "wizard_summary_cc_statusline": "Claude Code 状态栏",
        "wizard_summary_statusline": "Codex 状态栏",
        "wizard_restart": "重启 Claude Code / Codex 生效",
        "wizard_reconfig": "更改配置可再次运行 tt setup",
        "wizard_view_reports": "运行 tt status / daily / weekly / monthly 可直接查看报表",
        "wizard_signoff": "祝你使用愉快",
        # --- hooks.py ---
        "no_agent_install": "未检测到 Claude Code 或 Codex，请先安装其中之一",
        "auto_setup_hint": "非交互环境，已按推荐默认（语言跟随系统 / 主题 mocha / 不替换已有自定义 statusLine）配置\n如需自定义请在终端运行 tt setup",
        "first_setup": "首次使用，正在配置状态栏...",
        "cc_not_found": "未检测到 Claude Code，跳过",
        "codex_not_found": "未检测到 Codex，跳过",
        "sl_backup_replace": "检测到已有 statusLine，备份后替换",
        "cc_statusline_skipped": "已跳过 Claude Code statusLine 接管（保留你现有配置）",
        "cc_settings_corrupt": "无法解析 {path}（JSON 损坏），已跳过 Claude Code 配置；请修复后重跑 tt setup",
        "cc_settings_corrupt_unsetup": "无法解析 {path}（JSON 损坏），statusLine 未改动；请手动检查该文件",
        "cc_backup_corrupt": "备份文件 {path} 无法解析（已保留供手动恢复），statusLine 将直接移除",
        "cc_configured": "Claude Code statusLine 已配置",
        "restart_cc": "重启 Claude Code 后生效",
        "codex_configured": "Codex 已配置",
        "codex_statusline_hint": "已启用伪 statusline（每次回答后追加一行 5h/7d/Ctx）",
        "restart_codex": "重启 Codex 后生效",
        "sidebar_skill_installed": "已安装 Codex $tt-sidebar Skill：{path}",
        "sidebar_skill_conflict": "{path} 已存在非 Token Tracker 管理的同名 Skill，已保留且未覆盖",
        "sidebar_hook_installed": "已安装 $tt-sidebar 提示词同步 Hook",
        "sidebar_hook_trust": "在 Codex 中运行 /hooks 信任该 Hook；新 Skill 未出现时重启 Codex",
        "sidebar_hook_removed": "已移除 $tt-sidebar 提示词同步 Hook",
        "codex_hooks_corrupt": "无法解析 {path}（JSON 损坏），已跳过 $tt-sidebar Hook；请修复后重跑 tt setup",
        "codex_hooks_corrupt_unsetup": "无法解析 {path}（JSON 损坏），$tt-sidebar Hook 未改动；请手动检查该文件",
        "no_agent_detected": "未检测到 Claude Code 或 Codex",
        "deleted_file": "已删除: {path}",
        "cc_restored": "Claude Code statusLine 已恢复原配置",
        "cc_removed": "Claude Code statusLine 已移除",
        "deleted_cache": "已删除缓存: {path}",
        "codex_restored": "Codex status_line 已恢复原配置（老用户备份）",
    },
    "en": {
        # --- cli.py ---
        "unknown_sort_field": "Unknown sort field: {key}, available: {valid}",
        "no_token_data": "No token usage data",
        "no_data": "No usage records yet. Start using Claude Code or Codex and your data will show up here.",
        "detected": "Detected: {agents}",
        "unknown_cmd": "Unknown command: {cmd}",
        "agent_filter_conflict": "--claude and --codex are mutually exclusive; please pick one",
        "agent_not_detected": "No usage data for {flag}; use it in that agent first, then query again",
        "sessions_limit_invalid": "Session count must be a positive integer, got: {value}",
        "available_cmds": "Available commands: status, daily, weekly, monthly, sessions, sidebar, theme, setup, unsetup, --version",
        # --- sidebar（cli.py / ui/sidebar.py）---
        "sidebar_empty": "No active sessions in the window",
        "sidebar_waiting_prompt": "Waiting for the first prompt in this session…",
        "sidebar_state_running": "running",
        "sidebar_state_attention": "needs you",
        "sidebar_state_waiting": "awaiting input",
        "sidebar_state_idle": "idle",
        "sidebar_jump_no_target": "No terminal mapping for this session yet (appears after its statusline renders a frame)",
        "sidebar_jump_failed": "Jump failed: {err}",
        "sidebar_next": "Next",
        "sidebar_active_count": "{n} active sessions",
        "sidebar_tz_bj": "Beijing",
        "sidebar_tz_la": "LA",
        "sidebar_tz_ldn": "London",
        "sidebar_jump_gone": "Target terminal pane no longer exists (probably closed)",
        "sidebar_update_hint": "Claude Code is too old to detect closed sessions; consider updating",
        # --- status.py（会话表列名与 tips）---
        "recent_sessions": "Recent Sessions",
        "sessions_tips": "Tips: tt sessions <N> for count · --sort cost|tokens|time|messages · --asc/--desc to sort",
        "col_time": "Time",
        "col_agent": "Agent",
        "col_project": "Project",
        "col_model": "Model",
        "col_total_tokens": "Tokens",
        "col_cost": "Cost",
        "col_messages": "Msgs",
        # --- heatmap.py ---（图例 Less / More 不翻译、硬编码英文）
        "daily_peak": "Peak",
        "daily_streak": "Current/Longest Streak",
        "active_days": "Active Days",
        "weekday_grid": "Sun,Mon,Tue,Wed,Thu,Fri,Sat",  # 热力图左侧行标签（Sun 开头）
        "month_short": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",  # 热力图月份表头
        "unit_day": "d",   # 连续天数单位（daily streak）
        # --- theme (cli.py) ---
        "theme_current": "Current theme: {name}{src}",
        "theme_src_env": " (from env TT_THEME)",
        "theme_src_config": " (from config file)",
        "theme_src_auto": " (auto-selected)",
        "theme_unknown": "Unknown theme: {name}",
        "theme_options": "Available themes: {names}",
        "theme_set_ok": "Switched to theme {name}",
        "theme_set_statusline": "Status line regenerated, restart session to take effect",
        "theme_env_override": "Note: env TT_THEME is set and overrides this change",
        "theme_usage": "Usage: tt theme [show | list | set <name> | preview <name>]",
        # --- wizard (wizard.py) ---
        "wizard_pick_theme": "Pick a theme",
        "wizard_q_cc_statusline": "Take over Claude Code status line",
        "wizard_q_codex_statusline": "Enable Codex faux statusline",
        "theme_recommended": "(recommended)",
        "wizard_done": "Setup complete",
        "wizard_summary_lang": "Language",
        "wizard_summary_theme": "Theme",
        "wizard_summary_cc_statusline": "Claude Code statusline",
        "wizard_summary_statusline": "Codex statusline",
        "wizard_restart": "Restart Claude Code / Codex to take effect",
        "wizard_reconfig": "Run tt setup again to change settings",
        "wizard_view_reports": "Run tt status / daily / weekly / monthly to view reports",
        "wizard_signoff": "Enjoy!",
        # --- hooks.py ---
        "no_agent_install": "Claude Code or Codex not detected, please install one first",
        "auto_setup_hint": "Non-interactive env — configured with recommended defaults (language follows system / theme mocha / existing custom statusLine kept)\nRun tt setup in a terminal to customize",
        "first_setup": "First run, configuring status bar...",
        "cc_not_found": "Claude Code not detected, skipping",
        "codex_not_found": "Codex not detected, skipping",
        "sl_backup_replace": "Existing statusLine detected, backing up and replacing",
        "cc_statusline_skipped": "Skipped Claude Code statusLine takeover (your existing config kept)",
        "cc_settings_corrupt": "Cannot parse {path} (invalid JSON); skipped Claude Code setup — fix it and re-run tt setup",
        "cc_settings_corrupt_unsetup": "Cannot parse {path} (invalid JSON); statusLine untouched — please check the file",
        "cc_backup_corrupt": "Backup {path} is unreadable (kept for manual recovery); statusLine will be removed",
        "cc_configured": "Claude Code statusLine configured",
        "restart_cc": "Restart Claude Code to take effect",
        "codex_configured": "Codex configured",
        "codex_statusline_hint": "Faux statusline enabled (appends 5h/7d/Ctx line after each turn)",
        "restart_codex": "Restart Codex to take effect",
        "sidebar_skill_installed": "Codex $tt-sidebar Skill installed: {path}",
        "sidebar_skill_conflict": "A non-Token Tracker skill already exists at {path}; kept without changes",
        "sidebar_hook_installed": "$tt-sidebar prompt sync hook installed",
        "sidebar_hook_trust": "Run /hooks in Codex to trust this hook; restart Codex if the new Skill is not visible",
        "sidebar_hook_removed": "$tt-sidebar prompt sync hook removed",
        "codex_hooks_corrupt": "Cannot parse {path} (invalid JSON); skipped the $tt-sidebar hook — fix it and re-run tt setup",
        "codex_hooks_corrupt_unsetup": "Cannot parse {path} (invalid JSON); $tt-sidebar hook untouched — check it manually",
        "no_agent_detected": "Claude Code or Codex not detected",
        "deleted_file": "Deleted: {path}",
        "cc_restored": "Claude Code statusLine restored",
        "cc_removed": "Claude Code statusLine removed",
        "deleted_cache": "Deleted cache: {path}",
        "codex_restored": "Codex status_line restored (legacy backup)",
    },
}


def _detect_system_lang() -> str:
    """检测系统语言设置，**绕过 CLI 的 `LANG` 环境变量**（主人 CLI 多设 en，但系统可能是中文，
    同时区那套：读系统设置而非环境变量）。macOS 读 `defaults -g AppleLanguages` 首选语言、
    Windows 读用户界面语言（`GetUserDefaultUILanguage`）；其它平台 / 失败回退 `LANG` 等环境变量。
    zh 开头 → 中文，否则英文。"""
    import sys
    if sys.platform == "darwin":
        try:
            import re
            import subprocess
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            m = re.search(r'"([^"]+)"', out)  # 取数组首项，如 "zh-Hans-US"
            if m:
                return "zh" if m.group(1).lower().startswith("zh") else "en"
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            # GetUserDefaultUILanguage 返回 LANGID；主语言 ID = 低 10 位，0x04 = 中文（简/繁均是）
            if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04:
                return "zh"
            return "en"
        except Exception:
            pass
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        if os.environ.get(var, "").lower().startswith("zh"):
            return "zh"
    return "en"


def _detect_lang() -> str:
    # 1. 用户配置文件优先（wizard 选过）。延迟 import 避免顶层循环。
    try:
        from . import config
        saved = config.resolve_lang()
        if saved:
            return saved
    except Exception:
        pass
    # 2. TT_LANG 显式覆盖
    env_lang = os.environ.get("TT_LANG", "").lower()
    if env_lang:
        return "zh" if env_lang.startswith("zh") else "en"
    # 3. 系统语言设置（绕过 CLI LANG，见 _detect_system_lang）
    return _detect_system_lang()


LANG = _detect_lang()
_CURRENT = _STRINGS.get(LANG, _STRINGS["en"])


def set_lang(lang: str) -> None:
    """运行时切换语言（wizard 选完即时生效，后续 t() 调用返回新语言文案）。"""
    global LANG, _CURRENT
    LANG = lang if lang in _STRINGS else "en"
    _CURRENT = _STRINGS[LANG]


def t(msg_key: str, **kwargs) -> str:
    # 形参不能叫 key：unknown_sort_field 等字符串带 {key} 占位符，会与 t(..., key=...) 撞名
    s = _CURRENT.get(msg_key, msg_key)
    return s.format(**kwargs) if kwargs else s

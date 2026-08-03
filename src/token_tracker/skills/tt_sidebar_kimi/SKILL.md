---
name: tt-sidebar
description: Open a live Token Tracker sidebar for only the current Kimi Code session in a right-side terminal split at one-third width. Use when the user explicitly invokes /skill:tt-sidebar or asks to monitor the current session beside its terminal pane.
type: prompt
disableModelInvocation: true
---

# TT Sidebar

<!-- token-tracker-managed -->

Run the installed launcher exactly once:

```bash
__TT_SIDEBAR_COMMAND__
```

The launcher splits tmux, iTerm2, or Ghostty (macOS, ≥ 1.3.0) to open the sidebar pane, so it needs permission to control the terminal. If the runtime asks for approval, approve this exact command only; never approve Python generally.

Return the launcher's output directly. Do not run regular `tt sidebar`, inject a command into a transient shell, or open another sidebar in the current pane. If the launcher fails, report only its concise stage error.

The launcher must keep the source pane focused and open a right-side pane at one-third width. The split view is current-session-only and is intentionally independent from the regular all-session `tt sidebar` layout.

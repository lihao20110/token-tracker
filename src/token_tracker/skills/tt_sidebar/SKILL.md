---
name: tt-sidebar
description: Open a live Token Tracker sidebar for only the current Codex session in a right-side terminal split at one-third width. Use when the user explicitly invokes $tt-sidebar or asks to monitor the current Codex session beside its terminal pane.
---

# TT Sidebar

<!-- token-tracker-managed -->

Run the installed launcher exactly once:

```bash
__TT_SIDEBAR_COMMAND__
```

Return the launcher's output directly. Do not run regular `tt sidebar`, inject a command into a transient shell, or open another sidebar in the current pane. If the launcher fails, report only its concise stage error.

The launcher must keep the source pane focused and open a right-side pane at one-third width. The split view is current-session-only and is intentionally independent from the regular all-session `tt sidebar` layout.

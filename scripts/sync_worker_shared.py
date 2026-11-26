#!/usr/bin/env python3
"""Regenerate the shared blocks in workerlocal.py from modules/agent_shared.py.

The worker runs as ONE self-contained file on a Kaggle kernel and cannot import
backend modules, so the shared definitions (rules, tool schemas, context-note
builder, tool-loop policy helpers) are copied into workerlocal.py as literals
between SHARED-BEGIN / SHARED-END markers. Edit the canonical definitions in
modules/agent_shared.py and re-run this script — never hand-edit the generated
block. Re-running must produce no diff; that is the drift check.

Usage:  python3 scripts/sync_worker_shared.py [--check]
  --check: exit 1 if the worker is out of sync (for CI), without writing.
"""

import argparse
import inspect
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from modules import agent_shared as S  # noqa: E402

WORKER_PATH = os.path.join(REPO, "workerlocal.py")

# Worker's rule order in build_system_prompt (same rules as the cloud, the
# order the worker's prompt was already built in).
WORKER_RULE_ORDER = ["topic", "how_you_talk", "greet_first", "typos", "know_user", "rich_ui", "anti_loop", "deliver_files", "manage_files", "work_like_pro"]

# Policy helpers copied verbatim (source) from agent_shared — they only use
# json/re/time plus the constants below, so they run standalone in the worker.
HELPER_NAMES = [
    "_short_preview",
    "tool_call_signature",
    "is_error_result",
    "error_signature",
    "invalid_args_message",
    "repeat_stop_message",
    "repeat_warning",
    "stuck_final_message",
    "empty_retry_message",
    "clip_tool_result",
    "tool_detail",
    "search_image_urls",
    "fallback_summary",
    "build_context_note",
]

BEGIN = "# SHARED-BEGIN"
END = "# SHARED-END"


def _render_block():
    parts = []
    parts.append(BEGIN)
    parts.append("# Generated from modules/agent_shared.py by scripts/sync_worker_shared.py.")
    parts.append("# DO NOT EDIT BY HAND — edit agent_shared.py and re-run the script.")
    parts.append("# The worker is pushed to Kaggle as one file and cannot import backend modules,")
    parts.append("# so the shared definitions live here as generated literals.")
    parts.append("")
    parts.append(f"TOOL_RESULT_LIMIT = {S.TOOL_RESULT_LIMIT}")
    parts.append(f"TOOL_CALL_SIG_LIMIT = {S.TOOL_CALL_SIG_LIMIT}")
    parts.append(f"TOOL_REPEAT_PREVIEW_CHARS = {S.TOOL_REPEAT_PREVIEW_CHARS}")
    parts.append(f"TOOL_ERROR_SIG_CHARS = {S.TOOL_ERROR_SIG_CHARS}")
    parts.append("")
    rules_id = {name: S.RULES[name]["id"] for name in S.RULE_ORDER}
    parts.append(f"SHARED_RULES_ID = {rules_id!r}")
    parts.append(f"SHARED_RULE_ORDER_ID = {WORKER_RULE_ORDER!r}")
    parts.append("")
    parts.append("def _shared_rules_text():")
    parts.append('    """The shared rules as one prompt block, worker order, single-newline')
    parts.append('    separator — exactly how the worker prompt was already built."""')
    parts.append('    return "\\n".join(SHARED_RULES_ID[name] for name in SHARED_RULE_ORDER_ID)')
    parts.append("")
    parts.append(f"TOOLS = {S.tools_for('id')!r}")
    parts.append("")
    for name in HELPER_NAMES:
        src = inspect.getsource(getattr(S, name)).rstrip("\n")
        parts.append(src)
        parts.append("")
    parts.append(END)
    return "\n".join(parts).rstrip("\n")


def _replace_region(text, block):
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        return text[:start] + block + text[end:]
    # First run: no markers yet — replace the old _DELIVER_SYSTEM_HINT + TOOLS span.
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("_DELIVER_SYSTEM_HINT = ("))
    tools_start = next(i for i, l in enumerate(lines) if l.startswith("TOOLS = ["))
    end = next(i for i in range(tools_start, len(lines)) if lines[i] == "]")
    new_lines = lines[:start] + block.split("\n") + lines[end + 1:]
    return "\n".join(new_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    block = _render_block()
    current = open(WORKER_PATH).read()
    updated = _replace_region(current, block)
    if updated == current:
        print("workerlocal.py already in sync.")
        return 0
    if args.check:
        print("workerlocal.py is OUT OF SYNC with modules/agent_shared.py — run scripts/sync_worker_shared.py.")
        return 1
    open(WORKER_PATH, "w").write(updated)
    print("workerlocal.py regenerated from modules/agent_shared.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

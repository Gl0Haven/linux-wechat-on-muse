#!/usr/bin/env python3
"""Brave Search 封装（2026-10-06）。

通过 workspace skill 调用（connector custom.brave-search）。
Brave 额度用尽时不许回落到 browser.search。
"""
import json
import subprocess
import os

SKILL_CLI = os.path.expanduser(
    "~/workspace/skills/brave-search/bin/brave_search.py")


def brave_search(query, count=5):
    """调用 Brave Search，返回 [{"title","url","description"}] 或 None。"""
    try:
        out = subprocess.run(
            ["python3", SKILL_CLI, query],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        print("[brave] 调用失败: %s" % e, flush=True)
        return None
    if out.returncode != 0:
        print("[brave] CLI 错误: %s" % out.stderr[:200], flush=True)
        return None
    try:
        data = json.loads(out.stdout)
    except Exception:
        print("[brave] 解析失败", flush=True)
        return None
    if isinstance(data, dict) and "error" in data:
        print("[brave] API 错误: %s" % data["error"][:200], flush=True)
        return None
    results = data[:count] if isinstance(data, list) else []
    print("[brave] 搜索 '%s' 返回 %d 条" % (query, len(results)),
          flush=True)
    return results


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if not q:
        print("用法: brave_search.py <关键词>")
        sys.exit(1)
    r = brave_search(q)
    if r:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("搜索失败")

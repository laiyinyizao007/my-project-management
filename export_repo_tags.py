#!/usr/bin/env python3
"""把工作区索引里的标签导出成仓库内快照 repo-tags.json。

为什么需要快照：
    自动立项的归属判定靠标签，但标签住在工作区索引里（~/Projects/projects-index.json），
    而 GitHub Actions 的 runner 上根本没有那个目录。
    所以标签要随仓库走一份 —— 快照就是这份「随仓库走的标签」。

刷新时机：
    工作区索引重建之后（本机跑）：
        python3 export_repo_tags.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_registry import WORKSPACE_INDEX, load_workspace_tags  # noqa: E402

OUT = Path(__file__).resolve().parent / "repo-tags.json"


def main():
    index = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKSPACE_INDEX
    if not index.is_file():
        print(f"❌ 索引不存在：{index}", file=sys.stderr)
        return 2
    tags = load_workspace_tags(index)
    out = {name: sorted(t) for name, t in sorted(tags.items()) if t}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ 已导出 {len(out)} 个仓的标签 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

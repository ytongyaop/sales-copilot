"""首响超时自动回收：扫一遍线索，把超时还挂在某人名下的线索放回公海。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/scan_reclaim.py

边界：
1. **不联网、不调模型**：只用模板阈值与业务库；
2. **没有定时器**：这是显式一次扫描，想定期跑就自己交给系统调度（本进程跑完即退出）；
3. **幂等**：已是公海的线索不再回收，重复跑不会多写归属事件；
4. 库路径跟随环境变量 `SALESCOPILOT_DB`（与接口同一份配置）。

输出：有回收时逐条打印 `RECLAIM <lead_id> <from_owner> <at>`，没有回收只打印 `NO_RECLAIM`。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # 脚本可能脱离 pytest 直接跑，先把 src 放进 import 路径。
    sys.path.insert(0, str(SRC_DIR))

from salescopilot.entrypoints.bootstrap import build_reclaim_service


def main() -> int:
    reclaimed = build_reclaim_service().scan()
    if not reclaimed:
        print("NO_RECLAIM")
        return 0

    for item in reclaimed:
        print(f"RECLAIM {item['lead_id']} {item['from_owner']} {item['at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""造一份可用于演示的演示数据（只写「当前配置的数据库」）。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/seed_demo.py

边界：

1. 写进哪个库由 `SALESCOPILOT_DB` 决定（缺省 `data/app.sqlite3`），与后端跑的是同一份；
2. **幂等**：库里只要已有线索，就一条也不造，打印 `SEED_SKIP existing=<n>` 并退出 0，
   演示前反复跑不会把数据翻倍；
3. 业务路径不乱走：线索用 `build_lead_service()`（真实校验）、阶段推进用
   `build_flow_service()`（真实必填校验）、跟进用 `build_followup_service()`、
   事实用 `build_ingest_service(live=False)`（人工补录，不调模型、不需要 key）；
4. 以下三处属于**演示数据构造**，不是业务逻辑，各自在原处也注明了原因：
   - `created_at` 往回拨：演示需要「2 小时未首联」「约定时间已过 2 天」这类真实时间差，
     而接口不提供改创建时间的入口；
   - `fields_json` 预填必填字段：v1 没有「填字段」的服务入口，而推进校验要看这些字段；
   - 「缺下次跟进时间」的那条跟进直接经仓储写入：`FollowupService.record` 会拒收空的
     下次时间（这是对的），但演示需要这条历史数据存在，好让灰提醒出现在屏幕上。
5. 演示数据全是虚构人员与占位号码，**不含真实机构名**，也不含「保过」一类禁用词。
"""

import json
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # 脚本可能脱离 pytest 直接跑，先把 src 放进 import 路径（pytest 由 pyproject 配好了）。
    sys.path.insert(0, str(SRC_DIR))

from salescopilot.adapters.db.sqlite import (
    SqliteFollowupRepository,
    default_db_path,
)
from salescopilot.domain.models import Followup
from salescopilot.entrypoints.bootstrap import (
    build_flow_service,
    build_followup_service,
    build_ingest_service,
    build_lead_service,
)

# 整个脚本只取一次时钟：四条线索的「几小时前 / 几天前」互相对得上，不会各自漂移。
NOW = datetime.now(UTC)


def main() -> int:
    db_path = default_db_path()

    existing = len(build_lead_service().list())
    if existing > 0:
        print(f"SEED_SKIP existing={existing}")
        return 0

    lead_service = build_lead_service()
    flow_service = build_flow_service()
    followup_service = build_followup_service()
    # 只需要人工补录（不提炼），所以显式用假模型那套：没有 key 也能跑。
    ingest_service = build_ingest_service(live=False)

    # ① 张校长：入库阶段、2 小时前创建 → 首响超时（红）
    zhang = _create(
        lead_service, name="张校长", contact="13800001111", source="referral", owner="李娜"
    )
    _patch_lead(db_path, zhang.id, created_at=_iso(NOW - timedelta(hours=2)))

    # ② 王主任：无归属人（公海）、3 天前创建、停在首次联系 → 不出首响提醒
    wang = _create(lead_service, name="王主任", contact="13900002222", source="event", owner=None)
    _patch_lead(db_path, wang.id, created_at=_iso(NOW - timedelta(days=3)))
    _advance(flow_service, wang.id, ("first_contact",))

    # ③ 李老师：需求确认阶段、有一条「约定时间已过 2 天」的跟进 → 反馈窗口（黄）
    li = _create(lead_service, name="李老师", contact="13700003333", source="inbound", owner="王强")
    _patch_lead(
        db_path,
        li.id,
        created_at=_iso(NOW - timedelta(days=5)),
        fields={"decision_maker": "家长本人", "need": "成人在职考证课程，想尽快拿证"},
    )
    _advance(flow_service, li.id, ("first_contact", "qualify"))

    # ④ 陈校长：已成交（终止阶段）→ 不出任何提醒
    chen = _create(
        lead_service, name="陈校长", contact="13600004444", source="referral", owner="王强"
    )
    _patch_lead(
        db_path,
        chen.id,
        created_at=_iso(NOW - timedelta(days=6)),
        fields={
            "decision_maker": "校长本人",
            "need": "机构老师的技能提升课程",
            "appointment_at": (NOW - timedelta(days=3)).date().isoformat(),
            "amount": "6800",
            "conclusion": "已成交",
        },
    )
    _advance(
        flow_service,
        chen.id,
        ("first_contact", "qualify", "appointment", "proposal", "close"),
    )

    # 跟进 1：李老师 —— 下次跟进时间已过 2 天 → 黄提醒（三要素齐全，走正规 record）
    ok, issues, _ = followup_service.record(
        li.id,
        conclusion="家长关心价格，说下周和家里商量后再定",
        objection="price",
        next_followup_at=(NOW - timedelta(days=2)).date().isoformat(),
    )
    _require(ok, issues, "李老师跟进")

    # 跟进 2：王主任 —— 缺下次跟进时间 → 灰提醒（经仓储写入，原因见文件头第 4 条）
    SqliteFollowupRepository(db_path).add(
        Followup(
            id=str(uuid.uuid4()),
            lead_id=wang.id,
            conclusion="前一位同事跟过一次，客户在比价",
            objection="price",
            next_followup_at="",
            at=_iso(NOW),
        )
    )

    # 事实 1：李老师 objection=price，人工录入并锁定（source=manual / locked=True）
    fact, issues = ingest_service.set_manual(li.id, "objection", "price")
    _require(fact is not None, issues, "李老师事实")

    leads = lead_service.list()
    followups = sum(len(followup_service.list_for(lead.id)) for lead in leads)
    facts = sum(len(ingest_service.list_facts(lead.id)[0]) for lead in leads)

    print(f"SEED_OK leads={len(leads)} followups={followups} facts={facts}")
    print("下一步：")
    print(
        "  $env:PYTHONPATH='src'; .venv\\Scripts\\python.exe -m uvicorn "
        "salescopilot.entrypoints.api.app:app --port 8000"
    )
    print("  另开一个终端：cd web; npm install; npm run dev  → http://localhost:5173")
    print(
        "  要演示「生成话术」：先建一次索引 .venv\\Scripts\\python.exe scripts\\build_index.py --fake"
    )
    return 0


def _create(service, *, name: str, contact: str, source: str, owner: str | None):
    """新建一条线索；被校验拦下时直接退出（演示数据不该带着非法字段进库）。"""
    result = service.create(name=name, contact=contact, source=source, owner=owner)
    if isinstance(result, list):
        raise SystemExit(f"[seed] 新建线索「{name}」被校验拦下：{_issues_text(result)}")
    return result


def _advance(service, lead_id: str, stages: tuple[str, ...]) -> None:
    """按顺序推进阶段；每一跳都过 `FlowService` 的必填校验（缺字段就退出）。"""
    for target in stages:
        result = service.advance(lead_id, target)
        if not result.ok:
            raise SystemExit(f"[seed] 推进到 {target} 被拦下：{_issues_text(result.issues)}")


def _patch_lead(
    db_path: Path,
    lead_id: str,
    *,
    created_at: str,
    fields: dict[str, str] | None = None,
) -> None:
    """演示数据构造：把 `created_at` 往回拨（可选同时预填 `fields_json`）。

    只做这一件事，且只有本脚本用；业务代码里没有任何地方改 `created_at`。
    """
    fields_json = None if fields is None else json.dumps(fields, ensure_ascii=False)
    with sqlite3.connect(db_path) as conn:
        if fields_json is None:
            conn.execute("UPDATE leads SET created_at = ? WHERE id = ?", (created_at, lead_id))
        else:
            conn.execute(
                "UPDATE leads SET created_at = ?, fields_json = ? WHERE id = ?",
                (created_at, fields_json, lead_id),
            )


def _iso(moment: datetime) -> str:
    """统一写成 UTC 的 ISO8601（秒精度），与业务层时钟的写法一致。"""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _issues_text(issues) -> str:
    return "；".join(f"{issue.location} {issue.message}" for issue in issues)


def _require(ok: bool, issues, label: str) -> None:
    if ok:
        return
    raise SystemExit(f"[seed] {label}失败：{_issues_text(issues)}")


if __name__ == "__main__":
    raise SystemExit(main())

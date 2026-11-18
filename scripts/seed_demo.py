"""造一份可用于 ToB 演示的演示数据（只写「当前配置的数据库」）。

用法（在仓库根执行，无需先设 PYTHONPATH）：

    python scripts/seed_demo.py

边界：

1. 写进哪个库由 `SALESCOPILOT_DB` 决定（缺省 `data/app.sqlite3`），与后端跑的是同一份；
2. **幂等**：库里只要已有线索，就一条也不造，打印 `SEED_SKIP existing=<n>` 并退出 0，
   演示前反复跑不会把数据翻倍；
3. 业务路径不乱走：线索用 `build_lead_service()`（真实校验）、阶段推进用 `build_flow_service()`
   （真实必填校验）、跟进用 `FollowupService`（真实三要素校验）、事实用
   `build_ingest_service(live=False)` 的 `set_manual`（人工补录，不调模型、不需要 key）、
   分级用 `build_grading_service()` 的 `propose` + `decide`（留痕只经服务追加，不直接写库）；
4. 三处属于**演示数据构造**，不是业务逻辑，各自在原处也注明了原因：
   - `created_at` 往回拨：演示要「12 天没动静」「5 小时未首联」这类真实时间差，而接口不提供
     改创建时间的入口；
   - 跟进记录的写入时刻由注入时钟给出：`FollowupService` 允许注入 `clock`（测试用的同一个缝），
     这样「5 天前那次跟进」不必靠改库造出来；
   - 「没约下次」的那条跟进经仓储直写：`record` 会拒收空的下次时间（这是对的），但灰灯
     「没有下次跟进时间」需要这条历史数据存在；
5. 演示数据全是虚构机构与占位号码，**不含真实机构名**，也不含「保过」一类禁用词；
   ToB 的行业叫法只出现在这里与 `templates/tob_enterprise.yaml` / `data/knowledge/tob/*.md`，
   `domain/` 与 `services/` 里一个都没有（`tests/test_no_industry_words.py` 看着）。
6. 打印的 `grades` 是**人工定档次数**（`decide` 的条数）；恒信智能只走一次 `propose`，
   留一行 `needs_info` 用来演示「待补全」，不计入 `grades`。
"""

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
    SqliteLeadRepository,
    default_db_path,
)
from salescopilot.domain.models import Followup
from salescopilot.entrypoints.bootstrap import (
    build_flow_service,
    build_followup_service,
    build_grading_service,
    build_ingest_service,
    build_lead_service,
)
from salescopilot.services.followup import FollowupService

# 五条演示线索全部用主演示模板：换场后默认模板也是它（`domain/models.py` 的 `DEFAULT_TEMPLATE`）。
TEMPLATE = "tob_enterprise"

# 整个脚本只取一次时钟：五条线索的「几小时前 / 几天前」互相对得上，不会各自漂移。
NOW = datetime.now(UTC)
TODAY = NOW.date()


def main() -> int:
    db_path = default_db_path()

    existing = len(build_lead_service().list())
    if existing > 0:
        print(f"SEED_SKIP existing={existing}")
        return 0

    leads = build_lead_service()
    flow = build_flow_service()
    ingest = build_ingest_service(live=False)
    grading = build_grading_service()

    # ① 华远建设集团：A 级、信息卡 14 项全齐、约定 3 天前已过 → 黄灯，今日待办排第一
    huayuan = _create(leads, "华远建设集团", "13800002001", "referral", "李娜")
    _patch_created_at(db_path, huayuan.id, NOW - timedelta(days=6))
    _facts(
        ingest,
        huayuan.id,
        {
            "company": "华远建设集团",
            "industry": "建筑与市政工程施工",
            "size": "约 300 人",
            "need": "项目材料台账与施工进度要在一个系统里对得上",
            "pain_strength": "三个项目同时开工，材料台账对不上，月底结算每次多花两天",
            "our_fit": "材料台账与进度看板的联动，正好对上他们现在的痛点",
            "decision_maker": "集团副总裁（分管生产）",
            "influencer": "信息化部负责人",
            "approval_flow": "集团立项后走三家比价，最后由副总裁签批",
            "budget_range": "80–120 万",
            "procurement": "议价采购，不走公开招标",
            "next_step": "到现场演示材料台账与进度看板的联动",
            "customer_promise": "本周内把三个项目的材料清单发过来",
            "value_provided": "按他们三个项目做的材料损耗对比表",
            "amount": "120万",
            "appointment_at": (TODAY - timedelta(days=3)).isoformat(),
        },
    )
    _fields(
        leads,
        huayuan.id,
        {
            "decision_maker": "集团副总裁（分管生产）",
            "need": "项目材料台账与施工进度要在一个系统里对得上",
            "appointment_at": (TODAY - timedelta(days=3)).isoformat(),
            "amount": "120万",
        },
    )
    _advance(flow, huayuan.id, ("first_contact", "qualify", "appointment"))

    # ② 明远软件科技：B 级、缺「决策流程」与「预算区间」、约定就是今天 → today 待办
    mingyuan = _create(leads, "明远软件科技", "13800002002", "inbound", "李娜")
    _patch_created_at(db_path, mingyuan.id, NOW - timedelta(days=5))
    _facts(
        ingest,
        mingyuan.id,
        {
            "company": "明远软件科技",
            "industry": "工业软件研发与实施",
            "size": "约 90 人",
            "need": "销售过程与交付验收要打通，验收单别再靠邮件来回",
            "pain_strength": "验收单平均 20 多天才签回，回款总是拖半个月",
            "our_fit": "验收单与回款计划绑定的那块，正好对上他们的问题",
            "decision_maker": "总经理",
            "amount": "8万",
            "appointment_at": TODAY.isoformat(),
        },
    )
    _fields(
        leads,
        mingyuan.id,
        {
            "decision_maker": "总经理",
            "need": "销售过程与交付验收要打通，验收单别再靠邮件来回",
            "appointment_at": TODAY.isoformat(),
            "amount": "8万",
        },
    )
    _advance(flow, mingyuan.id, ("first_contact", "qualify"))

    # ③ 宏图建材：C 级、8 天没新增跟进 → 灰灯；那条跟进没写「本次提供的价值」
    hongtu = _create(leads, "宏图建材", "13800002003", "outbound", "王强")
    _patch_created_at(db_path, hongtu.id, NOW - timedelta(days=12))
    _facts(
        ingest,
        hongtu.id,
        {
            "company": "宏图建材",
            "industry": "建材批发与加工",
            "size": "约 40 人",
            "need": "老客户复购提醒与账期管理",
            "decision_maker": "老板本人",
            "amount": "3万",
            "appointment_at": (TODAY + timedelta(days=45)).isoformat(),
        },
    )
    _fields(
        leads,
        hongtu.id,
        {
            "decision_maker": "老板本人",
            "need": "老客户复购提醒与账期管理",
            "appointment_at": (TODAY + timedelta(days=45)).isoformat(),
            "amount": "3万",
        },
    )
    _advance(flow, hongtu.id, ("first_contact",))

    # ④ 恒信智能：四维里只缺「成交金额」→ 判不出档，留一行 needs_info 演示「待补全」
    hengxin = _create(leads, "恒信智能", "13800002004", "event", "李娜")
    _patch_created_at(db_path, hengxin.id, NOW - timedelta(days=1))
    _facts(
        ingest,
        hengxin.id,
        {
            "company": "恒信智能",
            "industry": "智能硬件集成",
            "size": "约 150 人",
            "need": "项目进度与客户对账要能随时查",
            "pain_strength": "对账单每月手工做一次，出错就得整份重做",
            "our_fit": "进度看板与对账报表这块，正好对上他们的工作量",
            "decision_maker": "运营总监",
            "approval_flow": "采购部提需求，总监审批后走年度预算",
            "appointment_at": (TODAY + timedelta(days=2)).isoformat(),
        },
    )
    _fields(
        leads,
        hengxin.id,
        {
            "decision_maker": "运营总监",
            "need": "项目进度与客户对账要能随时查",
            "appointment_at": (TODAY + timedelta(days=2)).isoformat(),
        },
    )
    _advance(flow, hengxin.id, ("first_contact", "qualify"))

    # ⑤ 王主任：没有归属人（公海非空）、5 小时未首联 → 红；用来演示「客户是公司的资产」
    wang = _create(leads, "王主任", "13800002005", "other", None)
    _patch_created_at(db_path, wang.id, NOW - timedelta(hours=5))

    # 跟进 3 条：两条写了「本次提供的价值」，一条留空（显式标记为无价值跟进）
    _followup(
        db_path,
        huayuan.id,
        at=NOW - timedelta(days=4),
        conclusion="对方认可材料台账这块，要求下周到现场看进度看板联动",
        objection="time",
        next_followup_at=(TODAY - timedelta(days=3)).isoformat(),
        value_provided="按他们三个项目做的材料损耗对比表",
    )
    _followup(
        db_path,
        mingyuan.id,
        at=NOW - timedelta(days=1),
        conclusion="总经理认可验收与回款绑定的思路，价格要再算一遍",
        objection="price",
        next_followup_at=TODAY.isoformat(),
        value_provided="同规模客户的上线周期与人员投入清单",
    )
    _followup(
        db_path,
        hongtu.id,
        at=NOW - timedelta(days=5),
        conclusion="老板说先看看，等这批货出了再说，这次没约下次时间",
        objection="time",
        next_followup_at="",
    )

    # 分级：规则先出档（`propose` 留一行 AI 值），人再确认（`decide` 留一行人工值 + 理由）。
    # 三档 A / B / C 各一条，恒信只走 `propose` 留 needs_info。
    _grade(grading, huayuan.id, "A", "四维齐全、预算与决策链都确认过了，按 A 级跟")
    _grade(grading, mingyuan.id, "B", "缺决策流程与预算区间，先按 B 级跟")
    _grade(grading, hongtu.id, "C", "金额小、约定在 45 天后，先按 C 级挂着")
    proposed, issues = grading.propose(hengxin.id)
    _require(proposed is not None, issues, "恒信智能分级提议")

    all_leads = leads.list()
    listed_followups = build_followup_service()
    followups_count = sum(len(listed_followups.list_for(lead.id)) for lead in all_leads)
    facts_count = sum(len(ingest.list_facts(lead.id)[0]) for lead in all_leads)

    print(
        f"SEED_OK leads={len(all_leads)} followups={followups_count} facts={facts_count} grades=3"
    )
    print("提醒颜色与「已过 N 天」按跑脚本那一刻的时间差算，隔天再看数字会变大（颜色通常不变）。")
    print("下一步：")
    print(
        "  $env:PYTHONPATH='src'; .venv\\Scripts\\python.exe -m uvicorn "
        "salescopilot.entrypoints.api.app:app --port 8000"
    )
    print("  另开一个终端：cd web; npm run dev  → http://localhost:5173")
    print(
        "  要演示「生成话术」：先建一次索引 "
        ".venv\\Scripts\\python.exe scripts\\build_index.py --fake --docs-dir data\\knowledge\\tob"
    )
    return 0


def _create(service, name: str, contact: str, source: str, owner: str | None):
    """新建一条线索（默认走主演示模板）；被校验拦下时直接退出。"""
    result = service.create(
        name=name, contact=contact, source=source, owner=owner, template=TEMPLATE
    )
    if isinstance(result, list):
        raise SystemExit(f"[seed] 新建线索「{name}」被校验拦下：{_issues_text(result)}")
    return result


def _facts(service, lead_id: str, values: dict[str, str]) -> None:
    """按顺序人工补录事实（每条都过 `set_manual` 的字段白名单与取值校验）。

    `set_manual` 会把事实标成 `manual` 并锁定：演示里这些值是「人填的」，
    所以明细页的来源徽标是「人工」、原文片段是「（人工录入）」——不假称 AI 提炼过。
    """
    for field, value in values.items():
        fact, issues = service.set_manual(lead_id, field, value)
        _require(fact is not None, issues, f"事实 {field}")


def _fields(service, lead_id: str, patch: dict[str, str]) -> None:
    """补流程要用的线索字段（推进校验看的是这里，不是事实表）。"""
    lead, issues = service.update_fields(lead_id, patch)
    _require(lead is not None, issues, "补线索字段")


def _advance(service, lead_id: str, stages: tuple[str, ...]) -> None:
    """按顺序推进阶段；每一跳都过 `FlowService` 的必填校验（缺字段就退出）。"""
    for target in stages:
        result = service.advance(lead_id, target)
        if not result.ok:
            raise SystemExit(f"[seed] 推进到 {target} 被拦下：{_issues_text(result.issues)}")


def _followup(
    db_path: Path,
    lead_id: str,
    *,
    at: datetime,
    conclusion: str,
    objection: str,
    next_followup_at: str,
    value_provided: str = "",
) -> None:
    """写一条跟进：写入时刻由注入时钟给出（同一套 `FollowupService.record` 校验）。

    `value_provided` 留空就是显式标记「这次没给客户新东西」，周复盘会把它算进无价值跟进。
    `next_followup_at` 留空时走仓储直写（见下）：`record` 会拒收空的下次时间（这是对的），
    但演示需要这条「没约下次」的历史数据存在——灰灯「没有下次跟进时间」就是从它来的。
    """
    if next_followup_at.strip():
        service = FollowupService(
            SqliteLeadRepository(db_path),
            SqliteFollowupRepository(db_path),
            clock=lambda: _iso(at),
        )
        ok, issues, _ = service.record(
            lead_id,
            conclusion=conclusion,
            objection=objection,
            next_followup_at=next_followup_at,
            value_provided=value_provided,
        )
        _require(ok, issues, f"跟进（{conclusion[:8]}…）")
        return

    SqliteFollowupRepository(db_path).add(
        Followup(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            conclusion=conclusion,
            objection=objection,
            next_followup_at="",
            at=_iso(at),
            value_provided=value_provided,
        )
    )


def _grade(service, lead_id: str, value: str, reason: str) -> None:
    """规则出档 + 人工定档：各留一行痕，历史里能看到「AI 值 → 人工值」。"""
    proposed, issues = service.propose(lead_id)
    _require(proposed is not None, issues, "分级提议")
    decided, decide_issues = service.decide(lead_id, value=value, reason=reason, actor="演示数据")
    _require(decided is not None, decide_issues, "人工定档")


def _patch_created_at(db_path: Path, lead_id: str, moment: datetime) -> None:
    """演示数据构造：把 `created_at` 往回拨。

    只做这一件事，且只有本脚本用；业务代码里没有任何地方改 `created_at`
    （接口不提供这个入口，而「几小时前创建」「12 天没动静」只能靠它造）。
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE leads SET created_at = ? WHERE id = ?", (_iso(moment), lead_id))


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

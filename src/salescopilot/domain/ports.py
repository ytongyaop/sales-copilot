"""端口协议：领域声明能力，实现放在 `adapters/`（结构性类型，不做运行时检查）。

`services` 只依赖这里声明的协议，不认识具体数据库；换存储只换适配器与装配。
"""

from typing import Protocol

from salescopilot.domain.knowledge import Chunk
from salescopilot.domain.matching import Candidate
from salescopilot.domain.models import (
    AssignmentEvent,
    Draft,
    DraftResult,
    ExtractionResult,
    ExtractRun,
    Fact,
    Followup,
    GradingRecord,
    Lead,
    PreferenceSample,
    RawItem,
    SortingBatch,
    SortingResult,
    StageEvent,
)


class LeadRepository(Protocol):
    """线索仓储端口：写一条、按时间倒序列全部、按 id 查单条、改阶段、改归属人、整份改字段。"""

    def add(self, lead: Lead) -> None: ...

    def list(self) -> list[Lead]: ...

    def get(self, lead_id: str) -> Lead | None: ...

    def update_stage(self, lead_id: str, stage: str) -> None: ...

    def update_owner(self, lead_id: str, owner: str | None) -> None: ...

    def update_fields(self, lead_id: str, fields: dict[str, str]) -> None: ...


class AssignmentEventRepository(Protocol):
    """归属事件端口：只追加、按线索按时间正序回查。"""

    def add(self, event: AssignmentEvent) -> None: ...

    def list_for(self, lead_id: str) -> list[AssignmentEvent]: ...


class StageEventRepository(Protocol):
    """阶段历史端口：只追加、按线索按时间正序回查。"""

    def add(self, event: StageEvent) -> None: ...

    def list_for(self, lead_id: str) -> list[StageEvent]: ...


class FollowupRepository(Protocol):
    """跟进留痕端口：只追加、按线索按时间正序回查。"""

    def add(self, item: Followup) -> None: ...

    def list_for(self, lead_id: str) -> list[Followup]: ...


class RawItemRepository(Protocol):
    """原始素材端口：追加、按 id 查单条、按（线索, 内容哈希）查重、按线索回查。"""

    def add(self, item: RawItem) -> None: ...

    def get(self, raw_id: str) -> RawItem | None: ...

    def find_by_hash(self, lead_id: str, content_hash: str) -> RawItem | None: ...

    def list_for(self, lead_id: str) -> list[RawItem]: ...


class FactRepository(Protocol):
    """事实端口：追加、按线索回查、按字段取单条（一个字段一条）、整条改。"""

    def add(self, fact: Fact) -> None: ...

    def list_for(self, lead_id: str) -> list[Fact]: ...

    def get_by_field(self, lead_id: str, field: str) -> Fact | None: ...

    def update(self, fact: Fact) -> None: ...


class ExtractRunRepository(Protocol):
    """提炼账本端口：只追加、按原始素材回查。"""

    def add(self, run: ExtractRun) -> None: ...

    def list_for_raw(self, raw_id: str) -> list[ExtractRun]: ...


class FactExtractor(Protocol):
    """提炼器端口：只知道「原文 + 已有事实」，返回候选与这次产出的证据。

    `ExtractionResult.candidates` 的每项形如 `{"field", "value", "source_span", "confidence"}`；
    候选是否合规、是否落库由 `domain/facts.py` 与用例层判定，提炼器不做取舍。
    `token` / `seconds` / `raw_output_hash` 由实现自行测量（假模型给确定性的 0）。
    失败（网络、超时、输出解析不了）一律抛 `domain/errors.py` 的 `ExtractError`。
    """

    def extract(self, *, raw: RawItem, facts: list[Fact]) -> ExtractionResult: ...


class DraftRepository(Protocol):
    """草稿仓储端口：追加、按 id 查单条、按线索回查、改状态（pending → confirmed/discarded）。"""

    def add(self, draft: Draft) -> None: ...

    def get(self, draft_id: str) -> Draft | None: ...

    def list_for(self, lead_id: str) -> list[Draft]: ...

    def update_status(self, draft_id: str, status: str) -> None: ...


class DraftGenerator(Protocol):
    """话术生成器端口：只知道「线索 + 事实 + 资料片段」，返回草稿与用到的资料编号。

    `used_hit_indexes` 是 `hits` 的**0 基下标**；一个都没有就表示「没用资料」，
    用例层会拒绝入库。失败（网络、超时、输出解析不了）抛 `DraftError`。
    """

    def draft(self, *, lead: Lead, facts: list[Fact], hits: list[dict]) -> DraftResult: ...


class SortingBatchRepository(Protocol):
    """分拣单端口：只追加、按 id 查单条、按状态回查（待认领池 = `status='pending_claim'`）。

    **故意没有 update / delete**：分拣单是审计流水，人工处置只在流水上另记两个窄口：
    - `decide`：只写 `status` / `decided_at` / `decided_action`；
    - `attach_lead`：只写 `lead_id`（可选同时写状态），认领与改归属用。
    两者都改不动 `suggestion_json` / `confidence` / `scorer` / `dropped_json`。
    """

    def add(self, batch: SortingBatch) -> None: ...

    def get(self, batch_id: str) -> SortingBatch | None: ...

    def list_by_status(self, status: str) -> list[SortingBatch]: ...

    def decide(self, batch_id: str, *, action: str, at: str) -> None: ...

    def attach_lead(self, batch_id: str, *, lead_id: str, status: str | None = None) -> None: ...


class GradingRepository(Protocol):
    """分级留痕端口：只追加、按线索按时间正序回查（AI 值 / 人工值 / 原因都在这里）。"""

    def add(self, record: GradingRecord) -> None: ...

    def list_for(self, lead_id: str) -> list[GradingRecord]: ...


class PreferenceSampleRepository(Protocol):
    """偏好样本端口：只追加、按时间正序全量回查（周复盘的历史作答）。"""

    def add(self, sample: PreferenceSample) -> None: ...

    def list_all(self) -> list[PreferenceSample]: ...


class SortingJudge(Protocol):
    """分拣判断端口：只知道「原文 + 归属灰区候选」，返回三块建议与产出证据。

    - 要素 / 分级 / 出库三块一次给：`SortingResult.payload` 的键是
      `elements` / `grading` / `next_action`；每块合不合规由 `domain/sorting.py` 判，
      模型说「有出处」不算数，对不上原文的要素一律丢弃。
    - `candidates` 只在归属落在灰区（`band='review'`）且候选 ≤3 时非空：那是「这票货
      可能是哪几家」，供模型参考。精确键命中或判为新客户时**不传**，模型也就不会
      被问归属——调用方可据实现里的计数断言这一点。
    - `review` 只给待认领池里的灰区单据做**一次归属复核**：从候选里挑一个（或明说
      挑不出来）并给理由。它只是建议，用例层不据此改 `lead_id`，等人认领。
    失败（网络、超时、输出解析不了）一律抛 `domain/errors.py` 的 `SortingError`。
    """

    def judge(self, *, content: str, candidates: list[Candidate]) -> SortingResult: ...

    def review(self, *, content: str, candidates: list[Candidate]) -> tuple[str | None, str]: ...


class Embedder(Protocol):
    """向量化端口：一批文本进、一批向量出；模型名由实现自己报（写进索引清单比对）。"""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    """向量库端口：清空、批量写入、按向量取 top-k、数块数。

    `query` 回来的每项形如 `{"text", "source", "score"}`；`score` 越大越相近。
    """

    def reset(self) -> None: ...

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...

    def query(self, vector: list[float], k: int = 4) -> list[dict]: ...

    def count(self) -> int: ...

"""信息卡四要素的纯规则：字段由模板声明，值来自该客户的事实。

三条纪律：
- 字段名、中文名、问题句只从模板 `card.sections` 读，本模块不认识任何行业字段；
- 覆盖度只看「模板声明的字段里填了几个」，分母恒为声明总数（没有字段时是 0.0）；
- 缺项顺序固定 `basic → needs → decision → followup`，每个缺项带一句「下次该问什么」。
"""

from dataclasses import dataclass, field

from salescopilot.domain.models import CARD_SECTIONS, Fact


@dataclass(frozen=True)
class CardField:
    """信息卡上的一个字段：模板给的问题句 + 该客户当前的取值与来源。

    `value` 为空表示还没填；`source` 取事实的来源（`llm` / `manual` / `rule`），
    `span` 是事实的原文片段，`locked` 是人工锁定标志。
    """

    key: str
    label: str
    question: str
    value: str = ""
    source: str = ""
    span: str = ""
    locked: bool = False


@dataclass(frozen=True)
class CardSection:
    """一个信息卡分区：字段列表 + 填了几个 / 一共几个（分母只算模板声明的字段）。"""

    key: str
    label: str
    fields: list[CardField] = field(default_factory=list)
    filled: int = 0
    total: int = 0


@dataclass(frozen=True)
class Card:
    """一张信息卡：四个分区 + 覆盖度（0..1）+ 缺项清单（按分区顺序）。

    `missing` 每项形如 `{"section", "section_label", "field", "label", "question"}`，
    前端直接拿 `question` 当「下次该问什么」。
    """

    sections: list[CardSection] = field(default_factory=list)
    coverage: float = 0.0
    missing: list[dict] = field(default_factory=list)


def build_card(spec: dict, facts: list[Fact]) -> Card:
    """按模板声明组装信息卡：字段值按 `field` 名与该客户的事实对表。

    - 同一个 `field` 有多条事实时取**列表里最后一条**（仓储按时间正序返回，
      即「当前那条」；历史留在 raw + 提炼账本里）；
    - 没声明字段的模板照样返回卡片（覆盖度 0.0），不抛异常；
    - 事实里出现模板没声明的字段：忽略（卡片只展示声明过的字段）。
    """
    current = _current_by_field(facts)
    sections: list[CardSection] = []
    missing: list[dict] = []
    filled_total = 0
    total = 0

    for section_key, section_label, declared in declared_sections(spec):
        fields: list[CardField] = []
        for item in declared:
            fact = current.get(item["key"])
            value = fact.value if fact is not None else ""
            filled = bool(value.strip())
            fields.append(
                CardField(
                    key=item["key"],
                    label=item["label"],
                    question=item["question"],
                    value=value,
                    source=fact.source if fact is not None else "",
                    span=fact.source_span if fact is not None else "",
                    locked=fact.locked if fact is not None else False,
                )
            )
            total += 1
            if filled:
                filled_total += 1
            else:
                missing.append(
                    {
                        "section": section_key,
                        "section_label": section_label,
                        "field": item["key"],
                        "label": item["label"],
                        "question": item["question"],
                    }
                )

        sections.append(
            CardSection(
                key=section_key,
                label=section_label,
                fields=fields,
                filled=sum(1 for item in fields if item.value.strip()),
                total=len(fields),
            )
        )

    coverage = filled_total / total if total else 0.0
    return Card(sections=sections, coverage=coverage, missing=missing)


def declared_sections(spec: dict) -> list[tuple[str, str, list[dict]]]:
    """模板声明了哪些分区与字段：返回 `(分区 key, 分区中文名, 字段规格列表)`。

    顺序以 `CARD_SECTIONS` 为准（basic → needs → decision → followup），
    模板里多声明、但不在四要素里的分区排在最后（合法与否由 `validate_template` 判，
    这里只管按稳定顺序展示）；字段规格只保留 key/label/question 都非空的项。
    """
    sections = _sections_of(spec)
    order = [key for key in CARD_SECTIONS if key in sections]
    order.extend(key for key in sections if key not in CARD_SECTIONS)

    declared: list[tuple[str, str, list[dict]]] = []
    for key in order:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        label = section.get("label")
        label_text = label.strip() if isinstance(label, str) else key
        fields = section.get("fields")
        items = (
            [item for item in fields if isinstance(item, dict)] if isinstance(fields, list) else []
        )
        declared.append((key, label_text, [_field_spec(item) for item in items]))
    return declared


def declared_field_keys(spec: dict | None) -> tuple[str, ...]:
    """模板声明的字段 key（按展示顺序去重）；没声明时返回空元组。"""
    if not isinstance(spec, dict):
        return ()
    keys: list[str] = []
    for _section_key, _label, items in declared_sections(spec):
        for item in items:
            keys.append(item["key"])
    return tuple(dict.fromkeys(keys))


def _field_spec(item: dict) -> dict:
    """字段规格只留三个给人看的串；缺的按空串处理（合法性由模板校验报）。"""
    return {
        "key": _text(item.get("key")),
        "label": _text(item.get("label")),
        "question": _text(item.get("question")),
    }


def _sections_of(spec: dict) -> dict:
    card = spec.get("card") if isinstance(spec, dict) else None
    sections = card.get("sections") if isinstance(card, dict) else None
    return sections if isinstance(sections, dict) else {}


def _current_by_field(facts: list[Fact]) -> dict[str, Fact]:
    """字段 → 当前那条事实：同字段多条时后出现的覆盖前面的（仓储按时间正序）。"""
    current: dict[str, Fact] = {}
    for fact in facts:
        if not isinstance(fact, Fact) or not fact.field:
            continue
        current[fact.field] = fact
    return current


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""

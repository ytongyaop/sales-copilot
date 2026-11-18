"""校准脚本的纯函数测试：混淆矩阵、建议值选择、funnel 的精确键、模板写回幂等。

不联网、不读真库；只碰 `scripts/calibrate_thresholds.py` 的纯逻辑与临时 YAML。
"""

import importlib.util
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_thresholds.py"
_spec = importlib.util.spec_from_file_location("calibrate_thresholds", SCRIPT)
assert _spec is not None and _spec.loader is not None
calib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calib)


def _item(
    pair_id: str,
    *,
    label: bool,
    fuzzy: float,
    suffix_only: bool = False,
    exact: bool = False,
) -> dict:
    return {
        "id": pair_id,
        "a": pair_id,
        "b": pair_id,
        "label": label,
        "note": "测试用",
        "fuzzy": fuzzy,
        "suffix_only": suffix_only,
        "exact": exact,
    }


def test_evaluate_at_confusion_matrix_hand_calculated() -> None:
    """4 对（2 正 2 负）在 95 分处：TP 1 / FP 1 / FN 1 → precision 0.5、recall 0.5。"""
    scored = [
        _item("tp", label=True, fuzzy=98.0),
        _item("fn", label=True, fuzzy=80.0),
        _item("fp", label=False, fuzzy=99.0),
        _item("suffix", label=False, fuzzy=100.0, suffix_only=True),
    ]

    result = calib.evaluate_at(scored, 95)

    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["fn"] == 1
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["f1"] == 0.5
    # 「仅组织形式不同」的负例即使 100 分也不算 auto，所以不在误配里。
    assert result["fp_ids"] == ["fp"]


def test_evaluate_at_l1_ignores_exact_flag() -> None:
    """旧口径不认精确键：exact=True 但分数不够时仍不进 auto。"""
    scored = [_item("exact-low", label=True, fuzzy=10.0, exact=True)]

    l1 = calib.evaluate_at(scored, 97, model="l1")
    funnel = calib.evaluate_at(scored, 97, model="funnel")

    assert (l1["tp"], l1["recall"]) == (0, 0.0)
    assert (funnel["tp"], funnel["recall"]) == (1, 1.0)


def test_funnel_suffix_only_pair_is_not_exact_auto() -> None:
    """规范化同名但原串一个是「有限公司」一个是「集团」：不是精确命中，只能复核。"""
    assert calib.is_exact_hit("华远科技有限公司", "华远科技集团") is False

    pair = _item(
        "company-vs-group",
        label=False,
        fuzzy=100.0,
        suffix_only=True,
        exact=calib.is_exact_hit("华远科技有限公司", "华远科技集团"),
    )
    assert calib.is_auto(pair, 99, model="funnel") is False


def test_funnel_identical_name_is_exact_auto() -> None:
    """完全同形（只差首尾空白）：L0 精确键直接 auto，不看阈值。"""
    assert calib.is_exact_hit("嘉禾食品有限公司", "嘉禾食品有限公司 ") is True

    pair = _item(
        "same-name",
        label=True,
        fuzzy=0.0,  # 故意压低：证明精确键分支不依赖 L1 分数
        exact=calib.is_exact_hit("嘉禾食品有限公司", "嘉禾食品有限公司 "),
    )
    assert calib.is_auto(pair, 99, model="funnel") is True


def test_recommend_auto_picks_highest_recall_under_precision_target() -> None:
    """precision ≥ 0.95 的候选里取 recall 最高；同 recall 取更低阈值。"""

    def row(threshold: int, precision: float, recall: float) -> dict:
        return {
            "threshold": threshold,
            "precision": precision,
            "recall": recall,
            "f1": 0.0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "fp_ids": [],
        }

    rows = [
        row(99, 1.0, 0.4),
        row(97, 0.96, 0.6),
        row(95, 0.90, 0.9),  # precision 不达标，不能选
        row(93, 1.0, 0.6),  # 与 97 同 recall，取更低阈值
    ]

    best = calib.recommend_auto(rows)

    assert best["threshold"] == 93
    assert best["recall"] == 0.6
    assert best["target_met"] is True


def test_recommend_auto_falls_back_when_target_unreachable() -> None:
    """没有阈值达到 precision 目标时，取 precision 最高并提示补样本。"""
    rows = [
        {"threshold": 99, "precision": 0.8, "recall": 0.2, "fp_ids": []},
        {"threshold": 97, "precision": 0.9, "recall": 0.1, "fp_ids": []},
    ]

    best = calib.recommend_auto(rows)

    assert best["threshold"] == 97
    assert best["target_met"] is False
    assert "补充样本" in best["note"]


def test_write_template_adds_section_once_and_keeps_other_content(tmp_path: Path) -> None:
    """对临时 YAML 写两次：matching 段只出现一次，其余内容与注释不变。"""
    path = tmp_path / "demo.yaml"
    path.write_text(
        "template: demo\n"
        "name: 演示模板\n"
        "thresholds:\n"
        "  first_response_minutes: 30\n"
        "# 这一段注释必须保留\n"
        "flow:\n"
        "  - block: intake\n",
        encoding="utf-8",
    )

    first = calib.write_template("demo", 97, 86, templates_dir=tmp_path)
    second = calib.write_template("demo", 97, 86, templates_dir=tmp_path)

    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    assert first, "首次写入应有改动"
    assert second == [], "重复写不应再改动"
    assert text.count("matching:") == 1
    assert data["matching"] == {"l1_auto": 97, "l1_review": 86}
    assert data["thresholds"]["first_response_minutes"] == 30
    assert data["flow"] == [{"block": "intake"}]
    assert "# 这一段注释必须保留" in text


def test_write_template_updates_existing_section_in_place(tmp_path: Path) -> None:
    """已有 matching 段时只改值、不追加第二段，段内其它键也保住。"""
    path = tmp_path / "demo.yaml"
    path.write_text(
        "template: demo\n"
        "matching:\n"
        "  l1_auto: 95\n"
        "  l1_review: 85\n"
        "  alias_table: {}\n"
        "flow:\n"
        "  - block: intake\n",
        encoding="utf-8",
    )

    calib.write_template("demo", 97, 86, templates_dir=tmp_path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert path.read_text(encoding="utf-8").count("matching:") == 1
    assert data["matching"] == {"l1_auto": 97, "l1_review": 86, "alias_table": {}}

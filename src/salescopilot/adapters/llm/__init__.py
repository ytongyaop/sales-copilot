"""LLM 适配器：提炼器的两种实现 —— 假模型（离线）与真模型（DeepSeek）。

这里只导出实现，**用哪个由 `entrypoints/bootstrap.py` 决定**；
业务层只认 `domain/ports.py` 的 `FactExtractor`，不认识这两个类。
"""

from salescopilot.adapters.llm.deepseek import DeepSeekExtractor
from salescopilot.adapters.llm.fake import FakeExtractor

__all__ = ["DeepSeekExtractor", "FakeExtractor"]

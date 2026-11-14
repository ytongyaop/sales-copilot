"""业务错误类型（纯类型，无 IO、无行业词）。

提炼器（`adapters/llm/*`）失败时抛 `ExtractError`，用例层（`services/ingest.py`）把它
翻译成一条 `ValidationIssue` 再返回给接口——服务层不能 import 适配器，所以错误类型
放在 domain，双方都只依赖这里。
"""


class ExtractError(Exception):
    """提炼失败：网络、超时，或模型输出解析不了。

    消息是给人看的中文，说明失败原因与尝试次数；不许带密钥、响应原文等敏感内容。
    """

"""模板适配器：只负责读 YAML 文件与把格式错误包装成 `TemplateError`。

业务规则校验在 `domain/rules.py`；本层不判断模板内容对不对，只判断文件能不能读成映射。
"""

from .loader import TemplateError, load_all, load_template

__all__ = ["TemplateError", "load_all", "load_template"]

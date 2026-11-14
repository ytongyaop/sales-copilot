"""知识库适配器层：向量化、向量库、索引清单。

这一层是全项目唯一允许 import `chromadb` / `openai` / SDK 的地方之一；
`domain/` 与 `services/` 只认端口，不认识这里的任何实现。
"""

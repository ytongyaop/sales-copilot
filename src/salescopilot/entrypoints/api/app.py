"""FastAPI 应用实例。

B1 只有一个健康检查端点：证明「进程能起、路由能通」。业务路由到 B2 之后再挂。
"""

from fastapi import FastAPI

app = FastAPI(title="SalesCopilot", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查：只回答进程是否活着，不含任何业务判断。"""
    return {"status": "ok"}

# web/ — 前端（Vue3 + Vite）

线索的新增与列表页。一个页面：上面是新增表单，下面是列表表格；只调用后端 HTTP 接口。

## 边界

- 前端**只通过 HTTP 调后端**，不直接连数据库、不直接调模型 SDK。
- 必填 / 阈值规则以后端为唯一真源，前端不复制一份当权威。
- `node_modules/` 与 `dist/` 已在 `.gitignore` 里忽略，**不提交**；依赖由 `package.json` + lock 文件还原。
- 后端停掉时，前端显示「服务未连接，请确认后端已启动（uvicorn ...）」，不白屏、不暴露原始报错。

## 怎么跑

先起后端（仓库根）：

```powershell
$env:PYTHONPATH='src'
uvicorn salescopilot.entrypoints.api.app:app --reload --port 8000
```

再起前端（本目录）：

```powershell
npm install
npm run dev        # http://localhost:5173
```

`vite.config.js` 把 `/api/*` 代理到 `http://127.0.0.1:8000`，并把前缀去掉：
前端请求 `/api/leads` → 后端 `/leads`。生产构建 `npm run build` 输出到 `dist/`。

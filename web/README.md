# web/ — 前端（Vue3 + Vite）

五个入口：**分拣台**（今日待办 / 投料 → 分拣单 / 待认领池）、**客户**（仪表盘 + 客户货架 + 线索管理）、
**客户详情**（四维分级 + 信息卡 + 跟进 + 话术草稿）、**复盘**（本周数字 + 校准问题）、**设置**（运行模式 + 模板 + 导入 CSV）。
只调用后端 HTTP 接口，不引路由：四个页签用 `v-show` 挂载，详情是从货架 / 待办点进去的视图。

## 边界

- 前端**只通过 HTTP 调后端**，不直接连数据库、不直接调模型 SDK。
- 必填 / 阈值 / 档位 / 提醒判定以后端为唯一真源，前端不复制一份当权威；界面上的数字都是接口返回的原值。
- 前端也**不认识默认模板**：新增线索的模板下拉留空即为「默认模板（后端决定）」，避免出现第二份默认值。
- `node_modules/` 与 `dist/` 已在 `.gitignore` 里忽略，**不提交**；依赖由 `package.json` + lock 文件还原。
- 后端停掉时，前端显示「服务未连接，请确认后端已启动（uvicorn ...）」，不白屏、不暴露原始报错；
  首屏先问一次 `/health`（最多 6 次 × 500ms）再挂载各页，后端刚启动那几秒不会停在「0 家」。

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

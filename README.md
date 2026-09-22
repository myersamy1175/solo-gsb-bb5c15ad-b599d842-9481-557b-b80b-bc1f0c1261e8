# 科研样品分装追溯 API

基于 **FastAPI + SQLite** 的科研样品谱系(lineage)追溯服务。支持样品的**取样、分装、合并**三类操作,在数据库事务中完成余量校验与环路检测,并通过唯一操作号实现**幂等提交**;可按任意样品回溯完整来源与去向,并对余量账本做一致性校验。此外提供**预留模块**:客户端可用一个预留号为多个样品分别锁定数量,服务在同一事务中校验各样品可用余量(当前余量减去未释放预留),全部足够才一次创建,支持按预留号幂等重放、整单释放与可用量查询。**盘点模块**:按样品编号集合在同一事务保存盘点时的余量/版本快照,提交实盘结果时逐项校验样品仍存在且数据库余量未被其他操作改变并计算差异,确认批次时在一个事务内再次校验版本、写入盘点调整流水(带操作者与原因)并更新样品余量,任一冲突整批不改库存。

## 一键启动

需要本机安装 Docker 与 Docker Compose(v2)。

```bash
docker compose up --build
```

启动后访问:

- API 服务地址:**http://localhost:8000**
- 健康检查:http://localhost:8000/health
- 交互式文档(Swagger UI):http://localhost:8000/docs
- OpenAPI 描述:http://localhost:8000/openapi.json

数据持久化在 Docker 卷 `sample-data`(容器内 `/data/samples.db`)。停止后数据保留,可用 `docker compose down -v` 连同数据卷一并清除。

### 本地直接运行(无 Docker)

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 同样打开 http://localhost:8000
```

可通过环境变量调整:`SAMPLE_DB_PATH`(数据库文件路径,默认 `./data/samples.db`)、`BALANCE_TOLERANCE`(余量校验容差,默认 `1e-6`)。

## 数据模型与规则

| 概念 | 说明 |
| --- | --- |
| 样品 `sample_id` | 业务方提供的唯一编号;带有物料、单位、初始量与当前余量 |
| 操作号 `op_no` | **全局唯一的幂等键**。同一操作号 + 同一请求体重放,返回首次提交的原始结果;同号但内容不同返回 `409 op_no_conflict` |
| 取样 sampling | 从外部获得全新**根样品**,记录来源边(`parent=null`)与入库流水 |
| 分装 aliquot | 一个父样品拆成若干**新**子样品;总取出量不得超过父样品余量 |
| 合并 merge | 多个来源(可部分取用)汇入输出;输出可为新样品或已有样品;支持守恒校验 |
| 谱系边 | 每次转移记录 `parent → child`、数量、单位与操作号,构成可追溯有向无环图 |
| 出入库流水 | 与谱系边对应的 in/out 账本,余量可随时按流水重算审计 |
| 预留单 | 一个预留号(`reservation_no`)为多个样品分别锁定数量;`active`=锁定中,`released`=已整单释放 |
| 已预留量 / 可用量 | 已预留量 = 该样品全部未释放预留的锁定合计;**可用量 = 当前余量 − 已预留量** |
| 预留审计事件 | 预留、幂等重放、释放与查询的结果快照均写入 `reservation_events`,随数据库持久化 |
| 盘点批次 `stocktake_no` | **盘点批次号(幂等键)**;状态机 `open`(已存快照)→ `counted`(已提交实盘结果)→ `confirmed`(已确认并调整库存) |
| 余量版本 `version` | `samples.version` 乐观版本号,初始为 0;取样/分装/合并/盘点确认等一切余量写操作都令其 +1 |
| 盘点快照 | 建批次时在同一事务记录各样品的余量与版本;提交实盘结果、确认批次都以快照版本判断余量是否被其他操作改变 |
| 系统量 / 实盘量 / 差异 | 系统量 = 提交/确认时点的数据库余量;实盘量 = 现场盘点数量;**差异 variance = 实盘量 − 系统量**(正为盘盈、负为盘亏) |
| 盘点调整流水 | 确认时逐项写入 `stocktake_adjustments`(差异为 0 也留痕),带操作者与原因;余量账本重算自动包含盘点差异 |

**事务保证**

- 余量校验、环路检测与写入在单个 `BEGIN IMMEDIATE` 事务中完成;任一操作冲突,**整批回滚**。
- 禁止环路:合并前在谱系图上检测"输出样品能否沿现有边到达任一输入"(即新边是否成环),冲突时返回完整环路径,如 `A1 -> B1 -> A1`。
- 禁止自合并、单位不一致、覆盖已有样品、负余量(数据库 `CHECK remaining >= 0` 兜底)。
- 冲突统一为 HTTP `409`,响应体 `conflicts[]` 携带冲突类型、操作号、批内下标(`batch_index`,从 0 起)、样品号、位置、请求量/可用量与环路径。

**预留事务保证**

- 创建预留时在单个 `BEGIN IMMEDIATE` 事务内检查每个样品的可用量(当前余量减去未释放预留);**任一样品不足即整单失败**,不写入任何预留(返回所有不足/不存在的样品及各自可用量)。
- 同一预留号 + 同一请求体重放返回首次结果(`replayed: true`),不重复锁定;同号不同内容返回 `409 reservation_no_conflict`;对已释放预留号再次创建返回 `409 reservation_closed`。
- `DELETE` 整单释放后锁定量立即归还,可被新预留使用;重复释放幂等;预留明细在释放后保留作为审计痕迹。
- 预留不改动样品余量与出入库流水,仅通过"未释放预留合计"影响可用量。

**盘点事务保证**

- 新建批次(`POST /api/v1/stocktakes`):在单个 `BEGIN IMMEDIATE` 事务内校验样品集合全部存在,并保存各样品盘点时的余量与版本快照;任一样品不存在则整批失败、不建批次。
- 提交实盘结果(`POST .../results`):逐项校验①样品仍存在;②数据库余量版本与快照一致(即盘点期间余量未被取样/分装/合并/其他盘点确认改变);提交的样品集合必须与批次快照**一致(与顺序无关,按样品号匹配)**,多出或缺失样品返回 `stocktake_items_mismatch`。全部通过才把实盘量、当时系统量落库(状态 `open → counted`)并给出差异;任一冲突则批次保持 `open`、不记录任何实盘结果。
- 确认批次(`POST .../confirm`):在**一个事务内再次**逐项校验版本(二次校验防止"提交后、确认前"的并发修改),全部无冲突才:逐项写入 `stocktake_adjustments` 调整流水(含快照量、系统量、实盘量、差异、操作者、原因),按差异更新样品余量并递增版本(差异为 0 时余量与版本不变,但仍写流水留痕);**任一项冲突则整批回滚,所有样品库存都不改变**。
- 盘点批次号是幂等键:同号 + 同请求体重放返回首次结果(`replayed: true`);同号不同内容返回 `409 stocktake_batch_conflict`。实盘结果与确认请求(操作者/原因)分别做幂等比对。
- 冲突可恢复:版本冲突后该批次无法再提交/确认(版本只增不减),需用**新批次号**重新建批次盘点;已确认批次不能再提交结果。
- 查询(`GET /api/v1/stocktakes/{batch_no}`)返回快照、当前余量、实盘量、提交时系统量、差异、是否发生过版本漂移、确认后余量与时间戳。
- 旧库自动迁移:新表与 `samples.version` 列由启动时的增量迁移(`schema_migrations` 记录版本,首个迁移 `0001_stocktake`)自动补齐,Docker 卷中的历史数据可直接升级。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/operations` | 提交单个操作(按 `type` 自动判别) |
| POST | `/api/v1/operations/sampling` | 取样 |
| POST | `/api/v1/operations/aliquot` | 分装 |
| POST | `/api/v1/operations/merge` | 合并 |
| POST | `/api/v1/batch` | 批量操作,单事务提交;`status` 为 `committed`/`replayed`/`mixed` |
| POST | `/api/v1/validate` | 单操作 dry-run 预校验(不写库) |
| POST | `/api/v1/batch/validate` | 批量 dry-run 预校验,冲突带 `batch_index` 定位 |
| GET  | `/api/v1/samples` | 样品列表(分页) |
| GET  | `/api/v1/samples/{id}/trace?direction=both` | 回溯来源 `upstream` / 去向 `downstream` / `both` |
| GET  | `/api/v1/samples/{id}/balance` | 单个样品余量账本校验 |
| GET  | `/api/v1/checks/balances` | 全量余量一致性校验,返回全部冲突及定位 |
| POST | `/api/v1/reservations` | 创建预留:一个预留号锁定多个样品,全部足够才一次创建 |
| GET  | `/api/v1/reservations?sample_id=...` | 查询样品当前余量 / 已预留量 / 可用量(可重复传参,不传返回全部) |
| GET  | `/api/v1/reservations/{reservation_no}` | 按预留号查询预留单 |
| DELETE | `/api/v1/reservations/{reservation_no}` | 整单释放预留(按预留号幂等) |
| POST | `/api/v1/stocktakes` | 新建盘点批次:单事务保存样品集合的余量/版本快照(按批次号幂等) |
| POST | `/api/v1/stocktakes/{batch_no}/results` | 提交实盘结果:逐项版本校验并计算系统量与实盘量差异 |
| POST | `/api/v1/stocktakes/{batch_no}/confirm` | 确认盘点:单事务再次校验版本、写调整流水并更新余量,任一冲突整批不改库存 |
| GET  | `/api/v1/stocktakes/{batch_no}` | 查询盘点批次:快照、实盘量、系统量、差异与确认状态 |
| GET  | `/api/v1/operations/{op_no}` | 按操作号取首次提交的原始结果(幂等凭证) |
| GET  | `/health` | 健康检查 |

## 使用示例

以下示例可在服务启动后直接执行(`http://localhost:8000`)。

### 1. 取样 100 mL

```bash
curl -X POST http://localhost:8000/api/v1/operations/sampling \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "sampling",
    "op_no": "OP-20260922-001",
    "sample_id": "S0",
    "material": "血清",
    "amount": 100,
    "unit": "mL",
    "location": "冷库A-1"
  }'
```

返回新建样品、谱系边与最新余量。再次提交**完全相同**的请求体,返回内容一致且 `replayed: true`,不会重复入库。

### 2. 批量分装(单事务)

```bash
curl -X POST http://localhost:8000/api/v1/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "ALI-0922",
    "operations": [
      {"type": "aliquot", "op_no": "OP-002", "parent_id": "S0",
       "outputs": [{"sample_id": "A1", "amount": 60},
                   {"sample_id": "A2", "amount": 30}],
       "location": "操作台2"}
    ]
  }'
# S0 余量:100 -> 10;新建 A1=60、A2=30
```

### 3. 余量不足冲突(返回操作与定位)

```bash
curl -X POST http://localhost:8000/api/v1/operations/aliquot \
  -H 'Content-Type: application/json' \
  -d '{"type": "aliquot", "op_no": "OP-003", "parent_id": "S0",
       "outputs": [{"sample_id": "A3", "amount": 20}]}'
```

```json
{
  "error": "conflict",
  "conflicts": [{
    "code": "insufficient_balance",
    "message": "父样品 S0 余量 10.0 mL 不足,本次分装需要 20.0 mL",
    "op_no": "OP-003",
    "batch_index": null,
    "sample_id": "S0",
    "location": null,
    "requested": 20.0,
    "available": 10.0,
    "cycle_path": null
  }]
}
```

### 4. 合并,并触发环路检测

```bash
# A1 取 40、A2 取 20 合并成 B1(60 mL)
curl -X POST http://localhost:8000/api/v1/operations/merge \
  -H 'Content-Type: application/json' \
  -d '{"type": "merge", "op_no": "OP-004",
       "inputs": [{"sample_id": "A1", "amount": 40},
                  {"sample_id": "A2", "amount": 20}],
       "output_id": "B1", "material": "混合血清"}'

# 试图把下游 B1 合回其上游 A1 -> 成环,409
curl -X POST http://localhost:8000/api/v1/operations/merge \
  -H 'Content-Type: application/json' \
  -d '{"type": "merge", "op_no": "OP-005",
       "inputs": [{"sample_id": "B1", "amount": 5}],
       "output_id": "A1"}'
# conflict code = cycle_detected, cycle_path = ["A1", "B1", "A1"]
```

合并时还可传 `expected_output_amount` 做输入/输出守恒校验;输出为已存在样品时则把总量并入该样品(不得成环)。

### 5. 回溯完整来源 / 去向

```bash
# 向上:B1 的全部来源根样品与逐跳路径
curl "http://localhost:8000/api/v1/samples/B1/trace?direction=upstream"
# {"roots": ["S0"], "nodes": [{"sample_id": "B1", "depth": 0},
#                              {"sample_id": "A1", "depth": 1},
#                              {"sample_id": "A2", "depth": 1},
#                              {"sample_id": "S0",  "depth": 2}], ...}

# 向下:最终流到哪些无下游的叶子样品
curl "http://localhost:8000/api/v1/samples/S0/trace?direction=downstream"
```

响应同时包含 `nodes`(样品及跳数)、`edges`(带操作号的谱系边)、`operations`(相关操作与时间/位置)、`roots`、`leaves`。遍历用递归 CTE 并带路径防环保护。

### 6. 余量账本校验

```bash
curl http://localhost:8000/api/v1/checks/balances
```

对每个样品比较**冗余余量**与按出入库流水重算的结果,`consistent: false` 时在 `conflicts` 中给出样品定位(可经 `?tolerance=` 调整容差)。

### 7. 批量预校验(dry-run)

```bash
curl -X POST http://localhost:8000/api/v1/batch/validate \
  -H 'Content-Type: application/json' \
  -d '{"operations": [
    {"type": "aliquot", "op_no": "X1", "parent_id": "S0",
     "outputs": [{"sample_id": "K1", "amount": 2}]},
    {"type": "aliquot", "op_no": "X2", "parent_id": "S0",
     "outputs": [{"sample_id": "K2", "amount": 9}]}
  ]}'
# valid=false,conflicts[0].batch_index = 1(S0 在第一条后仅剩 8)
# 预校验基于内存快照执行,主库不会被写入
```

### 8. 预留:一个预留号锁定多个样品

```bash
curl -X POST http://localhost:8000/api/v1/reservations \
  -H 'Content-Type: application/json' \
  -d '{
    "reservation_no": "RSV-20260922-001",
    "note": "实验A备料",
    "items": [
      {"sample_id": "S0", "amount": 30},
      {"sample_id": "A1", "amount": 10}
    ]
  }'
```

服务在**同一事务**内校验每个样品的「当前余量 − 未释放预留」:全部足够才创建,任一不足则整单回滚(任何样品的已预留量都不变)。返回每个样品的当前余量、已预留量与可用量:

```json
{
  "reservation_no": "RSV-20260922-001",
  "status": "active",
  "items": [
    {"sample_id": "S0", "amount": 30.0, "unit": "mL",
     "remaining": 100.0, "reserved": 30.0, "available": 70.0},
    {"sample_id": "A1", "amount": 10.0, "unit": "mL",
     "remaining": 60.0, "reserved": 10.0, "available": 50.0}
  ],
  "note": "实验A备料",
  "replayed": false
}
```

同一预留号用**完全相同**的请求体重放,返回首次结果且 `replayed: true`,不会重复锁定;同号不同内容返回 `409 reservation_no_conflict`。样品可用量不足时返回 `409 insufficient_available`,冲突体带 `requested`(本次锁定量)与 `available`(当前可用量)。

### 9. 查询可用量

```bash
# 查询指定样品(参数可重复)
curl "http://localhost:8000/api/v1/reservations?sample_id=S0&sample_id=A1"
# 不传 sample_id 则返回全部样品
curl http://localhost:8000/api/v1/reservations
```

```json
{"samples": [
  {"sample_id": "S0", "unit": "mL",
   "remaining": 100.0, "reserved": 30.0, "available": 70.0}
]}
```

### 10. 整单释放

```bash
curl -X DELETE http://localhost:8000/api/v1/reservations/RSV-20260922-001
```

释放后该单全部锁定量归还各样品(响应给出释放后的余量 / 已预留 / 可用量);对同一预留号重复释放返回相同结果且 `replayed: true`。

预留、重放、释放与查询事件均持久化在 `reservation_events` 表(含结果快照),新表随容器启动时的数据库初始化自动创建,数据保存在 Docker 卷 `sample-data` 中。

### 11. 盘点:快照 → 实盘 → 确认调整

```bash
# ① 新建盘点批次:同一事务为样品集合保存余量/版本快照
curl -X POST http://localhost:8000/api/v1/stocktakes \
  -H 'Content-Type: application/json' \
  -d '{"batch_no": "STK-20260922-001",
       "note": "冷库月度盘点",
       "sample_ids": ["S0", "A1"]}'
```

返回每一项的 `snapshot_remaining` / `snapshot_version`(此时 `status: open`)。同一批次号用完全相同的请求体重放返回首次结果且 `replayed: true`;同号不同内容返回 `409 stocktake_batch_conflict`。

```bash
# ② 提交实盘结果:逐项校验样品仍存在且余量版本与快照一致
curl -X POST http://localhost:8000/api/v1/stocktakes/STK-20260922-001/results \
  -H 'Content-Type: application/json' \
  -d '{"batch_no": "STK-20260922-001",
       "items": [{"sample_id": "S0", "counted_amount": 8},
                 {"sample_id": "A1", "counted_amount": 62}]}'
```

通过后 `status: counted`,每项给出 `system_remaining`(提交时数据库余量)与 `variance`(实盘量 − 系统量),并以 `changed_since_snapshot` 标注建批次到提交之间余量是否被改动过。若盘点期间某样品余量被其他操作改变(版本不一致),返回 `409 stocktake_version_conflict`,冲突体附 `requested`(快照余量)与 `available`(当前余量),批次保持 `open`,需用**新批次号**重新盘点。

```bash
# ③ 确认:一个事务内再次校验版本,写盘点调整流水并更新余量
curl -X POST http://localhost:8000/api/v1/stocktakes/STK-20260922-001/confirm \
  -H 'Content-Type: application/json' \
  -d '{"batch_no": "STK-20260922-001",
       "operator": "李四",
       "reason": "2026-09 月度盘点,盘亏记损耗"}'
```

确认后 `status: confirmed`,每项给出 `final_remaining`;`stocktake_adjustments` 中逐项保留快照量、系统量、实盘量、差异、操作者与原因(差异为 0 也留痕)。若"提交结果后、确认前"任一样品余量又被改动,确认返回 `409` 且**整批回滚——所有样品库存都不变**。相同请求体重复确认返回首次结果且 `replayed: true`(不重复写流水)。

```bash
# ④ 查询:快照、实盘量、系统量、差异、版本漂移与确认状态
curl http://localhost:8000/api/v1/stocktakes/STK-20260922-001
```

盘点调整纳入余量账本重算,确认后 `/api/v1/checks/balances` 仍保持 `consistent: true`。盘点表与 `samples.version` 列通过启动时的增量迁移自动创建,历史 Docker 卷可平滑升级。

## 冲突码

| code | 含义 |
| --- | --- |
| `insufficient_balance` | 父样品/输入余量不足,附 `requested`/`available` |
| `cycle_detected` | 合并会形成谱系环路,附 `cycle_path` |
| `sample_not_found` | 引用了不存在的样品 |
| `sample_already_exists` | 取样/分装目标样品已存在 |
| `unit_mismatch` | 操作单位与样品单位不一致 |
| `self_merge` | 样品合并到自身 |
| `output_amount_mismatch` | 守恒校验:期望输出量与输入合计不符 |
| `material_required` | 新建合并产物缺少 `material` |
| `op_no_conflict` | 操作号已存在但请求内容不同(幂等键冲突) |
| `duplicate_op_no_in_batch` | 同一批次内操作号重复,附 `batch_index` |
| `balance_inconsistent` | 余量账本重算与冗余余量不一致 |
| `insufficient_available` | 预留可用量不足(当前余量−未释放预留),附 `requested`/`available` |
| `reservation_no_conflict` | 预留号已存在但请求内容不同 |
| `reservation_closed` | 预留号已释放,不能再次创建 |
| `reservation_not_found` | 预留号不存在(查询/释放时返回 HTTP 404) |
| `stocktake_batch_not_found` | 盘点批次号不存在(查询/提交/确认时返回 HTTP 404) |
| `stocktake_batch_conflict` | 盘点批次号已存在但请求内容不同、批次状态不允许当前操作,或路径/请求体批次号不一致 |
| `stocktake_items_mismatch` | 提交的样品集合(含顺序)与批次快照不一致 |
| `stocktake_version_conflict` | 样品余量版本已偏离快照版本——数据库余量被其他操作改变;附 `requested`(快照余量)/`available`(当前余量) |

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 项目结构

```
app/
  config.py        # 数据库路径、容差等配置
  db.py            # SQLite 连接 / 建表(WAL、外键、CHECK 约束、预留表)与增量迁移(schema_migrations)
  schemas.py       # Pydantic 请求与响应模型(含冲突结构、预留与盘点模型)
  service.py       # 核心:事务提交、余量/环路校验、幂等回放、dry-run、递归回溯、账本审计、余量版本
  reservations.py  # 预留:单事务多样品锁定、幂等重放、整单释放、可用量查询、事件审计
  stocktake.py     # 盘点:批次快照、实盘版本校验/差异计算、确认调整流水、单事务整批回滚
  main.py          # FastAPI 路由
tests/
  test_api.py          # 操作链路端到端测试(TestClient)
  test_reservations.py # 预留模块端到端/并发/审计测试
  test_stocktake.py    # 盘点模块端到端/并发/旧库迁移测试
Dockerfile
docker-compose.yml
```

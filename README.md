# 联营餐盒租次与跨点退押服务

这是一个仅依赖 Python 3.11 标准库的共用服务，把：

- 一只实物餐盒（`box` / 二维码）的轨迹、保管人和可借状态；
- 每次领用租次（`issue`）的押金、七天计时、退款资格和到账状态；
- 归还机临时凭证、收箱点开箱点验、门店当面点验和同物争议；
- 原领用店、代收店、归还机归属商户之间的月度服务费及误认差额；

放到同一套业务对象中。顾客、领用门店、还盒点和管理端可以共用 HTTP/JSON 服务，也可以直接调用 Python 核心服务或 CLI。

## 运行

```bash
# 现有资料检查
python3 -m unittest discover -s tests -v

# 启动服务；当前目录没有 data/turnbox.json 时会自动载入 samples/
python3 app.py
# 或
python3 -m turnbox serve --host 0.0.0.0 --port 8000 --load-samples --db data/turnbox.json
```

打开 <http://127.0.0.1:8000/> 可使用简体中文运营台：

- 顾客：按顾客编号查每只盒、应退/已到账和争议；
- 门店/还盒点：按商户号或点号查押金、应退、代其他店点验和费用；
- 管理端：查争议、核实支持某一处点验、生成月末结算。

也可用 CLI：

```bash
python3 -m turnbox customer guest-a
python3 -m turnbox merchant hot-c
python3 -m turnbox point auto-4      # 也可直接走 HTTP /api/points/auto-4
python3 -m turnbox box box7
python3 -m turnbox issue rent1
python3 -m turnbox disputes
python3 -m turnbox settle 2026-11
python3 -m turnbox resolve-dispute disp-rent1 --uphold hot-d/c1 --by admin
```

## 核心规则实现位置

所有规则都在 `turnbox/service.py` 的 `TurnBoxService` 中，HTTP 适配在 `turnbox/server.py`。

1. **租次和实物分离**：`issue` 是一次领用，`box` 是可多次出租的实物。押金、退款、异议、追回和补退均带 `lease_scope=issue:<id>`，不会动用另一租次的押金。
2. **整单多盒、单盒独立**：可选提交 `order_id`/`order`，每个盒仍有独立 `issue`；一只盒退回只影响该租次。
3. **机器凭证不是退款/清洗责任**：`machine_in` 单独存在时资格为 `awaiting_box_check`，不能创建退款。有效的 `box_check.prior` 关联原机器凭证后，截止时间采用机器实际收件时间，而不是隔天点验时间。
4. **租次与实物必须匹配**：点验记录的 `issue`、`box`、领用时间不一致时不会成为退款依据；同一 `(point,event)` 的完全重复行幂等，内容冲突则报错。
5. **同物两处持有进入争议**：同一盒的两个独立点验/暂收主张会生成稳定争议，顾客、相关商户和点号均可查；管理员可 `uphold` 一个主张或 `reject_all`。
6. **七天全退**：`issued_at + 7*24h` 为截止时刻，`effective_at <= deadline` 即全退，相等也算全退；超过后按领用当时政策的 `deduction` 扣款。
7. **退款到账以渠道状态为准**：`accepted` 只表示收付方受理，只有存在 `settled` 事件才是 `settled`/已到账。外部 NDJSON 中的退款事件会原样保留并汇总。
8. **清洗和资金解耦**：`wash_ok` 且验收通过才使盒子 `rentable=true`；`wash_fail` 后只能维修/淘汰。清洗结果或正常磨损不会重算已经确认的全退金额。退款是否到账、服务费是否结算都不影响盒子状态。
9. **保管链可追踪**：`box_view` 合并领用、暂收、点验、交接和清洗反馈，输出 `rented`、`at_machine_pending_check`、`pending_cleaning`、`in_transit`、`available`、`wash_failed` 等状态、当前保管人和完整时间线。
10. **月度服务费**：`settlement_lines(YYYY-MM)` 按有效点验的实际收件月份和领用政策计算 `collection_fee`，付款方为原领用商户，收款方为有效归还点的归属商户。机器及其开箱点验只产生一笔费用，开箱点验本身不另计。
11. **争议挂起与结算后差额**：未解决争议的费用行进 `held`，不进入可支付 `balances`。若错误归属已进入定稿结算，管理员核实后会生成带 `original_settlement_id`/`original_line_id` 的 `ADJ-*` 差额和复式往来条目；下一租次仍生成自己的费用行，不抵扣旧租次。
12. **状态可持久化**：`--db data/turnbox.json` 使用原子替换写入 JSON；不给 `--db` 的 Python 调用可直接使用内存服务或 `sample_service()`。

## 主要 HTTP API

```text
GET  /api/health
GET  /api/customers/{customer_id}
GET  /api/issues/{issue_id}
GET  /api/issues/{issue_id}/business
GET  /api/boxes/{box_id}
GET  /api/merchants/{merchant_or_point_id}
GET  /api/points/{point_id}
GET  /api/disputes
POST /api/disputes/{dispute_id}/resolve
GET  /api/admin/overview
GET  /api/admin/integrity

POST /api/records
POST /api/ingest/ndjson?type=collections
POST /api/issues
POST /api/orders
POST /api/moves
POST /api/issues/{issue_id}/refunds
POST /api/issues/{issue_id}/entries
POST /api/refunds/{channel_ref}/accept
POST /api/refunds/{channel_ref}/settle

GET  /api/settlements?period=YYYY-MM
POST /api/settlements
POST /api/admin/settlements/{period}/finalize
POST /api/settlements/{settlement_id}/correct
```

示例：

```bash
curl -s http://127.0.0.1:8000/api/customers/guest-a | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/boxes/box7 | python3 -m json.tool

curl -s -X POST http://127.0.0.1:8000/api/disputes/disp-rent1/resolve \
  -H 'Content-Type: application/json' \
  -d '{"claim":["hot-d","c1"],"resolved_by":"admin-1"}'

curl -s -X POST http://127.0.0.1:8000/api/settlements \
  -H 'Content-Type: application/json' \
  -d '{"period":"2026-11","finalize":true}'
```

## Python 直接接入

```python
from turnbox import TurnBoxService

svc = TurnBoxService()
svc.load_directory("samples")

print(svc.customer_view("guest-a"))
print(svc.box_view("box7"))
print(svc.settlement_lines("2026-11"))

svc.resolve_dispute("disp-rent1", {"claim": ["hot-d", "c1"]})
svc.settle_month("2026-11")
```

交换格式见 [`protocol.md`](protocol.md)。样本中的编号、代号和金额均为演练数据；金额字段全部是整数分。

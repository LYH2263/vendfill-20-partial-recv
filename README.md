# VendFill 售货机补货

按货道容量、库存与在途量计算缺口，生成不超缺口、非负的补货单；支持分批到货按行部分核销，
行状态 / 货道库存与在途 / 整单完成态 / 汇总共用同一套原子事务规则。

## 核销规则

- 建单即占在途：每个正补量行按补量 `在途 += 补量`，行状态为「待核销」。
- 分批核销：可只勾选部分行。入选行在**同一事务**内 `库存 += 补量、在途 -= 补量`、
  行置「已核销」；未选中行不动，仍按补量继续占用在途。
- 整单仅当全部正补量行均已核销才标 `completed`；不存在「整单完成但仍有待核销行」
  或「行已核销但库存未加」。
- 本批任一行失败（含 `fail_after` 注入失败）整批回滚，未选中行也不被改动。
- 已完成 / 已作废整单再核销任何行一律 409 拒绝。

种子数据：建单后先核销 B1 行（薯片补 7：库存 3→10、在途 9→2），A1、C1 仍待核销，整单未完成。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/refills/run?location_id=1` | 生成补货单（草稿期幂等，不重复占在途） |
| GET | `/api/refills/latest` | 最新单据（含行核销态与实时库存/在途） |
| POST | `/api/refills/{id}/verify` | body `{"lane_ids": [...]}` 勾选核销；省略则核销全部待核销行；可带 `fail_after` 注入失败 |
| POST | `/api/refills/{id}/void` | 作废整单，释放待核销行占用的在途 |
| GET | `/api/refills/summary` | 建议合计 + 核销进度 + 整单完成态 |

技术栈：Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL / Vue 3 / TypeScript / Vite

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:4800 |
| API | http://localhost:9800 |
| API 文档 | http://localhost:9800/docs |
| Postgres | localhost:5449 |

健康检查：`GET http://localhost:9800/api/health`

## 使用说明

1. 在「点位」「货道」查看售货机布局与库存。
2. 在「销量」了解近期出货。
3. 打开「补货单」按缺口生成建议补货量。
4. 在「满仓」「汇总」查看已满货道与补货合计。

## 开发与测试

```bash
docker compose exec api pytest -q
```

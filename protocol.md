# 周转箱交接单据

`issues` 的 issue 和 box 分别为租次和实物编号。时刻为 ISO 8601，七天从 issued_at 起算为七个 24 小时，截止时刻归还仍全退。policy 指领用当时的 `policies` 条款，金额整数分。

`collections` 由 (point, event) 唯一识别，一次点验可关联之前临时收件凭证。现场人工 confirmed 是有实物的点验，machine_in 是归还机暂收，box_check 是收箱点对机器实物的确认，包含对应原接收凭证。

`moves` 的 move 关联实物、来源、目标、交接时刻，wash_ok 表示清洗方验收通过，wash_fail 表示只能进入维修或淘汰。重复反馈必须依据同一次保管移交引用。

`refunds` 通过 channel_ref 关联原退款提交，issue 是原租次，accepted 是收付方受理，settled 才表示到账。代收费用按领用条款且只给有效归还点的经营门店，机器及人工收件点的归属见 `points`。开箱替归还机做点验本身不另计一笔代收费，settlement 是跨店资金凭据，不参与盒子是否可借判断。

交换样本为 UTF-8 NDJSON，每行独立。文件中的行顺序保留这组资料的原始提供顺序。

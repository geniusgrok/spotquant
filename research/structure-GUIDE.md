# 固定结构研究入口

本轮结论与原始失败见structure-RESULT.md及evidence/btc-structure-20261004。登记文件structure-spec.json在结果前冻结；两个已测现货窗口已满足提前拒绝条件，**不要为了跑满四窗或795会话重跑**。正式默认/有限人工运行仍按canonical-crowding-GUIDE.md、edge-GUIDE.md。

本轮实际筛选入口（仅日后另一个已登记且有决策价值的任务需要时使用）：

```sh
python3.13 -m research.structure_screen   --market /path/to/spot-klines   --fx /path/to/original-usdcny.json   --features /path/to/edge-features-v1.json   --schedule evidence/btc-structure-20261004/session_schedule.json   --out /NEW/screen-output
```

代码走真实session/Lifecycle/历史成交模拟，独立钱包、原会话时钟及完成日输入。筛选账户complete=false/cagr=null；不将短窗口冒充原完整账户，不组合年度收益。--resume-receipt只接受SHA绑定、旧生产者为祖先、除输出恢复代码外经济依赖不变的已完成窗口；本次首窗缺失机会日志的明确金融投影只可作低成本筛选，不可改称完整原始账户证明。

固定历史输入和已验公共文件可复用，资金、订单、时钟不能共享。900秒筛选预算在窗口边界检查；提前风险失败立即停止。每个新候选先有限筛选，通过后才估算并登记原完整四压力账户需要回答的问题，复用适用旧基线，不能自动扩展参数或资金矩阵。按用户偏好最终全量软件检查一次，失败只补受影响部分。

当前新鲜公开市场不可用，前向仍pending。HTTP451原件为可用性诊断，不是账本观察；旧账本用原批准消费者f1383f1034e3f72df68bbda30793d22835e74557，禁止用当前研究HEAD重绑。无自动后台任务/守护进程，无新增私有交易或账户操作授权。

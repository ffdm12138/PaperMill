# Catalog Research Proposal Writer

"先射箭后画靶"的研究计划脚手架 skill：先根据用户划定的 catalog 范围完成
主题综述与研究空白（射箭），再针对用户自己的研究项目写方法设计，最后规划
结果与数据分析工作（画靶）。跑完之后，论文的 Introduction 和 Methods 已经
搭好，后面的内容有了可执行的研究骨架。

前置：`create_write_job.py --workflow proposal --categories <关键词...>` 建 job，
用户填写 `write/jobs/<job_id>/input/research_input.md`（研究问题、对象与数据、
已有条件、预期产出）。skill 对未填写的输入 fail-closed，绝不代填用户的项目
事实。

产出分三层：八列文献矩阵与四份规划 Markdown（大纲/空白/方法设计/结果规划）、
机器可核验的 `proposal_plan.json`（方法逐项带语料出处 `grounded_in`，分析
逐项 `status="planned"`）、以及 TeX 成品（introduction / literature_review /
research_gaps / methods / research_plan / conclusion）。

诚实性是本 skill 的核心约束：结果部分只写"计划做什么分析、预期什么形态的
产出、如何验证"，不虚构任何数据或结论；引用全部来自 job 内 metadata。
与相邻 skill 的分工：`catalog_review_writer` 只到综述+方向为止；
`catalog_tex_writer` 写 mini article；本 skill 输出完整研究计划骨架。

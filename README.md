# 连环画创作审稿

维护史料、脚本、分镜和多学科审稿意见之间的版本依赖。

## 运行

- `python3 service.py --check` 检查基础配置。
- `python3 service.py --port 8000` 启动服务;`GET /health` 为健康检查。
- `npm test` 运行全部测试(服务契约、领域规则、HTTP 接口)。

## 领域规则(domain.py)

- **版本互引**:脚本段落、人物设定、分镜页均按版本演进;页面版本记录其采用的
  脚本版本、设定版本与史料修订号,依赖陈旧即不可交付。史料每次修订冻结该修订的
  引文与授权快照。
- **专业签署**:党史/军史专家(史实)、作家/文学编辑(文字)、画家/美术编辑(画面)
  只签署自己专业范围内的意见;编辑负责采纳、驳回与状态流转。
- **意见绑定版本**:签署时记录目标版本,页面意见同时冻结该版本采用的脚本/设定/
  史料修订快照(`dependency_snapshot`)。采纳、驳回前再次核对目标是否仍为当前
  版本;针对旧版本的意见只能留档或由原签署人**转签**到新版本
  (`reassign_opinion`),不得直接改变新稿状态(报 `OutdatedOpinionError`,HTTP 409)。
- **意见留档**:意见状态为 提出 → 采纳/驳回,采纳后可撤回;曾被采纳的意见
  (含已撤回)任何人不得删除。
- **冲突会审**:只有**同一目标版本、同一专业范围**内结论冲突的已采纳意见才自动
  生成会审议题(议题带 `target_version`),页面转入联合会审;重大事实议题未关闭
  不得转入精稿。对立意见被撤回/驳回、同版本已无冲突时议题自动关闭留档;页面
  换版后旧版本议题关闭并标记 `superseded_by`,学员异议只随旧议题留档、不重开。
  学员对当前版本的已关闭议题提交附史料依据的异议时,议题重开,已进入精稿/
  可出版的页面退回联合会审。
- **授权检查**:授权期、保密期、出版范围按页面版本**所引用的史料修订快照**,
  在交付与导出时检查;过期授权、事实争议或依赖陈旧的页面明确保持不可交付。
- **批量导出**:`export_batch` 在开始时冻结候选页面、各页当前版本、画格、依赖
  最新版本号与未结议题集合;处理中途产生的页面新版本、史料修订、新增可出版页
  均不混入本批。许可按本次导出日期(`on`)与范围(`scope`)、依据页面引用修订的
  授权快照判断;被排除的页面原因标注对应版本(如 `史料SRC-1r2`)。
- **全程追溯**:`panel_trace` 从任一画格反查采用的史料(按引用修订快照)、
  文字版本、采纳/撤回的意见(含目标版本与转签来源)与决定人。
- **并发与乱序**:所有改稿操作携带 `base_version`/`base_revision` 做乐观并发
  校验(`StaleVersionError`);针对旧版本的迟到点评保留并标记 `outdated`。

## HTTP 接口

`POST /api/<action>`,JSON 请求与响应。action 与领域方法一一对应:
`register_source`、`revise_source`、`create_segment`、`new_segment_version`、
`create_design`、`new_design_version`、`create_page`、`new_page_version`、
`add_panel`、`sign_opinion`、`reassign_opinion`、`adopt_opinion`、
`reject_opinion`、`withdraw_opinion`、`delete_opinion`、`close_issue`、
`submit_objection`、`transition_page`、`page_blockers`、`panel_trace`、
`export_batch`。

`page_blockers` 与 `export_batch` 支持 `on`(YYYY-MM-DD)指定判断日期;
非法日期返回 400。

错误映射:404 对象不存在,403 越权,409 版本冲突(含旧版本意见被采纳/驳回),
400 其他领域规则或请求格式错误。

`fixtures/domain.json` 保存领域名词和状态样例,便于接口联调时保持一致语义。

## 构建检查

```bash
python3 -m compileall -q .
```

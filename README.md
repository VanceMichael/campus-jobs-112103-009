# 校园岗位与培养资料

本目录提供合成招聘来源、岗位信息、技能分类和培养反馈。招聘渠道、岗位性质和来源更新时间各自保留，供离线开发使用。企业、学生、岗位和课程均为虚构对象，项目不需要联网或第三方依赖。

## 服务简介

`careerlink` 是面向高校就业服务中心的岗位采集与培养跟进服务，解决四类问题：

1. **持续采集**：每周从企业招聘页、招聘会、校企合作单位收集岗位与反馈，按 `source_id` 去重，可反复增量导入；旧岗位不被误当在招（有效期 + 陈旧核实）。
2. **同一需求归并**：跨渠道转载按「企业 + 规范化标题 + 地点 + 用工性质」归并为同一招聘需求；同企业不同地点或不同用工性质（实习/正式/短期项目）仍是不同岗位，统计也不混合。标题相似不代表技能相同，技能以各岗位内容为准。
3. **反馈门槛**：只有收到「已录用」反馈才能把岗位标为已招满；面试反馈不足时企业申报招满会被列为 critical 核实项；未收到反馈不等于无人申请。
4. **可解释的培养跟进**：技能分类规则版本化，已发推送冻结当时依据可回看，之后推荐用当前规则；课程调整可追溯到仍有效的企业需求。

## 运行

使用 Python 3.11 或更新版本运行检查：

```sh
python3 -m unittest discover -s tests
```

命令行（输出均为 JSON）：

```sh
# 持续采集（可多次执行，重复来源自动跳过）
python3 -m careerlink ingest store.json fixtures/sources.json fixtures/sources-week2.json

# 按来源 / 状态 / 用工性质 / 有效期查看岗位
python3 -m careerlink postings store.json --source fair-feed-b --state open
python3 -m careerlink postings store.json --as-of 2026-09-05T00:00:00+08:00

# 岗位详情：技能要求、关联培养活动、报名、反馈、待核实项
python3 -m careerlink detail store.json <canonical_id>

# 仍需向企业核实的变化（critical 优先）
python3 -m careerlink verify store.json

# 生成学生推送（快照冻结当前依据）；回看已发推送的当时依据
python3 -m careerlink recommend store.json <canonical_id> --course COURSE-6
python3 -m careerlink push store.json PUSH-0001

# 课程调整依据：哪些仍有效的企业需求支撑新增技能
python3 -m careerlink course-explain store.json COURSE-6

# 汇总统计（实习/正式/短期项目分开计数）
python3 -m careerlink summary store.json
```

所有命令支持 `--now <ISO时间>` 固定“当前时间”，便于复现与演示。

## 来源记录格式

资料位于 `fixtures/sources.json`（第一周）与 `fixtures/sources-week2.json`（第二周，演示持续采集）。每条记录为信封结构：`source_id` / `record_kind` / `collected_at` / `payload`。

| record_kind | 用途 | 关键 payload 字段 |
| --- | --- | --- |
| `job_posting` | 岗位发布/内容更新 | `posting_ref` `employer_ref` `title` `location` `employment_kind` `updated_at` `skills`（可选 `expires_at`） |
| `posting_update` | 企业修订（rev 递增） | `posting_ref` `revision` `state`(open/closed/converted/filled) `effective_at`（可选字段变更、`converted_to`） |
| `course` | 课程（培养活动）版本 | `course_ref` `revision` `skills` `term` |
| `placement_feedback` | 面试/录用反馈 | `posting_ref` `stage` `response_state`（可选 `course_ref` `skill_revision`） |
| `application` | 学生报名（永不删除） | `application_ref` `posting_ref` `student_ref` `applied_at` |
| `skill_rules` | 技能分类规则版本 | `version` `effective_at` `aliases`（原始写法 → 规范名） |

## 主要行为规则

- **修订生效**：按 `revision` 号生效，乱序到达不影响结果；同号不同内容、跳号、关键字段（地点/用工性质）变更都会进入待核实清单。修订先于岗位到达时暂存，岗位出现后补应用。
- **岗位转化**：`converted` + `converted_to` 记录招聘会转正式申请的链路，双向可查；旧报名保留在原岗位下并标注“报名记录保留”。
- **有效期**：`expires_at` 到期自动标记“已过有效期”；在招但超过 30 天未更新的岗位标记“陈旧待核实”，不自动当作在招。
- **推送快照**：每条推送保存当时的技能规则版本、岗位状态、归一化结果与匹配说明；`push` 命令回看当时依据并对照当前信息列出差异。

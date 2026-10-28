> 历史设计文档（2026-10-15前后）。其中功能分类和行为可能已过时，以仓库README、现行架构说明和代码为准。

# 学术论文智能修改Multi-Agent系统

面向长文本学术论文的自适应Multi-Agent修改系统：由Orchestrator做任务规划与异常重规划，
把审查结果**分成三类**（改错`fix` / 拍板`decision` / 建议`advice`），
通过 **可寻址的段落ID + 逐条可接受的补丁** 构建
"问题发现 → 分类 →（必要时）人工拍板 → 提出补丁 → 应用 → 独立复核 → 写回Word"闭环，
并在此之上实现Agent Harness（Checkpoint/Resume、超时、重试、熔断、Trace、Context治理、
成本与延迟预算）与评测闭环（Golden Set + LLM-as-Judge + 回归退化检测）。

技术栈：**LangGraph StateGraph + DeepSeek API + LangChain**；网页版使用
**React + FastAPI + Celery + Redis + PostgreSQL**（部署见 [网页版启动与部署](../web_deployment.md)）。

---

## 一、核心设计

### 1. 每个段落都有稳定ID，改动必须引用ID

论文先被解析成**可寻址的块**（章节标题 / 段落 / 表格 / 列表）：

```text
[H01] 面向小样本场景的图神经网络节点分类方法研究
[H02] 摘要
[P01] (1) 近年来，图神经网络…… (2) 但是实际场景中……      <- 可下钻到句：P01:S2
[T01] | 方法 | Cora | Citeseer | Pubmed |
```

- **稳定ID**：`p3f9a2c1` = 类型前缀 + 内容哈希。改**别的**段落时它不变，
  所以历史补丁依然能对上；本段落被改后ID会变，我们在补丁里回填`span_id_after`，
  把新旧ID连成可回溯的链。
- **可读标签**：`P03` / `H02` / `T01`，只在单次Prompt里使用，模型写标签，
  系统负责解析回稳定ID。
- **句级定位**：补丁可引用`P03` + 第2句 + 逐字引用原文，三重校验后才允许落地。

为什么必须这样：没有ID就只能靠"把原文片段拿去字符串匹配"，
歧义时要么改错地方、要么整条丢掉，也无法"拒绝某一条"或"回溯改了什么"。

### 2. 问题分三类：只有"只能由你定"的才打断你

Audit给出的每一条问题都必须标一个`kind`：

| kind | 含义 | 典型例子 | 处理方式 |
| --- | --- | --- | --- |
| **`fix`** | 原文**确实写错了**，有唯一正确改法 | 错别字、乱码、多余字符、缺失标点、数值明显写错 | Revision **直接改**，不打扰你 |
| **`decision`** | **没有唯一正确答案，只有作者能定** | 同一个名称全文有几种写法（`MoCo` / `MOCO`）该统一成哪种；两处说法打架（正文0.09、参数表0.07）以哪一处为准 | 停下来，**决策闸门**列成a/b/c让你挑，不自动改 |
| **`advice`** | 原文**没写错**，只是可以更好；**以及所有涉及代码的问题** | 可复现性建议、润色；论文里的Python代码清单 | 只写 **Word批注**，不进修改流程 |

一条判据：**"改A还是改B"取决于作者的偏好或实验事实 → `decision`。**
拿不准时一律归`advice`（宁可不改，也不自作主张）。

### 3. 改动以三种批注落到Word

补丁清单和Word批注用同一套文案，作者不回到清单也能看懂：

| 批注类型 | 出现的场合 | 固定文案 |
| --- | --- | --- |
| **修改批注** | 已落实的改动（`fix`、用户拍板后的统一） | `原文：…` / `修改为：…` / `原因是：…` |
| **建议批注** | `advice`（没写错但可以更好，以及代码问题） | `此处可以…` / `原因是：…` |
| **需人工处理** | 确实要改、但工具改不了（引文域、公式、图片、跨单元格…） | `这个问题需要人工修改，因为…。` / `建议修改为：…` / `原因：…` |

### 4. 一次给出全部改动，用户最多被打扰两次

```text
[patch-001] P01（第4句）｜语言表达（高）
  原文：我们在三个公开数据集上进行了实验，实验结果表明本文提出的方法比基线方法要好。
  准备修改为：我们在Cora、Citeseer和Pubmed三个公开数据集上进行了实验……
  原因：补充摘要中缺失的数据集名称、评价指标与关键数值，使结论有据可依
        （数值取自正文表T01，未改动任何实验数据）。   <- 理由由Agent写，用户不用写
```

用户只需要说"要哪些"：回车=全接受，`none`=全拒绝，或输入编号。
被拒绝的改动**不会写进论文**，也不会在下一轮被重复提出（除非用户自己再提）。

### 5. Workflow：正常路径稳定，异常才重新规划

```mermaid
graph TD
    START([START]) --> O[Orchestrator规划]
    O -->|审查| A[Audit发现问题并分级]
    O -->|直接改| R[Revision提补丁]
    O -->|需要拍板| IR[决策闸门]
    O -->|无需处理| P[Report]
    A -->|有"只能由你定"的矛盾| IR
    A -->|其余问题| R
    A -->|无问题 / 审查失败| P
    IR -->|还有要改的| R
    IR -->|全被忽略| P
    R -->|默认：直接应用| AP[Apply改内存文本]
    R -->|PRA_PATCH_GATE=1| G[补丁闸门<br/>逐条接受/拒绝]
    G -->|有接受的| AP
    G -->|一条都没接受| P
    AP --> V[Review独立复核]
    V -->|PASS| P
    V -->|FAIL且未达上限| O
    V -->|FAIL但达上限/预算不足| P
    P -->|写Word前确认一次| FC[写入确认<br/>问题 / 改为什么 / 原因]
    FC --> E([END])
```

- Orchestrator只在两个时间点介入：任务开始、Review FAIL之后（避免每一步都调高成本Planner）。
- 全流程**最多三处人工闸门**，默认只有下面这两处会真正停下来，且都能"直接回车继续"：

  | 闸门 | 什么时候 | 解决什么 | 默认 | 决定存在哪 |
  | --- | --- | --- | --- | --- |
  | **决策闸门**（`issue_review`） | Audit **全部跑完之后** | 只问"没有唯一答案、只能由你定"的：术语该统一成哪种写法、两处数值以哪一处为准 | 开（`PRA_ISSUE_GATE=1`），**但有`decision`问题才停** | `user_rules`（带编号，可撤改） |
  | **补丁闸门**（`patch_review`） | Revision提出补丁之后 | 这批改动逐条接不接受 | **关**（`PRA_PATCH_GATE=0`）→ 直接应用 | 补丁状态 + `rejected_memory` |
  | **写入确认**（`final_confirm`） | 写Word **之前** | 本次**全部**改动（跨轮次）要不要写入副本 | 开（`PRA_FINAL_CONFIRM=1`） | `excluded_patch_ids` |

- **为什么决策闸门放在"Audit全部跑完之后"**：如果分块一结束就问，你是在只看到一部分问题的情况下做决定，
  整篇又发现新矛盾 → 只能再问你一次。放在所有环节跑完之后，你**只被问一次**。
  人工判断**不在分块之后，也不在整篇之后，而是在Audit整条流水线跑完之后**。
- 用户拒绝的、明确说不改的范围，**不计入"未完成"**，Review不会因此判fail。

### 6. 用户说的"点"存在哪里

统一存在State的 **`user_rules`**（带编号、可撤改、跨轮次有效），并落盘到
`data/runs/<run_id>/state.json`、`session.json`，随Checkpoint一起持久化：

```json
[
  {"id": "R1", "kind": "rule",   "text": "全文统一把「土豆」写成「马铃薯」",
   "status": "active",  "created_turn": 1},
  {"id": "R2", "kind": "ignore", "text": "不改 P13：消融实验…",
   "signature": {"span_label": "P13", "problem": "消融实验…"},
   "status": "active",  "created_turn": 1}
]
```

- **要求（rule）**：开局`--rule`、决策闸门、多轮追问里都能加；
- **不改（ignore）**：决策闸门里勾一下就有了；存的是**指纹**（位置＋分类＋问题摘要），
  所以下一轮重新审查时问题编号变了也认得出，不会再问一遍；
- 用户可以随时改主意：`取消R1`、`R1改成 ……`、`记住：……`（多轮追问里直接说）；
- 这份清单会进 **四个Agent的上下文**，尤其是Audit（不去报用户已经说不用管的问题）
  和Review（不把用户明确不改的东西算作"未完成"）。

这就是本项目需要的全部"记忆"：一次任务内的对话上下文 + Checkpoint，不需要额外的长期记忆系统。

### 7. 每次Agent调用都有输出

控制台每调用一次就有可读的一行（含耗时、Token与返回内容预览）：

```text
[orchestrator] initial_plan完成：5618ms · 1664 tokens
    → {"plan_kind": "full_review", "next_action": "audit", ...}
[audit] chunk_1完成：3431ms · 2590 tokens
[revision] batch_1完成：6263ms · 4076 tokens
[review] verify完成：2643ms · 5956 tokens
```

同时把**完整Prompt与返回**写进`data/runs/<run_id>/llm_calls.jsonl`
（默认开启，`PRA_LOG_LLM=0`关闭，`PRA_LOG_PROMPT_CHARS`可限制长度），
排查"这个Agent到底看到了什么"时直接翻这个文件。

### 8. 多轮会话

```python
result = harness.start("请全面检查并修改这篇论文。", document)  # 第 1 轮
result = harness.resume({"excluded_ids": ["patch-003"]})  # 写入确认
result = harness.follow_up("取消 R1；把引言里关于小样本困难的段落写得更具体")  # 第 2 轮
```

- 复用同一个`thread_id`，已接受的改动、被拒绝的改动、审查结果都在State里，模型不会"忘了上下文"；
- 被拒绝的改动、被忽略的问题都会出现在下一轮的Prompt里，避免重复打扰；
- 这一轮的话里可以夹"改要求"的指令（`记住：…` / `取消R1` / `R1改成 …`）；
- 只重置本轮的临时字段（迭代计数、计划、上一轮复核结论）。

### 9. State与Context分离

```text
完整任务状态（State）
        ↓  Context Manager（按Agent字段白名单 + 预算裁剪）
当前Agent真正需要看到的信息
        ↓
      LLM
```

- 分块**按段落边界**切（不是按字符），所以每个分块里的ID都是完整可引用的；
- Revision只对"被点名"的段落标出句子编号，其它块只给标签，省Token；
- 超预算时按优先级"头尾保留 + 中间省略"。

### 10. 成本与延迟预算

| 指标 | 配置项 | 代码默认 |
| --- | --- | --- |
| 每任务token上限 | `PRA_BUDGET_RUN_TOKENS` | 400000（**0 = 不限**） |
| 每轮token上限 | `PRA_BUDGET_TURN_TOKENS` | 150000（0 = 不限） |
| 墙钟上限 | `PRA_BUDGET_RUN_SECONDS` | 1800 |
| 单Agent调用次数上限 | `PRA_BUDGET_CALLS_PER_AGENT` | 40（0 = 不限） |
| 单次调用超时 | `PRA_AGENT_TIMEOUT` | 180 |
| 并发任务数（服务层） | `PRA_MAX_CONCURRENT_RUNS` | 2 |

> 当前仓库里的`.env`把三个token/次数上限都设成了`0`（**不设限额**，按你的要求）。
> 即便如此，防死循环的`PRA_MAX_ITERATIONS`（默认3）和`PRA_RECURSION_LIMIT`（默认60）依然生效。

超预算不是崩溃，而是**降级**：Audit不再开新分块、Revision不再开新批次、
Review直接收敛出报告（并标注"结论仅基于确定性检查"）。用量从trace实时汇总，
报告里会给出实际消耗、各Agent调用次数与降级原因。

**成本量级**（供估算，随模型定价与论文长度变化）：一篇1.5万字上下的论文跑一轮"全面检查并修改"，
主要开销在Audit（分块并发 + 专名核验 + 定向对照 + 整篇兜底）与Revision/Review，
合计大致在**十几万token量级**；是否开整篇兜底（见4.3）与是否进入第二轮复核，会让数字上下浮动。
先用`--inspect`看清结构、再用日志里的trace汇总核对实际用量。

---

## 二、目录结构

```text
paper_revision_agent/
├── main.py                     # 命令行入口（补丁勾选 + 多轮追问）
├── test_llm.py                 # DeepSeek连通性自检
├── app/
│   ├── config.py               # 全局配置（模型/超时/重试/熔断/预算/闸门/路径）
│   ├── document/               # ★ 文档模型层
│   │   ├── spans.py            #   段落/句子稳定ID、按块分块、带ID渲染
│   │   ├── patches.py          #   补丁对象、定位校验、逐条决定、一次性应用
│   │   ├── docx_model.py       # ★ 读Word：文字 ↔ XML锚点一一对应（含公式/图片/表格/代码）
│   │   └── docx_redline.py     # ★ 写回Word：原生修订痕迹（w:ins/w:del）+ 批注
│   ├── state/schema.py         # PaperState定义
│   ├── agents/                 # 4个Agent + Report
│   │   ├── prompts.py          #   所有Prompt模板
│   │   ├── orchestrator.py     #   规划 + 重规划（含规则兜底）
│   │   ├── audit.py            #   分块并发审查 + 专名核验 + 定向对照 + 跨批对齐 + 整篇兜底
│   │   ├── revision.py         #   一次提出本轮全部补丁 + 术语统一（程序执行）
│   │   ├── review.py           #   独立复核 + 确定性检查
│   │   └── report.py           #   汇总报告与落盘
│   ├── workflow/
│   │   ├── graph.py            # StateGraph装配 + PostgreSQL/SQLite Checkpointer
│   │   ├── nodes.py            # 节点（决策闸门 / 补丁闸门 / 写入确认 三个interrupt、apply）
│   │   ├── router.py           # 条件边与各类熔断
│   │   └── harness.py          # start / resume / follow_up / auto-approve
│   ├── utils/                  # LLM封装、JSON解析、文本、Context、重试熔断、Trace、预算、
│   │                           #   用户要求与决定（rules.py）、一致性扫描（consistency.py）、
│   │                           #   文件加载（loaders.py）
│   ├── service/runs.py         # ★ 与框架无关的任务服务层（后台线程 + 状态落盘）
│   └── ui/streamlit_app.py     # 可选界面
├── evals/                      # 评测闭环（Golden Set / LLM-as-Judge / 回归）
├── data/
│   ├── samples/sample_paper.md
│   ├── golden/golden_set.jsonl
│   ├── runs/                   # 运行产物（报告 / 补丁 / Trace / 状态快照）
│   └── checkpoints/            # SQLite Checkpoint（重启后继续）
└── tests/                      # 317个离线测试（unittest，可无网络运行）
```

---

## 三、快速开始

网页版已实现React + FastAPI + Redis + Celery + PostgreSQL的邀请制内测闭环。
启动、账号、额度、暂停恢复和真实容器验收见 [网页部署说明](../web_deployment.md)。
网页版支持DOCX、DOC、Word兼容的WPS、RTF、ODT，后台统一转为DOCX后处理；
交付带修订痕迹的DOCX和报告，原稿单独保留。旧式专有WPS、加密和宏文件不支持；
转换可能影响复杂排版，详见 [格式与转换限制](../web_deployment.md#输入格式与转换限制)。
CLI / Streamlit的文件入口仍以第4节的格式表为准。

Prompt当前原文见 [提示词导出](../prompts_current.md)，待确认的调整方向见
[Prompt重构提案](../prompt_redesign.md)（尚未切换运行版本）。

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入 DEEPSEEK_API_KEY

python test_llm.py                                   # 连通性自检
python main.py --show-graph                          # 查看 Workflow 结构

# 先看"论文被读成了什么"（不调用模型、不花钱）
python main.py --file "D:\我的论文.docx" --inspect

# 直接给文件：支持 .docx / .pdf / .md / .txt（路径带空格要用引号）
# 给 .docx 时，改完会在 data/runs/<run_id>/ 另存一份带修订痕迹的 Word 副本
python main.py --file "D:\我的论文.docx" --request "全面检查并修改这篇论文"
python main.py --file data/samples/sample_paper.md --request "全面检查并修改这篇论文" \
    --rule "全文统一把「土豆」写成「马铃薯」"
python main.py --file data/samples/sample_paper.docx --auto-approve   # 无人值守

# 全面检查并修改：中途你只会被问两处——
# ① Audit 全部跑完后，把「只能由你定」的矛盾（术语统一成哪种写法、数值以哪处为准）
#    列成 a/b/c 选项问你；写错的、可以更好的都不会打扰你。
# ② 写入 Word 之前，把本次全部改动列成「问题 / 改为什么 / 原因」让你最后剔除几处。
python main.py --file data/samples/sample_paper.md --request "全面检查并修改这篇论文" \
    --rule "全文统一把「土豆」写成「马铃薯」"

# 无人值守（全部接受）
python main.py --file data/samples/sample_paper.md --auto-approve --constraint "不要修改实验结果"

# 只改局部，跳过全文审查
python main.py --text "摘要太啰嗦" --request "只修改摘要，不要审查全文"
```

程序化调用：

```python
# 文件 -> 文本（.docx / .pdf / .md / .txt，自动识别编码）
from app.utils.loaders import load_document, load_bytes

loaded = load_document("D:/我的论文.docx")
print(loaded.describe())  # docx，2060 字符，34 段
document = loaded.text

# 界面/接口上传是字节流：
# loaded = load_bytes(uploaded.getvalue(), filename=uploaded.name)
```

```python
from app.workflow.harness import Harness

harness = Harness()
result = harness.start("全面检查并修改", document)
harness.start("全面检查并修改", document, rules=["全文统一把「土豆」写成「马铃薯」"])
while result.is_interrupted:
    request = result.pending_request
    if request["type"] == "issue_review":
        payload = {"rules": ["温度系数以 0.07 为准"]}  # 在 a/b/c 里挑，或写自己的判断
    elif request["type"] == "patch_review":
        patch_ids = [p["patch_id"] for p in request["patches"]]
        payload = {"accepted_ids": patch_ids[:3]}  # 只接受前 3 条
    else:  # final_confirm
        payload = {"excluded_ids": []}  # 回车 = 全部写入
    result = harness.resume(payload)

# 多轮：可以顺手改要求（记住：… / 取消 R1 / R1 改成 …）
result = harness.follow_up("取消 R1；把引言里关于小样本困难的段落写得更具体")
print([(rule["id"], rule["kind"], rule["text"]) for rule in result.state["user_rules"]])
print(result.final_report, result.revised_document)
```

---

## 四、上传论文（文件支持情况）

| 格式 | 支持情况 | 说明 |
| --- | --- | --- |
| `.docx` | ✅ 推荐 | 保留标题层级（Heading 1/2/3 → `#/##/###`）、列表与表格（→ Markdown表格），因此标题/段落/表格都会成为**带ID的可寻址块**；改完还能**写回Word原文件**（见4.6） |
| `.pdf` | ✅ 可用 | 优先pypdf，其次pdfplumber；**扫描版没有文字层**时会提示需要OCR；公式/分栏版式可能丢失，关键段落建议核对 |
| `.md` / `.txt` | ✅ | 自动识别UTF-8 / UTF-8-BOM / GBK / GB18030 / Big5（记事本存的ANSI中文不会乱码） |
| `.doc` / `.wps` / `.caj` | ❌ | 会给出可执行的提示（另存为 .docx或导出PDF） |

依赖：`python-docx`、`pypdf`、`pdfplumber`（在`requirements-extras.txt`里）。
没装也能跑：docx会退回内置的zipfile + XML解析器；PDF会明确告诉你装`pypdf`
或改用 .docx。

仓库里放了一份现成的Word样例`data/samples/sample_paper.docx`，可以直接拿来试上传。

### 4.1结构自检（强烈建议先跑一次）

```bash
python main.py --file "D:\我的论文.docx" --inspect
```

输出示例：

```
字符数：35085块数：749
分区：正文627，目录64，参考文献19，封面18，摘要10，致谢7
标题154个，表格0个，按噪声跳过51个
默认送审（摘要+正文）：588块（可用PRA_AUDIT_SCOPE=body+refs / all调整）
```

### 4.2结构是怎么还回来的

论文不是"一堆文字"，而是有分区的：

| 处理 | 说明 |
| --- | --- |
| 标题识别 | 除了Markdown标题，还认`摘    要`（人为拉开字距）、`目 录`、`致 谢`、`1.1 xxx`、`第一章`等 |
| 文档分区 | 自动区分 封面 / 前置材料 / 目录 / 摘要 / 正文 / 参考文献 / 致谢 / 附录；**封面、目录、承诺页、致谢默认不审**（`PRA_AUDIT_SCOPE=body`，可改`body+refs` / `all`） |
| 目录去噪 | 目录条目（"1.1研究背景 …… 1"）即使长得像章节标题，也不会混进正文 |
| 噪声跳过 | 页码、签名行、目录行会被标出并跳过（报告里给出数量） |
| **公式保留** | Word公式是OMML对象：现在会提取成`【公式：…】`。以前完全没取，模型看到"公式如下："后面是空的，就会误报"公式缺失" |
| 编码兼容 | UTF-8 / GBK / GB18030 / Big5自动识别 |

> PDF的公式与版式本质上拿不到（没有文字层），**能传 .docx就传 .docx**。

### 4.3审查怎么做到"不漏又不吵"

Audit同时用**四路**覆盖，最后收敛成一份清单：

| 机制 | 效果 |
| --- | --- |
| **① 分块并发审查** | 唯一的**逐段全覆盖**：按段落边界切块，`ThreadPoolExecutor`并行跑（`PRA_AUDIT_CONCURRENCY`，默认6）。**按分块序号归并结果**，所以issue编号不随网络抖动变化 |
| **② 专名核验** | 单独一次调用，拿全文专有名词清单判断**写法本身**对不对（`阿里云ESC`→`ECS`）。靠的是世界知识，正确写法不必在文中出现过 |
| **③ 定向对照**（主力） | 把**要互相对照的两份材料摆到一起**再查矛盾，而不是整篇送审：<br/>· 摘要 × 结论（数值 / 结论强度 / 方法数量）<br/>· 摘要承诺 × 各章产出（说了做的事有没有对应章节）<br/>· 正文数字 × 表格（同一指标数值是否一致）<br/>三组材料合计约为整篇的一半，三组独立并发 |
| **④ 整篇兜底** | 整篇一次送审，只查**跨章节矛盾**。默认门槛`PRA_AUDIT_WHOLE_PAPER_SMALL_DOC=20000`，所以常规长度论文（正文 ≲2万token）**每轮都会跑一次**；只有整篇超过门槛时才退化为"其它环节零发现才跑" |
| **⑤ 跨批对齐** | 四路候选**全部**摆到一起做一次全局收敛：合并"说的是同一件事"的重复项、指出互相打架的条目（`PRA_AUDIT_CROSS_CHECK`）。模型只输出**编号分组**，真正的合并由代码确定性完成，模型不能改写已有结论 |
| **⑥ 逐字核对 + 合并同类 + 严重程度门槛** | evidence在对应段落里找不到 → 直接丢弃（防幻觉、防错位）；同分类且高度相似的条目合并成一条并列出所有涉及段落；默认丢掉`low` |

另外几层精度控制：

| 机制 | 效果 |
| --- | --- |
| 证据逐字核对 | evidence必须逐字出现在对应段落，否则丢弃（报告里的`dropped_unverifiable`是被丢掉的条数） |
| 同类系统性问题合并 | "参考文献格式不统一"在十几处出现 → **只报一条**，并列出涉及段落（"共12处，另见P581、P583…"） |
| **一个问题可以带多个标号** | "不重复报"指的是同一问题只写一条，**不是**标号只能有一个；`span_labels`会列全所有位置 |
| 取消"每块5条"配额 | 改为"没有就返回空数组"，每块最多3条 |
| 代码段一律不碰 | `looks_like_code()`判据：多行程序清单（含`def` / `for` / `if` / `self.`）不出补丁，**连建议也只降级成批注** |
| 回写偏移修正 | 先把分块文本归一化再算偏移，杜绝"补丁位置整体错位" |

#### Audit的实际执行顺序

```text
论文 → 切成可寻址块 → 按段落边界分组
     → ⓪ 术语变体扫描（确定性）：同一个词多种写法 → 程序直接生成一条decision问题
     → ① 并发逐块审查（ThreadPoolExecutor，PRA_AUDIT_CONCURRENCY）
     → ② 专名核验（一次调用，判断写法本身对不对）
     → ③ 定向对照（摘要×结论 / 摘要承诺×各章产出 / 正文数字×表格，三组独立并发）
     → ④ 整篇兜底（正文 ≤ 门槛就整篇送；否则"零发现"时兜底一刀）
     → ⑤ 跨批对齐（把前面几路的全部候选摆到一起，合并重复 / 指出互相矛盾）
     → ⑥ 逐字核对证据 + 合并同类 + 严重程度取舍
     → 一份清单 →（有decision时）决策闸门
```

**用户只会看到一份合并后的清单、只会被问一次。**

为什么①和③要并存、而不是只留一个：
①**逐段覆盖**便宜、定位准，但每块只看得到自己那段，看不到"摘要说95%、结论说89%"；
③把该对照的两份材料**摆在一起**，准且便宜，但它只覆盖我们枚举的那几组配对；
④整篇兜底是唯一"不依赖我们枚举配对范围"的覆盖，代价是最贵——所以长文里它退居兜底。
它们**互补而非重复**：整篇送审替代不了分块（整篇看的是跨段矛盾，看不细段落里的错别字、病句），
分块也替代不了整篇（分块天然看不到首尾对照）。

②和③都是**可降级**的：模型输出解析不了就跳过并记进`audit_meta`
（`cross_check_error` / `whole_paper_error`），绝不因此丢掉分块已经查出的结论。

### 4.4术语统一：口径由人定，替换由程序做

`MoCo` / `MOCO`这类"同一个名称多种写法"是典型的高频问题，处理方式刻意分成两半：

1. **发现**：`find_naming_variants()`扫描全文（**排除代码段**）→ 由**程序**直接生成
   **一条`decision`问题**，带上**全部位置** `span_labels`与由变体生成的可执行`options`
   （"全文统一为`MOCO`" / "全文统一为`MoCo`"…）。
   为什么不让模型报这条：位置要穷尽（一篇论文40+ 处），模型在长文里列不全，
   而且模型给的自由文本没法可靠地拿去替换。
2. **决定**：用户在决策闸门里选一个口径（a/b/c）。
3. **执行**：`_unification_patches()`拿用户选定的口径**由程序全文替换**——不走LLM、
   跳过代码段、`quote`自动扩窗保证在段落内唯一。一次"全文统一MoCo→MOCO"会产生几十条补丁
   （Word里要**逐处留痕**，可逐条接受/拒绝），但在**写入确认清单里会合并成一条**展示。

> 摘要不算"第一次出现"：简称的全称只在**正文**（第一章之后）第一次出现处标注一次，
> 中英文摘要里都直接写简称，不重复标全称。

### 4.5审计到底审什么（以及审不到的）

| 内容 | 会审吗 | 说明 |
| --- | --- | --- |
| 正文段落、标题 | ✅ | 带段落ID（`[P03]`）逐块送审 |
| **表格内容** | ✅ | 表格转成Markdown表格后整块送审：格子里的文字、数字、方法名都会被看到，能发现"表格数值与正文结论不一致""表头术语与正文不一致" |
| **公式** | ⚠️ 只看到文字 | Word公式（OMML）会被拍平成`【公式：…】`。真分数`(a+b)/(c-d)`拍平后是`a+bc-d`——**分数/上下标/矩阵的结构会丢**。所以能发现"公式没解释、符号没定义、正文说'如式(3-1)'却没编号"，**不能**判断推导对不对 |
| **图 / 图表（图片）** | ⚠️ 只看到文字 | 图片是二进制对象，模型看不到像素。它会审图题、图注、正文里对图的描述与数值（如"如图3.1所示"是否有对应图），**不能**判断图里曲线、坐标轴、清晰度、配色 |
| 代码段 | ⚠️ 只写批注 | 软回车还原成换行，代码不会被糊成一行；但**代码一律不出补丁、只作建议** |
| 参考文献 | ❌ 默认不审 | `PRA_AUDIT_SCOPE=body+refs`才审 |
| 封面 / 诚信承诺 / 目录 / 致谢 / 附录 | ❌ 默认不审 | `PRA_AUDIT_SCOPE=all`才审（默认不审是不让排版内容被当成论文问题） |
| 页眉页脚、页码、批注 | ❌ | 不在正文里，读不到 |

审不到的部分主要来自几个可调的预算：

| 限制 | 默认值 | 影响 | 怎么调 |
| --- | --- | --- | --- |
| 单批大小 | `PRA_AUDIT_CHUNK_TOKENS=1500` | 每批约1500 token（中文1字 ≈ 1 token） | 调大以减少批数 |
| 批数上限 | `PRA_AUDIT_MAX_CHUNKS=24` | **最多只审前24批 ≈ 3.6万字**；超长论文后半段不会被审（报告与日志会明确写出来） | 调大以覆盖全文 |
| 问题上限 | `PRA_AUDIT_MAX_ISSUES=15` | 合并后进入清单的问题数 | 按需要调 |
| 严重程度门槛 | `PRA_AUDIT_MIN_SEVERITY=medium` | `low`（可改可不改）直接丢弃 | 改`low`放开 |
| 正文数字句上限 | `PRA_AUDIT_COMPARE_SENTENCES=40` | "正文数字 × 表格"这一组最多取多少句；**超出会打印出来**，不静默 | 调大覆盖更多数字句 |
| 整篇兜底门槛 | `PRA_AUDIT_WHOLE_PAPER_SMALL_DOC=20000` | 正文 ≤ 它 → 每轮整篇送审一次 | 调小可省token（代价是长文要靠"零发现"才兜底） |
| 整篇兜底视野 | `PRA_AUDIT_WHOLE_PAPER_TOKENS=24000` | 整篇一次送审、超过就按"头尾保留"截断。**它没有分块兜底，别设成0** | 按模型上下文窗口调 |
| 跨批对齐视野 | `PRA_AUDIT_CROSS_CHECK_TOKENS=0`（不限） | 候选清单全量送审、默认不截断（否则会漏掉被砍掉的那些候选） | 同上 |
| Review视野 | `PRA_CTX_REVIEW=8000` | 复核看的是"全文头部 + 全文尾部 + 中间省略"的视图 | 调大 |

> 确定性检查（术语 / 专有名词一致性、重复段落、补丁定位、被拒绝的改动是否泄漏、代码段判定）是**全文**跑的，不受上面这些上限影响。
> 所有"靠出现次数猜"的扫描（`find_near_miss_names`、`find_cross_section_gaps`）都**已停用**：
> `GNN`/`KNN`、`DIR`/`DIM`是真实存在的独立术语，出现次数少不能说明写错，实测只会制造噪音。

### 4.6改动写回Word（修订痕迹 + 批注）

前面几节解决的是"改得对不对"，这一节解决**"改在哪儿"**：
上传`.docx`时，被接受的改动会写回Word原文件，另存一份副本
`data/runs/<run_id>/revised_document.docx`，**原文件一个字都不动**。

打开副本你会看到：

- 每一处改动都是Word **原生修订痕迹**（`<w:ins>` / `<w:del>`），
  可以逐条接受或拒绝，也可以一键全部接受；
- 每处改动带一条 **Word批注**（格式见1.3）；
- 字体、字号、编号、页眉页脚、图、表、公式**原样保留**——
  只动被改的那几个字，其余内容连XML都没碰。

为什么能做到：读取时除了文字，还记下了"每个字在`document.xml`里对应哪个节点、
从第几个字到第几个字"（`app/document/docx_model.py`）；回写时按这份对应关系
定位到run级别，只切被改动的那一小段（`app/document/docx_redline.py`）。

**不确定就不改**。下面这些情况回写层会**拒绝**，改成"需人工处理"批注留在原位
（同时列在报告的"需要在Word里人工处理的改动"一节）：

| 情况 | 批注里写的原因 |
| --- | --- |
| 改动要跨越**引文域**（要同步后续文献引用） | `这条改动需要同步后面文献引用，需要人工处理` |
| 改动跨了多个表格单元格 | Markdown表格里的竖线在Word里根本不存在，无法一一对应 |
| 半句改动却把公式圈进了范围 | 公式是OMML对象，不是文字；硬改会破坏公式 |
| 改动范围里有图片等图形对象，且不是整段重写 | 宁可不动，也不要把图删掉 |
| 原文与补丁记的"修改前"对不上 | 说明文档在中间被别的程序改过，定位不可靠 |

能自动处理的三种情况是：**整段重写**（连公式一起替换，前提是这段没有图片）、
**句内替换**（按run切开，只切被改的那几个字）、**表格单元格内替换**。
位于`，`与下一个词之间的**插入类**改动（如`……Suricata[2]，`后插入"这类系统"）也支持。

多轮会话也支持：回写不是"把最后一次的文字翻译成Word"，而是
**原文件 + 第1轮改动 + 第2轮改动……** 逐轮重放，
所以第二轮改的可能是第一轮刚插进去的文字，链条照样完整。

开关（`.env`）：

```bash
PRA_DOCX_OUTPUT=1                 # 0 = 只输出 Markdown，不生成 Word 副本
PRA_REDLINE_AUTHOR=论文修改助手    # Word 里显示的"谁改的/谁写的批注"
PRA_REDLINE_COMMENTS=1            # 0 = 只留修订痕迹，不写批注
```

依赖：`lxml`（`pip install lxml`，或`pip install python-docx`会一起装上）。
没装不影响修改流程，只是退回"只输出Markdown"，并在读取时就提示你。

---

## 五、产物

每次运行写入`data/runs/<run_id>/`：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 完整报告：问题清单、逐条补丁（含理由与决定）、复核结论、预算用量 |
| `revised_document.md` | 修改后的论文（只包含被接受的改动） |
| `revised_document.docx` | **把改动写回Word的副本**：原生修订痕迹 + 批注，原文件不动（上传 .docx时才有） |
| `original_document.md` | 原始论文 |
| `edits.json` | 各轮补丁明细与新旧段落ID |
| `state.json` | 状态快照（重启后重建"待确认补丁"面板） |
| `session.json` | 服务层任务状态（等待人工 / 已完成等），供Web端回访 |
| `trace.jsonl` | 每个Agent的耗时/重试/Token，逐行追加 |
| `trace_summary.json` | 上述trace的汇总 |
| `llm_calls.jsonl` | 每次模型调用的完整Prompt与返回（`PRA_LOG_LLM=0`可关） |

---

## 六、Harness：让长任务跑得稳

| 能力 | 实现位置 | 说明 |
| --- | --- | --- |
| Checkpoint / Resume | `workflow/graph.py`、`harness.py` | 生产使用PostgreSQL `PostgresSaver`，本地可用SQLite；完整人工请求原样持久化，重启后按真实中断恢复 |
| 异常隔离 | `workflow/nodes.py` | 单Agent失败只降级该节点 |
| Timeout / Retry / 熔断 | `utils/resilience.py` | 每个Agent独立统计，连续失败到阈值后快速失败 |
| 迭代上限 | `workflow/router.py` | Review FAIL循环到上限即输出当前最优结果 |
| Trace | `utils/tracing.py` | 从trace文件汇总，重启不丢 |
| 预算 | `utils/budget.py` | token / 时间 / 调用次数，超限降级而不是崩 |
| Context治理 | `utils/context.py` | 按Agent白名单裁剪，长文分块按段落边界 |

部署前设置`PRA_CHECKPOINT_BACKEND=postgres`和`PRA_CHECKPOINT_URL`，安装
`requirements-web.txt`，执行一次`python main.py --setup-checkpoint`。
PostgreSQL失败不会退回内存。切换后端不自动迁移既有SQLite暂停任务。
Streamlit、CLI、服务层统一按`request.type`分发；完整请求、选项和最终剔除结果会持久化。
协议、恢复策略与PostgreSQL集成测试见 [部署与交互协议](../interaction_protocol.md)。

---

## 七、评测闭环

```bash
python -m evals.runner --limit 5 --tag prompt-v2
python -m evals.regression 基线.json 新版本.json --threshold 0.2   # 退化则退出码 1
```

四个维度：**语法 / 原意 / 学术规范 / 降重**。结果会带系统指纹，指纹不一致会提示"仅供参考"。

---

## 八、测试

```bash
python -m unittest discover -s tests -t .      # 离线假 LLM；PostgreSQL 实例测试按配置启用
pytest -q                                      # 也可以
```

覆盖：段落/句子ID稳定性、补丁定位与歧义处理、逐条接受/拒绝、一次性应用与重叠检测、
问题忽略的跨轮记忆、要求的新增/修改/撤销、模型调用落盘、
docx/pdf/txt加载与编码兼容（含内置docx解析器）、
结构分区（封面/目录/摘要/正文/参考文献/致谢）、Audit精度（证据核对/同类合并/严重程度门槛）、
**三类问题（fix/decision/advice）**、**术语变体扫描与程序化统一**、**代码段只批注不改**、
跨批次术语一致性、删除类修改、OMML公式提取、
Audit分块并发（结果按分块序号归并）与跨批对齐、整篇兜底、新环节失败时降级、
新旧ID回溯、人工暂停与恢复（含模拟重启）、多轮会话与拒绝记忆、预算降级、
各类熔断与路由、报告与评测回归。

---

## 九、常见问题

**Q：为什么要给段落编ID？直接改不行吗？**
A：没有ID就无法"定位、拒绝、回溯"：模型说改了哪里、用户拒绝了哪一条、
这版论文和上一版对应哪一段，全都说不清。ID是逐条审阅与多轮会话的前提。

**Q：为什么只有"前后矛盾"才停下来问我？**
A：因为问题分了三类（见1.2）：写错的（`fix`）有唯一正确改法，直接改；
可以更好的（`advice`）只写批注；真正卡住人的只有"两处说法打架、以哪一处为准"——
实验事实在作者手上，模型看不到，只能问你。

**Q：为什么让"出现次数"决定不了对不对？**
A：出现次数少 ≠ 写错。`GNN`/`KNN`/`CNN`/`RNN`是四个独立术语，
判断一个缩写对不对，只能把它放回**那句话**里读（"尝试运用Transformer、GNN等**网络结构**"成立；
换成KNN反而不通）。所有"靠次数猜"的扫描都已停用。

**Q：术语统一为什么要用户先选，而不是自动挑一个？**
A：`MoCo`和`MOCO`都是合法写法，没有客观对错，只能由作者定口径。
定了口径之后，全文替换由**程序**完成（穷尽、确定、跳过代码），不再交给模型。

**Q：用户说的"土豆都写成马铃薯"这种点存在哪里？**
A：存在State的`user_rules`里（带编号R1/R2、可撤改），随Checkpoint与
`state.json`持久化，并注入四个Agent的上下文。它只在本次任务内有效——
这正是一次任务里的多轮对话，不需要额外的长期记忆系统。

**Q：为什么理由不是用户写？**
A：理由由Revision Agent写（改动内容 + 依据），用户只做勾选/剔除。
需要拒绝时可以一键拒绝，不必打字。

**Q：为什么改动要一次性给出来？**
A：避免"提一条问一次"的来回确认。Revision一次产出本轮全部补丁，
用户在同一张清单上一次性处理，然后一次应用。

**Q：为什么不做长期Memory？**
A：这是单论文、单任务的有限生命周期场景；跨任务的用户偏好属于未来扩展。
当前用State + Checkpoint保存任务上下文（含已接受/已拒绝的改动）。

---

## 十、后端服务化：逐环节选什么

这一节回答"要做成一个很多人同时提交论文的网站，每个环节该用什么、有哪几种、为什么选它"。

网页版已按此路线落地，实际服务代码为`app/web/`：任务使用Celery prefork，
业务状态与完整人工请求保存在PostgreSQL，Checkpoint使用PostgresSaver；
进度采用短轮询，尚未实现SSE。`app/service/runs.py`的线程执行和`session.json`
仅保留给CLI / Streamlit本地模式。以下表格保留了方案比较，实际部署以 [网页版文档](../web_deployment.md) 为准。

**先把Agent内核的形态定死**：这个Agent是**同步阻塞**的——每个Agent内部顺序调用LLM，
只有Audit的分块用`ThreadPoolExecutor`并发（等的是网络，不是CPU）。改成`async`的收益很有限
（瓶颈是LLM的网络等待与API配额，不是本地IO），但成本是：`python-docx` / `lxml` / `pypdf`
这些同步库都要`to_thread`包一层，调试复杂度明显上升。**结论：内核保持同步，只在Web接入层用async。**

| 环节 | 需要什么 | 可选方案 | 本项目选的 | 为什么选它 | 为什么不选另一种 |
| --- | --- | --- | --- | --- | --- |
| **Web接入层** | 接收上传、立刻返回`run_id`、推送进度、回传人工选择 | FastAPI(async) / Flask / Django / 只留CLI | **FastAPI**（async） | 原生async + SSE + Pydantic校验；和LangGraph的等待型IO天然契合；服务层`app/service/runs.py`与框架无关，FastAPI只是薄适配 | Flask是同步WSGI，长连接/SSE要额外方案；Django对这个体量太重 |
| **任务执行** | 一次修改要几十秒到几分钟，**不能阻塞HTTP** | 后台线程 / Celery等队列 / `asyncio.create_task` | 现在：**后台线程**；规模化：**Celery** | Harness是同步阻塞代码，放进Celery worker最省事；**每进程跑一个run**，正好隔离内存与崩溃 | 直接在HTTP请求里同步跑会超时；用`asyncio.create_task`跑同步内核会**阻塞事件循环**（除非再用`to_thread`包一层） |
| **关系数据库** | 任务、闸门选择、补丁、审计轨迹要持久化、可查询 | PostgreSQL + psycopg3 / MySQL / SQLite | **PostgreSQL + SQLAlchemy + psycopg3（同步）** | psycopg3是psycopg2的现代重写：同一套驱动既支持sync又支持async、支持服务端参数绑定与pipeline、类型适配更好；Celery侧是同步调用，用psycopg3同步模式最顺 | `psycopg2`只sync、已进入维护模式；`asyncpg`只async，和同步的Celery worker不搭（要维护两套SQL层）；SQLite扛不住多人并发写 |
| **缓存 / 消息** | 进度广播、分布式锁、全局LLM令牌桶 | Redis / RabbitMQ / Postgres LISTEN·NOTIFY | **Redis** | 一处解决三件事：Celery broker + pub/sub广播 + 限流计数 | RabbitMQ功能更全但运维更重；Postgres `LISTEN/NOTIFY`能做广播，但拿它做高频计数限流不合适 |
| **任务队列** | 削峰、重试、多机水平扩展 | Celery / RQ / Dramatiq / Arq | **Celery**（prefork） | prefork **每进程一个run**，正好匹配"同步跑Harness、进程内互不干扰"；重试 / 路由 / 监控生态成熟，可配Redis或RabbitMQ | RQ / Dramatiq更轻但生态小；Arq是async，而内核是同步 |
| **人工闸门（跨请求）** | 用户可能隔几分钟、甚至第二天才回来确认；服务重启不能丢 | LangGraph Checkpoint（SQLite / Postgres）/ 应用侧run状态 / 内存 | 现在：**SQLite Checkpoint + `session.json`**；多机：**Postgres Checkpoint** | 闸门做成**服务端可回访的状态**：中断点存Checkpoint，前端按`run_id`取回待确认请求，刷新/换浏览器/重启都能续 | 存内存里重启即丢；存前端不可信（用户能改）；只存文件在多机下不共享 |
| **全局LLM限流** | DeepSeek有速率/并发上限，多用户同时跑会撞 | Redis全局令牌桶 / 每进程信号量 / 不限 | **Redis全局令牌桶 + 每进程并发上限`PRA_MAX_CONCURRENT_RUNS`** | **真正的瓶颈是全局LLM配额，不是CPU**：多机部署时只有全局计数才管用 | 只做每进程信号量，多机时会乘以机器数，照样撞限流 |

几条落地要点：

- **异步用在哪**：只有Web接入层（FastAPI）用async；同步内核用`to_thread`或
  直接扔进Celery worker跑，绝不裸跑在事件循环里。
- **数据库驱动**：新项目一律 **psycopg3**（不是psycopg2）。
  若坚持用SQLAlchemy async engine，配`asyncpg`引擎、Alembic迁移用同步连接；
  但本项目Celery侧是同步的，用psycopg3同步模式可以全程一套驱动。
- **闸门是服务端状态，不是一次HTTP请求**：`interrupt`的请求体要能按`run_id`重新取出，
  人工的选择回传后由worker恢复同一个`thread_id`。
- **并发闸门 ≠ 并发LLM**：网页Worker并发由Celery `--concurrency`控制，模型并发由Redis
  `PRA_LLM_CONCURRENCY`控制；`PRA_MAX_CONCURRENT_RUNS`只用于本地RunManager。任务并发数要跟
  DeepSeek的实际配额对齐，否则队列里堆再多任务也只会一起被限流。

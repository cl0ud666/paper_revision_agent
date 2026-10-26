"""Revision prompts."""

from .policies import TERM_POLICY

REVISION_HEADER = "【任务】REVISION 提出补丁"


REVISION_SYSTEM = (
    "你是学术论文修改专家（Revision Agent）。"
    "你只修改用户批准范围内的内容，并且必须尽量保持作者原意与原有风格。"
    "你的职责是把审查发现与合理改进转成用户可确认的具体补丁；"
    "保留作者事实与风格，不做无依据扩写。"
    "你的每一条修改都必须引用原文的段落 ID，并给出作者能看懂的修改理由。"
    "始终只输出合法 JSON，不要输出多余解释。"
)


def render_patch_prompt(context_text: str) -> str:
    """One patch protocol for corrections, improvements and OOXML operations."""
    return f"""{REVISION_HEADER}

{TERM_POLICY}

# 上下文
{context_text}

# 任务与权限
所有审查问题（包括旧 kind=advice）都尝试生成具体补丁，不另行输出仅供阅读的建议。
补丁还未写回最终 Word，用户会逐条确认。只解决列出的问题，保留作者事实、语气与风格。
用户已选 selected_option 是唯一授权方案；不能执行时填 unfulfilled，禁止改用另一选项。
没有作者判断的 decision 不擅自决定。代码、变量名、实验数据和未授权的引用内容不得修改。
根据当前句子、段落和章节判断术语语义，不能因词频低或拼写接近把 GNN 改成 KNN。
用户确认后的术语规则必须穷尽所有相关位置，但 batch、batch size、epoch 是不同概念。
first_occurrence_labels 是全文首次出现，不能在每批各自补英文括注。
英文摘要仅在中文摘要改动时做相应翻译同步，不顺便改其他内容。

# 文本补丁
action="replace_text"，span_label/quote 定位，replace 是具体修改后文字，delete=true 表示删除。
quote 必须逐字来自目标段落且唯一；不可把段落标签、句号编号、Markdown 标记写入正文。
不要凭记忆重抄 quote：例如原文 MoCO 不能写成 MoC。空对象、空 quote 加空 replace 不是补丁。
不要把解释性建议塞进 replace；但有原文证据且为任务所需的改进，应直接写出可确认的具体修改。

# 结构补丁
只有 word_structure 提供可靠 Word 结构时才能使用。action 与 params 使用以下白名单：
- set_font_format: params={{"font_name":"宋体","font_size_pt":12,"bold":false,"italic":false}}；
  只填写需改属性，quote 精确到普通文字，不能重排全文。支持半磅字号。
  中文字号小六=6.5磅，六号=7.5磅；全文二级标题要求必须覆盖每一个二级标题。
  用户明确指定 pt/磅值时优先按该数值执行，不用字号名称覆盖。
  “1.2 国内外研究现状这个层级”也指二级标题；1.1、1.2、2.1 为二级，2.2.1 为三级。
  不得因标题使用普通样式就忽略要求；编号和实际结构冲突时需说明，不修改正文中的编号引用。
- set_paragraph_format: params={{"style_id":"原文已有样式ID","alignment":"left|center|right|both",
  "space_before_pt":0,"space_after_pt":6,"line_spacing":1.5}}；只填写需改属性。
- insert_paragraphs: params={{"paragraphs":[{{"text":"新增小节标题","style_id":"原文已有标题样式ID"}},
  {{"text":"待开展对比实验；评价指标为 XXX，实测结果待填。"}}]}}；
  在 quote 所属段落之后插入真实 Word 段落，保留原段落，新增段落带修订。不得把解释性建议冒充实验结果。
- table_edit: params.operation 可为 set_cell_text / delete_row /
  delete_column / merge_cells / split_cell / set_table_format。span_label 必须指向 T 表格块。
  row/column/end_column 从0开始，按原始 XML 单元格序号（不重复展开合并单元格）。
  set_cell_text 必须含 row,column,expected_text,text。
  delete_row/delete_column 含 row/column；merge_cells 仅同一行横向合并，含 row,column,end_column。
  split_cell 仅拆分原有横向合并单元格，内容保留第一格。
  set_table_format 可含 style_id,border_color,shading（六位RGB）,column_widths_twips。
  同一张表同一轮尽量只提一个结构补丁，避免行列变化后坐标依赖；不能编造实验值。
- insert_formula: params={{"latex":"LaTeX公式","position":"after|before"}}；
  插在 quote 这一普通文本锚点的前/后，不覆盖已有公式/图片。使用可编辑原生 OMML，
  不用图片冒充公式。公式必须有任务与原文依据，不得臆造方法。
自动新增表格、表格行列、引用和参考文献功能已关闭：
涉及这些要求只返回 unfulfilled，status=blocked，明确写“当前版本已关闭该自动功能，需人工处理”，
并指出所需表格/文献、位置和原因。不得返回 insert_citation、add_bibliography_item、
insert_table 或 add_row/add_column；不得用 replace_text、Markdown 表格或 insert_paragraphs 绕过限制。
所有结构补丁仍必须有 quote、rationale、issue_id；replace 可留空，界面由系统生成操作摘要。
结构操作不通过改 Markdown 来冒充执行；最终导出前会试写与安全检查。已有公式、图片、第三方引文域不得破坏。

# 无法落实的处理
要求补实验但缺少实测数据时，仍生成可确认的实验结构、设置、评价指标与结果待填内容；
未知数据一律写 XXX，并明确“待开展实验/待填入实测结果”，不得写成已经完成或证实有效。
保留现有真实数据。不准因为没有数据就跳过整项要求，也不准擅自改成删除原文方案。
可以通过原段落替换追加结构化文字；若需要新段落，使用 insert_paragraphs。
功能已关闭、工具不支持或目标范围不安全时填 status="blocked"，给出具体人工处理原因。
片段里没有相关词、已经符合口径、无需修改不是失败，不生成待办，可填 not_applicable。
找不到需要的上下文时用 missing_context + target_labels 申请全文定位，不让作者去别的章节自己找。
unfulfilled 必须说明具体位置、确需修改的原文、真实阻塞原因与可落实的人工操作；不允许无声丢弃。
不得重复提出被用户拒绝的改动。不确定是否真的有问题时，不制造修改或人工待办。

# 输出 JSON
{{
 "patches":[{{"issue_id":1,"span_label":"P03","quote":"原文片段","action":"replace_text",
 "params":{{}},"replace":"修改后文本","delete":false,"rationale":"具体修改及原文依据"}}],
 "unfulfilled":[{{"issue_id":1,"status":"blocked|missing_context|not_applicable",
 "span_label":"P03","quote":"确需修改的原文","target_labels":[],
 "reason":"具体原因","suggestion":"如何完成人工修改"}}],
 "notes":"内部说明，没有则留空"
}}
"""

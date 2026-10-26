"""Product-level restrictions, enforced independently of model instructions."""

CITATION_DISABLED = (
    "当前版本已关闭自动新增引用和参考文献功能，需人工核对文献来源、正文引用位置及文末条目"
)
TABLE_DISABLED = "当前版本已关闭自动新增表格及行列功能，需人工建立表格并核对表头、数据、编号和版式"


def disabled_operation_reason(action, params=None):
    params = params if isinstance(params, dict) else {}
    if action in {"insert_citation", "add_bibliography_item"}:
        return CITATION_DISABLED
    if action in {"insert_table", "add_table", "create_table"} or (
        action == "table_edit" and params.get("operation") in {"add_row", "add_column"}
    ):
        return TABLE_DISABLED
    return ""

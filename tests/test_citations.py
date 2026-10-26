from app.document.citations import citation_consistency_audit


def test_numeric_consistency_finds_missing_unused_duplicate_and_order():
    doc = "# 正文\n\n参考研究[2]提出方法。随后引用[1]和[9]。\n\n# 参考文献\n\n[1] 第一篇文献\n\n[2] 第二篇文献\n\n[2] 重复编号文献\n\n[3] 未引用的文献\n"
    issues = citation_consistency_audit(doc)
    assert any("[9]" in i["problem"] and "没有对应条目" in i["problem"] for i in issues)
    assert any("[3]" in i["problem"] and "未发现" in i["problem"] for i in issues)
    assert any("重复" in i["problem"] for i in issues)
    assert any("不递增" in i["problem"] for i in issues)
    assert all(i["evidence"] in doc for i in issues)


def test_author_year_style_is_not_flagged_as_missing_numeric_citations():
    assert not citation_consistency_audit(
        "# 正文\n\nHe et al. (2020) 提出方法。\n\n# 参考文献\n\nHe et al. Momentum Contrast. 2020.\n"
    )

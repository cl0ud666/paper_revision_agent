"""Convert a bounded LaTeX expression into Word math without executing TeX."""

import re

from lxml import etree

from app.document.docx_model import W_NS
from app.document.operations.xml import M


def formula_xml(latex):
    # A parser, not a TeX execution engine. File IO/macros/URLs are disallowed.
    if re.search(
        r"\\(?:input|include|write|read|def|newcommand|href|url|html|begin\s*\{document)", latex
    ):
        raise ValueError("公式包含不允许的命令")
    try:
        from latex2mathml.converter import convert as to_mathml
        from mathml2omml import convert as to_omml
    except ImportError as exc:
        raise ValueError("公式转换组件尚未安装，需更新部署依赖") from exc
    try:
        result = to_omml(to_mathml(latex))
        # mathml2omml 0.0.2 closes groupChrPr as groupChr in MOver/MUnder.
        # Correct ONLY those two known serializer tokens, then strictly parse;
        # never recover arbitrary malformed XML or silently discard formulae.
        for position in ("top", "bot"):
            result = result.replace(
                f'<m:pos m:val="{position}"/></m:groupChr>',
                f'<m:pos m:val="{position}"/></m:groupChrPr>',
            )
        root = etree.fromstring(
            f'<root xmlns:m="{M}" xmlns:w="{W_NS}">{result}</root>'.encode(),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )
        math = root.find(f"{{{M}}}oMath")
        if math is None or not len(math):
            raise ValueError("转换结果为空")
        # Unknown commands must not silently survive as literal LaTeX.
        if "\\" in "".join(math.itertext()):
            raise ValueError("公式包含转换器未识别的命令")
        return math
    except Exception as exc:
        raise ValueError(f"公式无法可靠转为原生 Word 公式：{exc}") from exc

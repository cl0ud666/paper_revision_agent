"""Validate Word-family uploads and normalize them without changing the original.

Conversion belongs in the background worker, not the HTTP upload request.
WPS is accepted only when its actual container is Word-compatible.
"""

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

WORD_SUFFIXES = {".docx", ".doc", ".wps", ".rtf", ".odt"}
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MAX_EXPANDED = 80 * 1024 * 1024
CONVERSION_WARNING = "输入文件已转换为DOCX，原文件保留；复杂公式、字体和分页可能变化，请对照原稿检查。输出统一为DOCX。"


class WordFormatError(ValueError):
    """Safe, user-facing validation/conversion error."""


def validate_word(data: bytes, filename: str, *, max_bytes=20 * 1024 * 1024) -> tuple[str, str]:
    name = Path(filename.replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    if suffix not in WORD_SUFFIXES or len(name) > 200:
        raise WordFormatError("支持DOCX、DOC、Word兼容的WPS、RTF、ODT；文件名不能超过200字符")
    if not data or len(data) > max_bytes:
        raise WordFormatError("文件为空或超过上传大小限制")
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                items = archive.infolist()
                names = archive.namelist()
                if len(items) > 10000 or sum(i.file_size for i in items) > MAX_EXPANDED:
                    raise WordFormatError("文档解压后过大")
                if len(names) != len(set(names)) or any(i.flag_bits & 1 for i in items):
                    raise WordFormatError("不支持加密或结构异常的文档")
                if any(
                    "vbaproject" in n.lower() or n.lower().startswith(("scripts/", "basic/"))
                    for n in names
                ):
                    raise WordFormatError("不支持包含宏的文件")
                if {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    if b"macroEnabled" in archive.read("[Content_Types].xml"):
                        raise WordFormatError("不支持包含宏的文件")
                    if suffix not in {".docx", ".doc", ".wps"}:
                        raise WordFormatError("文件扩展名与实际格式不一致")
                    return name, "docx"
                if suffix == ".odt" and {"mimetype", "content.xml"}.issubset(names):
                    if archive.read("mimetype") == b"application/vnd.oasis.opendocument.text":
                        if b"encryption-data" in archive.read("META-INF/manifest.xml"):
                            raise WordFormatError("不支持加密的文档")
                        return name, "odt"
        except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
            raise WordFormatError("文档压缩包损坏或格式不受支持") from exc
    elif data.startswith(OLE_SIGNATURE) and suffix in {".doc", ".wps"}:
        try:
            import olefile

            with olefile.OleFileIO(io.BytesIO(data)) as document:
                streams = ["/".join(p).lower() for p in document.listdir()]
                if any(
                    "vba" in p or "macros" in p or p in {"encryptedpackage", "encryptioninfo"}
                    for p in streams
                ):
                    raise WordFormatError("不支持加密或包含宏的文档")
                if "worddocument" not in streams:
                    raise WordFormatError("该WPS/DOC不是兼容Word的格式，请用原软件另存为DOCX")
                header = document.openstream("WordDocument").read(12)
                if len(header) < 12 or int.from_bytes(header[10:12], "little") & 0x8100:
                    raise WordFormatError("不支持加密的DOC文档")
                return name, "doc"
        except ImportError as exc:
            raise WordFormatError("服务器缺少DOC格式检测依赖olefile，请联系管理员") from exc
        except (OSError, IOError) as exc:
            raise WordFormatError("DOC/WPS文件损坏，无法读取") from exc
    elif data.lstrip().startswith(b"{\\rtf") and suffix in {".rtf", ".doc", ".wps"}:
        if b"\\objdata" in data.lower():
            raise WordFormatError("暂不支持包含嵌入对象的RTF，请另存为DOCX")
        return name, "rtf"
    raise WordFormatError("文件不是有效或受支持的Word文档；旧版专有WPS请用WPS另存为DOCX")


def normalize_word(
    data: bytes, filename: str, *, max_bytes=20 * 1024 * 1024
) -> tuple[bytes, list[str]]:
    _, kind = validate_word(data, filename, max_bytes=max_bytes)
    if kind == "docx":
        return data, []
    executable = (
        os.getenv("PRA_LIBREOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")
    )
    if not executable:
        raise WordFormatError("服务器未安装LibreOffice转换器，请安装后重试，或先将文件另存为DOCX")
    with tempfile.TemporaryDirectory(prefix="pra-word-") as directory:
        root = Path(directory)
        source = root / f"source.{kind}"
        source.write_bytes(data)
        profile = root / "profile"
        user = profile / "user"
        user.mkdir(parents=True)
        (user / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            "</item></oor:items>",
            encoding="utf-8",
        )
        output = root / "output"
        output.mkdir()
        try:
            completed = subprocess.run(
                [
                    executable,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--norestore",
                    "--convert-to",
                    "docx:Office Open XML Text",
                    "--outdir",
                    str(output),
                    str(source),
                ],
                timeout=120,
                check=False,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise WordFormatError("文档转换超过120秒；请用原软件另存为DOCX后上传") from exc
        except OSError as exc:
            raise WordFormatError("文档转换器无法启动，请联系管理员") from exc
        target = output / "source.docx"
        if completed.returncode != 0 or not target.is_file():
            raise WordFormatError(
                "文档无法转换，可能已加密、损坏或属于不兼容的旧版WPS；请另存为DOCX"
            )
        if target.stat().st_size > MAX_EXPANDED:
            raise WordFormatError("转换后的文档过大")
        normalized = target.read_bytes()
        validate_word(normalized, "source.docx", max_bytes=MAX_EXPANDED)
        return normalized, [CONVERSION_WARNING]

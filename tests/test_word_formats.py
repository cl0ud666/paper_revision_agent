"""Word-family upload validation and bounded background conversion."""
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from app.document.word_formats import normalize_word, validate_word, WordFormatError
from tests.test_web_api import paper_bytes


class TestWordFormats(unittest.TestCase):
    def test_docx_and_word_compatible_wps_are_lossless(self):
        data = paper_bytes()
        with patch("app.document.word_formats.subprocess.run") as converter:
            for name in ("paper.docx", "paper.wps", "paper.doc"):
                self.assertEqual(normalize_word(data, name), (data, []))
            converter.assert_not_called()

    def test_names_signatures_limits_and_macros(self):
        data = paper_bytes()
        self.assertEqual(validate_word(data, "../../论文.docx")[0], "论文.docx")
        for name, raw in (("paper.exe", data), ("paper.docx", b"bad"), ("paper.wps", b"old wps"),
                          ("paper.rtf", data), ("paper.doc", b"{\\rtf1 \\objdata 0011}")):
            with self.subTest(name=name), self.assertRaises(WordFormatError):
                validate_word(raw, name)
        with self.assertRaises(WordFormatError):
            validate_word(data, "paper.docx", max_bytes=5)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("word/document.xml", "document")
            archive.writestr("word/vbaProject.bin", "macro")
        with self.assertRaisesRegex(WordFormatError, "宏"):
            validate_word(output.getvalue(), "paper.wps")

    def test_conversion_has_private_profile_timeout_and_validated_output(self):
        result = paper_bytes()
        def convert(args, **kwargs):
            self.assertEqual(kwargs["timeout"], 120)
            self.assertNotIn("shell", kwargs)
            self.assertTrue(args[1].startswith("-env:UserInstallation=file:"))
            target = Path(args[args.index("--outdir") + 1]) / "source.docx"
            target.write_bytes(result)
            return subprocess.CompletedProcess(args, 0)
        with patch.dict(os.environ, {"PRA_LIBREOFFICE": "test-soffice"}), patch(
            "app.document.word_formats.subprocess.run", side_effect=convert
        ):
            normalized, warnings = normalize_word(b"{\\rtf1 hello}", "hello.rtf")
        self.assertEqual(normalized, result)
        self.assertIn("分页", warnings[0])

    def test_missing_converter_timeout_and_missing_output_are_actionable(self):
        with patch.dict(os.environ, {"PRA_LIBREOFFICE": ""}), patch("app.document.word_formats.shutil.which", return_value=None):
            with self.assertRaisesRegex(WordFormatError, "未安装"):
                normalize_word(b"{\\rtf1 hello}", "hello.rtf")
        for effect, message in ((subprocess.TimeoutExpired("soffice", 120), "120"),
                                (subprocess.CompletedProcess([], 0), "无法转换")):
            with patch.dict(os.environ, {"PRA_LIBREOFFICE": "test-soffice"}), patch(
                "app.document.word_formats.subprocess.run", **({"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect})
            ), self.assertRaisesRegex(WordFormatError, message):
                normalize_word(b"{\\rtf1 hello}", "hello.rtf")

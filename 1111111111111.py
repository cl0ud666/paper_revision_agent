# create_structure.py
from pathlib import Path

# 空目录
DIRS = [
    "data",
    "data/samples",
    "tests",
]

# 文件（内容留空）
FILES = {
    "app/agents/__init__.py": "",
    "app/workflow/__init__.py": "",
    "app/workflow/graph.py": "",
    "app/workflow/nodes.py": "",
    "app/workflow/router.py": "",
    "app/state/__init__.py": "",
    "app/state/schema.py": "",
    "app/utils/__init__.py": "",
    "app/utils/llm.py": "",
    ".env": "",
    ".gitignore": "",
    "main.py": "",
    "test_llm.py": "",
    "requirements.txt": "",
    "README.md": "",
}

base = Path(".")

for d in DIRS:
    p = base / d
    p.mkdir(parents=True, exist_ok=True)
    print(f"dir  ok : {p}")

for f, content in FILES.items():
    p = base / f
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        print(f"skip    : {p} (已存在)")
    else:
        p.write_text(content, encoding="utf-8")
        print(f"created : {p}")

# 空目录占位（可选）
for d in ["data/samples", "tests"]:
    gk = base / d / ".gitkeep"
    if not gk.exists():
        gk.write_text("", encoding="utf-8")
        print(f"created : {gk}")

print("✅ 结构创建完成")
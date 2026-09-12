"""Create the allow-listed HackerRank submission archive."""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path


def build_package(root: Path, output: Path) -> dict[str, object]:
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        output.unlink()
    allowed_suffixes = {".py", ".txt", ".md", ".json"}
    included: list[str] = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_root in (Path("code"), Path("README.md"), Path("problem_statement.md")):
            source = root / relative_root
            paths = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file())
            for path in paths:
                relative = path.relative_to(root)
                if path.suffix not in allowed_suffixes or any(part in {"cache", "__pycache__"} for part in relative.parts):
                    continue
                archive.write(path, relative.as_posix())
                included.append(relative.as_posix())
        usage = root / "code" / "evaluation" / "usage_report.md"
        archive.write(usage, "evaluation/usage_report.md")
        included.append("evaluation/usage_report.md")
    return {"output": str(output), "file_count": len(included), "files": included}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an allow-listed code.zip")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or args.root / "code.zip"
    result = build_package(args.root, output)
    print({"output": result["output"], "file_count": result["file_count"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

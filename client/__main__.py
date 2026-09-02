"""客户端入口：交互记录界面（python -m client）。"""

# 用绝对导入而非相对导入：PyInstaller 会把本文件作为顶层 __main__ 执行
# （无父包），相对导入会抛 "attempted relative import with no known parent package"。
# 绝对导入在 `python -m client` 与打包 exe 两种入口下都成立。
from client.cli import run_interactive


def main() -> int:
    run_interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
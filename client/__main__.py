"""客户端入口：交互记录界面（python -m client）。"""

# 模块启动（python -m client / python -m <改名后的包>）时用相对导入，使客户端工程改名
# 不影响内部代码。打包成 exe 时 PyInstaller 会把本文件作为顶层 __main__ 执行（无父包），
# 相对导入会抛 "attempted relative import with no known parent package"，此时回退绝对导入
# （build.yml 固定以 client/__main__.py 为入口，包名恒为 client）。
if __package__:
    from .cli import run_interactive
else:
    from client.cli import run_interactive


def main() -> int:
    return run_interactive()


if __name__ == "__main__":
    raise SystemExit(main())

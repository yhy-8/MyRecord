"""客户端入口：交互记录界面。"""

from .cli.app import run_interactive


def main() -> int:
    run_interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
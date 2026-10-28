"""`python -m readyclip` opens the GUI; with arguments it runs the CLI."""

import sys


def main() -> None:
    if len(sys.argv) > 1:
        from readyclip.cli import main as cli_main
        sys.exit(cli_main())
    from readyclip.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()

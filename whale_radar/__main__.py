"""``python -m whale_radar`` 模块入口，与命令行入口完全一致。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

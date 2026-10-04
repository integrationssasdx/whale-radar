"""``python -m whale_radar`` 入口，与 ``bin/whale-radar`` 启动器行为完全一致。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

"""测试用「立刻崩溃」插件：验证崩溃标记与不重启风暴。"""

import sys

sys.stderr.write("boom: 我立刻退出\n")
sys.exit(3)

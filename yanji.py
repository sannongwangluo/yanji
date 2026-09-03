# -*- coding: utf-8 -*-
"""yanji.py —— 会议记录工具英文入口。

等价于 `python 会议记录.py`：定位到同目录的 `会议记录.py` 并以脚本方式执行其
main 入口，`if __name__ == "__main__"` 的语义不变。中文文件名在命令行不便输入时，
可直接用 `python yanji.py` 启动。
"""
import os
import runpy
import sys


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    runpy.run_path(os.path.join(here, "会议记录.py"), run_name="__main__")


if __name__ == "__main__":
    main()

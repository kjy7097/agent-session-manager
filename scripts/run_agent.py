"""cwd-independent launcher for the agent (used by the Windows scheduled task).

Adds its own directory to sys.path so `from csm.agent import main` works
regardless of the working directory the task starts in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csm.agent import main  # noqa: E402

main()

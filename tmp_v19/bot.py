"""Compatibility entrypoint.

Some Railway templates start apps with: `python -m bot`.
This module forwards to main.py.
"""

from main import main

if __name__ == "__main__":
    main()

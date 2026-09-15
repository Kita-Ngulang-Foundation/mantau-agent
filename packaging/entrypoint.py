"""PyInstaller's single entry point -- see packaging/README.md for how this
gets frozen into a standalone binary per platform.
"""

from mantau_agent.main import main

if __name__ == "__main__":
    main()

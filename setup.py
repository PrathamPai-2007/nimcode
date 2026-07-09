from pathlib import Path
from setuptools import setup

setup(
    name="nimcode",
    version="0.1.0",
    py_modules=[
        f.stem for f in Path(__file__).parent.glob("*.py") if f.name != "setup.py"
    ],
    install_requires=[
        "typer",
        "openai",
        "python-dotenv",
        "tomli",
        "rich",
        "prompt_toolkit",
    ],
    entry_points={
        "console_scripts": [
            "nimcode = main:app",
        ],
    },
)

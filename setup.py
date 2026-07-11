from setuptools import setup, find_packages

setup(
    name="nimcode",
    version="0.4.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "typer>=0.9.0",
        "openai>=1.0.0",
        "python-dotenv>=1.0.0",
        "tomli>=2.0.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0.0",
    ],
    entry_points={
        "console_scripts": [
            "nimcode = nimcode.cli:app",
        ],
    },
)

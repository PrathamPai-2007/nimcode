import os
import sys
import shutil
import urllib.request
import zipfile
import subprocess
import re
from pathlib import Path
from urllib.parse import urlparse

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILLS_DIR = Path.home() / ".nimcode" / "skills"


def init_skills_dir():
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _get_skill_name_from_url(url: str) -> str:
    path = urlparse(url).path
    basename = path.strip("/").split("/")[-1]
    if basename.endswith(".git"):
        basename = basename[:-4]
    elif basename.endswith(".zip"):
        basename = basename[:-4]
    elif basename.endswith(".md"):
        basename = basename[:-3]
    # Sanitize basename to prevent directory traversal (`..`) or illegal chars
    basename = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", basename)
    basename = basename.strip(".")
    return basename or "unnamed_skill"


def install_skill(url: str) -> str:
    init_skills_dir()
    skill_name = _get_skill_name_from_url(url)
    target_path = (SKILLS_DIR / skill_name).resolve()

    if not hasattr(target_path, "is_relative_to"):
        if str(SKILLS_DIR.resolve()) not in str(target_path):
            return f"Error: Skill name '{skill_name}' escapes skills directory."
    elif not target_path.is_relative_to(SKILLS_DIR.resolve()):
        return f"Error: Skill name '{skill_name}' escapes skills directory."

    try:
        # Handle .zip files
        if url.endswith(".zip"):
            zip_path = SKILLS_DIR / f"{skill_name}.zip"
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                for member in zip_ref.namelist():
                    resolved_member = (target_path / member).resolve()
                    if hasattr(resolved_member, "is_relative_to"):
                        if not resolved_member.is_relative_to(target_path):
                            raise Exception(
                                "Zip Slip vulnerability detected in skill archive."
                            )
                    else:
                        if str(target_path) not in str(resolved_member):
                            raise Exception(
                                "Zip Slip vulnerability detected in skill archive."
                            )
                zip_ref.extractall(target_path)
            os.remove(zip_path)
            return f"Successfully installed skill '{skill_name}' from zip."

        # Handle git repositories
        elif url.endswith(".git") or (
            "github.com" in url and "raw.githubusercontent.com" not in url
        ):
            if target_path.exists():
                shutil.rmtree(target_path)
            try:
                subprocess.run(
                    ["git", "clone", url, str(target_path)],
                    check=True,
                    capture_output=True,
                )
                return f"Successfully cloned skill '{skill_name}'."
            except subprocess.CalledProcessError as e:
                return f"Failed to clone skill: {e.stderr.decode('utf-8', errors='replace')}"

        # Handle raw markdown files
        else:
            if not skill_name.endswith(".md"):
                skill_name += ".md"
                target_path = SKILLS_DIR / skill_name
            urllib.request.urlretrieve(url, target_path)
            return (
                f"Successfully installed raw skill '{skill_name.replace('.md', '')}'."
            )
    except Exception as e:
        return f"Error installing skill from {url}: {e}"


def list_skills() -> list[str]:
    init_skills_dir()
    skills = []
    for item in SKILLS_DIR.iterdir():
        if item.name.startswith(".") or item.name.startswith("__"):
            continue
        if item.is_dir():
            skills.append(item.name)
        elif item.is_file() and item.name.endswith(".md"):
            skills.append(item.stem)
    return sorted(skills)


def get_skill_content(skill_name: str) -> str:
    init_skills_dir()
    # Check for directory
    dir_path = SKILLS_DIR / skill_name
    if dir_path.is_dir():
        for filename in ["SKILL.md", "README.md", "instructions.md"]:
            candidate = dir_path / filename
            if candidate.exists():
                return candidate.read_text(encoding="utf-8", errors="replace")
        return f"Error: No SKILL.md or README.md found in {skill_name} directory."

    # Check for file
    file_path = SKILLS_DIR / f"{skill_name}.md"
    if file_path.exists():
        return file_path.read_text(encoding="utf-8", errors="replace")

    return ""

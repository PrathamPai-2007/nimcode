import os
import shutil
import zipfile
import subprocess
import re
from pathlib import Path
from urllib.parse import urlparse


SKILLS_DIR = Path.home() / ".nimcode" / "skills"


def _download_with_limits(url: str, dest: Path, max_bytes: int, timeout: int = 60):
    """Download URL to dest with size cap and timeout (no urlretrieve)."""
    import urllib.request as _req

    request = _req.Request(url, headers={"User-Agent": "nimcode-skill-installer/1.0"})
    # Stream to a temp file and rename on success: a network error used to
    # leave a truncated skill behind that then blocked reinstall.
    tmp = dest.with_name(dest.name + ".part")
    try:
        with _req.urlopen(request, timeout=timeout) as resp, open(tmp, "wb") as fh:
            final = urlparse(resp.geturl())
            if (final.scheme or "").lower() not in ("https", "http"):
                raise Exception("Redirect to non-http(s) URL refused.")
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise Exception(f"Download exceeds size limit ({max_bytes} bytes).")
                fh.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


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


def _is_url_allowed_for_skill(url: str) -> tuple[bool, str]:
    """Allowlist https/http only; deny file://, ext::, ssh, localhost bypasses."""
    from urllib.parse import urlparse as _up

    try:
        p = _up(url)
    except Exception:
        return False, "unparseable URL"
    scheme = (p.scheme or "").lower()
    if scheme not in ("https", "http"):
        return False, f"scheme '{scheme}' not allowed (https/http only)"
    lowered = url.lower()
    if lowered.startswith("ext::") or "ext::" in lowered:
        return False, "ext:: transport not allowed"
    return True, ""


def install_skill(url: str) -> str:
    init_skills_dir()
    ok, reason = _is_url_allowed_for_skill(url)
    if not ok:
        return f"Error: refusing to install skill: {reason}."
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
            if target_path.exists():
                return (
                    f"Error: skill '{skill_name}' already exists. "
                    "Remove it first to reinstall."
                )
            try:
                _download_with_limits(url, zip_path, max_bytes=20_000_000, timeout=60)
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    for member in zip_ref.namelist():
                        # Reject absolute paths outright.
                        if (
                            member.startswith("/")
                            or member.startswith("\\")
                            or (len(member) > 2 and member[1] == ":")
                        ):
                            raise Exception(
                                "Zip Slip vulnerability detected in skill archive."
                            )
                        # Reject symlinks (recreated by extractall -> escape).
                        try:
                            info = zip_ref.getinfo(member)
                            if (info.external_attr >> 16) & 0o120000 == 0o120000:
                                raise Exception(
                                    "Symlink member rejected in skill archive."
                                )
                        except KeyError:
                            pass
                        resolved_member = (target_path / member).resolve()
                        try:
                            resolved_member.relative_to(target_path)
                        except ValueError:
                            raise Exception(
                                "Zip Slip vulnerability detected in skill archive."
                            )
                    # Extract manually with per-file and total caps (zip bomb:
                    # many 5MB members used to be unbounded in total).
                    infos = zip_ref.infolist()
                    if len(infos) > 2000 or sum(i.file_size for i in infos) > (
                        50_000_000
                    ):
                        raise Exception("Skill archive too large when extracted.")
                    target_path.mkdir(parents=True, exist_ok=True)
                    for info in infos:
                        if info.file_size > 5_000_000:
                            raise Exception(
                                f"Skill archive member too large: {info.filename}"
                            )
                        dest = (target_path / info.filename).resolve()
                        dest.relative_to(target_path)
                        if info.is_dir():
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zip_ref.open(info) as src, open(dest, "wb") as fh:
                                shutil.copyfileobj(src, fh, length=65536)
            except BaseException:
                # Don't leave a half-extracted skill that blocks reinstall.
                shutil.rmtree(target_path, ignore_errors=True)
                raise
            finally:
                if zip_path.exists():
                    os.remove(zip_path)
            return f"Successfully installed skill '{skill_name}' from zip."

        # Handle git repositories
        elif url.endswith(".git") or (
            "github.com" in url and "raw.githubusercontent.com" not in url
        ):
            if target_path.exists():
                return (
                    f"Error: skill '{skill_name}' already exists. "
                    "Remove it first to reinstall."
                )
            try:
                subprocess.run(
                    # `--` stops option parsing; no local-file transport.
                    [
                        "git",
                        "-c",
                        "protocol.file.allow=never",
                        "clone",
                        "--depth",
                        "1",
                        "--",
                        url,
                        str(target_path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                return f"Successfully cloned skill '{skill_name}'."
            except subprocess.CalledProcessError as e:
                return f"Failed to clone skill: {e.stderr.decode('utf-8', errors='replace')}"
            except subprocess.TimeoutExpired:
                return "Failed to clone skill: timed out after 60s."

        # Handle raw markdown files
        else:
            if not skill_name.endswith(".md"):
                skill_name += ".md"
                target_path = SKILLS_DIR / skill_name
            if target_path.exists():
                return (
                    f"Error: skill '{skill_name}' already exists. "
                    "Remove it first to reinstall."
                )
            _download_with_limits(url, target_path, max_bytes=2_000_000, timeout=60)
            return (
                f"Successfully installed raw skill '{skill_name.replace('.md', '')}'."
            )
    except Exception as e:
        return f"Error installing skill from {url}: {e}"


def list_skills() -> list[str]:
    init_skills_dir()
    if not SKILLS_DIR.is_dir():
        return []
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
    if not isinstance(skill_name, str) or not skill_name:
        return "Error: skill '' not found."
    # Reject traversal / separators outright.
    if (
        "/" in skill_name
        or "\\" in skill_name
        or ".." in skill_name
        or skill_name.startswith(".")
    ):
        return f"Error: skill '{skill_name}' not found."
    if not re.fullmatch(r"[a-zA-Z0-9_\-\.]+", skill_name):
        return f"Error: skill '{skill_name}' not found."
    # Check for directory
    dir_path = SKILLS_DIR / skill_name
    if dir_path.is_dir():
        for filename in ["SKILL.md", "README.md", "instructions.md"]:
            candidate = dir_path / filename
            if candidate.exists():
                try:
                    if candidate.stat().st_size > 200_000:
                        return f"Error: skill '{skill_name}' content too large."
                    return candidate.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    return f"Error: cannot read skill '{skill_name}': {e}"
        return f"Error: No SKILL.md or README.md found in {skill_name} directory."

    # Check for file
    file_path = SKILLS_DIR / f"{skill_name}.md"
    if file_path.exists():
        try:
            if file_path.stat().st_size > 200_000:
                return f"Error: skill '{skill_name}' content too large."
            return file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Error: cannot read skill '{skill_name}': {e}"

    return f"Error: skill '{skill_name}' not found."

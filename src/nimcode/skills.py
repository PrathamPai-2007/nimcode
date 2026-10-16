import os
import json
import time
import shutil
import hashlib
import tempfile
import zipfile
import subprocess
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


SKILLS_DIR = Path.home() / ".nimcode" / "skills"
_SKILL_DOC_NAMES = ("SKILL.md", "README.md", "instructions.md")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Run every redirect hop through the same URL check as the first URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = _is_url_allowed_for_skill(newurl)
        if not ok:
            raise urllib.error.URLError(f"redirect to {newurl} refused: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_with_limits(url: str, dest: Path, max_bytes: int, timeout: int = 60):
    """Download URL to dest with size cap and timeout (no urlretrieve)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "nimcode-skill-installer/1.0"}
    )
    opener = urllib.request.build_opener(_GuardedRedirect)
    # Stream to a temp file and rename on success: a network error used to
    # leave a truncated skill behind that then blocked reinstall.
    tmp = dest.with_name(dest.name + ".part")
    try:
        with opener.open(request, timeout=timeout) as resp, open(tmp, "wb") as fh:
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
    """Allowlist https/http only; deny file://, ext::, ssh, and private hosts."""
    try:
        p = urlparse(url)
    except Exception:
        return False, "unparseable URL"
    scheme = (p.scheme or "").lower()
    if scheme not in ("https", "http"):
        return False, f"scheme '{scheme}' not allowed (https/http only)"
    if "ext::" in url.lower():
        return False, "ext:: transport not allowed"
    from nimcode.tools.base import _is_url_allowed

    # ponytail: DNS is checked here, not pinned; a rebinding host could still
    # swap IPs between check and connect. Pin via a custom HTTPSConnection if needed.
    return _is_url_allowed(url)


def _staging_root() -> Path:
    return SKILLS_DIR / ".staging"


def _skill_exists(name: str) -> bool:
    return (SKILLS_DIR / name).exists() or (SKILLS_DIR / f"{name}.md").exists()


def _extract_zip(zip_path: Path, target_path: Path) -> None:
    """Extract with Zip Slip, symlink and size (zip bomb) guards."""
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        infos = zip_ref.infolist()
        if len(infos) > 2000 or sum(i.file_size for i in infos) > 50_000_000:
            raise Exception("Skill archive too large when extracted.")
        for info in infos:
            member = info.filename
            if (
                member.startswith(("/", "\\"))
                or (len(member) > 2 and member[1] == ":")
                or not (target_path / member).resolve().is_relative_to(target_path)
            ):
                raise Exception("Zip Slip vulnerability detected in skill archive.")
            # Symlinks would be recreated by extractall -> escape.
            if (info.external_attr >> 16) & 0o120000 == 0o120000:
                raise Exception("Symlink member rejected in skill archive.")
            if info.file_size > 5_000_000:
                raise Exception(f"Skill archive member too large: {member}")
        for info in infos:
            dest = (target_path / info.filename).resolve()
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zip_ref.open(info) as src, open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh, length=65536)


def _skill_files(skill_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in skill_dir.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(skill_dir).parts
    )


def _hash_and_preview(skill_dir: Path, name: str, url: str) -> tuple[str, str]:
    """sha256 over (relative path, content) of every file, plus a review text."""
    digest = hashlib.sha256()
    files = _skill_files(skill_dir)
    total = 0
    for f in files:
        rel = f.relative_to(skill_dir).as_posix()
        data = f.read_bytes()
        total += len(data)
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    sha = digest.hexdigest()

    listing = [f"  {f.relative_to(skill_dir).as_posix()}" for f in files[:50]]
    if len(files) > 50:
        listing.append(f"  ... and {len(files) - 50} more")
    doc = next(
        (skill_dir / n for n in _SKILL_DOC_NAMES if (skill_dir / n).is_file()), None
    )
    if doc is not None:
        with open(doc, encoding="utf-8", errors="replace") as fh:
            head = "".join(line for _, line in zip(range(40), fh)).rstrip()
        doc_text = f"--- {doc.name} (first 40 lines) ---\n{head}"
    else:
        doc_text = "(no SKILL.md / README.md found: this skill will have no content)"
    preview = "\n".join(
        [
            f"Skill '{name}' from {url}",
            f"Files ({len(files)}, {total} bytes total):",
            *listing,
            f"sha256: {sha}",
            doc_text,
        ]
    )
    return sha, preview


def fetch_skill(url: str) -> tuple[Path, str, str]:
    """Download a skill into a staging dir for review; nothing is installed yet.

    Returns (staging_dir, preview, sha256). Pass them to commit_skill() to
    install, or discard_skill() to drop. Raises ValueError with a
    user-readable message on any failure (staging is cleaned up).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("no skill URL given.")
    url = url.strip()
    ok, reason = _is_url_allowed_for_skill(url)
    if not ok:
        raise ValueError(f"refusing to install skill: {reason}.")
    name = _get_skill_name_from_url(url)
    if _skill_exists(name):
        raise ValueError(
            f"skill '{name}' already exists. Remove it first to reinstall."
        )

    init_skills_dir()
    _staging_root().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{name}_", dir=_staging_root())).resolve()
    try:
        if url.lower().endswith(".zip"):
            zip_path = staging.with_name(staging.name + ".zip")
            try:
                _download_with_limits(url, zip_path, max_bytes=20_000_000)
                _extract_zip(zip_path, staging)
            finally:
                zip_path.unlink(missing_ok=True)
        elif url.endswith(".git") or (
            "github.com" in url and "raw.githubusercontent.com" not in url
        ):
            try:
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "protocol.file.allow=never",
                        # Redirects would skip the URL check above.
                        "-c",
                        "http.followRedirects=false",
                        "clone",
                        "--depth",
                        "1",
                        "--",  # stops option parsing
                        url,
                        str(staging),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode("utf-8", errors="replace").strip()
                raise Exception(f"git clone failed: {err}")
            except subprocess.TimeoutExpired:
                raise Exception("git clone timed out after 60s.")
        else:
            _download_with_limits(url, staging / "SKILL.md", max_bytes=2_000_000)
        sha, preview = _hash_and_preview(staging, name, url)
    except BaseException as e:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(e, Exception):
            raise ValueError(f"cannot fetch skill from {url}: {e}") from e
        raise
    return staging, preview, sha


def _owned_staging(staging_dir: Path) -> Optional[Path]:
    """Resolve staging_dir, or None if it isn't one of our staging dirs."""
    try:
        staging = Path(staging_dir).resolve()
    except Exception:
        return None
    if staging.parent != _staging_root().resolve() or not staging.is_dir():
        return None
    return staging


def discard_skill(staging_dir: Path) -> None:
    """Delete a fetched-but-rejected skill's staging dir."""
    staging = _owned_staging(staging_dir)
    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)


def commit_skill(staging_dir: Path, name: str, url: str, sha256: str) -> str:
    """Move a fetch_skill() staging dir into place and record its provenance."""
    staging = _owned_staging(staging_dir)
    if staging is None:
        return "Error: invalid skill staging directory."
    if not isinstance(name, str) or not re.fullmatch(
        r"[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*", name
    ):
        discard_skill(staging)
        return f"Error: invalid skill name '{name}'."
    if _skill_exists(name):
        discard_skill(staging)
        return f"Error: skill '{name}' already exists. Remove it first to reinstall."
    try:
        (staging / ".source.json").write_text(
            json.dumps(
                {"url": url, "sha256": sha256, "installed_at": time.time()}, indent=2
            ),
            encoding="utf-8",
        )
        os.replace(staging, SKILLS_DIR / name)
    except OSError as e:
        discard_skill(staging)
        return f"Error installing skill '{name}': {e}"
    return f"Successfully installed skill '{name}' (sha256 {sha256[:12]})."


def install_skill(url: str) -> str:
    """Fetch + commit without review (compat wrapper)."""
    try:
        staging, _preview, sha = fetch_skill(url)
    except ValueError as e:
        return f"Error: {e}"
    return commit_skill(staging, _get_skill_name_from_url(url.strip()), url, sha)


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


def _read_skill_file(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > 200_000:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def get_skill_content(skill_name: str) -> Optional[str]:
    """Skill text, or None if missing/unreadable/too large (never an error string,
    which would be injected into the prompt as if it were skill content)."""
    init_skills_dir()
    if not isinstance(skill_name, str) or not re.fullmatch(
        r"[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*", skill_name
    ):
        return None
    if ".." in skill_name:
        return None
    dir_path = SKILLS_DIR / skill_name
    if dir_path.is_dir():
        for filename in _SKILL_DOC_NAMES:
            candidate = dir_path / filename
            if candidate.exists():
                return _read_skill_file(candidate)
        return None
    file_path = SKILLS_DIR / f"{skill_name}.md"
    if file_path.exists():
        return _read_skill_file(file_path)
    return None

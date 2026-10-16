"""Track B security: subagent host refusal, docker limits, safe skill install."""

import socket
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import nimcode.tools.cmd as cmd  # noqa: E402
from nimcode import skills  # noqa: E402
from nimcode.config import Config, _validate_config  # noqa: E402

PUBLIC_IP = "93.184.216.34"


def _cfg(**kw):
    base = dict(disable_docker=False, timeout=30, output_cap=100000)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr(cmd, "is_docker_available", lambda: False)


@pytest.fixture
def public_dns(monkeypatch):
    def fake(host, *a, **k):
        if host == "internal.example":
            return [(socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))]
        return [(socket.AF_INET, 0, 0, "", (PUBLIC_IP, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


# --- 7. subagents never run on the host ---


def test_subagent_without_docker_is_refused(tmp_path, no_docker):
    sub = _cfg(_subagent_depth=1)
    for tool, args in (
        (cmd.execute_command_tool, {"command": "echo hi"}),
        (cmd.run_tests_tool, {"command": "echo hi"}),
        (cmd.start_background_process_tool, {"command": "echo hi"}),
    ):
        assert tool(args, tmp_path, sub) == cmd.SUBAGENT_HOST_REFUSAL
    # disable_docker / use_docker=false can't open the host either.
    sub_off = _cfg(_subagent_depth=2, disable_docker=True)
    out = cmd.execute_command_tool({"command": "echo hi"}, tmp_path, sub_off)
    assert out == cmd.SUBAGENT_HOST_REFUSAL
    assert (
        cmd.execute_test_runner(tmp_path, sub, "echo hi") == cmd.SUBAGENT_HOST_REFUSAL
    )
    assert not cmd.BACKGROUND_PROCESSES


def test_main_agent_falls_back_to_host_with_note(tmp_path, no_docker):
    out = cmd.execute_command_tool({"command": "echo hi"}, tmp_path, _cfg())
    assert "fell back to host" in out and "hi" in out


def test_no_fallback_note_when_host_requested(tmp_path, no_docker):
    out = cmd.execute_command_tool(
        {"command": "echo hi"}, tmp_path, _cfg(disable_docker=True)
    )
    assert "fell back" not in out and "[exit code: 0]" in out
    out = cmd.execute_command_tool(
        {"command": "echo hi", "use_docker": False}, tmp_path, _cfg()
    )
    assert "fell back" not in out


# --- 8. docker limits ---


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_docker_argv_default_limits(tmp_path):
    for flags in (["--rm"], ["-d", "--name", "x"]):
        argv = cmd._docker_run_argv(flags, "echo", tmp_path, Config())
        assert _flag(argv, "--memory") == "2g"
        assert _flag(argv, "--pids-limit") == "512"
        assert _flag(argv, "--cpus") == "2"
        assert _flag(argv, "--network") == "bridge"
        assert argv[-4] == "python:3.11-slim"


def test_docker_argv_network_none_and_disabled_limits(tmp_path):
    cfg = Config(docker_network="none", docker_memory="", docker_pids_limit=0)
    argv = cmd._docker_run_argv(["--rm"], "echo", tmp_path, cfg)
    assert _flag(argv, "--network") == "none"
    assert "--memory" not in argv and "--pids-limit" not in argv
    # Even an unvalidated config can't select the host network.
    argv = cmd._docker_run_argv(["--rm"], "echo", tmp_path, _cfg(docker_network="host"))
    assert _flag(argv, "--network") == "bridge"


def test_validate_docker_limits():
    cfg = Config(
        docker_network="host",
        docker_memory="lots",
        docker_cpus="0",
        docker_pids_limit="-3",
    )
    with pytest.warns(UserWarning):
        _validate_config(cfg)
    assert cfg.docker_network == "bridge"
    assert cfg.docker_memory == "2g"
    assert cfg.docker_cpus == ""
    assert cfg.docker_pids_limit == 512
    cfg = Config(docker_network="NONE", docker_memory="512m", docker_cpus="1.5")
    _validate_config(cfg)
    assert (cfg.docker_network, cfg.docker_memory, cfg.docker_cpus) == (
        "none",
        "512m",
        "1.5",
    )


# --- 9. safe skill install ---


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    return d


def test_skill_url_check_blocks_private_hosts(public_dns):
    assert skills._is_url_allowed_for_skill("https://example.com/a.md")[0]
    for bad in (
        "http://10.0.0.1/a.md",
        "http://169.254.169.254/latest",
        "https://internal.example/a.md",
        "file:///etc/passwd",
        "ext::sh -c id",
    ):
        assert not skills._is_url_allowed_for_skill(bad)[0], bad


def test_redirect_to_private_host_is_blocked(public_dns):
    handler = skills._GuardedRedirect()
    req = urllib.request.Request("https://example.com/a.md")
    with pytest.raises(urllib.error.URLError, match="refused"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://10.0.0.1/x")
    ok = handler.redirect_request(
        req, None, 302, "Found", {}, "https://example.com/b.md"
    )
    assert ok.full_url == "https://example.com/b.md"


def test_fetch_preview_commit_and_discard(skills_dir, public_dns, monkeypatch):
    body = "\n".join(f"line {i}" for i in range(100))

    def fake_download(url, dest, max_bytes, timeout=60):
        dest.write_text(body, encoding="utf-8")

    monkeypatch.setattr(skills, "_download_with_limits", fake_download)

    staging, preview, sha = skills.fetch_skill("https://example.com/demo.md")
    assert staging.is_dir() and not skills.list_skills()
    assert sha in preview and "SKILL.md" in preview
    assert "line 39" in preview and "line 40" not in preview

    msg = skills.commit_skill(staging, "demo", "https://example.com/demo.md", sha)
    assert msg.startswith("Successfully"), msg
    assert skills.list_skills() == ["demo"]
    assert skills.get_skill_content("demo") == body
    assert '"sha256"' in (skills_dir / "demo" / ".source.json").read_text()
    with pytest.raises(ValueError, match="already exists"):
        skills.fetch_skill("https://example.com/demo.md")

    staging, _, _ = skills.fetch_skill("https://example.com/other.md")
    skills.discard_skill(staging)
    assert not staging.exists() and skills.list_skills() == ["demo"]
    # commit_skill refuses dirs outside the staging area.
    assert skills.commit_skill(skills_dir / "demo", "x", "u", "s").startswith("Error")


def test_fetch_failure_raises_value_error_and_cleans_up(
    skills_dir, public_dns, monkeypatch
):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(skills, "_download_with_limits", boom)
    with pytest.raises(ValueError, match="network down"):
        skills.fetch_skill("https://example.com/demo.md")
    assert not any((skills_dir / ".staging").iterdir())
    assert skills.install_skill("http://10.0.0.1/a.md").startswith("Error:")


def test_git_clone_disables_redirects(skills_dir, public_dns, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        Path(argv[-1], "SKILL.md").write_text("hi", encoding="utf-8")

    monkeypatch.setattr(skills.subprocess, "run", fake_run)
    staging, preview, _ = skills.fetch_skill("https://github.com/acme/tool.git")
    assert "http.followRedirects=false" in seen["argv"]
    assert seen["argv"][-1] == str(staging)
    skills.discard_skill(staging)


def test_get_skill_content_returns_none_on_error(skills_dir):
    skills_dir.mkdir()
    (skills_dir / "empty").mkdir()
    (skills_dir / "big.md").write_text("x" * 300_000)
    for name in ("missing", "empty", "big", "../etc", "", None):
        assert skills.get_skill_content(name) is None


def test_split_form_denied_flag_drops_its_value():
    from types import SimpleNamespace

    from nimcode.tools.cmd import _safe_docker_extra_args

    cfg = SimpleNamespace(
        docker_extra_args=[
            "--privileged",
            "-e",
            "A=1",
            "-v",
            "/:/host",
            "--mount",
            "type=bind,src=/,dst=/h",
            "-itv",
            "/:/x",
            "--rm",
        ]
    )
    assert _safe_docker_extra_args(cfg) == ["-e", "A=1", "--rm"]

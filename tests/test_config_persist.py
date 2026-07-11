import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nimcode.config import (
    Config,
    save_last_used_model,
    load_last_used_model,
    load_config,
)
import tempfile


def test_config_docker_fields():
    config = Config()
    assert hasattr(config, "docker_image")
    assert hasattr(config, "docker_expose_ports")
    assert hasattr(config, "docker_extra_args")
    assert hasattr(config, "disable_docker")
    assert config.docker_image == "python:3.11-slim"
    assert config.docker_expose_ports == []
    assert config.docker_extra_args == []
    assert config.disable_docker is False


def test_save_load_model_with_docker():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        config = Config()
        config.docker_image = "custom:latest"
        config.docker_expose_ports = [8080, 3000]
        config.docker_extra_args = ["--memory=512m"]
        config.disable_docker = True
        config.fast_model = "test/model"

        save_last_used_model(config, config.fast_model, workspace)

        loaded = load_last_used_model(workspace)
        assert loaded is not None
        assert loaded["model"] == "test/model"
        assert loaded["docker_image"] == "custom:latest"
        assert loaded["docker_expose_ports"] == [8080, 3000]
        assert loaded["docker_extra_args"] == ["--memory=512m"]
        assert loaded["disable_docker"] is True


def test_load_config_applies_docker_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        config = Config()
        config.docker_image = "custom:latest"
        config.docker_expose_ports = [8080]
        config.docker_extra_args = ["--memory=512m"]
        config.disable_docker = True
        config.fast_model = "test/model"

        save_last_used_model(config, config.fast_model, workspace)

        loaded_config = load_config(workspace)
        assert loaded_config.docker_image == "custom:latest"
        assert loaded_config.docker_expose_ports == [8080]
        assert loaded_config.docker_extra_args == ["--memory=512m"]
        assert loaded_config.disable_docker is True
        assert loaded_config.fast_model == "test/model"


def test_get_project_nimcode_dir_hashing_and_migration():
    from nimcode.config import get_project_nimcode_dir
    import shutil

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        
        # Test basic directory retrieval (hashed name)
        p_dir = get_project_nimcode_dir(workspace)
        assert p_dir.name.startswith(f"{workspace.name}_")
        assert len(p_dir.name) == len(workspace.name) + 9  # name + _ + 8-char hash
        
        # Test migration from legacy un-hashed directory
        legacy_dir = Path.home() / ".nimcode" / workspace.name
        legacy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = legacy_dir / "test.txt"
        dummy_file.write_text("hello", encoding="utf-8")
        
        # Clean the hashed project directory if it was created
        if p_dir.exists():
            shutil.rmtree(p_dir)
            
        p_dir = get_project_nimcode_dir(workspace)
        # The hashed dir should have been created, and the file migrated
        assert p_dir.exists()
        assert (p_dir / "test.txt").exists()
        assert (p_dir / "test.txt").read_text(encoding="utf-8") == "hello"
        assert not legacy_dir.exists()
        
        # Cleanup
        if p_dir.exists():
            shutil.rmtree(p_dir)


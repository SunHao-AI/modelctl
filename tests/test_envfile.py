import os

from modelctl.core.envfile import PROJECT_ROOT, load_env, parse_env_file


def test_parse_env_file_basic(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# 注释\nFOO=bar\nEMPTY=\nQUOTED=\"a b\"\nSINGLE='x'\n", encoding="utf-8")
    assert parse_env_file(p) == {"FOO": "bar", "EMPTY": "", "QUOTED": "a b", "SINGLE": "x"}


def test_parse_env_file_missing(tmp_path):
    assert parse_env_file(tmp_path / "nope.env") == {}


def test_load_env_no_override(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("KEEP=from_file\nNEW=new_value\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "from_env")
    monkeypatch.delenv("NEW", raising=False)
    load_env(p)
    assert os.environ["KEEP"] == "from_env"
    assert os.environ["NEW"] == "new_value"


def test_project_root_points_at_repo_root():
    """回归测试：PROJECT_ROOT 必须解析到仓库根目录（而非 src/）。"""
    assert PROJECT_ROOT.name == "modelctl"
    assert (PROJECT_ROOT / "models").is_dir()
    assert (PROJECT_ROOT / ".env").parent == PROJECT_ROOT

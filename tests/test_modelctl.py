from modelctl import cli


def test_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    assert rc == 0


def test_profile_error_exit_code(tmp_path, capsys):
    rc = cli.main(["start", "ghost", "--models-dir", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    # 错误消息经 loguru 输出到 stderr
    assert "不存在" in captured.out or "不存在" in captured.err


def test_status_output(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.yaml").write_text("name: a\nengine: vllm\nport: 8000\n", encoding="utf-8")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["status", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "a" in out and "vllm" in out and "8000" in out


def test_restart_accepts_timeout():
    """restart 转调 start，必须提供 --timeout 参数（默认 300）。"""
    args = cli.build_parser().parse_args(["restart", "x"])
    assert args.timeout == 300
    args = cli.build_parser().parse_args(["restart", "x", "--timeout", "60"])
    assert args.timeout == 60


def test_nginx_snippet_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "qwen.yaml").write_text("name: qwen3.8\nengine: ollama\nport: 7000\n", encoding="utf-8")
    rc = cli.main(["nginx-snippet", "--node", "240", "--host", "9.9.9.90", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "~^/240/llm/qwen3.8/  http://9.9.9.90:7000;" in out


def test_gateway_start_detaches(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(name=name, cmd=cmd) or 123)
    monkeypatch.setattr(cli, "is_running", lambda name: False)
    rc = cli.main(["gateway", "start"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["cmd"][-1].endswith("modelctl.core.gateway")


def test_gateway_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "stop_instance", lambda name, port, patterns: called.update(name=name, port=port))
    rc = cli.main(["gateway", "stop"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["port"] == 5003


def _write_unsloth_ui_profile(tmp_path) -> None:
    (tmp_path / "u.yaml").write_text(
        "name: u\nengine: unsloth\nport: 30000\napi_key: k\n"
        "unsloth:\n  model: m\n  ui:\n    port: 8888\n    allow_from: [192.168.77.202]\n",
        encoding="utf-8",
    )


def test_ui_start_detaches_and_adds_ufw_rule(tmp_path, monkeypatch):
    _write_unsloth_ui_profile(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(name=name, cmd=cmd) or 123)
    monkeypatch.setattr(cli, "is_running", lambda name: False)
    rules: list = []
    monkeypatch.setattr(cli, "ensure_ufw_allow", lambda src, port: rules.append((src, port)) or True)
    rc = cli.main(["ui", "start", "u", "--models-dir", str(tmp_path)])
    assert rc == 0
    assert called["name"] == "ui-u"
    assert called["cmd"][0] == "unsloth" and "-p" in called["cmd"] and "8888" in called["cmd"]
    assert rules == [("192.168.77.202", 8888)]


def test_ui_start_cli_overrides_yaml(tmp_path, monkeypatch):
    _write_unsloth_ui_profile(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(cmd=cmd) or 1)
    monkeypatch.setattr(cli, "is_running", lambda name: False)
    rules: list = []
    monkeypatch.setattr(cli, "ensure_ufw_allow", lambda src, port: rules.append((src, port)) or True)
    rc = cli.main(
        ["ui", "start", "u", "--port", "9999", "--allow-from", "1.2.3.4", "--models-dir", str(tmp_path)]
    )
    assert rc == 0
    assert "9999" in called["cmd"]
    assert rules == [("1.2.3.4", 9999)]


def test_ui_stop(tmp_path, monkeypatch):
    _write_unsloth_ui_profile(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "stop_instance", lambda name, port, patterns: called.update(name=name, port=port, patterns=patterns))
    monkeypatch.setattr(cli, "is_running", lambda name: True)
    rc = cli.main(["ui", "stop", "u", "--models-dir", str(tmp_path)])
    assert rc == 0
    assert called["name"] == "ui-u" and called["port"] == 8888
    assert called["patterns"] == []  # 不按进程名 pkill，避免误杀推理实例


def test_ui_start_rejects_unsupported_engine(tmp_path, monkeypatch):
    (tmp_path / "v.yaml").write_text("name: v\nengine: vllm\nport: 8100\napi_key: k\n", encoding="utf-8")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["ui", "start", "v", "--models-dir", str(tmp_path)])
    assert rc == 2


def test_ensure_ufw_allow_runs_rule(monkeypatch):
    import modelctl.core.ufw as ufw_mod

    monkeypatch.setattr(ufw_mod.shutil, "which", lambda name: "/usr/sbin/ufw")
    ran: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        ran["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(ufw_mod.subprocess, "run", fake_run)
    assert ufw_mod.ensure_ufw_allow("192.168.77.202", 8888) is True
    assert ran["cmd"] == ["ufw", "allow", "from", "192.168.77.202", "to", "any", "port", "8888", "proto", "tcp"]


def test_ensure_ufw_allow_missing_binary(monkeypatch):
    import modelctl.core.ufw as ufw_mod

    monkeypatch.setattr(ufw_mod.shutil, "which", lambda name: None)
    assert ufw_mod.ensure_ufw_allow("1.2.3.4", 8888) is False

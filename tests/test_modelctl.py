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

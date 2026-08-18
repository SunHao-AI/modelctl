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

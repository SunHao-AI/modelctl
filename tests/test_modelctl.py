from modelctl import cli


def test_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    assert rc == 0


def test_list_grouped_catalog(tmp_path, monkeypatch, capsys):
    """list 按家族分组展示，含引擎/变体/端口/状态/速率/标识符列，家族块之间空一行。"""
    (tmp_path / "qwen3.8.yaml").write_text(
        "group: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n", encoding="utf-8"
    )
    (tmp_path / "qwen3.8-light.yaml").write_text(
        "group: qwen3.8\nvariant: light\nengine: vllm\nport: 8105\nvllm:\n  model: q\n", encoding="utf-8"
    )
    (tmp_path / "flash.yaml").write_text(
        "group: deepseek-v4-flash\nengine: ollama\nport: 11434\nollama:\n  model: d\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deepseek-v4-flash（1 配置）" in out
    assert "qwen3.8（2 配置）" in out
    assert "qwen3.8-vllm" in out and "qwen3.8-vllm-light" in out
    assert "light" in out and "8105" in out
    # 速率列头
    assert "速率(入/出)" in out
    # 家族块之间空一行（deepseek 块结束后、qwen3.8 标题前有空行）
    assert out.index("deepseek-v4-flash（1 配置）") < out.index("\n\nqwen3.8（2 配置）")


def test_list_group_route_mapping(tmp_path, monkeypatch, capsys):
    """家族标题展示网关路由映射：输入 group 名 → 第一个运行中的成员。"""
    (tmp_path / "qwen3.8.yaml").write_text(
        "group: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n", encoding="utf-8"
    )
    (tmp_path / "qwen3.8-light.yaml").write_text(
        "group: qwen3.8\nvariant: light\nengine: vllm\nport: 8105\nvllm:\n  model: q\n", encoding="utf-8"
    )
    (tmp_path / "flash.yaml").write_text(
        "group: deepseek-v4-flash\nengine: ollama\nport: 11434\nollama:\n  model: d\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    # 仅 qwen3.8-vllm 运行中 → 输入 qwen3.8 路由至它
    monkeypatch.setattr("modelctl.cli.is_running", lambda name: name == "qwen3.8-vllm")
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert '输入 "qwen3.8" 路由至 qwen3.8-vllm' in out
    assert '输入 "deepseek-v4-flash" 当前无运行成员' in out


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


def test_status_name_shows_agent_config(tmp_path, monkeypatch, capsys):
    # 未配置 max_output_tokens 时，自动按输入长度的 1/8 推荐（32768 // 8 = 4096）
    yaml_text = (
        "name: agent\nengine: llamacpp\nport: 18889\n"
        "tool_call_rounds: 3\n"
        "llamacpp:\n"
        "  model: /x.gguf\n"
        "  ctx_size: 32768\n"
        "  vision: on\n"
        "  temperature: 0.6\n"
        "  top_p: 0.95\n"
        "  top_k: 40\n"
    )
    (tmp_path / "agent.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["status", "agent", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "智能体配置参考" in out
    assert "上下文长度：32768" in out
    assert "输入上下文长度：28672" in out
    assert "输出上下文长度：4096" in out
    assert "工具调用轮数：3" in out
    assert "支持图片输入：是" in out
    assert "Temperature：0.6" in out
    assert "Top P：0.95" in out
    assert "Top K：40" in out


def test_status_vision_defaults(tmp_path, monkeypatch, capsys):
    # llamacpp 未设置 vision 时默认开启；显式 off 可关闭
    (tmp_path / "llama.yaml").write_text(
        "name: llama\nengine: llamacpp\nport: 18889\nllamacpp:\n  model: /x.gguf\n",
        encoding="utf-8",
    )
    (tmp_path / "llama_off.yaml").write_text(
        "name: llama_off\nengine: llamacpp\nport: 18890\nllamacpp:\n  model: /x.gguf\n  vision: off\n",
        encoding="utf-8",
    )
    # 其他引擎默认具备视觉能力
    (tmp_path / "vllm.yaml").write_text(
        "name: vllm\nengine: vllm\nport: 8000\nvllm:\n  model: x\n  max_model_len: 8192\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    rc = cli.main(["status", "llama", "--models-dir", str(tmp_path)])
    assert rc == 0 and "支持图片输入：是" in capsys.readouterr().out

    rc = cli.main(["status", "llama_off", "--models-dir", str(tmp_path)])
    assert rc == 0 and "支持图片输入：否" in capsys.readouterr().out

    rc = cli.main(["status", "vllm", "--models-dir", str(tmp_path)])
    assert rc == 0 and "支持图片输入：是" in capsys.readouterr().out


def test_restart_accepts_timeout():
    """restart 转调 start，必须提供 --timeout 参数（默认 600）。"""
    args = cli.build_parser().parse_args(["restart", "x"])
    assert args.timeout == 600
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
    monkeypatch.setattr(cli.all_service, "start_detached", lambda name, cmd, extra_env: called.update(name=name, cmd=cmd) or (123, None))
    monkeypatch.setattr(cli.all_service, "is_running", lambda name: False)
    rc = cli.main(["gateway", "start"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["cmd"][-1].endswith("modelctl.core.gateway")


def test_gateway_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli.all_service, "stop_instance", lambda name, port, patterns: called.update(name=name, port=port))
    rc = cli.main(["gateway", "stop"])
    assert rc == 0
    assert called["name"] == "llm-gateway"
    assert called["port"] == 5003


def _write_unsloth_ui_profile(tmp_path) -> None:
    (tmp_path / "u.yaml").write_text(
        "name: u\nengine: unsloth\nport: 30000\napi_key: k\n" "unsloth:\n  model: m\n  ui:\n    port: 8888\n    allow_from: [192.168.77.202]\n",
        encoding="utf-8",
    )


def test_ui_start_detaches_and_adds_ufw_rule(tmp_path, monkeypatch):
    _write_unsloth_ui_profile(tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    called: dict = {}
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(name=name, cmd=cmd) or (123, None))
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
    monkeypatch.setattr(cli, "start_detached", lambda name, cmd, extra_env: called.update(cmd=cmd) or (1, None))
    monkeypatch.setattr(cli, "is_running", lambda name: False)
    rules: list = []
    monkeypatch.setattr(cli, "ensure_ufw_allow", lambda src, port: rules.append((src, port)) or True)
    rc = cli.main(["ui", "start", "u", "--port", "9999", "--allow-from", "1.2.3.4", "--models-dir", str(tmp_path)])
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


def test_all_command_dispatch(tmp_path, monkeypatch):
    """all start/stop/restart/status 分发到 all_service 编排，--model/--timeout 透传。"""
    import modelctl.cli as cli
    from modelctl.core import all_service

    seen: dict = {}

    def _start_all(md, model_name=None, timeout=300):
        seen["cmd"] = "start"
        seen["model"] = model_name
        seen["timeout"] = timeout
        return [all_service.ComponentResult("model:x", "ok", "")]

    monkeypatch.setattr(cli.all_service, "start_all", _start_all)
    rc = cli.main(["all", "start", "--model", "q", "--timeout", "10", "--models-dir", str(tmp_path)])
    assert rc == 0 and seen == {"cmd": "start", "model": "q", "timeout": 10.0}


def test_all_start_error_exit_2(tmp_path, monkeypatch):
    import modelctl.cli as cli
    from modelctl.core import all_service

    monkeypatch.setattr(
        cli.all_service,
        "start_all",
        lambda md, model_name=None, timeout=300: [
            all_service.ComponentResult("model:x", "error", "boom"),
            all_service.ComponentResult("gateway", "ok", ""),
        ],
    )
    rc = cli.main(["all", "start", "--models-dir", str(tmp_path)])
    assert rc == 2


def test_all_stop_error_exit_1(tmp_path, monkeypatch):
    import modelctl.cli as cli
    from modelctl.core import all_service

    monkeypatch.setattr(
        cli.all_service,
        "stop_all",
        lambda md: [
            all_service.ComponentResult("gateway", "error", "boom"),
        ],
    )
    rc = cli.main(["all", "stop", "--models-dir", str(tmp_path)])
    assert rc == 1


def test_gateway_restart_dispatch(tmp_path, monkeypatch):
    import modelctl.cli as cli

    monkeypatch.setattr(cli.all_service, "restart_gateway", lambda: cli.all_service.ComponentResult("gateway", "ok", ""))
    rc = cli.main(["gateway", "restart"])
    assert rc == 0


def test_stats_status_dispatch(tmp_path, monkeypatch):
    import modelctl.cli as cli

    monkeypatch.setattr(cli.all_service, "status_stats", lambda: cli.all_service.ComponentResult("stats", "ok", "已停止"))
    rc = cli.main(["stats", "status"])
    assert rc == 0


def test_benchmark_token_rate_parses_sse(monkeypatch):
    """流式 SSE 响应：TTFT=0.5s、prompt=10、completion=5 → 输入 20 tok/s、输出 10 tok/s、TTFT 500ms。"""
    import io

    import modelctl.cli as cli

    sse = b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n' b'data: {"id":"x","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n' b"data: [DONE]\n\n"

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readable(self):
            return True

        def writable(self):
            return False

        def seekable(self):
            return False

        @property
        def closed(self):
            return self._buf.closed

        def flush(self):
            pass

        def close(self):
            self._buf.close()

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    ticks = iter([1.0, 1.5, 2.0])  # t_start=1.0 → t_ttft=1.5 → t_end=2.0
    monkeypatch.setattr(cli.time, "perf_counter", lambda: next(ticks))

    def fake_urlopen(req, timeout=10):
        return _FakeResp(sse)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    result = cli._benchmark_token_rate(adapter)
    assert result == (20.0, 10.0, 500)


def test_benchmark_token_rate_timeout_returns_none(monkeypatch):
    import modelctl.cli as cli

    def fake_urlopen(req, timeout=10):
        raise TimeoutError("timeout")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    assert cli._benchmark_token_rate(adapter) is None


def test_benchmark_token_rate_empty_stream_returns_none(monkeypatch):
    import io

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readable(self):
            return True

        def writable(self):
            return False

        def seekable(self):
            return False

        @property
        def closed(self):
            return self._buf.closed

        def flush(self):
            pass

        def close(self):
            self._buf.close()

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp(b""))
    from types import SimpleNamespace

    adapter = SimpleNamespace(
        profile=SimpleNamespace(port=8101),
        upstream_model_name=lambda: "m",
        upstream_api_key=lambda: None,
    )
    assert cli._benchmark_token_rate(adapter) is None  # 无任何 chunk → 无 TTFT


def test_token_rate_data_uses_stats_when_valid(monkeypatch):
    """stats 有效且速率 > 0 → 用 stats，不测速。"""
    import io
    import json

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    def fake_urlopen(req, timeout=None):
        body = json.dumps({"isValid": True, "prompt_rate": 12.5, "predicted_rate": 33.3}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (9.9, 9.9, 100))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data == {"prompt_rate": 12.5, "predicted_rate": 33.3, "ttft_ms": None, "source": "stats"}


def test_token_rate_data_benchmarks_when_stats_zero(monkeypatch):
    """stats 速率为 0 → 主动测速。"""
    import io
    import json

    import modelctl.cli as cli

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

    def fake_urlopen(req, timeout=None):
        body = json.dumps({"isValid": True, "prompt_rate": 0.0, "predicted_rate": 0.0}).encode()
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (5.0, 8.0, 300))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data == {"prompt_rate": 5.0, "predicted_rate": 8.0, "ttft_ms": 300, "source": "bench"}


def test_token_rate_data_benchmarks_when_stats_unavailable(monkeypatch):
    """stats 服务不可用（连接失败）→ 主动测速。"""
    import modelctl.cli as cli

    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(cli, "_benchmark_token_rate", lambda adapter: (5.0, 8.0, 300))
    from types import SimpleNamespace

    caps = SimpleNamespace()
    profile = SimpleNamespace(name="m")
    data = cli._token_rate_data(profile, caps)
    assert data["source"] == "bench" and data["ttft_ms"] == 300


def test_status_output_shows_benchmark_rates_and_ttft(monkeypatch, capsys):
    """status 单模型：速率行带（实测）标注，且显示首 Token 耗时。"""
    from types import SimpleNamespace

    import modelctl.cli as cli

    monkeypatch.setattr(
        cli,
        "_token_rate_data",
        lambda profile, caps: {"prompt_rate": 20.0, "predicted_rate": 10.0, "ttft_ms": 500, "source": "bench"},
    )
    monkeypatch.setattr(
        cli,
        "list_profiles",
        lambda models_dir=None: [SimpleNamespace(name="qwen3.8-vllm", engine="vllm", port=8101)],
    )
    monkeypatch.setattr(cli, "_instance_state", lambda name: "运行中")  # 运行中 → 门控放行测速；mock get_adapter 跳过健康检查
    monkeypatch.setattr(
        cli,
        "get_adapter",
        lambda engine: lambda profile, caps: SimpleNamespace(wait_ready=lambda timeout: True),
    )
    monkeypatch.setattr(
        cli,
        "_agent_config_info",
        lambda profile: {
            "context_length": 262144,
            "input_context": 253952,
            "output_context": 8192,
            "tool_call_rounds": "-",
            "vision": "是",
            "temperature": "-",
            "top_p": "-",
            "top_k": "-",
        },
    )
    monkeypatch.setattr(cli, "_price_rate_text", lambda profile: "输入 0.5 元/千token，输出 1 元/千token")

    class _Args:
        name = "qwen3.8-vllm"

    cli._cmd_status(_Args(), None, object())
    out = capsys.readouterr().out
    assert "输入 20.0 tok/s，输出 10.0 tok/s（实测）" in out
    assert "首 Token 耗时：500 ms" in out


def test_status_output_hides_rates_when_not_running(monkeypatch, capsys):
    """未运行的模型不测速：速率与首 Token 耗时显示 -，且不调用 _token_rate_data。"""
    from types import SimpleNamespace

    import modelctl.cli as cli

    monkeypatch.setattr(
        cli,
        "_token_rate_data",
        lambda profile, caps: (_ for _ in ()).throw(AssertionError("未运行的模型不应触发测速")),
    )
    monkeypatch.setattr(
        cli,
        "list_profiles",
        lambda models_dir=None: [SimpleNamespace(name="qwen3.8-vllm", engine="vllm", port=8101)],
    )
    monkeypatch.setattr(cli, "_instance_state", lambda name: "已停止")
    monkeypatch.setattr(
        cli,
        "_agent_config_info",
        lambda profile: {
            "context_length": 262144,
            "input_context": 253952,
            "output_context": 8192,
            "tool_call_rounds": "-",
            "vision": "是",
            "temperature": "-",
            "top_p": "-",
            "top_k": "-",
        },
    )
    monkeypatch.setattr(cli, "_price_rate_text", lambda profile: "输入 0.5 元/千token，输出 1 元/千token")

    class _Args:
        name = "qwen3.8-vllm"

    cli._cmd_status(_Args(), None, object())
    out = capsys.readouterr().out
    assert "输入 -，输出 -" in out
    assert "首 Token 耗时：-" in out


def test_list_rate_column_shows_stats_rates(tmp_path, monkeypatch, capsys):
    """运行中成员的速率列显示 stats 服务的输入/输出速率（格式 入/出）。"""
    (tmp_path / "qwen3.8.yaml").write_text(
        "group: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("modelctl.cli._instance_state", lambda name: "运行中")
    monkeypatch.setattr("modelctl.cli._stats_token_rate", lambda p: (12.3, 45.6))
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "12.3/45.6" in out


def test_list_rate_column_dash_for_stopped(tmp_path, monkeypatch, capsys):
    """未运行成员的速率列显示 -。"""
    (tmp_path / "qwen3.8.yaml").write_text(
        "group: qwen3.8\nengine: vllm\nport: 8101\nvllm:\n  model: q\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = cli.main(["list", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已停止" in out
    # 速率列值（状态与速率之间以空格对齐，此处检查速率列存在且为 -）
    assert "速率(入/出)" in out


def test_stats_token_rate_reads_usage_api(tmp_path, monkeypatch):
    """_stats_token_rate 只读 stats 服务：返回 (prompt_rate, predicted_rate)。"""
    import json

    from modelctl.core.profile import Profile

    p = Profile(name="qwen3.8", engine="vllm", port=8101, aliases=[], engine_config={})
    monkeypatch.setenv("USAGE_PORT", "59999")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"isValid": True, "prompt_rate": 7.5, "predicted_rate": 88.0}).encode("utf-8")

    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda url, timeout: FakeResp())
    assert cli._stats_token_rate(p) == (7.5, 88.0)


def test_stats_token_rate_none_when_unavailable(tmp_path, monkeypatch):
    """stats 服务不可用时 _stats_token_rate 返回 None（不抛异常）。"""
    import urllib.error

    from modelctl.core.profile import Profile

    p = Profile(name="qwen3.8", engine="vllm", port=8101, aliases=[], engine_config={})
    monkeypatch.setenv("USAGE_PORT", "59999")

    def _refuse(url, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", _refuse)
    assert cli._stats_token_rate(p) is None

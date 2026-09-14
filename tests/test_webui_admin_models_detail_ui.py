"""admin_models 详情/YAML/Unsloth-UI/SSE 日志流的长路径补测。

与 test_webui_admin_models_lifecycle（启停投递契约）互补，覆盖其未触达的 45% 缺口：
- get_model 详情的存活/脱敏/日志路径拼接；
- get_model_yaml 的真实文件读取与 404；
- Unsloth Web 控制台 start/stop（真 ufw / 真 start_detached **必须**打桩）；
- 日志辅助纯函数（_mask_key/_docker_json_line_text/_launch_log_effective/_tail_from_launch_log）；
- _sse_log_stream 的 venv/docker 两分支帧序（真 tmp 文件当日志，sleep 打桩提速）。
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from modelctl.core.gateway import create_app  # noqa: E402
from modelctl.core.profile import Profile  # noqa: E402
from modelctl.core.webui import admin_models  # noqa: E402

KEY = "test_key_models_detail"


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    logger.remove()
    app = create_app(admin=True)
    monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
    with TestClient(app) as c:
        yield c


def _h():
    return {"Authorization": f"Bearer {KEY}"}


def _profile(name="m1", engine="vllm", **kw):
    p = Profile(name=name, engine=engine, port=kw.pop("port", 8000),
                engine_config=kw.pop("engine_config", {"model": "/m/x"}), **kw)
    return p


def _patch_profiles(monkeypatch, *profiles):
    monkeypatch.setattr("modelctl.core.profile.list_profiles", lambda d=None: list(profiles))


def _frames(payload: str) -> list[str]:
    return [b.split("event: ", 1)[1].split("\n", 1)[0]
            for b in payload.split("\n\n") if b.startswith("event: ")]


class TestGetModel:
    def test_running_detail_masks_key(self, admin_client, monkeypatch):
        p = _profile(api_key="sk-secret-3876",
                     engine_config={"model": "/m/x", "api_key": "cfg-key-9999"})
        _patch_profiles(monkeypatch, p)
        monkeypatch.setattr("modelctl.core.process.is_running_any", lambda n, pr: True)
        lp = admin_client.get("/admin/api/models/m1", headers=_h()).json()
        assert lp["state"] == "running" and lp["health"] == "healthy"
        assert lp["api_key_masked"] == "***3876"
        assert lp["engine_config"]["api_key"] == "***9999"  # engine_config 内的 key 也脱敏
        assert lp["model_path"] == "/m/x"

    def test_stopped_when_probe_raises(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile())

        def boom(n, pr):
            raise OSError("ps 失败")

        monkeypatch.setattr("modelctl.core.process.is_running_any", boom)
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: None)
        body = admin_client.get("/admin/api/models/m1", headers=_h()).json()
        assert body["state"] == "stopped" and body["health"] is None
        assert body["log_path"] is None and body["pid"] is None

    def test_404(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch)
        assert admin_client.get("/admin/api/models/ghost", headers=_h()).status_code == 404


class TestModelYaml:
    def test_reads_real_file(self, admin_client, monkeypatch, tmp_path):
        f = tmp_path / "m1.yaml"
        f.write_text("name: m1\nengine: vllm\n", encoding="utf-8")
        p = _profile()
        p.path = f
        _patch_profiles(monkeypatch, p)
        body = admin_client.get("/admin/api/models/m1/yaml", headers=_h()).json()
        assert body["content"].startswith("name: m1")
        assert body["path"] == str(f)

    @pytest.mark.parametrize("path_attr", [None, "missing"])
    def test_404_paths(self, admin_client, monkeypatch, tmp_path, path_attr):
        p = _profile()
        p.path = None if path_attr is None else tmp_path / "gone.yaml"
        _patch_profiles(monkeypatch, p)
        r = admin_client.get("/admin/api/models/m1/yaml", headers=_h())
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


class TestUnslothUi:
    def _ui_profile(self, monkeypatch, spec=None):
        p = _profile(name="u1", engine="unsloth")
        _patch_profiles(monkeypatch, p)
        import types

        adapter = types.SimpleNamespace(ui_spec=lambda **kw: spec)
        monkeypatch.setattr("modelctl.engines.get_adapter", lambda e: lambda pr, c: adapter)
        return adapter

    def test_404_and_412_engine(self, admin_client, monkeypatch):
        _patch_profiles(monkeypatch, _profile(name="m1", engine="vllm"))
        assert admin_client.post("/admin/api/models/ghost/ui/start", headers=_h()).status_code == 404
        assert admin_client.post("/admin/api/models/m1/ui/start", headers=_h()).status_code == 412
        assert admin_client.post("/admin/api/models/m1/ui/stop", headers=_h()).status_code == 412

    def test_start_happy_path_stubs_ufw_and_spawn(self, admin_client, monkeypatch, tmp_path):
        spec = {"cmd": ["python", "-m", "ui"], "env": {"E": "1"},
                "host": "0.0.0.0", "port": 8888, "allow_from": ["10.0.0.0/8"]}
        self._ui_profile(monkeypatch, spec=spec)
        ufw_calls, log = [], tmp_path / "launch-ui-u1.log"
        monkeypatch.setattr("modelctl.core.ufw.ensure_ufw_allow",
                            lambda src, port: ufw_calls.append((src, port)))
        monkeypatch.setattr("modelctl.core.process.start_detached",
                            lambda inst, cmd, env: (4242, object()))
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: False)
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)
        r = admin_client.post("/admin/api/models/u1/ui/start", json={"allow_from": "192.168.1.0/24"},
                              headers=_h())
        body = r.json()
        assert body["ok"] and body["pid"] == 4242
        assert body["url"] == "http://0.0.0.0:8888"
        assert body["allow_from"] == ["192.168.1.0/24"]  # 请求体覆盖 spec 默认
        assert ufw_calls == [("192.168.1.0/24", 8888)]
        assert body["log_path"] == str(log)

    def test_start_already_running(self, admin_client, monkeypatch):
        spec = {"cmd": ["x"], "env": {}, "host": "h", "port": 1, "allow_from": []}
        self._ui_profile(monkeypatch, spec=spec)
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: True)
        body = admin_client.post("/admin/api/models/u1/ui/start", headers=_h()).json()
        assert body["already_running"] is True

    def test_start_ufw_failure_tolerated(self, admin_client, monkeypatch):
        spec = {"cmd": ["x"], "env": {}, "host": "h", "port": 2, "allow_from": ["a", "b"]}
        self._ui_profile(monkeypatch, spec=spec)

        def boom(src, port):
            raise OSError("no ufw")

        monkeypatch.setattr("modelctl.core.ufw.ensure_ufw_allow", boom)
        monkeypatch.setattr("modelctl.core.process.start_detached",
                            lambda inst, cmd, env: (7, object()))
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: False)
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: None)
        body = admin_client.post("/admin/api/models/u1/ui/start", headers=_h()).json()
        assert body["ok"] and body["log_path"] is None  # ufw 全失败仍照常启动

    def test_start_no_spec_412(self, admin_client, monkeypatch):
        self._ui_profile(monkeypatch, spec=None)
        assert admin_client.post("/admin/api/models/u1/ui/start", headers=_h()).status_code == 412

    def test_stop_not_running(self, admin_client, monkeypatch):
        self._ui_profile(monkeypatch, spec={"port": 8888})
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: False)
        body = admin_client.post("/admin/api/models/u1/ui/stop", headers=_h()).json()
        assert body == {"ok": True, "detail": "Web 控制台未在运行"}

    def test_stop_running_calls_stop_instance(self, admin_client, monkeypatch):
        self._ui_profile(monkeypatch, spec={"port": 8888})
        stopped = []
        monkeypatch.setattr("modelctl.core.process.is_running", lambda n: True)
        monkeypatch.setattr("modelctl.core.process.stop_instance",
                            lambda inst, port, pats: stopped.append((inst, port)) or True)
        body = admin_client.post("/admin/api/models/u1/ui/stop", headers=_h()).json()
        assert body["ok"] and stopped == [("ui-u1", 8888)]


class TestLogHelpers:
    def test_mask_key(self):
        assert admin_models._mask_key(None) is None
        assert admin_models._mask_key("") is None
        assert admin_models._mask_key("abc") == "***"
        assert admin_models._mask_key("abcdefgh") == "***efgh"

    @pytest.mark.parametrize("entry,expect", [
        ('{"log":"hi\\n","stream":"stdout"}', "hi"),
        ('{"log":"","stream":"stdout"}', None),
        ('{"nolog":1}', None),
        ("not-json", "not-json"),
        ("", None),
    ])
    def test_docker_json_line_text(self, entry, expect):
        assert admin_models._docker_json_line_text(entry) == expect

    def test_launch_log_effective(self):
        assert admin_models._launch_log_effective(["Loaded 40B weights"]) is True
        assert admin_models._launch_log_effective(["e2b3c1d4f5a6"]) is False  # 容器 ID 行
        assert admin_models._launch_log_effective(["vllm"]) is False
        assert admin_models._launch_log_effective([]) is False

    def test_tail_from_launch_log(self, monkeypatch, tmp_path):
        log = tmp_path / "launch-m1.log"
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: None)
        assert admin_models._tail_from_launch_log("m1") == []
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)
        monkeypatch.setattr("modelctl.core.process.tail_file", lambda p, lines: "")
        assert admin_models._tail_from_launch_log("m1") == []
        monkeypatch.setattr("modelctl.core.process.tail_file", lambda p, lines: "a\nb\n")
        assert admin_models._tail_from_launch_log("m1") == ["a", "b"]

    def test_read_file_range(self, tmp_path):
        f = tmp_path / "f.log"
        f.write_bytes(b"0123456789")
        assert admin_models._read_file_range(f, 2, 5) == "234"


def _fast_stream(monkeypatch, on_first_poll=None):
    """把 SSE 流的 2s 轮询 sleep 打掉（仅影响 admin_models 模块内引用）。

    admin_models.asyncio 就是全局 asyncio，必须先捕获真 sleep，
    否则替换函数内部再调 asyncio.sleep 会无限递归。

    on_first_poll 在生成器**第一次进入轮询 sleep 时**执行：venv 分支的字节
    基线 pos 在首帧 yield *之后* 才计算（admin_models.py L807-813），拉帧间隙
    写文件会被基线吞掉；挂在首轮 sleep 里才能确定性触发"基线之后的新增"。
    """
    real_sleep = asyncio.sleep
    state = {"n": 0}

    async def no_sleep(_s):
        state["n"] += 1
        if state["n"] == 1 and on_first_poll is not None:
            on_first_poll()
        await real_sleep(0)

    monkeypatch.setattr(admin_models.asyncio, "sleep", no_sleep)


class TestSseLogStream:
    def _run(self, gen, steps):
        """steps: [(动作|None, 取帧数), ...]，全部帧在**同一个** event loop 内拉取。

        每个 asyncio.run 收尾都会 shutdown_asyncgens，把还挂起在 yield 的生成器
        直接 aclose；跨多个 asyncio.run 续拉必然只会拿到 StopAsyncIteration。
        """

        async def drive():
            frames = []
            for action, n in steps:
                if action is not None:
                    action()
                raw = ""
                for _ in range(n):
                    try:
                        raw += await gen.__anext__()
                    except StopAsyncIteration:
                        break
                frames.append(raw)
            await gen.aclose()
            return frames

        return asyncio.run(drive())

    def test_venv_replays_then_streams_new_lines(self, monkeypatch, tmp_path):
        log = tmp_path / "launch-m1.log"
        log.write_text("boot\n", encoding="utf-8")
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)

        def append_ready():
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("ready\n")

        _fast_stream(monkeypatch, on_first_poll=append_ready)
        frames = self._run(admin_models._sse_log_stream("m1"), [(None, 1), (None, 1)])
        assert _frames(frames[0]) == ["log"] and "boot" in frames[0]
        assert "ready" in frames[1]

    def test_venv_file_deleted_emits_placeholder(self, monkeypatch, tmp_path):
        log = tmp_path / "launch-m1.log"
        log.write_text("boot\n", encoding="utf-8")
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)
        _fast_stream(monkeypatch, on_first_poll=log.unlink)
        frames = self._run(admin_models._sse_log_stream("m1"), [(None, 1), (None, 1)])
        assert "日志文件已删除" in frames[1]

    def test_docker_initial_dump_and_json_increment(self, monkeypatch, tmp_path):
        _fast_stream(monkeypatch)
        only_id = tmp_path / "launch-d1.log"
        only_id.write_text("e2b3c1d4f5a6\n", encoding="utf-8")  # 仅容器 ID → docker 分支
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: only_id)
        monkeypatch.setattr(admin_models, "_read_docker_logs",
                            lambda n, tail=200: ["weight loaded"])
        jsonlog = tmp_path / "json.log"
        jsonlog.write_text("", encoding="utf-8")  # 必须先存在：stat 失败会关 used_json_log
        monkeypatch.setattr(admin_models, "docker_core_log_path", lambda n: jsonlog)

        def append_json_line():
            jsonlog.write_text(json.dumps({"log": "serving on :8000\n", "stream": "stdout"}) + "\n",
                               encoding="utf-8")

        frames = self._run(admin_models._sse_log_stream("d1"),
                           [(None, 1), (append_json_line, 1)])
        assert "weight loaded" in frames[0]  # 首次 dump
        assert "serving on :8000" in frames[1]  # json.log 增量行解析

    def test_docker_jsonlog_gone_stops(self, monkeypatch, tmp_path):
        _fast_stream(monkeypatch)
        only_id = tmp_path / "launch-d2.log"
        only_id.write_text("e2b3c1d4f5a6\n", encoding="utf-8")
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: only_id)
        monkeypatch.setattr(admin_models, "_read_docker_logs", lambda n, tail=200: ["x"])
        jsonlog = tmp_path / "json.log"
        jsonlog.write_text("", encoding="utf-8")
        monkeypatch.setattr(admin_models, "docker_core_log_path", lambda n: jsonlog)
        # 第二帧取 2 次：stopped 后生成器必须立即结束（第二次 StopAsyncIteration 被吞）
        frames = self._run(admin_models._sse_log_stream("d2"),
                           [(None, 1), (jsonlog.unlink, 2)])
        assert "jsonlog-gone" in frames[1]
        assert frames[1].count("event: stopped") == 1  # 之后无任何帧

    def test_no_logs_at_all_placeholder(self, monkeypatch, tmp_path):
        _fast_stream(monkeypatch)
        empty = tmp_path / "launch-d3.log"
        empty.write_text("", encoding="utf-8")  # 空 launch log + docker logs 空 → 占位提示
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: empty)
        monkeypatch.setattr(admin_models, "_read_docker_logs", lambda n, tail=200: [])
        monkeypatch.setattr(admin_models, "docker_core_log_path", lambda n: None)
        frames = self._run(admin_models._sse_log_stream("d3"), [(None, 1)])
        assert "docker logs 不可用" in frames[0]

    def test_heartbeat_after_silence(self, monkeypatch, tmp_path):
        log = tmp_path / "launch-h.log"
        log.write_text("boot\n", encoding="utf-8")
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)

        # 绝不能 patch time.monotonic 本体：admin_models.time 就是全局 time 模块，
        # asyncio 事件循环的定时器同样用它，冻结即死锁。只替换模块命名空间里的名字，
        # 且每次取值前进 5s → 静默 3 轮内必然跨过 10s 心跳阈值。
        state = {"t": 1000.0}

        def ticking():
            now = state["t"]
            state["t"] += 5.0
            return now

        monkeypatch.setattr(admin_models, "time", types.SimpleNamespace(monotonic=ticking))
        _fast_stream(monkeypatch)
        frames = self._run(admin_models._sse_log_stream("h"), [(None, 1), (None, 1)])
        assert "boot" in frames[0]
        assert "heartbeat" in frames[1]


class TestDockerPureHelpers:
    def test_docker_log_cmd_rejects_non_docker_engine(self, monkeypatch):
        _patch_profiles(monkeypatch, _profile(name="m1", engine="llamacpp"))
        assert admin_models._docker_log_cmd("m1") is None

    def test_docker_log_cmd_requires_image(self, monkeypatch):
        _patch_profiles(monkeypatch, _profile(name="m1", engine="vllm"))  # 无 docker_image
        assert admin_models._docker_log_cmd("m1") is None

    def test_docker_log_cmd_falls_back_to_launch_log(self, monkeypatch, tmp_path):
        _patch_profiles(monkeypatch,
                        _profile(name="m1", engine="vllm",
                                 engine_config={"model": "/x", "docker_image": "img"}))
        monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
        monkeypatch.setattr("modelctl.engines.get_adapter",
                            lambda e: (lambda p, c: object()))  # 无 _container_name
        log = tmp_path / "launch-m1.log"
        log.write_text("e2b3c1d4f5a6\n", encoding="utf-8")  # 第二来源要求真文件
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: log)
        monkeypatch.setattr("modelctl.core.process.tail_file", lambda p, lines: "e2b3c1d4f5a6\n")
        assert admin_models._docker_log_cmd("m1", 10) == ["docker", "logs", "--tail", "10", "e2b3c1d4f5a6"]

    def test_docker_log_cmd_empty_when_nothing_found(self, monkeypatch):
        _patch_profiles(monkeypatch,
                        _profile(name="m1", engine="vllm",
                                 engine_config={"model": "/x", "docker_image": "img"}))
        monkeypatch.setattr("modelctl.core.capabilities.probe", lambda *a, **k: object())
        monkeypatch.setattr("modelctl.engines.get_adapter", lambda e: (lambda p, c: object()))
        monkeypatch.setattr("modelctl.core.process.launch_log", lambda n: None)
        assert admin_models._docker_log_cmd("m1") is None

    def test_read_docker_logs_empty_cmd_returns_list(self, monkeypatch):
        _patch_profiles(monkeypatch)
        assert admin_models._read_docker_logs("ghost") == []

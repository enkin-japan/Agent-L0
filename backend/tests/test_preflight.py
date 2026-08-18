"""`devctl preflight` 事实包金测试（波 A1）。

这份 JSON 的用途是**被整段粘进委托里当环境事实**，所以它骗人的代价特别高：一条
「GPU 空闲」的假话就可能让人在单 GPU 上撞进第二个真模型进程。守的全是「不知道」
不许被写成「没问题」：

  ★ **`probe_auth` 只在 401/403 时判否**；不可达、连接重置、404/500、权重仍在加载，
    它一律返回 0。所以 rc=0 只能读作「那道门没判否」，**绝不能写成 ok / 钥匙对**。
  ★ **没有正信号 ≠ GPU 空闲**（进程内加载 MLX 的 bench 既不占端口也无 pidfile），
    且**已结束战役留下的 campaign.pid 不算正信号**（必须 `kill -0` 验活）。
  ★ **事实流必须自证完整**：采集半途死掉时若拼出半截 facts 的 `status: ready`，
    这份事实包就成了一张空头保证书。
  ★ **判据只认 ASCII token**，中文说明随便改都不该让阻断静默降级成通过。
  ★ **密钥只报在不在**，且**不输出任何完整命令行**——两者都是要粘进对话的文本面。

⚠️ **宿主隔离**：`listen_pids` 是 `lsof -ti tcp:<port>`，**全局作用于宿主**，与临时
ROOT 无关。不隔离的话，用户把 dev 栈起起来这些用例就会红——而它们在 pre-commit 里，
等于**卡死全部提交**；更糟的是那时会拿测试用的假钥匙去打真实的生产服务。
故端到端用例一律把 `*_PORT` 覆盖成无人使用的高位端口。
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_JSON_SRC = os.path.join(_REPO, "scripts", "preflight_json.py")
_DEVCTL_SRC = os.path.join(_REPO, "devctl.sh")
_LIB_EDGE_SRC = os.path.join(_REPO, "scripts", "lib_edge.sh")
_REAL_BACKEND_PY = os.path.join(_REPO, "backend", ".venv", "bin", "python")

_SENTINEL = ("meta", "complete", "true", "bool")


# ============ 一、拼装层：纯 python，不需要 bash / venv ============
# 刻意不挂模块级 skipif：这半边与 shell 无关，在最小镜像上也该真跑
# （否则整份文件会「靠跳过变绿」）。

def _tsv(*rows, sentinel=True):
    rows = list(rows) + ([_SENTINEL] if sentinel else [])
    return "".join("\t".join(r) + "\n" for r in rows)


def _fmt(payload):
    return subprocess.run([sys.executable, _JSON_SRC], input=payload,
                          capture_output=True, text=True, timeout=60)


_OK_ROWS = (
    ("venv", "backend", "ok：/x/backend/.venv/bin/python (python 3.13.2)", "str"),
    ("venv", "rag", "ok：/x/rag/.venv/bin/python (python 3.13.2)", "str"),
    ("ports", "backend_14159", "free：本机无监听", "str"),
    ("gpu", "status", "busy", "str"),
    ("gpu", "evidence", "战役 pid 1 存活", "str"),
)


def test_clean_facts_have_no_blockers_and_flag_the_busy_gpu():
    r = _fmt(_tsv(*_OK_ROWS))
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["blockers"] == []
    assert d["status"] == "ready_with_unknowns"        # gpu busy 必须留痕
    assert d["schema"].startswith("agent-l0/preflight/")


def test_truncated_fact_stream_is_internal_error_not_ready():
    """★ 采集在 `set -u` 下半途死掉时，管道右侧照样能拼出一份 JSON。只判「一行都没有」
    是不够的：半截 facts 的 `status: ready` 就是一张空头保证书。"""
    r = _fmt(_tsv(("venv", "backend", "ok：/x/py", "str"), sentinel=False))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "事实流不完整" in r.stderr
    assert "ready" not in r.stdout


def test_empty_input_is_internal_error_not_ready():
    r = _fmt("")
    assert r.returncode == 1, r.stdout + r.stderr


def test_malformed_row_is_internal_error():
    r = _fmt("venv\tbackend\t只有三段\n")
    assert r.returncode == 1, r.stdout + r.stderr


def test_unknown_token_is_internal_error_not_a_silent_pass():
    """★ 采集侧改了词汇却没同步判据时，判据已经失效。此时若因为「不匹配任何阻断
    规则」而放行，就是把失效的守卫读成了绿灯。"""
    r = _fmt(_tsv(("ports", "backend_14159", "kinda_free：新词", "str")))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "词汇" in r.stderr


def test_verdict_keys_on_token_not_on_chinese_prose():
    """★ 中文说明改写不该让阻断降级成通过。"""
    a = _fmt(_tsv(("ports", "backend_14159", "foreign：措辞甲", "str")))
    b = _fmt(_tsv(("ports", "backend_14159", "foreign：完全不同的措辞乙", "str")))
    assert a.returncode == b.returncode == 3
    assert json.loads(a.stdout)["status"] == json.loads(b.stdout)["status"] == "blocked"


def test_missing_backend_venv_is_a_blocker():
    r = _fmt(_tsv(("venv", "backend", "missing：不存在", "str")))
    assert r.returncode == 3, r.stdout
    assert any("backend venv" in b for b in json.loads(r.stdout)["blockers"])


def test_missing_rag_venv_is_flagged_but_not_a_blocker(tmp_path):
    """rag venv 只在动检索侧时才要 → 不阻断；但绝不能像原先那样**整个不提**。"""
    r = _fmt(_tsv(("venv", "rag", "missing：不存在", "str")))
    assert r.returncode == 0, r.stdout
    assert any("rag venv" in n for n in json.loads(r.stdout)["needs_confirmation"])


def test_foreign_port_is_a_blocker():
    r = _fmt(_tsv(("ports", "backend_14159", "foreign：被占", "str")))
    assert r.returncode == 3, r.stdout


def test_remote_service_port_is_not_reported_free():
    """`not_local` 与 `free` 是两回事：前者「服务在别的机器上」，后者「端口空着可以起」。"""
    r = _fmt(_tsv(("ports", "infer_14161", "not_local：按配置在远端", "str")))
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["facts"]["ports"]["infer_14161"].startswith("not_local")


def test_remote_mode_with_empty_host_is_a_blocker():
    """新克隆的 config 是空模板：mode=remote 而地址为空，start 必被 require_host 拒。
    这种配置报 ready 就是把一次注定失败的启动说成可开工。"""
    r = _fmt(_tsv(("selection", "infer_host", "empty_but_remote：地址为空", "str")))
    assert r.returncode == 3, r.stdout


def test_auth_failure_is_a_blocker():
    r = _fmt(_tsv(("keys", "INFER_verified", "FAIL：401/403", "str")))
    assert r.returncode == 3, r.stdout


@pytest.mark.parametrize("state", ["not_running：没监听", "no_key：未配置钥匙",
                                   "not_rejected：那道门没判否"])
def test_every_inconclusive_auth_state_needs_confirmation(state):
    """★ 三种「没测出结论」的形态都必须留痕。尤其 `not_rejected`——它最像「通过」，
    而 `probe_auth` 对不可达/404/500 也返回 0，把它读成「钥匙对」正是本波要防的事。"""
    r = _fmt(_tsv(("keys", "RAG_verified", state, "str")))
    assert r.returncode == 0, r.stdout
    d = json.loads(r.stdout)
    assert d["blockers"] == []
    assert any("RAG_verified" in n for n in d["needs_confirmation"]), d


def test_unknown_gpu_is_flagged_but_does_not_go_red():
    """GPU 在本机几乎恒为 unknown。必须响亮出现，但**不能**把退出码判红——
    否则这道门天天红，最终一定被删掉。"""
    r = _fmt(_tsv(("gpu", "status", "unknown", "str"), ("gpu", "evidence", "无正信号", "str")))
    assert r.returncode == 0, r.stdout
    d = json.loads(r.stdout)
    assert d["status"] == "ready_with_unknowns"
    assert any("GPU" in n for n in d["needs_confirmation"])


def test_busy_gpu_is_flagged_too():
    r = _fmt(_tsv(("gpu", "status", "busy", "str"), ("gpu", "evidence", "pid 1 存活", "str")))
    assert r.returncode == 0, r.stdout
    assert any("GPU 正忙" in n for n in json.loads(r.stdout)["needs_confirmation"])


def test_waived_is_always_present():
    """`waived` 留空数组而不是省略——省略会让人以为这个概念不存在。"""
    assert json.loads(_fmt(_tsv(*_OK_ROWS)).stdout)["waived"] == []


# ============ 二、采集层：临时 ROOT + 隔离端口，实跑 devctl preflight ============

_shell_ok = shutil.which("bash") is not None and os.path.exists(_REAL_BACKEND_PY)
shellmark = pytest.mark.skipif(not _shell_ok, reason="需要 bash 与仓库真实 backend/.venv")


def _free_ports(n):
    """借一批当前无人监听的高位端口，让采集与宿主真栈互不干扰。"""
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:                       # 拿到号就放开，只求「此刻没人用」
        s.close()
    return ports


# TEST-NET-3（RFC 5737），保证不可路由：既能造出「服务在远端」的形态，
# 又绝不会真打到任何机器上。
_UNROUTABLE = "203.0.113.9"

_CONFIG_LOCAL = ('inference:\n  host: "127.0.0.1"\n  mode: "inprocess"\n'
                 '  remote:\n    host: ""\n    port: 14161\n'
                 'rag:\n  host: "127.0.0.1"\n  compute:\n    host: ""\n    port: 14162\n')

_CONFIG_REMOTE = (f'inference:\n  host: "127.0.0.1"\n  mode: "remote"\n'
                  f'  remote:\n    host: "{_UNROUTABLE}"\n    port: 14161\n'
                  f'rag:\n  host: "127.0.0.1"\n  compute:\n    host: ""\n    port: 14162\n')


def _scaffold(tmp_path, *, campaign_pid=None, config=None):
    devctl = tmp_path / "devctl.sh"
    shutil.copy(_DEVCTL_SRC, devctl)
    devctl.chmod(devctl.stat().st_mode | stat.S_IEXEC)

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(_LIB_EDGE_SRC, scripts / "lib_edge.sh")
    shutil.copy(_JSON_SRC, scripts / "preflight_json.py")

    (tmp_path / "config.yaml").write_text(config or _CONFIG_LOCAL, encoding="utf-8")

    vbin = tmp_path / "backend" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    os.symlink(_REAL_BACKEND_PY, vbin / "python")

    if campaign_pid is not None:
        d = tmp_path / "benchmarks" / "baseline" / "fake-campaign"
        d.mkdir(parents=True)
        (d / "campaign.pid").write_text(f"{campaign_pid}\n", encoding="utf-8")
    return devctl


def _run(devctl, cwd, *args):
    b, r, i, s, f, e = _free_ports(6)
    env = dict(PATH="/usr/bin:/bin:/usr/sbin:/opt/homebrew/bin:/usr/local/bin",
               HOME=os.environ.get("HOME", "/tmp"),
               # ★ 宿主隔离：全部端口指向此刻无人监听的高位端口，既不受用户 dev 栈
               #   影响，也绝不会拿测试用的假钥匙去打真实的生产服务。
               BACKEND_PORT=str(b), RAG_PORT=str(r), INFER_PORT=str(i),
               SEARX_PORT=str(s), FRONT_PORT=str(f), RAG_EVAL_PORT=str(e))
    return subprocess.run(["bash", str(devctl), "preflight", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180, env=env)


def _facts(devctl, cwd):
    r = _run(devctl, cwd)
    assert r.returncode in (0, 3), r.stdout + r.stderr
    return json.loads(r.stdout)


@shellmark
def test_preflight_emits_valid_json_end_to_end(tmp_path):
    d = _facts(_scaffold(tmp_path), tmp_path)
    for section in ("repo", "venv", "ports", "gpu", "keys", "policy", "selection"):
        assert section in d["facts"], list(d["facts"])
    assert "PYTHONUNBUFFERED=1" in d["facts"]["venv"]["test_cmd"]


@shellmark
def test_scaffolded_config_is_actually_read(tmp_path):
    """确认脚手架那份 config 真被读进去了——否则后面几条端到端断言可能是在
    测一个静默失效的夹具。"""
    d = _facts(_scaffold(tmp_path), tmp_path)
    assert d["facts"]["selection"]["inference_mode"] == "inprocess"


@shellmark
def test_stale_campaign_pid_is_not_cited_as_evidence(tmp_path):
    """★ 已结束战役会留下 campaign.pid（真仓库里现在就有一个）。断言写成「不出现在
    证据里」而不是「gpu 必须 unknown」——后者会因宿主上恰好有别的正信号而假红。"""
    dead = 999999
    d = _facts(_scaffold(tmp_path, campaign_pid=dead), tmp_path)
    assert str(dead) not in d["facts"]["gpu"]["evidence"], d["facts"]["gpu"]


@shellmark
def test_live_campaign_pid_is_cited_and_makes_gpu_busy(tmp_path):
    """反向：真有存活的战役 pid 时必须报 busy 并点名它。用本测试进程自己的 pid 当活体。"""
    d = _facts(_scaffold(tmp_path, campaign_pid=os.getpid()), tmp_path)
    assert d["facts"]["gpu"]["status"] == "busy", d["facts"]["gpu"]
    assert str(os.getpid()) in d["facts"]["gpu"]["evidence"]


@shellmark
def test_gpu_is_never_reported_free(tmp_path):
    """★ 事实包里不许出现 `free` 这个 GPU 结论——探不出正信号只能是 unknown。"""
    assert _facts(_scaffold(tmp_path), tmp_path)["facts"]["gpu"]["status"] in ("busy", "unknown")


@shellmark
def test_auth_is_never_reported_ok(tmp_path):
    """★ 采集侧根本不该产出 `ok` 这个词：`probe_auth` 判否才有信息量，
    它返回 0 只说明「没判否」。"""
    keys = _facts(_scaffold(tmp_path), tmp_path)["facts"]["keys"]
    for k in ("INFER_verified", "RAG_verified"):
        assert not keys[k].startswith("ok"), keys[k]
        assert keys[k].split("：")[0] in ("no_key", "not_running", "not_rejected", "FAIL")


@shellmark
def test_process_scan_carries_no_command_lines(tmp_path):
    """★ `pgrep -fl` 会打完整 argv，可能带 --api-key/--hf-token。这份 JSON 的用途正是
    被粘进对话，故只允许 `pid:可执行名`，不许出现斜杠路径或参数。"""
    scan = _facts(_scaffold(tmp_path), tmp_path)["facts"]["gpu"]["process_scan"]
    for item in scan.split():
        assert "/" not in item and "--" not in item, scan


@shellmark
def test_no_key_material_leaks_into_the_json(tmp_path):
    """★ 这份 JSON 是要粘进对话的：只报在不在，绝不出现值、也不出现任何摘要。"""
    import base64
    import hashlib
    devctl = _scaffold(tmp_path)
    # 钥匙里刻意放冒号：早先的实现把 "名:端口:URL:钥匙" 拼成一个串再逐段切，
    # 而 URL 本身就含冒号——切错一格就会把钥匙的一截留在 url 里。
    secret = "sk:preflight:must-never-appear-9f3a2b"
    (tmp_path / ".env.local").write_text(
        f'AGENT_API_KEY="{secret}"\nINFER_API_KEY="{secret}"\nRAG_API_KEY="{secret}"\n',
        encoding="utf-8")

    r = _run(devctl, tmp_path)
    blob = r.stdout + r.stderr
    assert secret not in blob
    # 摘要面按多种常见形态一起挡，别只挡 sha256 前 8 位那一种
    raw = secret.encode()
    digests = [hashlib.sha256(raw).hexdigest(), hashlib.sha1(raw).hexdigest(),
               hashlib.md5(raw).hexdigest(), base64.b64encode(raw).decode()]
    for dg in digests:
        for n in (6, 8, 12, len(dg)):
            assert dg[:n] not in blob, f"输出里出现了密钥摘要片段 {dg[:n]!r}"
    assert secret[-4:] not in blob                      # 尾部片段也不许
    assert json.loads(r.stdout)["facts"]["keys"]["AGENT_API_KEY"].startswith("present")


@shellmark
def test_absent_keys_are_reported_as_absent(tmp_path):
    keys = _facts(_scaffold(tmp_path), tmp_path)["facts"]["keys"]
    assert keys["AGENT_API_KEY"].startswith("absent")
    assert keys["INFER_verified"].startswith("no_key")   # 没钥匙就别假装探过


@shellmark
def test_dirty_is_unknown_outside_a_git_repo(tmp_path):
    """★ `dirty` 是唯一一个「问不出来」会被答成「没问题（干净）」的字段。"""
    d = _facts(_scaffold(tmp_path), tmp_path)           # 脚手架刻意不 git init
    assert d["facts"]["repo"]["dirty"].startswith("unknown"), d["facts"]["repo"]


@shellmark
def test_help_does_not_masquerade_as_a_successful_preflight(tmp_path):
    r = _run(_scaffold(tmp_path), tmp_path, "--help")
    assert r.returncode == 0
    assert "schema" not in r.stdout                     # 没有伪造出一份事实包
    assert "用法" in r.stdout


@shellmark
@pytest.mark.parametrize("arg", ["-h", "--nope"])
def test_bad_flags_are_usage_errors(tmp_path, arg):
    r = _run(_scaffold(tmp_path), tmp_path, arg)
    assert r.returncode == 2, r.stdout + r.stderr


@shellmark
def test_missing_assembler_is_internal_error_not_usage_error(tmp_path):
    """python3 自身的退出码（缺文件=2）会与三值表撞车：2 表「用法错」。
    凡不是 0/3 的一律归 1，别让「跑不起来」被读成「你参数打错了」。"""
    devctl = _scaffold(tmp_path)
    (tmp_path / "scripts" / "preflight_json.py").unlink()
    r = _run(devctl, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr


# ---- 以下四条各自补一个「原先根本走不到的分支」。它们第一轮做变异证明时全部存活，
#      复查发现不是断言写错，而是**测试压根没到达那条分支**（三条恒真 + 一条被前置
#      检查挡在门外）。教训与 A0/A3 那两次同源：变异体存活先查夹具能不能到达。----

@shellmark
def test_remote_service_port_is_not_called_free(tmp_path):
    """★ mode=remote 时本机没监听是**正常**的，报 `free`（端口空着可以起）就是误导。
    需要一份 remote 配置才走得到这条分支——原先的脚手架恒为 inprocess。"""
    d = _facts(_scaffold(tmp_path, config=_CONFIG_REMOTE), tmp_path)
    infer = [v for k, v in d["facts"]["ports"].items() if k.startswith("infer_")][0]
    assert infer.startswith("not_local"), infer


@shellmark
def test_unreachable_remote_auth_is_not_rejected_never_ok(tmp_path):
    """★ 走到 probe_auth 真被调用的那条分支：钥匙在、服务在远端且**不可达**。
    probe_auth 此时返回 0——绝不能读成「钥匙对」，只能是 `not_rejected`。
    原先脚手架没有 .env.local，`no_key` 分支先短路，这条分支从没被测到。"""
    devctl = _scaffold(tmp_path, config=_CONFIG_REMOTE)
    (tmp_path / ".env.local").write_text('INFER_API_KEY="k-not-a-real-key"\n', encoding="utf-8")
    d = _facts(devctl, tmp_path)
    v = d["facts"]["keys"]["INFER_verified"]
    assert v.startswith("not_rejected"), v
    assert not v.startswith("ok")


@shellmark
def test_process_scan_strips_command_lines_of_a_real_matching_process(tmp_path):
    """★ 真起一个命令行能被 pgrep 模式命中、且**带着假密钥**的子进程。
    原先这条是恒真的：本机没有匹配进程，`scan.split()` 是空循环。"""
    fake = tmp_path / "bench_fake_probe.py"
    fake.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    leak = "tok-must-not-appear-in-scan-4c1d"
    proc = subprocess.Popen([sys.executable, str(fake), "--api-key", leak],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = _run(_scaffold(tmp_path), tmp_path)
        scan = json.loads(r.stdout)["facts"]["gpu"]["process_scan"]
        assert str(proc.pid) in scan, f"没扫到刚起的进程：{scan!r}"   # 夹具确实到达了
        assert leak not in scan and leak not in r.stdout, scan
        for item in scan.split():
            assert "/" not in item and "--" not in item, scan
    finally:
        proc.kill()
        proc.wait(timeout=10)


@shellmark
def test_assembler_exiting_two_is_remapped_to_internal_error(tmp_path):
    """★ python3 自身的退出码会与三值表撞车（2 表「用法错」）。
    原先这条删掉 preflight_json.py 来造错，但 do_preflight 有前置存在性检查，
    根本走不到 python——归一逻辑从没被测到。改成让拼装器**真的 exit 2**。"""
    devctl = _scaffold(tmp_path)
    (tmp_path / "scripts" / "preflight_json.py").write_text(
        "import sys; sys.stdin.read(); sys.exit(2)\n", encoding="utf-8")
    r = _run(devctl, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "跑不起来" in r.stderr

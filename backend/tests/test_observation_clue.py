"""驱逐桩的检索线索 observation_clue（D43 (a)，规则 R2）。

守的是什么：b2 批 57K 档每 run 有 42–69 个长得一样的驱逐桩（只有首部 200 字 + 句柄），模型只能
挨个猜着回读、迭代耗尽。线索要说清「这条里有什么」，而且不能变成噪声——每一步规则都对应一种
在真实 spill 上量到过的失败：不解码 JSON 转义就切出 `nqueue_…`；不去首部已见词，第 1 名永远是
观察自己的路径；不卡单例，模板键（`status: ok`）每条都有、等于没说。
"""
import json
import sys
import time
from pathlib import Path

from framework.memory.compactor import assign_key_counts, observation_clue

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))


def _clue(text, head="", key_counts=None):
    kc = assign_key_counts([text]) if key_counts is None else key_counts
    return observation_clue(text, head=head, key_counts=kc)


def _toks(clue):
    assert clue.startswith("; mentions: "), clue
    return clue[len("; mentions: "):].split(", ")


# ---- 1. 解码 ----

def test_json_escapes_are_decoded_before_tokenizing():
    """★ 单行 JSON 里的 `\\n` 不解码，键会被切成 `nqueue_depth_alert_threshold`。"""
    text = json.dumps(["alpha\nqueue_depth_alert_threshold = 4800\nbeta"])
    assert "\\nqueue_depth" in text                       # 前提：原文里确实是转义串
    assert _clue(text) == "; mentions: queue_depth_alert_threshold = 4800"


def test_decoding_does_not_touch_the_text():
    text = json.dumps({"content": "a\nretry_budget = 7"})
    before = str(text)
    _clue(text)
    assert text == before


# ---- 2. 首部已见词丢掉 + 去重 ----

def test_token_visible_in_head_is_dropped():
    """★ 观察自己的路径已在桩首部 200 字里，不许再占线索第 1 名。"""
    text = json.dumps({"path": "/srv/legacy/svc/app.log",
                       "content": "x\n" * 5 + "see /etc/halyard/main.toml"})
    head = text[:40]
    assert "/srv/legacy/svc/app.log" in head
    toks = _toks(_clue(text, head=head, key_counts={"path": 9, "content": 9}))   # JSON 模板键
    assert "/srv/legacy/svc/app.log" not in toks
    assert toks == ["/etc/halyard/main.toml"]


def test_assign_visible_in_head_is_dropped():
    text = "lead_owner = alice\n" + "pad " * 60 + "\nzone_name = west"
    toks = _toks(_clue(text, head=text[:30]))
    assert toks == ["zone_name = west"]


def test_duplicates_keep_first_occurrence_only():
    text = "OPS-11111 then OPS-22222 then OPS-11111 again"
    assert _toks(_clue(text)) == ["OPS-11111", "OPS-22222"]


# ---- 3. 单例规则 ----

def test_key_twice_in_this_observation_is_dropped():
    text = "mode_flag = on\nother_thing = 3\nmode_flag = off"
    assert _toks(_clue(text)) == ["other_thing = 3"]


def test_key_once_here_but_twice_in_range_is_dropped():
    text = "mode_flag = on\nother_thing = 3"
    kc = {"mode_flag": 2, "other_thing": 1}
    assert _toks(_clue(text, key_counts=kc)) == ["other_thing = 3"]
    # 正对照：全段也只出现一次就留
    assert _toks(_clue(text, key_counts={"mode_flag": 1, "other_thing": 1})) == [
        "mode_flag = on", "other_thing = 3"]


def test_assign_whose_value_is_a_captured_path_or_id_is_not_repeated():
    text = "config_file = /opt/app/conf/a.toml\nticket_ref: OPS-48213"
    assert _toks(_clue(text)) == ["OPS-48213", "/opt/app/conf/a.toml"]


# ---- 4. 排序 ----

def test_order_is_id_then_path_then_assign_by_first_occurrence():
    """★ 文本里 assign 在前、路径居中、ID 在后；线索要倒过来：ID → 路径 → assign。"""
    text = "alpha_key = 1\nbeta_key = 2\nload conf/app.yaml and /var/lib/x.db\nticket SKU-120 and OPS-48213"
    assert _toks(_clue(text)) == ["SKU-120", "OPS-48213", "conf/app.yaml", "/var/lib/x.db",
                                  "alpha_key = 1", "beta_key = 2"]


def test_urls_are_not_paths():
    text = "fetched https://example.com/a/b.html then wrote out/report.csv"
    assert _toks(_clue(text)) == ["out/report.csv"]


# ---- 5. 上限 ----

def test_token_cap_is_eight():
    text = " ".join(f"OPS-{10000 + i}" for i in range(9))
    toks = _toks(_clue(text))
    assert toks == [f"OPS-{10000 + i}" for i in range(8)]


def test_char_cap_is_inclusive_and_stops_at_the_first_overflow():
    """三个路径拼起来恰好 240 字（含两个 ", "）全留——上限含等号；第 4 个放不下。"""
    p1 = "/" + "a" * 72 + ".txt"          # 77
    p2 = "/" + "b" * 72 + ".txt"          # 77
    p3 = "/" + "c" * 77 + ".txt"          # 82 → 77 + 2 + 77 + 2 + 82 = 240
    big = "/" + "d" * 40 + ".txt"
    text = " ".join([p1, p2, p3, big])
    clue = _clue(text)
    toks = _toks(clue)
    assert toks == [p1, p2, p3]
    assert len(clue) - len("; mentions: ") == 240


def test_char_overflow_stops_rather_than_skips():
    """放不下的那个之后就停：不跳过它去补后面更短的（单个就超上限的才跳过，见下一条）。"""
    p1 = "/" + "a" * 72 + ".txt"          # 77
    p2 = "/" + "b" * 72 + ".txt"          # 77 → 156
    big = "/" + "d" * 95 + ".txt"         # 100 → 258 > 240
    text = f"{p1} {p2} {big} tiny = 1"    # tiny = 1 只要 10 字、本可放下
    assert _toks(_clue(text)) == [p1, p2]


def test_single_oversize_token_is_skipped_not_fatal():
    huge = "/" + "z" * 300 + ".log"
    text = f"OPS-48213 {huge} region_tag = eu"
    assert _toks(_clue(text)) == ["OPS-48213", "region_tag = eu"]


def test_custom_caps_are_honoured():
    text = "OPS-10001 OPS-10002 OPS-10003"
    assert _toks(observation_clue(text, head="", key_counts={}, max_tokens=2)) == [
        "OPS-10001", "OPS-10002"]


# ---- 6. 空 ----

def test_no_tokens_gives_empty_string():
    assert _clue("") == ""
    assert _clue("X" * 1000) == ""
    assert _clue("plain prose, nothing to point at.") == ""


def test_everything_already_in_head_gives_empty_string():
    text = "OPS-48213 at /opt/a/b.toml"
    assert _clue(text, head=text) == ""


# ---- 7. knee 的三种卡形态（filler.card_line）从单行 JSON 观察里全部抽到 ----

def test_three_knee_card_shapes_extracted_from_single_line_json():
    from context_knee.filler import card_line
    vals = {"config_path": "/opt/halyard/conf/halyard-a1b2c3.toml", "ticket": "OPS-73519",
            "threshold": "4630"}
    lines = [f"2026-01-0{i} INFO  [svc-{i}] worker heartbeat ok" for i in range(1, 8)]
    lines.insert(4, card_line("config_path", vals["config_path"], "log", "2026-01-04T10:00:00"))
    lines.insert(6, card_line("ticket", vals["ticket"], "md"))
    lines.append(card_line("threshold", vals["threshold"], "yaml"))
    obs = json.dumps({"path": "/srv/legacy-2025q3/ops/audit.log", "content": "\n".join(lines)},
                     ensure_ascii=False)
    head = obs.strip()[:200].rstrip()
    assert not any(v in head for v in vals.values())       # 前提：三卡都不在首部
    clue = _clue(obs, head=head)
    for v in vals.values():
        assert v in clue, (v, clue)
    assert "/srv/legacy-2025q3/ops/audit.log" not in clue
    assert "queue_depth_alert_threshold = 4630" in _toks(clue)


# ---- 8. 线性时间（正则起点断言） ----

def test_pathological_inputs_stay_linear():
    for text in ("X" * 200_000, "a." * 100_000, "a/" * 100_000, ("k" * 50 + " ") * 4000):
        t0 = time.monotonic()
        _clue(text)
        assert time.monotonic() - t0 < 5.0
    # 一审实测过的两种 O(n²) 形态（扩展名正则回溯 / 赋值键从每个 `.` 后起跑），N=32K 各 < 0.2 s
    n = 32_000
    for text in ("a/." + "a" * n + ".1", "x.." * n + "!"):
        t0 = time.monotonic()
        _clue(text)
        assert time.monotonic() - t0 < 0.2, text[:8]


# ---- 一审修补：冒号赋值、密钥、URL、首部词界、ID ----

def test_prose_colon_mid_line_is_not_an_assignment():
    """★ 行中冒号多半是散文（`escalation: incident …`）；只认 JSON 键与行首的 `key:`。"""
    text = "- escalation: incident tracked\n12:00 halyard: loaded config\nretry_limit: 5\n{\"zone_id\": \"eu-1\"}"
    toks = _toks(_clue(text))
    assert toks == ["retry_limit = 5", "zone_id = eu-1"]


def test_secret_looking_assignments_never_enter_the_clue():
    text = ("password = hunter2\napi_key = ghp_abcdefghijklmnopqrstuvwxyz\n"
            "AWS_SECRET_ACCESS_KEY = X\nsession_token: abc\nopaque_ref = Zq8Kd93jfLq0aPwe7Rt2\n"
            "region_tag = eu")
    clue = _clue(text)
    for bad in ("hunter2", "ghp_", "AWS_SECRET", "session_token", "Zq8Kd93"):
        assert bad not in clue, bad
    assert _toks(clue) == ["region_tag = eu"]


def test_url_userinfo_host_and_scheme_yield_nothing():
    """★ `https://user:pw@host/x.json`：不出 `user = pw`、不出 `url = https`、主机路径不当路径。"""
    text = ("fetched url = https://user:pw@host.example.com/x.json ok\nalso https://host.example.com/y.json"
            "\nand ftp://deploy_user=abc@files.example.com/z.tar ok")
    assert _clue(text) == ""


def test_head_filter_is_word_bounded():
    """首部里只有 `OPS-12345` 时，`OPS-1234` 不算已见。"""
    text = "OPS-12345 header line\n" + "pad " * 60 + "\nthen OPS-1234 and OPS-12345"
    assert _toks(_clue(text, head=text[:30])) == ["OPS-1234"]
    assert _clue("see /opt/a.toml.", head="loaded /opt/a.toml.") == ""      # 句末句点不算紧贴


def test_id_numeric_continuation_and_standard_prefixes():
    text = "CVE-2024-1234 SHA-256 ISO-8601 RFC-9110 UTF-8000 MD5-1234 OPS-48213"
    assert _toks(_clue(text)) == ["CVE-2024-1234", "OPS-48213"]

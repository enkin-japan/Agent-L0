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

from framework.memory.compactor import (CLUE_ENTROPY_MIN_CHARS, CLUE_ENTROPY_MIN_CLASSES,
                                        CLUE_RULE, assign_key_counts, observation_clue)

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
    # 键名 11-05 由 alpha_key / beta_key 改成 *_ref：R2.1 起末词 `key` 进密钥黑名单，本例只测排序
    text = "alpha_ref = 1\nbeta_ref = 2\nload conf/app.yaml and /var/lib/x.db\nticket SKU-120 and OPS-48213"
    assert _toks(_clue(text)) == ["SKU-120", "OPS-48213", "conf/app.yaml", "/var/lib/x.db",
                                  "alpha_ref = 1", "beta_ref = 2"]


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


# ---- R2.1（11-05 阶段复审 M2 + 一审修补）：键名末词、值形态、URL 查询串 / 片段 ----
# 样例全是明显合成的假值（重复字符 / 顺序字母），只有形态像凭据。每条子规则至少有一个只靠它拦的样例。

CTRL = "\nregion_tag = eu"          # 对照：无害的单例赋值，必须留下


def _only_ctrl(text, bads):
    clue = _clue(text + CTRL)
    for bad in bads:
        assert bad not in clue, bad
    assert _toks(clue) == ["region_tag = eu"]


def test_rule_version_is_r2_1():
    assert CLUE_RULE == "R2.1"


def test_secret_key_last_word_never_enters_the_clue():
    """值本身无害，只靠键名末词拦：`key` / `keys` / `pass` / `pwd` / `sig` / `signature`，含驼峰、`.` 切词。"""
    _only_ctrl('{"key": "abc1"}\n{"pass": "foo2"}\n{"pwd": "bar3"}\nlicense_key = 42\nlicenseKey = 7\n'
               "OPENAI_KEY=12\nreq_sig = ab12\nlicense.key = 5\nssh_keys = 6\nreq_signature = 77",
               ("abc1", "foo2", "bar3", "license", "OPENAI", "req_sig", "ssh_keys", "req_signature"))


def test_secret_key_camel_acronyms():
    _only_ctrl("DBPass = 8\nSSHKey = 9\nAWSKey = 10", ("DBPass", "SSHKey", "AWSKey"))


def test_secret_key_tail_modifiers_are_stripped():
    """尾部编号 / 版本 / 编码修饰剥掉再看末词：每个修饰各一例。"""
    _only_ctrl("db_pass2 = 11\nsigning_key_v2 = 12\nkey_b64 = 13\nclient_key_data = 14\n"
               "encryption_key_hex = 15\ntls_key_pem = 16\nmaster_key_raw = 17\nhmac_sig_value = 18\n"
               "backup_key_2 = 19",
               ("db_pass2", "signing_key", "key_b64", "client_key", "encryption_key", "tls_key",
                "master_key", "hmac_sig", "backup_key"))


def test_secret_key_extra_words():
    _only_ctrl("wifi_passphrase = a1\ndoor_passcode = a2\nlogin_otp = a3\ndb_pswd = a4",
               ("passphrase", "passcode", "otp", "pswd"))


def test_credential_shaped_values_never_enter_the_clue():
    """键名无害，只靠值形态拦（都短于 20 字或含 `-`/`.`，旧的不透明值规则拦不住）。"""
    _only_ctrl("SLACK_BOT=xoxb-1111-aaaa\nslack_user = xoxp-2222-bbbb\nslack_cfg = xoxe.xoxp-1\n"
               "slack_app = xapp-1-cccc\ndeploy_ref = sk-xxxxxxxx\ngh_ref = ghp_xxxx\ngh_app_ref = gho_xxxx\n"
               "gl_ref = glpat-xxxx",
               ("xoxb", "xoxp", "xoxe", "xapp", "sk-", "ghp_", "gho_", "glpat"))
    # AWS 访问键 ID 单独成值（20 字、无 ./-）时旧的不透明值规则已拦；带后缀时只剩前缀规则能拦
    _only_ctrl("aws_id = AKIAXXXXXXXXXXXXXXXX-01\naws_tmp = ASIAXXXXXXXXXXXXXXXX-01\nmaps_ref = AIzaxxxx\n"
               "goog_ref = ya29.xxxx\npem_head = -----BEGIN RSA\nhdr = Bearer xxxx",
               ("AKIA", "ASIA", "AIza", "ya29", "BEGIN", "Bearer"))


def test_credential_prefix_near_misses_are_kept():
    """`sk-` 后不足 8 字（`sk-SK` 地区码）、AKIA 后差一位（15 个）不算凭据前缀。"""
    akia15 = "AKIA" + "X" * 15
    assert _toks(_clue(f"locale = sk-SK\nshort_ref = {akia15}")) == ["locale = sk-SK", f"short_ref = {akia15}"]


def test_high_entropy_threshold_boundary():
    """分隔符之间的一段字母数字 ≥ 20 字、且大小写数字三类俱全才拦；19 字或只有 2 类照收；
    三类俱全但每段都不到 20 字（按段量、不按整值）照收。"""
    assert (CLUE_ENTROPY_MIN_CHARS, CLUE_ENTROPY_MIN_CLASSES) == (20, 3)
    seg = "Abcdefghijklmnopqrs1"
    hi, short, two, split = "x-" + seg, "x-" + seg[:18] + "1", "x-" + seg.lower(), "Abcdefghij-klmnopqr12"
    assert (len(seg), len(short) - 2, len(split)) == (20, 19, 21)
    clue = _clue(f"v_hi = {hi}\nv_short = {short}\nv_two = {two}\nv_split = {split}")
    assert _toks(clue) == [f"v_short = {short}", f"v_two = {two}", f"v_split = {split}"]


def test_url_query_string_never_enters_the_clue():
    """★ `?user=alice&…` 整段不进线索（有无 scheme 都一样），连带查询串里的 ID。"""
    _only_ctrl("fetched https://host.example.com/search?user=alice&ticket=OPS-55555 ok\n"
               "GET /api/v1/items?sort=asc&owner=bob\n{\"link\": \"https://h.example.com/x?q=zzz\"}",
               ("alice", "OPS-55555", "sort", "bob", "zzz"))


def test_url_query_flag_bracket_and_fragment_are_masked():
    """首参无值 flag、方括号参数、`#k=v` 片段同样整段遮。"""
    # 两行键名不同：同键出现两次会被单例规则先丢，测不到遮罩
    _only_ctrl("GET /a?verbose&user=alice1 ok\nGET /b?a[]=1&owner=alice2 ok", ("alice1", "alice2"))
    # 片段单独一条（文本里没有 `?`）：遮罩不能只在有 `?` 时才跑
    _only_ctrl("redirect https://h.example.com/cb#state=s1&user=alice3 ok", ("alice3", "state"))


def test_secret_values_shaped_like_ids_or_paths_are_not_captured():
    """密钥键下的值即便是 ID / 路径形态也不经 ID / 路径通道漏进线索；无害的 ID 照收。"""
    text = ('{"password": "XK-998877"}\napi_token: ABC-123456\nssh_key = /home/u/.ssh/id_x.pem\n'
            "ticket OPS-48213")
    clue = _clue(text)
    for bad in ("XK-998877", "ABC-123456", "id_x.pem"):
        assert bad not in clue, bad
    assert _toks(clue) == ["OPS-48213"]


def test_r2_1_filters_do_not_overblock_keys():
    """不误伤：`keyword` / `monkey` / `signal` 不是整词命中，`pass_rate` / `key_count` 的末词不是密钥词，
    20 字以上的连字符英文词组只有 2 个字符类；散文里的问号不当查询串。"""
    text = ("keyword = foo\nmonkey = 3\nsignal = high\npass_rate = 98\nkey_count = 4\n"
            "motto = keep-calm-and-carry-on-now\nreally? retry_limit = 5")
    assert _toks(_clue(text)) == ["keyword = foo", "monkey = 3", "signal = high", "pass_rate = 98",
                                  "key_count = 4", "motto = keep-calm-and-carry-on-now",
                                  "retry_limit = 5"]


def test_r2_1_filters_do_not_overblock_values():
    """一审 Minor 1 钉住：UUID、主机名、路径、版本串、区域名、带大写的包名都必须进线索。"""
    vals = [("rid", "550e8400-e29b-41d4-a716-446655440000"), ("host", "db-primary-01.internal.example"),
            ("dir", "/var/lib/postgres/data_v12"), ("ver", "v2.14.0-beta.1-linux-amd64"),
            ("zone", "us-east-1-prod-cluster"), ("svc", "com.example.OrderService2")]
    text = "\n".join(f"{k} = {v}" for k, v in vals)
    assert _toks(_clue(text)) == [f"{k} = {v}" for k, v in vals]


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

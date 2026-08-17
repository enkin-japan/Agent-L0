"""会话级 prompt 字节流（上位解「只增不改」，2026-08-17 开工）。

目标不变量：同一会话内，第 N 轮 prompt 以第 N-1 轮「prompt + 实际生成字节」为
严格前缀 ⇒ 跨轮 token 公共前缀 == 上轮全流长 ⇒ excess==0 ⇒ 后端 T8 回滚能力门
走纯追加快路，对**全部**缓存类型（含旋转态滑窗 / ArraysCache 线性注意力）无损
放行前缀复用。此前每轮从 messages 全量重渲染，环境锚点块逐轮后移 + 回复重渲染
与生成字节不逐位一致，跨轮 excess>0，不可回滚的缓存类型整段退冷预填
（A8 实测 qwen38 五轮对话 t5 ttft 23s）。

设计（台账「上位解开工准备」两案中的 A 案，08-17 拍板）：
- 引擎在每次**成功**生成后记录「本轮实际喂入的 prompt + 实际生成的原始字节」；
- 下一轮组装时若流仍有效（指纹校验），只让方言渲染**增量段**（收口符 + 新消息
  + 变化了的尾块 + 生成头），历史段按记录字节原样回放——回复回放逐位一致由
  构造保证，不再依赖 parse→重渲染的字节恒等（§S.17 打地鼠类）；
- 任何流失效情形（历史被压缩改写/回滚、工具集变化、方言不支持该增量形态、
  取消后的残局）一律**静默退全量重渲染**并重新记录——退路就是今天的行为，
  正确性永不受损，只是慢。

本模块只管流的生命周期（有效性判定/指纹/提交）；增量段的字节形态全部归方言
（prompt_builder.build_prompt_delta），与全量渲染同属一处字节权威。
"""
import hashlib
import json
from typing import Any, Dict, List, Optional


def messages_fingerprint(messages: List[Dict[str, Any]]) -> str:
    """已覆盖历史段的内容指纹。历史的任何改写（摘要压缩、turn_runner 回滚、
    审批合并）都必须让流失效——按内容哈希而非条数：回滚后再追加可以凑回条数，
    凑不回内容。default=str 兜底不可序列化对象（宁可指纹变化多退全量，不可漏检）。"""
    try:
        canon = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        canon = repr(messages)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def tools_fingerprint(tools: Optional[List[Dict[str, Any]]]) -> str:
    """工具声明属稳定前缀、只在全量渲染时落字节：工具集一变（rescan 增减），
    流里的声明就是旧的，必须整体退全量重渲染。"""
    if not tools:
        return ""
    try:
        canon = json.dumps(tools, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        canon = repr(tools)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class PromptStream:
    """一段已喂入+已生成的 prompt 字节流及其有效性凭据。

    covered 语义：stream 完整表示 history[:seen] 的渲染 + 第 seen 条（即本次生成
    产出、由编排器随后追加的 assistant 消息）的原始生成字节——提交发生在编排器
    追加之前，故 assistant 消息以「预期占位」记账，下一轮用角色校验兜底。
    """

    def __init__(self, stream: str, seen: int, fingerprint: str,
                 tools_key: str, tail: Dict[str, Any],
                 expected_text: Optional[str] = None):
        self.stream = stream          # 已喂 prompt + 生成原始字节（含思考/工具块）
        self.seen = seen              # 生成时刻的 history 长度
        self.fingerprint = fingerprint  # messages_fingerprint(history[:seen])
        self.tools_key = tools_key
        # 最后落进流的尾块内容（方言的「变了才追加」判据）：
        # {"env": 环境锚点块内容, "task": 任务状态块内容（无则 ""),
        #  "task_appends": 任务块已追加次数（收拢阈值判据，方言消费）}
        self.tail = dict(tail or {})
        # 占位 assistant 消息的预期正文（首过评审 M7，门面路必传）：引擎路的
        # 编排器是单一写者（会话锁），角色校验足够；门面路的 history 来自客户端
        # 下一次请求，客户端改写过 assistant 正文（编辑重答类 UI）时必须察觉并
        # 退全量——否则模型吃到的是被用户改掉的原始生成。None = 不校验（引擎路）。
        self.expected_text = expected_text


def stream_valid(ps: Optional[PromptStream], history: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]]) -> bool:
    """流是否仍可作为本轮 prompt 的前缀。四道校验，任一不过即退全量：
    ① 流存在；② 占位的 assistant 消息已就位且角色相符（history[seen]）；
    ③ 工具集未变；④ 已覆盖历史段逐字节未被改写。"""
    if ps is None or not ps.stream:
        return False
    if len(history) < ps.seen + 1:
        return False
    if history[ps.seen].get("role") != "assistant":
        return False
    if ps.expected_text is not None \
            and history[ps.seen].get("content") != ps.expected_text:
        return False   # 客户端改写了 assistant 正文（门面路）→ 退全量
    if tools_fingerprint(tools) != ps.tools_key:
        return False
    return messages_fingerprint(history[:ps.seen]) == ps.fingerprint


def commit(prompt: str, gen_text: str, history: List[Dict[str, Any]],
           tools: Optional[List[Dict[str, Any]]],
           tail: Dict[str, Any],
           expected_text: Optional[str] = None) -> PromptStream:
    """成功生成后的流提交。gen_text 是本轮生成的**原始**字节（含思考/工具块，
    不含停止符——底层在 add_token 前就 break，停止符从不进文本；后端记录侧
    相应截到真实已喂长度，增量段恒以收口符开头，两侧对齐见 gemma4_generate
    的 _fed_length）。**不提交**的三种情形由调用方把关：取消（残局由旧流 +
    前缀匹配消化）、本轮带一次性尾注（tail_notes 冻结进流会把「只此一轮」的
    提醒变成永久指令——首过评审 C1，跳过提交让下一轮退全量、注不进流）、
    本轮产出解析失败的调用（全量渲染刻意不回放 error 调用，冻结原始坏字节
    等于给纠错闭环喂反例——首过评审 M4）。"""
    return PromptStream(stream=prompt + gen_text, seen=len(history),
                        fingerprint=messages_fingerprint(history),
                        tools_key=tools_fingerprint(tools), tail=tail,
                        expected_text=expected_text)

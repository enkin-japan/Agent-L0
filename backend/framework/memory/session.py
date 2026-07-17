from pathlib import Path
import asyncio
import os
import json
import re
import time
import uuid
import shutil
from typing import List, Dict, Any, Optional

from ..core.task_state import TaskState


def _strip_internal_meta(response_data: dict) -> dict:
    """落盘前剥离工具观察里的 `_` 前缀内部元数据（_safe/_target/_compacted 等，仅供内存中的
    压缩层使用）。只浅拷贝需要改动的层级，不触碰内存中的原 dict（否则会破坏 KV 前缀与压缩状态）。
    上层键（如 _rollback_idx）由 _public_turn 另行处理，这里只清理嵌套的 tool_responses。"""
    resp = response_data.get("response")
    if not isinstance(resp, list):
        return response_data
    changed = False
    cleaned = []
    for msg in resp:
        trs = msg.get("tool_responses") if isinstance(msg, dict) else None
        if isinstance(trs, list) and any(
            isinstance(tr, dict) and any(str(k).startswith("_") for k in tr) for tr in trs
        ):
            new_trs = [
                {k: v for k, v in tr.items() if not str(k).startswith("_")}
                if isinstance(tr, dict) else tr
                for tr in trs
            ]
            cleaned.append({**msg, "tool_responses": new_trs})
            changed = True
        else:
            cleaned.append(msg)
    if not changed:
        return response_data
    return {**response_data, "response": cleaned}


DEFAULT_SYSTEM_PROMPT = """
You are an autonomous agent running on the user's Mac. You can call tools (terminal, application scripting, GUI automation, knowledge base, etc.) to complete desktop tasks on the user's behalf. Your default mode is "investigate and get it done", not "ask the user back".

Core behavior:
- Find things out yourself before asking the user. When the request lacks details but tools can uncover them (where a file is, what a project contains, what is on the Desktop, whether an app is running), just use the tools. For example, if the user says "look at that project on my desktop", run ls ~/Desktop to locate it and inspect it — do not ask "what kind of project is it?".
- Only ask the user when you are truly blocked: the missing information is something only the user knows (personal preferences, credentials, a choice between several reasonable options, or which target an irreversible action should apply to). Anything discoverable by investigation must be investigated first.
- When the user mentions something on the system (a file, project, app, or data), locate and read it with tools before answering.
- Goal ambiguity is different from missing facts: when the desired OUTCOME is genuinely underspecified — the request implies MODIFYING or reorganizing the user's data (e.g. "tidy this folder up", "make this file better"), or is a big multi-step effort whose scope or target you would otherwise just be guessing at — investigate what you can first, then state your intended approach and ask ONE specific question to confirm the goal, offering 2-3 concrete options when it is a choice so a single reply settles it. Do this BEFORE writing any changes or sinking real work into a guess. Asking does not end the task — it pauses and continues with the user's answer on their next message (your plan and scratchpad carry over), so a well-placed mid-task question is fine, not a failure. Never burn a question on a request with a clear outcome — then just do it.
- Long-term memory: earlier conversation may have been folded out of the current context by compaction. When the user refers to something said or done before that you cannot find in context, search the agent_memory category with rag_search instead of guessing or claiming you don't remember.

Task execution:
- For tasks that need multiple steps and multiple tool calls, first create a concise plan with update_plan, then execute step by step, calling complete_step as each step finishes — put the concrete value(s) the step produced (numbers, ids, urls, paths) into its result, and save anything a later step will need with remember. When assembling the final answer, read the values back from the recorded step results and scratchpad instead of trusting your memory of long-scrolled context. For single-step actions or plain conversation, just do it — no plan needed.
- Tool preference for desktop tasks: prefer the terminal (run_shell_command) when it can do the job; if the terminal cannot but the target app is scriptable (Mail, Calendar, Safari, Finder, etc.), use run_applescript; only fall back to GUI tools (read_ui_tree etc.) when neither works.
- For sensitive or side-effectful operations (deleting data, modifying files, etc.), call the tool directly instead of asking for permission in chat; the system automatically prompts the user for confirmation, and the tool result will tell you whether they approved or declined.
- Math: never compute non-trivial math in your head. One quick calculation goes through run_math (prefer one high-level SymPy call over hand-coded formulas); a complete multi-step problem (word problems, systems, calculus chains, anything needing verification) goes to math_research as one self-contained problem statement. Tool output overrides your own mental estimate: if it contradicts what you remember, the tool is right and your memory is wrong. Copy numbers from tool output digit-for-digit — no rounding, no thousands separators, no reformatting.
- Charts: when the user would benefit from seeing data as a chart, call render_chart with the real numbers and copy the returned markdown image line into your reply verbatim.
- Presentations: to make slides / a PPT, use the deck tools — deck_init once, then deck_set_slide one slide at a time (the user watches the deck build in a live preview). Plan the outline first with update_plan for decks of more than a few slides. Charts inside slides take real data, same rule as render_chart. The deck changes ONLY through deck tool calls: to revise or fill in an existing slide, call deck_set_slide with that slide's position and the complete new content. Never tell the user you refined, polished, or completed slides unless you actually called the tools in this turn.

Code fixes & debugging:
- When the user asks you to fix or change code, APPLY the change yourself with edit_file / write_file. Do NOT just describe the fix in prose or paste a code snippet for the user to apply, and do NOT ask the user to make the edit — you have the tools, so make the edit.
- Before editing, make sure your fix matches the actual symptom: would this change produce EXACTLY the behavior the user reported? If they say "nothing happens", check that the trigger is even wired up and called (e.g. a button whose handler is never invoked) before changing the logic inside the handler.
- After editing, verify: re-read the changed section, and whenever feasible run a real check (execute the code or command). Claim success only for what you actually verified; if you could not verify, say so plainly.

Style:
- Be accurate; say so when you are speculating. Be as concise as clarity allows.
- Always respond in the same language as the user's most recent message. Never switch to another language just because tool output, search results, or system text are in English or any other language.
"""

class ChatSession:
    """
    单个会话状态机，负责管理该会话的上下文、历史记录及物理文件生命周期
    支持冷热状态切换（释放/预热内存）
    """
    def __init__(self, session_id: str, base_sessions_dir: Path, system_prompt: str = "",
                 feed=None, backend=None):
        self.session_id = session_id

        # 推理后端（InferenceBackend，app 装配注入）：会话缓存的生命周期属主。
        # None = 无后端环境（金测试用 FakeEngine，不触碰真实生成路径）。
        self._backend = backend
        # 内存状态（热数据）：会话级推理缓存句柄（CacheHandle，实现不透明）
        self.brain_memory_state = backend.new_cache() if backend is not None else None
        self.history: List[Dict[str, Any]] = []
        self.images: List[str] = []
        self.audios: List[str] = []
        # 环境上下文注入开关（想法1）：仅主编排会话为 True。沙箱一次性会话替身不设此属性
        # （getattr 默认 False），故机器/个人环境事实绝不进沙箱子生成上下文——隐私边界 opt-in。
        self.inject_env_context = True
        
        # 生命周期控制
        self.last_access_time: float = time.time()
        self.is_cold: bool = False  # 被驱逐后为 True

        # 敏感工具审批挂起状态（仅存内存）：
        # pending_tool_calls 为等待用户决议的工具调用；pending_turn 为该轮暂存的
        # 完整响应数据（决议后合并落盘）。冻结/关闭时整轮以 fail 落盘作废。
        self.pending_tool_calls: Optional[List[Dict[str, Any]]] = None
        self.pending_turn: Optional[Dict[str, Any]] = None

        # 逻辑轮次累计迭代数（跨审批挂起/续跑存续）：总迭代熔断（max_total_iterations）
        # 的计数分母。每次审批续跑都是一个新的 run_turn（局部 iterations 从 0 起），若各自独立
        # 计数，模型可借「反复吐坏写→弹审批→续跑」无限绕过熔断（实测的审批死循环）。故把消耗量
        # 累积在会话上：新用户轮次归零，续跑在其上累加，熔断按累计值判定。
        self.carried_iterations: int = 0
        # 跨审批段的整轮仪表累计（与 carried_iterations 同生命周期）：审批把一轮切成
        # 多个 run_turn 段，history.jsonl 只落最终段的 metrics——不累计则跨段轮次的
        # generations/tokens 被严重低估（实测四测 662eac7d：20+ 迭代记成 3）。
        self.carried_turn_stats: Dict[str, Any] = {}

        # 多步任务状态（编排循环的状态对象）：任务可跨轮存续（如中途向用户提问），
        # 空闲冻结时随 task.json 落盘、恢复时重建，保证冷启动后任务不丢
        self.task_state: Optional[TaskState] = None
        # 取消标志：/task/cancel 接口置位，引擎逐 token 段与编排循环迭代边界检查
        self.cancel_requested: bool = False
        # 挂起标志：/task/suspend 接口或自动调度策略经调度器置位，编排循环在迭代边界
        # （非生成中途，保证在安全点让出、零浪费）响应，返回 suspended。KV 与 task_state
        # 依设计常驻内存，恢复时直接再进 run_turn 从 task_state + 已提交 history 续跑。
        self.suspend_requested: bool = False
        # 挂起中暂存的未完结轮次（含累计消息与轮次账目）：/task/resume 决议后合并落盘，
        # 语义与审批挂起（pending_turn）平行，二者互斥。冻结/关闭时以 warning 落盘作废。
        self.suspended_turn: Optional[Dict[str, Any]] = None

        # 实时步骤反馈（仅内存、按轮，不落盘）：编排循环在各节点写入当前动作与已完成步骤，
        # /task 接口轮询读取展示，把"黑盒等待"变成看得见的步骤流。
        # 并发安全依赖单线程事件循环：编排在推理/工具 I/O 的 await 处让出，轮询读到最新快照。
        self.activity: Dict[str, Any] = {"phase": "idle", "current": "", "steps": []}

        # 会话级互斥锁：同一会话的并发请求必须整轮串行
        # （history 追加、KV Cache 读写、日志落盘都不是并发安全的），
        # 分支克隆 KV Cache 时也需持有父会话锁，防止克隆到写入一半的缓存
        self.lock = asyncio.Lock()
        
        # 物理层目录配置
        self.session_dir = base_sessions_dir / session_id
        self.image_dir = self.session_dir / "image"
        self.audio_dir = self.session_dir / "audio"
        self.attach_dir = self.session_dir / "attach"  # 原始非原生文件（音频/PDF/文档/代码）
        self.system_file = self.session_dir / "system.json"
        self.history_log = self.session_dir / "history.jsonl"
        self.meta_file = self.session_dir / "meta.json"  # 会话血缘元数据（分支溯源）
        self.task_file = self.session_dir / "task.json"  # 跨轮多步任务的落盘快照
        self.memory_file = self.session_dir / "memory.json"  # 摘要式压缩旁挂（方案A）

        # 摘要式压缩状态（方案A）：rolling_summary 为已折叠旧轮次的滚动摘要，
        # summarized_turns 为已并入摘要的「前导日志行数」水位线。全量日志 history.jsonl
        # 始终保留（前端展示/审计），但 model-facing 上下文重建为
        # [system] + [摘要块] + [水位线之后的尾部轮次]，从而有界。
        self.rolling_summary: str = ""
        self.summarized_turns: int = 0
        # 最近一次生成的 prompt token 数（上下文规模估计，摘要压缩阈值判定用；空闲维护回合亦读它）
        self.last_prompt_tokens: int = 0

        # 确保物理结构存在
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.attach_dir.mkdir(parents=True, exist_ok=True)

        # 会话事件账本(PROTOCOL.md):编排/TurnRunner 的唯一事件出口,SSE 与状态
        # 查询的唯一数据源。seq 从磁盘账本续增;冻结不清账本(轮次状态独立于 KV)。
        # feed:生命周期事件镜像进全局任务 feed(SessionManager 注入,测试可 None)。
        from ..core.journal import TurnJournal
        self.journal = TurnJournal(self.session_dir, session_id=session_id, feed=feed)

        self._init_system_prompt(system_prompt)
        # 为根会话补齐血缘元数据（分支会话的 meta 由 SessionManager 预先落盘）
        if not self.meta_file.exists():
            self.save_meta()

    def _init_system_prompt(self, system_prompt: str):
        """初始化系统提示词并落盘"""
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT
            if self.system_file.exists():
                try:
                    with open(self.system_file, "r", encoding="utf-8") as f:
                        item = json.load(f)
                        system_prompt = item.get("content", system_prompt)
                except Exception:
                    pass
        
        item = {"role": "system", "content": system_prompt}
        self.history.append(item)

        # 落盘持久化
        with open(self.system_file, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False)
        
        self.touch()

    def touch(self):
        """刷新活跃时间"""
        self.last_access_time = time.time()

    def freeze(self):
        """
        进入冷存储状态：销毁高占用的内存数据（MLX Cache），保留磁盘关联
        """
        if not self.is_cold:
            print(f"[Memory] Session {self.session_id} 正在冻结，释放内存...")

            # 未决的审批随冻结作废：整轮以 fail 落盘（restore 会跳过，不污染记忆），
            # 前端历史里仍可见该轮的用户消息与未执行的工具调用。
            # 该轮进行中的多步任务一并作废（其上下文随该轮回滚，不能孤立存续）
            if self.pending_turn is not None:
                self.pending_turn.pop("_rollback_idx", None)
                self.pending_turn.pop("_task_snapshot", None)
                self.pending_turn["status"] = "fail"
                self.pending_turn.setdefault("response", []).append(
                    {"role": "system", "content": "The approval request expired (session frozen or server shutdown); the pending sensitive operation was NOT executed."}
                )
                try:
                    self.append_log(self.pending_turn)
                    if self.journal.turn_state != "none":
                        self.journal.emit("turn_ended",
                                          {"status": "fail", "turn_index": self.disk_turn_count() - 1})
                except Exception as e:
                    print(f"[Memory] 未决审批轮次落盘失败: {e}")
                self.pending_turn = None
                self.task_state = None
            self.pending_tool_calls = None

            # 挂起中的未完结轮次随冻结作废（正常情况下挂起会话被调度器钉住不冻，
            # 此处兜底 shutdown/delete 等强制路径）：以 warning 落盘，其 KV 已随冻结释放，
            # 无法再热恢复续跑，故整轮作废，前端历史仍可见该轮已产出的消息。
            if self.suspended_turn is not None:
                self.suspended_turn.pop("_rollback_idx", None)
                self.suspended_turn.pop("_task_snapshot", None)
                self.suspended_turn.pop("_history_start_idx", None)
                self.suspended_turn["status"] = "warning"
                self.suspended_turn.setdefault("response", []).append(
                    {"role": "system", "content": "The suspended task expired (session frozen or server shutdown) and was not continued."}
                )
                try:
                    self.append_log(self.suspended_turn)
                    if self.journal.turn_state != "none":
                        self.journal.emit("turn_ended",
                                          {"status": "warning", "turn_index": self.disk_turn_count() - 1})
                except Exception as e:
                    print(f"[Memory] 挂起轮次落盘失败: {e}")
                self.suspended_turn = None
                self.task_state = None
            self.suspend_requested = False

            # 完结轮次留下的跨轮任务随冻结落盘，恢复时重建
            try:
                self.save_task()
            except Exception as e:
                print(f"[Memory] 任务状态落盘失败: {e}")
            self.task_state = None
            self.cancel_requested = False

            # 释放推理缓存的底层内存（后端负责物理释放，句柄换新）
            if self._backend is not None:
                if self.brain_memory_state is not None:
                    self._backend.release_cache(self.brain_memory_state)
                self.brain_memory_state = self._backend.new_cache()
            
            # 清空内存中的历史文本与资源路径引用
            self.history.clear()
            self.images.clear()
            self.audios.clear()
            
            self.is_cold = True

    def restore_from_disk(self):
        """
        从物理磁盘重建内存对象的基础结构（不包括大模型推理预热，预热由 Engine 层接管）。
        方案A：model-facing 上下文 = [system] + [摘要块] + [水位线之后的尾部轮次]。
        """
        print(f"[Memory] Session {self.session_id} 正在从磁盘重建结构...")

        # 1. 区分是从内存启动和从磁盘启动，从内存中启动没有初始化过程，需要恢复系统提示词
        if self.is_cold:
            self._init_system_prompt("")

        # 2. 载入摘要旁挂并注入摘要块（若有），再只重放水位线之后的尾部轮次
        self.load_memory()
        self._seed_summary_block()
        self._replay_history_log(self.summarized_turns)
        # 3. 恢复跨轮多步任务（若有）
        self.load_task()
        self.is_cold = False
        self.touch()

    def rebuild_from_log(self):
        """
        以磁盘日志为准整体重建内存上下文（history / images / audios）。
        用于删除最后轮次、摘要折叠后等的内存同步：KV Cache 刻意保留，
        下次推理时底层 stream_generate 经前缀匹配自动裁剪对齐（与分支复用同一机制）。
        同样遵循方案A：[system] + [摘要块] + [水位线之后的尾部轮次]。
        """
        self.history.clear()
        self.images.clear()
        self.audios.clear()
        self._init_system_prompt("")  # 从 system.json 恢复原提示词
        self.load_memory()
        self._seed_summary_block()
        self._replay_history_log(self.summarized_turns)
        self.touch()

    def _seed_summary_block(self):
        """把滚动摘要作为一个 system 记忆块注入历史（紧随系统提示词之后、尾部轮次之前）。"""
        if self.rolling_summary.strip():
            self.history.append({
                "role": "system",
                "content": ("[Memory summary of earlier conversation (system-compacted, for reference)]\n"
                            + self.rolling_summary.strip()),
            })

    def _replay_history_log(self, start_turn: int = 0):
        """
        按 history.jsonl 重放轮次，重建内存中的历史与多模态引用。
        start_turn：跳过前导的已摘要轮次（方案A 的水位线），只重放其后的尾部。
        """
        if self.history_log.exists():
            try:
                with open(self.history_log, "r", encoding="utf-8") as f:
                    lines = [line for line in f if line.strip()]
                for line in lines[start_turn:]:
                        resp = json.loads(line)
                        # fail（引擎故障）与 cancelled（用户取消）轮次均不进内存记忆：
                        # 与在线路径的整轮回滚语义一致，仅保留落盘记录供前端展示
                        if resp.get("status") in ("fail", "cancelled"):
                            continue

                        # 恢复多模态资源路径
                        self.images.extend(resp.get("image", []))
                        self.audios.extend(resp.get("audio", []))
                        
                        # 重建用户输入（含摄入层解析文本 ingested_text，烘进日志、原样回放免重解析）。
                        # 纯图片/音频轮（prompt 为空）也要重建，否则重放后媒体引用在而用户消息缺失
                        prompt = resp.get("prompt", "")
                        ingested_text = resp.get("ingested_text", "")
                        if prompt or ingested_text or resp.get("image") or resp.get("audio"):
                            self.add_user_message(prompt, resp.get("image", []), resp.get("audio", []),
                                                  extra_text=ingested_text)
                            
                        # 重建动作与结果
                        tool_calls = resp.get("tool_calls", [])
                        if tool_calls:
                            self.add_assistant_message(tool_calls=tool_calls)
                            tool_responses = resp.get("tool_responses", []) 
                            if tool_responses:
                                self.add_tool_message(tool_responses)
                                
                        # 重建模型回复
                        new_message = resp.get("response", [])
                        if new_message:
                            self.history.extend(new_message)
            except Exception as e:
                print(f"[Memory] Session {self.session_id} 历史读取异常: {e}")

    # --- 实时步骤反馈（编排循环与长耗时工具的进度钩子写入，/task 轮询与 SSE 读取；
    #     钩子可能从工具线程写：字典赋值在 GIL 下安全，快照浅拷贝，最后写入者为准） ---
    def reset_activity(self):
        self.activity = {"phase": "idle", "current": "", "steps": []}

    def set_activity(self, current: str, phase: str = "running"):
        self.activity["current"] = current
        self.activity["phase"] = phase

    def push_activity(self, kind: str, label: str, detail: str = ""):
        """记录一条已完成的步骤（kind: tool/plan/note）；步骤列表有界，仅保留最近若干条。"""
        steps = self.activity.setdefault("steps", [])
        steps.append({"kind": kind, "label": str(label), "detail": str(detail)[:160]})
        if len(steps) > 40:
            del steps[: len(steps) - 40]

    def activity_snapshot(self) -> Dict[str, Any]:
        """返回活动状态的浅拷贝快照（供 /task 序列化，避免读到写一半的列表）。"""
        return {
            "phase": self.activity.get("phase", "idle"),
            "current": self.activity.get("current", ""),
            "steps": list(self.activity.get("steps", [])),
        }

    # --- 多步任务状态的落盘与恢复 ---
    def save_task(self):
        """任务快照落盘：有任务则写 task.json，无任务则清除（保证磁盘与内存一致）"""
        if self.task_state is not None:
            with open(self.task_file, "w", encoding="utf-8") as f:
                json.dump(self.task_state.snapshot(), f, ensure_ascii=False)
        else:
            self.task_file.unlink(missing_ok=True)

    def load_task(self):
        """从 task.json 恢复跨轮任务；损坏或缺失则视为无任务"""
        self.task_state = None
        if self.task_file.exists():
            try:
                with open(self.task_file, "r", encoding="utf-8") as f:
                    self.task_state = TaskState.from_snapshot(json.load(f))
            except Exception as e:
                print(f"[Memory] 任务状态恢复失败（按无任务处理）: {e}")
                self.task_file.unlink(missing_ok=True)

    def clear_task(self):
        """内存与磁盘同时清除任务状态"""
        self.task_state = None
        self.task_file.unlink(missing_ok=True)

    # --- 摘要式压缩旁挂的落盘与恢复（方案A） ---
    def save_memory(self):
        """滚动摘要与水位线落盘到 memory.json（无摘要则清除，保证磁盘与内存一致）。"""
        if self.rolling_summary.strip() or self.summarized_turns > 0:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump({
                    "summary": self.rolling_summary,
                    "summarized_turns": self.summarized_turns,
                    "updated_at": time.time(),
                }, f, ensure_ascii=False)
        else:
            self.memory_file.unlink(missing_ok=True)

    def load_memory(self):
        """从 memory.json 恢复滚动摘要与水位线；损坏或缺失则视为无摘要。"""
        self.rolling_summary = ""
        self.summarized_turns = 0
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.rolling_summary = str(data.get("summary", ""))
                self.summarized_turns = int(data.get("summarized_turns", 0))
            except Exception as e:
                print(f"[Memory] 摘要旁挂恢复失败（按无摘要处理）: {e}")
                self.memory_file.unlink(missing_ok=True)

    # --- 统一的消息拼装接口 ---
    def add_user_message(self, text: str, images: List[str] = None, audios: List[str] = None,
                         extra_text: str = ""):
        """
        拼装一条用户消息。extra_text 为摄入层解析/转写出的文本（音频转写、PDF/文档正文、
        带 provenance 标记），作为紧随用户文本的独立文本块注入；它已被烘进历史日志，
        重放时原样回放，绝不重解析。images 仍走原生视觉（含渲染出的 PDF 页）。
        """
        content = [{"type": "text", "text": text}]
        if extra_text and extra_text.strip():
            content.append({"type": "text", "text": extra_text})
        if images:
            content.extend([{"type": "image_url", "image_url": {"url": img}} for img in images])
        if audios:
            content.extend([{"type": "audio_url", "audio_url": {"url": aud}} for aud in audios])
        self.history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str = "", thoughts: list = None, tool_calls: list = None):
        message = {"role": "assistant"}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if content:
            message["content"] = content
        if thoughts:
            message["thoughts"] = thoughts
        if len(message) > 1:
            self.history.append(message)

    def add_tool_message(self, tool_responses: List[dict]):
        self.history.append({"role": "tool", "tool_responses": tool_responses})

    def append_log(self, response_data: dict):
        """将一轮的最终结果落盘追加到历史文件"""
        # 会话目录已被外部删除（如用户在「挂起审批 / 进行中」时删掉该会话，审批/恢复回调
        # 随后才落盘）：无处可写，优雅跳过而非抛 FileNotFoundError 让整个请求 500。
        # 该会话本就要销毁，不应因一次残留写入被半复活（父目录不存在时 open('a') 会失败）。
        if not self.session_dir.exists():
            print(f"[Memory] Session {self.session_id} 目录已不存在，跳过落盘（会话可能已被删除）")
            return
        # 轮次完结时间戳（epoch 秒）：落盘是唯一漏斗，在这里盖章即覆盖所有路径；
        # 有意 setdefault 在**调用方的原 dict** 上——/chat 响应与 turn_end 事件
        # 在落盘之后引用同一 dict，三个面（响应/事件/历史）天然同值。
        response_data.setdefault("timestamp", time.time())
        with open(self.history_log, "a", encoding="utf-8") as f:
            # default=str 兜底：个别字段（如 Path）不可序列化时降级为字符串，
            # 避免推理已成功却因落盘序列化失败导致整个请求 500
            f.write(json.dumps(_strip_internal_meta(response_data),
                               ensure_ascii=False, default=str) + "\n")
        # 轮次已成为落盘事实 → 撤销预写标记（write-ahead 闭环，见 write_inflight）
        self.clear_inflight()

    def disk_turn_count(self) -> int:
        """history.jsonl 的落盘轮数(行数)。turn_ended 事件的 turn_index 依据。"""
        try:
            if not self.history_log.exists():
                return 0
            with open(self.history_log, "rb") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    # --- 轮次预写标记（write-ahead）：进行中的轮次防丢 ---
    # 用户消息在轮次完结前只活在内存里，进程死亡（重启/崩溃）会让输入无痕蒸发、
    # 前端重拉历史得到空会话。轮次开始即落 inflight.json（prompt + 生成参数），
    # append_log 时撤销；启动时扫描到残留标记 → 补一条 fail 轮存档
    # （SessionManager.recover_inflight_turns）。待审批/已挂起的暂存轮不撤销标记——
    # 它们同样只活在内存、同样会随进程蒸发，决议/恢复后 append_log 才是终点。
    @property
    def inflight_path(self) -> Path:
        return self.session_dir / "inflight.json"

    def write_inflight(self, prompt: str, gen: dict):
        if not self.session_dir.exists():
            return
        payload = {"prompt": prompt, "gen": gen, "started_at": time.time()}
        tmp = self.inflight_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.inflight_path)

    def clear_inflight(self):
        try:
            self.inflight_path.unlink(missing_ok=True)
        except Exception:
            pass

    # --- 会话血缘元数据（分支溯源） ---
    def save_meta(self, parent_session_id: Optional[str] = None, branch_point: Optional[int] = None) -> dict:
        """落盘会话元数据。根会话 parent_session_id 为 None，分支会话记录父会话与分支节点。"""
        meta = {
            "session_id": self.session_id,
            "parent_session_id": parent_session_id,
            "branch_point": branch_point,
            "created_at": time.time(),
        }
        with open(self.meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return meta

    def load_meta(self) -> dict:
        """读取会话血缘元数据，缺失时返回根会话默认值。老会话缺 pinned 键回落 False。"""
        if self.meta_file.exists():
            try:
                with open(self.meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta.setdefault("pinned", False)
                return meta
            except Exception:
                pass
        return {"session_id": self.session_id, "parent_session_id": None,
                "branch_point": None, "pinned": False}

    def clone_brain_memory_from(self, source: "ChatSession") -> bool:
        """
        从父会话克隆一份独立的 KV Cache 与 token 轨迹，实现分支的“热复用”。

        克隆经后端 clone_cache 完成（物理隔离由后端保证）；分支首次推理时，底层
        生成流会用现有的 prefix-matching 自动对齐到分支节点并裁剪多余部分，
        无需在此精确计算 token 边界。返回是否成功复用（失败则冷启动，正确性不受影响）。
        """
        if self._backend is None or source.brain_memory_state is None:
            return False
        cloned = self._backend.clone_cache(source.brain_memory_state)
        if cloned is None:
            return False
        self.brain_memory_state = cloned
        return True


class SessionManager:
    """
    负责维护所有活跃的 ChatSession 实例，提供冷热管理能力
    """
    def __init__(self, sessions_dir: Path, idle_timeout: int = 1800):
        # 全局任务 feed(app 装配注入;None=不镜像,测试/脚本单用不受影响)
        self.feed = None
        # 推理后端(app 装配注入;None=无后端环境,会话缓存句柄为 None、分支只冷启动)
        self.backend = None
        self.sessions_dir = sessions_dir
        self.idle_timeout = idle_timeout # 默认 30 分钟未交互则转入冷状态
        self._all_sessions: Dict[str, ChatSession] = {}
        # 任务调度器（由 app 装配时注入）：驱逐器据此避免冻结掉调度活跃
        # （排队/运行/挂起）会话的内存 KV。挂起任务的 KV 按设计常驻内存，必须钉住不冻。
        self.scheduler = None
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def recover_inflight_turns(self) -> int:
        """启动时扫描各会话残留的轮次预写标记（inflight.json）：上次进程死亡时正在
        执行/待审批/已挂起的轮次，补一条 fail 轮存档（用户输入随 prompt 字段保留、
        会话窗口可见死因），随后删除标记。纯磁盘操作，不拉起任何会话。"""
        recovered = 0
        for d in self.sessions_dir.iterdir():
            marker = d / "inflight.json"
            if not d.is_dir() or not marker.exists():
                continue
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            gen = data.get("gen") or {}
            turn = {
                "status": "fail",
                "prompt": str(data.get("prompt", "")),
                "image": [], "audio": [], "ingested_text": "", "attachments": [],
                "enable_thinking": bool(gen.get("enable_thinking", False)),
                "thinking_budget": int(gen.get("thinking_budget", 2048)),
                "max_tokens": int(gen.get("max_tokens", 4096)),
                "temperature": float(gen.get("temperature", 0.2)),
                "min_p": float(gen.get("min_p", 0.05)),
                "response": [{"role": "system",
                              "content": "服务在本轮执行中被重启/中断，任务未完成，未产生任何记忆变更。"
                                         "您的输入已保留在上方，可直接重发。"}],
            }
            try:
                with open(d / "history.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(turn, ensure_ascii=False, default=str) + "\n")
                marker.unlink(missing_ok=True)
                recovered += 1
                print(f"[Memory] 恢复中断轮次存档: {d.name}")
            except Exception as e:
                print(f"[Memory] 中断轮次存档失败 {d.name}: {e}")
        return recovered

    def get_or_create(self, session_id: str, system_prompt: str = "") -> ChatSession:
        """获取热状态的会话，如果不存在或已冷冻，则拉起并恢复数据"""
        # 1. 尝试触发闲置会话清理
        self._evict_idle_sessions()
        
        session = self._all_sessions.get(session_id)
        if not session:
            # 内存里没有，新建会话
            session = ChatSession(session_id, self.sessions_dir, system_prompt,
                                  feed=self.feed, backend=self.backend)
            self._all_sessions[session_id] = session
            # 检查磁盘中是否有历史记录，若有，则恢复历史记录
            if session.history_log.exists():
                session.restore_from_disk()
        # 2. 如果数据在冷状态，则读取磁盘进内存（但不预热大模型，预热由引擎在推理前检查 is_cold 来做）
        elif session.is_cold:
            session.restore_from_disk()
            
        # 3. 如果是活动中的会话，则仅刷新活跃时间
        session.touch()
        return session

    def get_system_prompt(self, session_id: str) -> Dict[str, str]:
        """
        只读查询会话的系统提示词（供前端上下文面板展示）。
        不触发会话创建/恢复等副作用：热会话取内存，其余读磁盘，都没有则返回默认值。
        source: "session" 表示该会话已有落盘/在用的提示词，"default" 表示会话尚未创建。
        """
        session = self._all_sessions.get(session_id)
        if session and not session.is_cold and session.history:
            first = session.history[0]
            if first.get("role") == "system":
                return {"system_prompt": str(first.get("content", "")), "source": "session"}

        system_file = self.sessions_dir / session_id / "system.json"
        if system_file.exists():
            try:
                with open(system_file, "r", encoding="utf-8") as f:
                    item = json.load(f)
                return {"system_prompt": str(item.get("content", "")), "source": "session"}
            except Exception:
                pass
        return {"system_prompt": DEFAULT_SYSTEM_PROMPT, "source": "default"}

    def _evict_idle_sessions(self):
        """检查并清理超时未活跃的 Session（调度活跃的会话钉住不冻，保住其内存 KV）"""
        current_time = time.time()
        scheduler = self.scheduler
        for session_id, session in list(self._all_sessions.items()):
            if session.is_cold or (current_time - session.last_access_time <= self.idle_timeout):
                continue
            # 排队/运行/挂起中的任务不能被冻结：挂起任务的 KV 依设计常驻内存等待恢复
            if scheduler is not None and scheduler.is_active(session_id):
                continue
            session.freeze()

    async def delete_session(self, session_id: str) -> dict:
        """
        物理销毁会话与文件。
        并发安全：热会话先置取消标志，再在会话锁内冻结——生成中的轮次会在最近的
        检查点（token 段 / 迭代边界）以 cancelled 干净退出并释放锁，之后才清内存与磁盘；
        绝不在 run_turn 使用中的 history/KV 上直接动手（否则整轮产出被静默丢弃、行为未定义）。
        注意：调度句柄要等轮次退出后再 discard——若该轮还在排队等 worker，先 discard 会让
        它永远等不到准入信号、锁也永远不释放（死锁）。
        """
        result = {"status": False, "frozen": False, "response": ""}
        session = self._all_sessions.get(session_id)
        if session is not None:
            session.cancel_requested = True  # 生成中：引擎逐 token 段响应，整轮以 cancelled 退出
            async with session.lock:         # 等进行中的轮次在检查点干净退出
                self._all_sessions.pop(session_id, None)
                session.freeze()
                session.journal.close()      # 终结账本：/events 附着流收流
                result["frozen"] = True
        if self.scheduler is not None:
            self.scheduler.discard(session_id)  # 清理调度句柄，防止状态表泄漏

        session_dir = self.sessions_dir / session_id
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
                result["status"] = True
                result["response"] = f"✅ session {session_id} 删除成功"
            except Exception as e:
                print(f"[Memory] 销毁物理文件夹失败: {e}")
                result["status"] = False
                result["response"] = f"❌ session {session_id} 删除失败: {e}"
        else:
            if result["frozen"]:
                result["status"] = True
                result["response"] = f"✅ session {session_id} 删除成功"
            else:
                result["status"] = False
                result["response"] = f"❌ 未发现session {session_id} : 请确认该会话是否存在"
        return result

    # ==========================================
    # 会话记录清理：反向映射索引 + pin + 确定性磁盘回收
    # ==========================================
    def set_meta(self, session_id: str, *, title: Optional[str] = None,
                 pinned: Optional[bool] = None) -> bool:
        """会话元数据落盘(读-改-写 meta.json,保留血缘等其余字段,不经 save_meta 以免覆盖)。
        title:用户改名(服务端一等,双端同源;空串=清除改名回退派生标题);
        pinned:置/撤保留(pin 的会话不被自动清扫)。返回是否落盘成功。"""
        session_dir = self.sessions_dir / session_id
        if not session_dir.is_dir():
            return False
        meta_file = session_dir / "meta.json"
        meta: Dict[str, Any] = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.setdefault("session_id", session_id)
        if pinned is not None:
            meta["pinned"] = bool(pinned)
        if title is not None:
            t = str(title).strip()[:120]
            if t:
                meta["title"] = t
            else:
                meta.pop("title", None)
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[Memory] pin 落盘失败 {session_id}: {e}")
            return False

    def _scan_session_dir(self, d: Path) -> Optional[Dict[str, Any]]:
        """扫描单个会话目录，返回真相元数据（纯磁盘只读，不拉起会话）。非目录返回 None。
        title 从 history 首条轮次的 prompt 字段派生（与前端 localStorage 无关，孤儿也有人话名）。"""
        if not d.is_dir():
            return None
        sid = d.name
        meta: Dict[str, Any] = {}
        meta_file = d / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        hist = d / "history.jsonl"
        turn_count = 0
        last_updated: Optional[float] = None
        title = ""
        try:
            has_content = hist.exists() and hist.stat().st_size > 0
        except OSError:
            has_content = False
        if has_content:
            try:
                last_updated = hist.stat().st_mtime
                with open(hist, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        turn_count += 1
                        if not title:
                            try:
                                title = str(json.loads(line).get("prompt", "")).strip()[:80]
                            except Exception:
                                pass
            except Exception:
                pass
        created_at = meta.get("created_at")
        if created_at is None:
            try:
                created_at = d.stat().st_ctime
            except OSError:
                created_at = None
        scheduler = self.scheduler
        return {
            "session_id": sid,
            # 用户改名(meta.json title)优先;未改名回退 history 首轮 prompt 派生
            "title": str(meta.get("title") or "").strip() or title,
            "turn_count": turn_count,
            "created_at": created_at,
            "last_updated": last_updated if last_updated is not None else created_at,
            "pinned": bool(meta.get("pinned", False)),
            "parent_session_id": meta.get("parent_session_id"),
            "active": bool(scheduler.is_active(sid)) if scheduler is not None else False,
        }

    def list_sessions_meta(self) -> List[Dict[str, Any]]:
        """扫描 sessions 目录返回全部会话的真相元数据（供前端反向映射/对账）。
        只读、不拉起会话；按最后更新时间倒序。"""
        out: List[Dict[str, Any]] = []
        for d in self.sessions_dir.iterdir():
            item = self._scan_session_dir(d)
            if item is not None:
                out.append(item)
        out.sort(key=lambda m: (m.get("last_updated") or 0), reverse=True)
        return out

    async def prune_expired_sessions(self, retention_days: float, empty_ttl_hours: float) -> List[str]:
        """确定性磁盘回收：pin / 调度活跃 / 进行中 一律跳过；有内容会话按最后更新时间过期，
        空壳（无历史/0 轮）按创建时间过期。复用 delete_session（并发安全）。返回被回收的 id 列表。
        先物化候选再删除——避免边遍历边 rmtree。"""
        now = time.time()
        content_cutoff = retention_days * 86400.0
        empty_cutoff = empty_ttl_hours * 3600.0
        scheduler = self.scheduler
        candidates: List[str] = []
        for d in list(self.sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            sid = d.name
            # 定时任务的执行会话（schedule_{id}）由 ScheduleStore 独立管理、前端反向映射也豁免——
            # 一律不回收，避免删掉低频计划的执行日志（与前端对账策略对齐）
            if sid.startswith("schedule_"):
                continue
            # 进行中（预写标记在轮首写、append_log 清）——绝不碰
            if (d / "inflight.json").exists():
                continue
            # 调度活跃（运行/排队/挂起）——钉住不删
            if scheduler is not None and scheduler.is_active(sid):
                continue
            meta: Dict[str, Any] = {}
            meta_file = d / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            if meta.get("pinned"):
                continue
            hist = d / "history.jsonl"
            try:
                has_content = hist.exists() and hist.stat().st_size > 0
            except OSError:
                has_content = False
            if has_content:
                try:
                    if (now - hist.stat().st_mtime) <= content_cutoff:
                        continue
                except OSError:
                    continue
            else:
                created = meta.get("created_at")
                if created is None:
                    try:
                        created = d.stat().st_ctime
                    except OSError:
                        continue
                if (now - float(created)) <= empty_cutoff:
                    continue
            candidates.append(sid)

        reaped: List[str] = []
        for sid in candidates:
            try:
                res = await self.delete_session(sid)
                if res.get("status"):
                    reaped.append(sid)
            except Exception as e:
                print(f"[Cleanup] 回收会话失败 {sid}: {e}")
        return reaped

    # ==========================================
    # 轮次回退能力（删除最后一轮：内存、KV、落盘三方同步）
    # ==========================================
    def _delete_turn_media(self, session_id: str, turn: Dict[str, Any]):
        """物理删除某轮引用的多模态文件（仅限本会话目录内，防止越界删除）"""
        session_dir = (self.sessions_dir / session_id).resolve()
        # image/audio 为路径字符串列表；attachments 为 {path,type,name} 字典列表
        media_paths = list(turn.get("image") or []) + list(turn.get("audio") or [])
        media_paths.extend(a.get("path") for a in (turn.get("attachments") or []) if isinstance(a, dict))
        for p in media_paths:
            if not p:
                continue
            try:
                f = Path(p)
                if f.is_file() and f.resolve().is_relative_to(session_dir):
                    f.unlink()
            except Exception as e:
                print(f"[Memory] 清理轮次媒体文件失败 ({p}): {e}")

    async def delete_last_turn(self, session_id: str) -> Dict[str, Any]:
        """
        删除会话的最后一轮问答，内存记忆、KV Cache 与落盘日志三方同步：
        - 落盘：截断 history.jsonl 的最后一行，并物理删除该轮的媒体文件；
        - 内存（热会话）：按截断后的日志整体重建，防止冷启动后不同步；
        - KV Cache：保留不动，下次推理经前缀匹配自动裁剪对齐（与分支复用同一机制）；
        - 挂起审批轮：尚未落盘，直接回滚内存并丢弃挂起状态。
        """
        result: Dict[str, Any] = {
            "status": False,
            "session_id": session_id,
            "remaining_turns": None,
            "response": "",
        }
        history_log = self.sessions_dir / session_id / "history.jsonl"
        session = self._all_sessions.get(session_id)

        if session is not None:
            # 与对话/审批接口互斥：等待进行中的轮次完整结束后再回退
            async with session.lock:
                return await asyncio.to_thread(self._delete_last_turn_locked, session, history_log, result)
        return await asyncio.to_thread(self._delete_last_turn_locked, None, history_log, result)

    def _delete_last_turn_locked(self, session: Optional[ChatSession], history_log: Path, result: Dict[str, Any]) -> Dict[str, Any]:
        session_id = result["session_id"]

        # 1. 未落盘的挂起中轮次（等待审批 / 已挂起，仅存内存）：丢弃挂起状态，
        #    以日志为准整体重建内存（history 与 images/audios 引用一并回退）
        if session is not None and (session.pending_tool_calls or session.pending_turn or session.suspended_turn):
            pending = session.pending_turn or session.suspended_turn or {}
            session.pending_tool_calls = None
            session.pending_turn = None
            session.suspended_turn = None
            session.suspend_requested = False
            if self.scheduler is not None:
                self.scheduler.discard(session_id)  # 清理残留调度句柄
            session.clear_task()  # 该轮任务上下文一并回退
            session.rebuild_from_log()
            self._delete_turn_media(session_id, pending)
            remaining = 0
            if history_log.exists():
                with open(history_log, "r", encoding="utf-8") as f:
                    remaining = sum(1 for line in f if line.strip())
            result.update({
                "status": True,
                "remaining_turns": remaining,
                "response": "✅ 已撤销等待审批的未完结轮次",
            })
            return result

        # 2. 常规轮次：截断日志最后一行
        if not history_log.exists():
            result["response"] = f"❌ 会话 {session_id} 不存在或尚无历史记录"
            return result
        with open(history_log, "r", encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        if not lines:
            result["response"] = f"❌ 会话 {session_id} 没有可删除的轮次"
            return result

        last_line = lines.pop()
        try:
            deleted_turn = json.loads(last_line)
        except Exception:
            deleted_turn = {}

        with open(history_log, "w", encoding="utf-8") as f:
            f.writelines(line if line.endswith("\n") else line + "\n" for line in lines)

        # 3. 清除跨轮任务状态：被删轮次可能是任务的一部分，回退后任务上下文已失效
        if session is not None:
            session.clear_task()
        else:
            (self.sessions_dir / session_id / "task.json").unlink(missing_ok=True)

        # 4. 热会话：以截断后的日志为准重建内存（KV Cache 保留，推理时自动对齐）
        if session is not None and not session.is_cold:
            session.rebuild_from_log()

        # 5. 物理清理该轮的媒体文件
        self._delete_turn_media(session_id, deleted_turn)

        result.update({
            "status": True,
            "remaining_turns": len(lines),
            "response": f"✅ 已删除会话 {session_id} 的最后一轮（剩余 {len(lines)} 轮）",
        })
        return result

    # ==========================================
    # 会话分支能力（复制对话窗口 / 复用 KV Cache 与历史文本）
    # ==========================================
    def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """
        读取某会话落盘的历史节点列表（每个元素为一轮完整的 response_data）。
        供前端选取分支节点使用，节点下标即列表索引。
        """
        turns: List[Dict[str, Any]] = []
        history_log = self.sessions_dir / session_id / "history.jsonl"
        if not history_log.exists():
            return turns
        with open(history_log, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    turns.append(json.loads(line))
                except Exception:
                    continue
        return turns

    def _relink_media(self, src_path_str: str, dest_dir: Path) -> str:
        """
        将父会话引用的多模态文件“零拷贝”复用到分支目录：优先硬链接（共享物理数据块），
        跨设备等失败时回退为物理复制。返回分支目录下的新路径；源文件缺失时原样保留。
        """
        try:
            src = Path(src_path_str)
            if not src.exists() or not src.is_file():
                return src_path_str
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if not dest.exists():
                try:
                    os.link(src, dest)  # 硬链接：共享底层数据块，删除父会话也不会影响分支
                except Exception:
                    shutil.copy2(src, dest)
            return str(dest)
        except Exception:
            return src_path_str

    def _materialize_branch(self, parent_id: str, new_id: str,
                            kept_lines: List[str], branch_point: int) -> None:
        """
        在磁盘上物化分支会话：复制系统提示、写入血缘元数据、截断复制历史日志，
        并将历史节点引用的多模态文件重挂到分支目录，保证分支自包含且可独立冷恢复。
        """
        parent_dir = self.sessions_dir / parent_id
        new_dir = self.sessions_dir / new_id
        new_image_dir = new_dir / "image"
        new_audio_dir = new_dir / "audio"
        new_attach_dir = new_dir / "attach"
        new_image_dir.mkdir(parents=True, exist_ok=True)
        new_audio_dir.mkdir(parents=True, exist_ok=True)
        new_attach_dir.mkdir(parents=True, exist_ok=True)

        # 1. 复制系统提示词
        src_system = parent_dir / "system.json"
        if src_system.exists():
            shutil.copy2(src_system, new_dir / "system.json")

        # 1.1 复制摘要旁挂（方案A）：不带上它，分支会从 0 重放全量原文——上下文膨胀回
        # 未压缩状态，且与克隆 KV 的 [system]+[摘要]+[尾部] 前缀发散、热复用全废。
        # 仅当摘要水位线落在分支窗口内（summarized_turns ≤ 保留行数）才复制；
        # 分支点在已折叠区内时摘要覆盖了分支点之后的内容，不能用，退回全量重放（正确性优先）。
        src_memory = parent_dir / "memory.json"
        if src_memory.exists():
            try:
                with open(src_memory, "r", encoding="utf-8") as f:
                    mem = json.load(f)
                if int(mem.get("summarized_turns", 0)) <= len(kept_lines):
                    shutil.copy2(src_memory, new_dir / "memory.json")
            except Exception as e:
                print(f"[Memory] 分支复制摘要旁挂失败（分支将全量重放）: {e}")

        # 2. 写入血缘元数据（协调改进后的日志结构：记录父会话与分支节点）
        meta = {
            "session_id": new_id,
            "parent_session_id": parent_id,
            "branch_point": branch_point,
            "created_at": time.time(),
        }
        with open(new_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        # 3. 截断复制历史日志，并重挂多模态文件
        with open(new_dir / "history.jsonl", "w", encoding="utf-8") as out:
            for line in kept_lines:
                try:
                    turn = json.loads(line)
                except Exception:
                    out.write(line if line.endswith("\n") else line + "\n")
                    continue
                if turn.get("image"):
                    turn["image"] = [self._relink_media(str(p), new_image_dir) for p in turn["image"]]
                if turn.get("audio"):
                    turn["audio"] = [self._relink_media(str(p), new_audio_dir) for p in turn["audio"]]
                # 原始附件（音频/PDF/文档/代码）重挂到分支目录，保证分支自包含、逃生舱可用。
                # 注：ingested_text 里的 provenance 路径仍指向父目录（best-effort，父文件经
                # 硬链接共享数据块不会因父会话删除而失效，除非父目录整体被删）。
                if turn.get("attachments"):
                    for a in turn["attachments"]:
                        if isinstance(a, dict) and a.get("path"):
                            a["path"] = self._relink_media(str(a["path"]), new_attach_dir)
                out.write(json.dumps(turn, ensure_ascii=False) + "\n")

    async def branch_session(self, parent_session_id: str, node_index: int,
                             new_session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        从父会话的任意历史节点派生一个新的分支会话（复制对话窗口）。

        - 历史文本：截断复制父会话在 [0, node_index] 区间的历史节点并重建到内存。
        - KV Cache：若父会话处于热状态，克隆一份独立 Cache 供分支热复用，
          底层推理时经 prefix-matching 自动对齐分支节点，避免重复预填充。
        - 落盘：分支自成目录，记录父会话血缘，多模态文件以硬链接零拷贝复用。
        - 并发安全：全程持有父会话锁，父会话推理中/落盘中不会被克隆到中间状态。
        """
        result: Dict[str, Any] = {
            "status": False,
            "session_id": None,
            "parent_session_id": parent_session_id,
            "branch_point": node_index,
            "turns": None,
            "kv_cache_reused": None,
            "response": "",
        }

        # 1. 父会话存在性预检
        parent_log = self.sessions_dir / parent_session_id / "history.jsonl"
        if not parent_log.exists():
            result["response"] = f"❌ 源会话 {parent_session_id} 不存在或尚无历史记录"
            return result

        # 2. 确保父会话在内存中且尽量为热态（以便克隆其 KV Cache），
        #    并持有其会话锁：等待正在进行的对话轮次完成后再做快照
        parent = self.get_or_create(parent_session_id)
        async with parent.lock:
            # 3. 锁内读取历史并校验节点（保证读到的是完整落盘的轮次）
            with open(parent_log, "r", encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
            total = len(lines)
            if total == 0:
                result["response"] = f"❌ 源会话 {parent_session_id} 没有可分支的历史节点"
                return result
            if node_index < 0:  # 支持负索引，-1 表示最后一个节点
                node_index += total
            if node_index < 0 or node_index >= total:
                result["response"] = f"❌ 非法的历史节点 index={result['branch_point']}，有效范围 [0, {total - 1}]"
                return result

            # 4. 生成并校验分支会话 ID（字符白名单：ID 会直接拼进文件系统路径，
            #    放行 `..`/分隔符等载荷会把分支目录物化到 sessions/ 之外）
            if not new_session_id:
                new_session_id = f"{parent_session_id}_branch_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", new_session_id) or ".." in new_session_id:
                result["response"] = "❌ 非法的目标会话 ID：仅允许字母/数字开头，且只含字母/数字/._-"
                return result
            if new_session_id in self._all_sessions or (self.sessions_dir / new_session_id).exists():
                result["response"] = f"❌ 目标会话 {new_session_id} 已存在，请更换 ID"
                return result

            # 5. 磁盘物化：截断复制历史窗口 [0, node_index]
            self._materialize_branch(parent_session_id, new_session_id, lines[: node_index + 1], node_index)

            # 6. 内存重建分支会话（复用共同的历史文本）
            branch = ChatSession(new_session_id, self.sessions_dir, "",
                                 feed=self.feed, backend=self.backend)
            if branch.history_log.exists():
                branch.restore_from_disk()

            # 7. 复用共同的 KV Cache（热态父会话才有可克隆的缓存）
            kv_reused = branch.clone_brain_memory_from(parent)

        self._all_sessions[new_session_id] = branch
        branch.touch()

        result.update({
            "status": True,
            "session_id": new_session_id,
            "branch_point": node_index,
            "turns": node_index + 1,
            "kv_cache_reused": kv_reused,
            "response": (
                f"✅ 已从会话 {parent_session_id} 的节点 {node_index} 派生分支 {new_session_id} "
                f"({'已复用 KV Cache' if kv_reused else '父会话非热态，将冷启动预填充'})"
            ),
        })
        return result
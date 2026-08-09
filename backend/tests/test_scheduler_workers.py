"""任务调度器多 worker 语义（2026-08-09 并发落地）：容量内并行准入、满员排队、
释放递补、受害者选择。默认 max_workers=1 的旧单 worker 语义由既有金测试覆盖。"""
import asyncio

from framework.core.scheduler import TaskScheduler, ClassPolicy


def test_two_workers_admit_two_then_queue_third():
    async def main():
        sch = TaskScheduler(max_workers=2)
        h1 = await sch.acquire("s1")
        h2 = await sch.acquire("s2")
        assert h1.status == "running" and h2.status == "running"

        t3 = asyncio.ensure_future(sch.acquire("s3"))
        await asyncio.sleep(0)          # 给调度一次机会：满员，s3 应仍在排队
        assert not t3.done()
        assert sch.status_of("s3") == "queued"

        await sch.release("s1")         # 腾出一个名额 → s3 递补
        h3 = await asyncio.wait_for(t3, timeout=1)
        assert h3.status == "running"
        assert sch.snapshot()["running"] == ["s2", "s3"]
        await sch.release("s2"); await sch.release("s3")
    asyncio.run(main())


def test_single_worker_legacy_serialization():
    async def main():
        sch = TaskScheduler(max_workers=1)
        await sch.acquire("a")
        tb = asyncio.ensure_future(sch.acquire("b"))
        await asyncio.sleep(0)
        assert not tb.done()            # 旧互斥语义：第二个必须等
        await sch.release("a")
        await asyncio.wait_for(tb, timeout=1)
        await sch.release("b")
    asyncio.run(main())


def test_preempt_victim_prefers_background():
    async def main():
        suspended, cancelled = [], []
        sch = TaskScheduler(
            policy=ClassPolicy(preempt_background=True),
            on_suspend_request=suspended.append,
            on_cancel_request=cancelled.append,
            max_workers=2,
        )
        await sch.acquire("chat1", kind="chat", priority=5)
        await sch.acquire("bg1", kind="summarize", priority=0)
        # 高优先级新任务在满员时到场 → 受害者应是后台 summarize（cancel 原语），
        # 用户自己的 chat 任务永不被自动打断
        t = asyncio.ensure_future(sch.acquire("chat2", kind="chat", priority=9))
        await asyncio.sleep(0)
        assert cancelled == ["bg1"] and suspended == []
        # 后台被取消后其执行侧会 release；这里模拟之 → chat2 递补
        await sch.release("bg1")
        await asyncio.wait_for(t, timeout=1)
        await sch.release("chat1"); await sch.release("chat2")
    asyncio.run(main())

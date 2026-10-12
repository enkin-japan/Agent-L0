# 撤销未落盘暂停轮

最后一轮暂停后被删除：`turn_ended(cancelled)` 携带可选 `discarded: true`，没有历史下标。归约器清理审批/挂起/预览，保留已完成轮 transcript，不新增历史消息。后续真正落盘轮可使用下标 1。

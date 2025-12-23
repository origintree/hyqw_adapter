"""报文穷举录制器 - 支持窗帘/空调/地暖/新风/灯具全量穷举"""
import asyncio
import logging
from typing import Dict, Any, List, Callable, Optional

from .replay_manager import ReplayManager

_LOGGER = logging.getLogger(__name__)


class CurtainEnumerator:
    """窗帘穷举执行器"""

    def __init__(self, coordinator, replay: ReplayManager, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.coordinator = coordinator
        self.replay = replay
        self._update_status = status_updater or (lambda d: None)
        self._running = False
        self._status: Dict[str, Any] = {
            "device_count": 0,
            "processed_devices": 0,
            "current_device": None,
            "sent": 0,
            "recorded": 0,
            "timeouts": 0,
        }

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    async def run(self) -> None:
        self._running = True
        try:
            # 重置内部计数，避免多次启动时继承上次的统计导致进度异常
            self._status["processed_devices"] = 0
            self._status["sent"] = 0
            self._status["recorded"] = 0
            self._status["timeouts"] = 0
            self._status["current_device"] = None
            # 仅挑选窗帘设备，并按 si 去重，避免同一物理设备被重复录制
            raw_devices = [d for d in (self.coordinator.data or {}).get("devices", []) if d.get("typeId") == 14]
            devices = []
            seen_si = set()
            for d in raw_devices:
                si = d.get("si")
                if si in seen_si:
                    continue
                seen_si.add(si)
                devices.append(d)
            self._status["device_count"] = len(devices)
            self.replay.start_recording()
            self._update_status({
                "running": True,
                "total_devices": len(devices),
                "processed_devices": 0,
                "current_device": None,
                "current_fn": None,
                "current_fv": None,
                "current_cmd_index": 0,
                "current_cmd_total": 0,
                "current_state": "就绪",
            })

            for dev in devices:
                if not self._running:
                    break
                await self._enumerate_device(dev)
                self._status["processed_devices"] += 1
                self._update_status({
                    "processed_devices": self._status["processed_devices"],
                })
        finally:
            self.replay.stop_recording()
            self._running = False
            self._update_status({"running": False})

    async def stop(self) -> None:
        self._running = False

    async def _enumerate_device(self, device: Dict[str, Any]) -> None:
        self._status["current_device"] = device.get("deviceName")
        si = device["si"]
        st = 20201  # 窗帘控制st固定
        type_id = device.get("typeId", 14)
        name = device.get("deviceName", str(si))
        total_cmds = 3 + 101
        current_index = 0
        self._update_status({
            "current_device": name,
            "current_cmd_total": total_cmds,
            "current_cmd_index": 0,
        })

        # 1) fn1: 0,1,2
        for fv in (0, 1, 2):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 1,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=1, fv=fv)

        # 2) fn2: 0..100 全量
        for fv in range(0, 101):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 2,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=2, fv=fv)

    async def _send_and_record(self, device: Dict[str, Any], st: int, si: int, type_id: int, name: str, fn: int, fv: int) -> None:
        max_retries = 2
        for attempt in range(max_retries + 1):  # 总共尝试3次（初次 + 2次重试）
            # 通过立即控制触发云端下发
            ok = await self.coordinator.async_control_device_immediate(
                device_id=device["deviceId"],
                st=st,
                si=si,
                fn=fn,
                fv=fv,
                entity_id=f"replay_recorder:{si}:{fn}:{fv}",
            )
            self._status["sent"] += 1
            
            if not ok:
                if attempt < max_retries:
                    _LOGGER.warning(f"设备si={si} fn={fn} fv={fv} 控制失败，第{attempt + 1}次重试...")
                    self._update_status({"current_state": f"控制失败，重试{attempt + 1}"})
                    await asyncio.sleep(1.0)  # 重试前等待1秒
                    continue
                else:
                    _LOGGER.error(f"设备si={si} fn={fn} fv={fv} 控制失败，已重试{max_retries}次，跳过")
                    self.replay.add_failed_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, reason="control_failed")
                    self._update_status({"current_state": "控制失败"})
                    return

            # 等待下行报文
            payload_hex = await self.replay.capture_next_down(timeout=8.0)
            if not payload_hex:
                if attempt < max_retries:
                    _LOGGER.warning(f"设备si={si} fn={fn} fv={fv} 等待下行超时，第{attempt + 1}次重试...")
                    self._update_status({"current_state": f"录制超时，重试{attempt + 1}"})
                    await asyncio.sleep(1.0)  # 重试前等待1秒
                    continue
                else:
                    self._status["timeouts"] += 1
                    _LOGGER.error(f"设备si={si} fn={fn} fv={fv} 等待下行超时，已重试{max_retries}次，跳过")
                    # 添加失败指令到失败列表
                    self.replay.add_failed_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, reason="timeout")
                    self._update_status({"current_state": "录制超时"})
                    return

            # 记录样本成功
            self.replay.record_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, payload_hex=payload_hex, qos=0)
            self._status["recorded"] += 1
            _LOGGER.info(f"已录制 si={si} fn={fn} fv={fv} 的下行报文")
            self._update_status({"current_state": "已录制"})
            
            # 成功录制后等待0.5秒立即发送下一指令
            await asyncio.sleep(0.5)
            return  # 成功后直接返回


class ReplayRecorder:
    """对外的录制器入口（支持多设备全量穷举）"""

    def __init__(self, coordinator, replay: ReplayManager):
        self.coordinator = coordinator
        self.replay = replay
        self._task: asyncio.Task | None = None
        self._curtain = CurtainEnumerator(coordinator, replay, self._update_status)
        self._current_runner = None  # 当前运行的枚举器
        # 统一状态与监听
        self._status: Dict[str, Any] = {
            "running": False,
            "total_devices": 0,
            "processed_devices": 0,
            "current_device": None,
            "current_fn": None,
            "current_fv": None,
            "current_cmd_index": 0,
            "current_cmd_total": 0,
            "current_state": "空闲",
        }
        self._listeners: List[Callable[[], None]] = []

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)

    def get_status_text(self) -> str:
        s = self._status
        current_device = s.get("current_device") or "-"
        fn = s.get("current_fn")
        fv = s.get("current_fv")
        idx = s.get("current_cmd_index", 0)
        total = s.get("current_cmd_total", 0)
        state = s.get("current_state") or "-"
        processed = s.get("processed_devices", 0)
        total_dev = s.get("total_devices", 0)
        return (
            f"当前设备： {current_device}\n"
            f"请求指令：fn={fn},fv={fv} （ {idx} / {total} ）\n"
            f"当前状态：{state}\n"
            f"总进度：{processed} / {total_dev}"
        )

    def add_status_listener(self, cb: Callable[[], None]) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_status_listener(self, cb: Callable[[], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                # 监听者负责在主线程调用 async_write_ha_state
                cb()
            except Exception:
                pass

    def _update_status(self, partial: Dict[str, Any]) -> None:
        self._status.update(partial)
        # 调度通知（无需跨线程）
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.call_soon(self._notify_listeners)
        else:
            self._notify_listeners()

    async def start_curtain_full(self) -> None:
        if self.is_running():
            _LOGGER.warning("录制器已在运行，忽略启动请求")
            return
        self._current_runner = self._curtain
        self._task = asyncio.create_task(self._curtain.run())

    async def stop(self) -> None:
        await self._curtain.stop()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._current_runner = None


# ===== 其它设备枚举器 =====

class BaseEnumerator:
    def __init__(self, coordinator, replay: ReplayManager, type_id: int, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.coordinator = coordinator
        self.replay = replay
        self.type_id = type_id
        self._update_status = status_updater or (lambda d: None)
        self._running = False
        self._status: Dict[str, Any] = {
            "device_count": 0,
            "processed_devices": 0,
            "current_device": None,
            "sent": 0,
            "recorded": 0,
            "timeouts": 0,
        }

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    async def run(self) -> None:
        self._running = True
        try:
            # 重置内部计数，避免多次启动时继承上次的统计导致进度异常
            self._status["processed_devices"] = 0
            self._status["sent"] = 0
            self._status["recorded"] = 0
            self._status["timeouts"] = 0
            self._status["current_device"] = None
            
            # 获取指定类型的设备，并按 si 去重，避免同一物理设备被重复录制
            raw_devices = [d for d in (self.coordinator.data or {}).get("devices", []) if d.get("typeId") == self.type_id]
            devices = []
            seen_si = set()
            for d in raw_devices:
                si = d.get("si")
                if si in seen_si:
                    continue
                seen_si.add(si)
                devices.append(d)
            
            self._status["device_count"] = len(devices)
            self.replay.start_recording()
            
            # 更新开始状态
            self._update_status({
                "running": True,
                "total_devices": len(devices),
                "processed_devices": 0,
                "current_device": None,
                "current_fn": None,
                "current_fv": None,
                "current_cmd_index": 0,
                "current_cmd_total": 0,
                "current_state": "就绪",
            })
            
            for dev in devices:
                if not self._running:
                    break
                await self._enumerate_device(dev)
                self._status["processed_devices"] += 1
                self._update_status({
                    "processed_devices": self._status["processed_devices"],
                })
        finally:
            self.replay.stop_recording()
            self._running = False
            self._update_status({"running": False})

    async def stop(self) -> None:
        self._running = False

    async def _send_and_record(self, device: Dict[str, Any], st: int, si: int, type_id: int, name: str, fn: int, fv: int) -> None:
        max_retries = 2
        for attempt in range(max_retries + 1):  # 总共尝试3次（初次 + 2次重试）
            # 通过立即控制触发云端下发
            ok = await self.coordinator.async_control_device_immediate(
                device_id=device["deviceId"],
                st=st,
                si=si,
                fn=fn,
                fv=fv,
                entity_id=f"replay_recorder:{si}:{fn}:{fv}",
            )
            self._status["sent"] += 1
            
            if not ok:
                if attempt < max_retries:
                    _LOGGER.warning(f"设备si={si} fn={fn} fv={fv} 控制失败，第{attempt + 1}次重试...")
                    self._update_status({"current_state": f"控制失败，重试{attempt + 1}"})
                    await asyncio.sleep(1.0)  # 重试前等待1秒
                    continue
                else:
                    _LOGGER.error(f"设备si={si} fn={fn} fv={fv} 控制失败，已重试{max_retries}次，跳过")
                    self.replay.add_failed_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, reason="control_failed")
                    self._update_status({"current_state": "控制失败"})
                    return

            # 等待下行报文
            payload_hex = await self.replay.capture_next_down(timeout=8.0)
            if not payload_hex:
                if attempt < max_retries:
                    _LOGGER.warning(f"设备si={si} fn={fn} fv={fv} 等待下行超时，第{attempt + 1}次重试...")
                    self._update_status({"current_state": f"录制超时，重试{attempt + 1}"})
                    await asyncio.sleep(1.0)  # 重试前等待1秒
                    continue
                else:
                    self._status["timeouts"] += 1
                    _LOGGER.error(f"设备si={si} fn={fn} fv={fv} 等待下行超时，已重试{max_retries}次，跳过")
                    # 添加失败指令到失败列表
                    self.replay.add_failed_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, reason="timeout")
                    self._update_status({"current_state": "录制超时"})
                    return

            # 记录样本成功
            self.replay.record_command(si=si, st=st, type_id=type_id, name=name, fn=fn, fv=fv, payload_hex=payload_hex, qos=0)
            self._status["recorded"] += 1
            _LOGGER.info(f"已录制 si={si} fn={fn} fv={fv} 的下行报文")
            self._update_status({"current_state": "已录制"})
            
            # 成功录制后等待0.5秒立即发送下一指令
            await asyncio.sleep(0.5)
            return  # 成功后直接返回


class ACEnumerator(BaseEnumerator):
    def __init__(self, coordinator, replay: ReplayManager, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(coordinator, replay, type_id=12, status_updater=status_updater)

    async def _enumerate_device(self, device: Dict[str, Any]) -> None:
        self._status["current_device"] = device.get("deviceName")
        si = device["si"]
        st = device.get("st", 10101)
        type_id = device.get("typeId", 12)
        name = device.get("deviceName", str(si))
        
        # 计算总命令数：fn1(2个) + fn2(12个) + fn3(4个) + fn4(4个) = 22个
        total_cmds = 2 + 12 + 4 + 4
        current_index = 0
        
        self._update_status({
            "current_device": name,
            "current_cmd_total": total_cmds,
            "current_cmd_index": 0,
        })

        # fn1: 开关 (0,1)
        for fv in (0, 1):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 1,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=1, fv=fv)

        # fn2: 温度设置 (18-29)
        for fv in range(18, 30):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 2,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=2, fv=fv)

        # fn3: 模式设置 (0-3)
        for fv in range(0, 4):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 3,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=3, fv=fv)

        # fn4: 风速设置 (0-3)
        for fv in range(0, 4):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 4,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=4, fv=fv)


class FloorHeatingEnumerator(BaseEnumerator):
    def __init__(self, coordinator, replay: ReplayManager, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(coordinator, replay, type_id=16, status_updater=status_updater)

    async def _enumerate_device(self, device: Dict[str, Any]) -> None:
        self._status["current_device"] = device.get("deviceName")
        si = device["si"]
        st = device.get("st", 10101)
        type_id = device.get("typeId", 16)
        name = device.get("deviceName", str(si))
        
        # 计算总命令数：fn1(2个) + fn2(31个) = 33个
        total_cmds = 2 + 31
        current_index = 0
        
        self._update_status({
            "current_device": name,
            "current_cmd_total": total_cmds,
            "current_cmd_index": 0,
        })

        # fn1: 开关 (0,1)
        for fv in (0, 1):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 1,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=1, fv=fv)

        # fn2: 温度设置 (5-35)
        for fv in range(5, 36):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 2,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=2, fv=fv)


class FreshAirEnumerator(BaseEnumerator):
    def __init__(self, coordinator, replay: ReplayManager, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(coordinator, replay, type_id=36, status_updater=status_updater)

    async def _enumerate_device(self, device: Dict[str, Any]) -> None:
        self._status["current_device"] = device.get("deviceName")
        si = device["si"]
        st = device.get("st", 10101)
        type_id = device.get("typeId", 36)
        name = device.get("deviceName", str(si))
        
        # 计算总命令数：fn1(2个) + fn3(4个) = 6个
        total_cmds = 2 + 4
        current_index = 0
        
        self._update_status({
            "current_device": name,
            "current_cmd_total": total_cmds,
            "current_cmd_index": 0,
        })

        # fn1: 开关 (0,1)
        for fv in (0, 1):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 1,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=1, fv=fv)

        # fn3: 风速设置 (0-3)
        for fv in range(0, 4):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 3,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=3, fv=fv)


class LightEnumerator(BaseEnumerator):
    def __init__(self, coordinator, replay: ReplayManager, status_updater: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(coordinator, replay, type_id=8, status_updater=status_updater)

    async def _enumerate_device(self, device: Dict[str, Any]) -> None:
        self._status["current_device"] = device.get("deviceName")
        si = device["si"]
        st = device.get("st", 10101)
        type_id = device.get("typeId", 8)
        name = device.get("deviceName", str(si))
        
        # 计算总命令数：fn1(2个) = 2个
        total_cmds = 2
        current_index = 0
        
        self._update_status({
            "current_device": name,
            "current_cmd_total": total_cmds,
            "current_cmd_index": 0,
        })

        # fn1: 开关 (0,1)
        for fv in (0, 1):
            if not self._running:
                return
            current_index += 1
            self._update_status({
                "current_fn": 1,
                "current_fv": fv,
                "current_cmd_index": current_index,
                "current_state": "发送控制",
            })
            await self._send_and_record(device, st, si, type_id, name, fn=1, fv=fv)


# 为 ReplayRecorder 增加启动接口
async def _start_runner(recorder: "ReplayRecorder", runner) -> None:
    if recorder.is_running():
        _LOGGER.warning("录制器已在运行，忽略启动请求")
        return
    recorder._current_runner = runner
    recorder._task = asyncio.create_task(runner.run())


async def start_ac_full(recorder: "ReplayRecorder") -> None:
    await _start_runner(recorder, ACEnumerator(recorder.coordinator, recorder.replay, recorder._update_status))


async def start_floor_full(recorder: "ReplayRecorder") -> None:
    await _start_runner(recorder, FloorHeatingEnumerator(recorder.coordinator, recorder.replay, recorder._update_status))


async def start_freshair_full(recorder: "ReplayRecorder") -> None:
    await _start_runner(recorder, FreshAirEnumerator(recorder.coordinator, recorder.replay, recorder._update_status))


async def start_light_full(recorder: "ReplayRecorder") -> None:
    await _start_runner(recorder, LightEnumerator(recorder.coordinator, recorder.replay, recorder._update_status))



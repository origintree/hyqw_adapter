"""报文回放与录制管理器"""
import asyncio
import logging
import os
from typing import Dict, Any, Optional, List

import yaml

from .const import REPLAY_STORAGE_FILENAME

_LOGGER = logging.getLogger(__name__)


class ReplayStorage:
    """YAML 存储封装"""
    def __init__(self, hass, hass_config_path: str) -> None:
        self.hass = hass
        self._path = os.path.join(hass_config_path, REPLAY_STORAGE_FILENAME)
        self._data: Dict[str, Any] = {}

    def _sync_load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}

    async def async_load(self) -> None:
        try:
            await self.hass.async_add_executor_job(self._sync_load)
        except Exception as err:
            _LOGGER.error(f"加载回放YAML失败: {err}")
            self._data = {}

    def _sync_save(self) -> None:
        base_dir = os.path.dirname(self._path)
        if base_dir and not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    async def async_save(self) -> None:
        try:
            await self.hass.async_add_executor_job(self._sync_save)
        except Exception as err:
            _LOGGER.error(f"保存回放YAML失败: {err}")

    def get(self) -> Dict[str, Any]:
        return self._data

    def set(self, data: Dict[str, Any]) -> None:
        self._data = data


class ReplayManager:
    """报文回放与录制管理器
    - 负责基于 (project_code, device_sn, si, fn, fv) 的报文查询与回放
    - 负责录制期间的状态与下行报文接收
    """

    def __init__(self, hass, project_code: str, device_sn: str, mqtt_gateway) -> None:
        self.hass = hass
        self.project_code = project_code
        self.device_sn = device_sn
        self.mqtt_gateway = mqtt_gateway
        self.storage = ReplayStorage(hass, hass.config.path("."))
        self._loaded = False
        self._recording = False
        self._record_event: Optional[asyncio.Event] = None
        self._last_down_payload: Optional[bytes] = None
        self._last_down_topic: Optional[str] = None

        # 延迟加载，等待 async_setup_entry 调用 async_load()
    async def async_load(self) -> None:
        await self.storage.async_load()
        data = self.storage.get() or {}
        if "replay" not in data:
            data["replay"] = {
                "project_code": self.project_code,
                "device_sn": self.device_sn,
                "version": 1,
                "devices": [],
                "failed_commands": [],  # 未成功录制的指令列表
            }
            self.storage.set(data)
            await self.storage.async_save()
        self._loaded = True

    # ====== 回放查询与执行 ======
    def _ensure_device_entry(self, si: int, st: int, type_id: int, name: str) -> Dict[str, Any]:
        data = self.storage.get()
        replay = data.setdefault("replay", {})
        devices = replay.setdefault("devices", [])
        for dev in devices:
            if dev.get("si") == si:
                return dev
        new_dev = {
            "si": si,
            "type_id": type_id,
            "st": st,
            "name": name,
            "topic": f"FMQ/{self.project_code}/{self.device_sn}/DOWN/2001",
            "commands": {},
        }
        devices.append(new_dev)
        return new_dev

    def record_command(self, si: int, st: int, type_id: int, name: str, fn: int, fv: int, payload_hex: str, qos: int = 0) -> None:
        dev = self._ensure_device_entry(si, st, type_id, name)
        key = f"fn={fn};fv={fv}"
        dev.setdefault("commands", {})[key] = {
            "payload_hex": payload_hex,
            "qos": qos,
        }
        # 成功录制时，从失败列表中删除对应条目
        self._remove_failed_command(si, fn, fv)
        # 异步持久化
        try:
            self.hass.async_create_task(self.storage.async_save())
        except Exception:
            pass

    def find_command(self, si: int, fn: int, fv: int) -> Optional[Dict[str, Any]]:
        data = self.storage.get()
        replay = data.get("replay", {})
        devices = replay.get("devices", [])
        for dev in devices:
            if dev.get("si") == si:
                key = f"fn={fn};fv={fv}"
                cmd = dev.get("commands", {}).get(key)
                if cmd:
                    return {"topic": dev.get("topic"), **cmd}
        return None

    def replay(self, topic: str, payload_hex: str, qos: int = 0) -> bool:
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError:
            _LOGGER.error("回放payload_hex格式错误")
            return False
        return self.mqtt_gateway.publish_raw(topic, payload, qos=qos)

    # ====== 失败指令管理 ======
    def add_failed_command(self, si: int, st: int, type_id: int, name: str, fn: int, fv: int, reason: str = "timeout") -> None:
        """添加失败录制的指令到失败列表"""
        data = self.storage.get()
        replay = data.setdefault("replay", {})
        failed_commands = replay.setdefault("failed_commands", [])
        
        failed_cmd = {
            "si": si,
            "st": st,
            "type_id": type_id,
            "name": name,
            "fn": fn,
            "fv": fv,
            "reason": reason,
            "timestamp": self.hass.config.time_zone.now().isoformat(),
        }
        
        # 检查是否已存在，避免重复添加
        for existing in failed_commands:
            if (existing.get("si") == si and 
                existing.get("fn") == fn and 
                existing.get("fv") == fv):
                # 更新现有条目
                existing.update(failed_cmd)
                break
        else:
            # 添加新条目
            failed_commands.append(failed_cmd)
        
        # 异步持久化
        try:
            self.hass.async_create_task(self.storage.async_save())
        except Exception:
            pass

    def _remove_failed_command(self, si: int, fn: int, fv: int) -> None:
        """从失败列表中删除指定指令"""
        data = self.storage.get()
        replay = data.get("replay", {})
        failed_commands = replay.get("failed_commands", [])
        
        # 删除匹配的条目
        failed_commands[:] = [
            cmd for cmd in failed_commands
            if not (cmd.get("si") == si and cmd.get("fn") == fn and cmd.get("fv") == fv)
        ]
        
        # 异步持久化
        try:
            self.hass.async_create_task(self.storage.async_save())
        except Exception:
            pass

    def get_failed_commands(self) -> List[Dict[str, Any]]:
        """获取失败指令列表"""
        data = self.storage.get()
        replay = data.get("replay", {})
        return replay.get("failed_commands", [])

    # ====== 录制期下行捕获 ======
    def _on_down_message(self, topic: str, payload: bytes) -> None:
        # 保存最近一次下行，用于和发出的控制配对
        self._last_down_topic = topic
        self._last_down_payload = payload
        if self._record_event:
            self._record_event.set()

    def start_recording(self) -> None:
        self._recording = True
        self._last_down_payload = None
        self._last_down_topic = None
        self.mqtt_gateway.enable_downstream_recording(True, self._on_down_message)
        _LOGGER.info("已开启下行报文录制模式")

    def stop_recording(self) -> None:
        self._recording = False
        self.mqtt_gateway.enable_downstream_recording(False, None)
        _LOGGER.info("已关闭下行报文录制模式")

    async def capture_next_down(self, timeout: float = 2.0) -> Optional[str]:
        self._record_event = asyncio.Event()
        try:
            try:
                await asyncio.wait_for(self._record_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
            if self._last_down_payload is None:
                return None
            return self._last_down_payload.hex().upper()
        finally:
            self._record_event = None



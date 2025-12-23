"""温度传感器绑定管理器 - 为地暖设备自动绑定同区域空调的温度传感器."""
import logging
from typing import Dict, List, Optional, Set

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TemperatureSensorBinder:
    """温度传感器绑定管理器
    
    负责：
    1. 识别同区域内的空调和地暖设备
    2. 为地暖设备绑定空调的温度传感器
    3. 管理温度传感器的共享使用
    """
    
    def __init__(self, hass: HomeAssistant) -> None:
        """初始化温度传感器绑定管理器."""
        self.hass = hass
        self.device_reg = device_registry.async_get(hass)
        self.entity_reg = entity_registry.async_get(hass)
        self._binding_cache: Dict[str, str] = {}  # 地暖设备ID -> 空调温度传感器实体ID
        self._processed_devices: Set[str] = set()  # 已处理的设备ID集合
    
    def get_devices_by_room(self, devices: List[Dict]) -> Dict[str, List[Dict]]:
        """按房间分组设备."""
        room_devices = {}
        
        for device in devices:
            room_name = device.get("roomName", "未知房间")
            if room_name not in room_devices:
                room_devices[room_name] = []
            room_devices[room_name].append(device)
        
        return room_devices
    
    def find_room_temperature_sensor(self, room_name: str, devices: List[Dict]) -> Optional[Dict]:
        """在指定房间内查找空调温度传感器 (通过 unique_id 精确匹配)."""
        for device in devices:
            if device.get("typeId") == 12:
                device_id = str(device.get("deviceId"))
                unique_id = f"{DOMAIN}_{device_id}_temperature"
                # 通过 unique_id 查找真实的 entity_id，避免硬编码
                entity_id = self.entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
                if entity_id:
                    _LOGGER.debug(f"在房间 {room_name} 找到空调温度传感器: {device.get('deviceName')} -> {entity_id}")
                    return device
                else:
                    _LOGGER.debug(f"空调 {device.get('deviceName')} 的温度传感器尚未注册 (unique_id={unique_id})")
        return None
    
    def bind_floor_heating_to_ac_sensor(self, floor_heating_device: Dict, ac_device: Dict) -> bool:
        """将地暖设备绑定到空调温度传感器 (通过 unique_id 查找实体)."""
        try:
            floor_heating_id = str(floor_heating_device.get("deviceId"))
            ac_id = str(ac_device.get("deviceId"))

            # 通过 unique_id 获取空调温度传感器 entity_id
            temp_unique_id = f"{DOMAIN}_{ac_id}_temperature"
            temp_sensor_entity_id = self.entity_reg.async_get_entity_id("sensor", DOMAIN, temp_unique_id)
            if not temp_sensor_entity_id:
                _LOGGER.debug(
                    f"空调 {ac_device.get('deviceName')} 的温度传感器尚未注册, unique_id={temp_unique_id}"
                )
                return False

            # 通过 unique_id 获取地暖 climate entity_id
            floor_unique_id = f"{DOMAIN}_{floor_heating_id}"
            floor_heating_entity_id = self.entity_reg.async_get_entity_id("climate", DOMAIN, floor_unique_id)
            if not floor_heating_entity_id:
                _LOGGER.debug(
                    f"地暖 {floor_heating_device.get('deviceName')} 的climate实体尚未注册, unique_id={floor_unique_id}"
                )
                return False

            # 缓存绑定关系
            self._binding_cache[floor_heating_id] = temp_sensor_entity_id
            _LOGGER.debug(
                f"地暖 '{floor_heating_device.get('deviceName')}' 绑定到温度传感器 {temp_sensor_entity_id}"
            )
            return True

        except Exception as err:
            _LOGGER.error(f"绑定地暖设备到空调温度传感器失败: {err}")
            return False
    
    def get_bound_temperature_sensor(self, floor_heating_device_id: str) -> Optional[str]:
        """获取地暖设备绑定的温度传感器实体ID."""
        return self._binding_cache.get(str(floor_heating_device_id))
    
    def process_room_devices(self, room_name: str, devices: List[Dict]) -> None:
        """处理房间内的设备，建立温度传感器绑定关系."""
        # 查找空调设备
        ac_devices = [d for d in devices if d.get("typeId") == 12]
        # 查找地暖设备
        floor_heating_devices = [d for d in devices if d.get("typeId") == 16]
        
        if not ac_devices or not floor_heating_devices:
            _LOGGER.debug(f"房间 {room_name} 没有空调或地暖设备，跳过温度传感器绑定")
            return
        
        # 为每个地暖设备绑定第一个空调的温度传感器
        for floor_heating in floor_heating_devices:
            floor_heating_id = str(floor_heating.get("deviceId"))
            
            # 避免重复处理
            if floor_heating_id in self._processed_devices:
                continue
            
            # 查找可用的空调温度传感器
            for ac_device in ac_devices:
                if self.bind_floor_heating_to_ac_sensor(floor_heating, ac_device):
                    self._processed_devices.add(floor_heating_id)
                    break
    
    def process_all_devices(self, devices: List[Dict]) -> None:
        """处理所有设备，建立温度传感器绑定关系."""
        _LOGGER.debug("开始处理温度传感器绑定...")
        
        # 统计设备类型
        ac_count = len([d for d in devices if d.get("typeId") == 12])
        floor_heating_count = len([d for d in devices if d.get("typeId") == 16])
        _LOGGER.debug(f"发现 {ac_count} 个空调设备，{floor_heating_count} 个地暖设备")
        
        # 按房间分组设备
        room_devices = self.get_devices_by_room(devices)
        _LOGGER.debug(f"设备分布在 {len(room_devices)} 个房间中")
        
        # 处理每个房间的设备
        for room_name, room_device_list in room_devices.items():
            self.process_room_devices(room_name, room_device_list)
        
        _LOGGER.info(f"温度传感器绑定完成，共处理 {len(self._processed_devices)} 个地暖设备")
    
    def get_binding_summary(self) -> Dict[str, str]:
        """获取绑定关系摘要."""
        summary = {}
        for floor_heating_id, temp_sensor_id in self._binding_cache.items():
            # 获取设备名称
            floor_heating_entity = self.entity_reg.async_get(f"climate.hyqw_adapter_{floor_heating_id}")
            temp_sensor_entity = self.entity_reg.async_get(temp_sensor_id)
            
            floor_heating_name = floor_heating_entity.name if floor_heating_entity else f"地暖_{floor_heating_id}"
            temp_sensor_name = temp_sensor_entity.name if temp_sensor_entity else f"温度传感器_{temp_sensor_id}"
            
            summary[floor_heating_name] = temp_sensor_name
        
        return summary
    
    def clear_bindings(self) -> None:
        """清除所有绑定关系."""
        self._binding_cache.clear()
        self._processed_devices.clear()
        _LOGGER.info("温度传感器绑定关系已清除")

"""Area management for HYQW Adapter integration."""
import logging
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry, device_registry

_LOGGER = logging.getLogger(__name__)


class AreaManager:
    """Manages areas and device area assignments for HYQW Adapter."""
    
    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the area manager."""
        self.hass = hass
        self.area_reg = area_registry.async_get(hass)
        self.device_reg = device_registry.async_get(hass)
        self._room_area_mapping = {}
    
    def set_room_mapping(self, room_mapping: Dict[str, str]) -> None:
        """Set the room to area mapping."""
        self._room_area_mapping = room_mapping
        _LOGGER.info(f"房间映射已设置: {room_mapping}")
    
    async def ensure_areas_exist(self, room_mapping: Dict[str, str]) -> Dict[str, str]:
        """Ensure all mapped areas exist, create if necessary."""
        area_id_mapping = {}
        
        for room_name, area_name in room_mapping.items():
            if not area_name or area_name.strip() == "":
                # 跳过空的映射
                continue
                
            # 查找现有区域
            existing_area = None
            for area in self.area_reg.areas.values():
                if area.name == area_name:
                    existing_area = area
                    break
            
            if existing_area:
                area_id_mapping[room_name] = existing_area.id
                _LOGGER.debug(f"找到现有区域: {room_name} -> {area_name} (ID: {existing_area.id})")
            else:
                # 创建新区域
                new_area = self.area_reg.async_create(area_name)
                area_id_mapping[room_name] = new_area.id
                _LOGGER.info(f"创建新区域: {room_name} -> {area_name} (ID: {new_area.id})")
        
        return area_id_mapping
    
    async def assign_device_to_area(self, device_id: str, room_name: str) -> bool:
        """Assign a device to its corresponding area based on room mapping."""
        if room_name not in self._room_area_mapping:
            _LOGGER.warning(f"房间 '{room_name}' 没有映射到任何区域")
            return False
        
        area_name = self._room_area_mapping[room_name]
        if not area_name or area_name.strip() == "":
            _LOGGER.debug(f"房间 '{room_name}' 映射为空，跳过区域分配")
            return False
        
        # 查找区域ID
        area_id = None
        for area in self.area_reg.areas.values():
            if area.name == area_name:
                area_id = area.id
                break
        
        if not area_id:
            _LOGGER.error(f"找不到区域 '{area_name}'")
            return False
        
        # 查找设备
        device = None
        for dev in self.device_reg.devices.values():
            # 检查设备标识符是否匹配
            try:
                for identifier_tuple in dev.identifiers:
                    if len(identifier_tuple) >= 2:
                        domain, identifier = identifier_tuple[0], identifier_tuple[1]
                        if domain == "hyqw_adapter" and str(identifier) == str(device_id):
                            device = dev
                            break
                if device:
                    break
            except (ValueError, TypeError) as err:
                _LOGGER.debug(f"跳过设备 {dev.name}，标识符解析错误: {err}")
                continue
        
        if not device:
            _LOGGER.warning(f"找不到设备 ID: {device_id}")
            return False
        
        # 更新设备的区域
        try:
            self.device_reg.async_update_device(device.id, area_id=area_id)
            _LOGGER.info(f"设备已分配到区域: {device.name} -> {area_name}")
            return True
        except Exception as err:
            _LOGGER.error(f"分配设备到区域失败: {err}")
            return False
    
    def get_suggested_areas(self, rooms: List[Dict]) -> Dict[str, str]:
        """Get suggested area mappings based on room names."""
        suggestions = {}
        
        # 房间名称到推荐区域的映射
        room_suggestions = {
            "玄关": "玄关",
            "客厅": "客厅", 
            "餐厅": "餐厅",
            "主卧": "主卧室",
            "次卧": "次卧室",
            "次卧1": "次卧1",
            "次卧2": "次卧2", 
            "厨房": "厨房",
            "卫生间": "卫生间",
            "阳台": "阳台",
            "书房": "书房",
            "儿童房": "儿童房",
            "一楼": "",  # 结构性房间，通常不映射
            "二楼": "",
            "地下室": "地下室",
        }
        
        for room in rooms:
            room_name = room.get("name", "")
            if room_name:
                # 查找精确匹配
                suggested_area = room_suggestions.get(room_name)
                
                if suggested_area is None:
                    # 尝试模糊匹配
                    if "卧" in room_name:
                        suggested_area = f"{room_name}"
                    elif "厅" in room_name:
                        suggested_area = f"{room_name}"
                    elif "房" in room_name:
                        suggested_area = f"{room_name}"
                    elif "室" in room_name:
                        suggested_area = f"{room_name}"
                    else:
                        suggested_area = room_name
                
                suggestions[room_name] = suggested_area if suggested_area else ""
        
        return suggestions
    
    def get_room_device_summary(self, devices: List[Dict]) -> Dict[str, List[str]]:
        """Get a summary of devices by room."""
        room_devices = {}
        
        for device in devices:
            room_name = device.get("roomName", "未知房间")
            device_name = device.get("deviceName", "未知设备")
            device_type = device.get("device_type_name", "未知类型")
            
            if room_name not in room_devices:
                room_devices[room_name] = []
            
            room_devices[room_name].append(f"{device_name} ({device_type})")
        
        return room_devices

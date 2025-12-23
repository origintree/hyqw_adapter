"""HAR file parser for HYQW Adapter integration."""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)


class HARParser:
    """Parser for HAR (HTTP Archive) files from HYQW Adapter app."""
    
    def __init__(self, har_content: str) -> None:
        """Initialize the parser with HAR content."""
        self.har_data = json.loads(har_content)
        self.parsed_data = {
            "base_url": None,
            "token": None,
            "device_sn": None,
            "project_code": None,
            "home_info": None,
            "rooms": [],
            "devices": [],
        }
    
    def parse(self) -> Dict[str, Any]:
        """Parse the HAR file and extract HYQW Adapter data."""
        try:
            entries = self.har_data.get("log", {}).get("entries", [])
            
            for entry in entries:
                self._parse_entry(entry)
            
            # 验证必要的信息是否已解析
            if not all([
                self.parsed_data["base_url"],
                self.parsed_data["token"],
                self.parsed_data["device_sn"]
            ]):
                raise ValueError("HAR文件中缺少必要的认证信息")
            
            _LOGGER.info(f"成功解析HAR文件，发现 {len(self.parsed_data['devices'])} 个设备")
            return self.parsed_data
            
        except Exception as err:
            _LOGGER.error(f"解析HAR文件失败: {err}")
            raise ValueError(f"解析HAR文件失败: {err}")
    
    def _parse_entry(self, entry: Dict[str, Any]) -> None:
        """Parse a single HAR entry."""
        request = entry.get("request", {})
        response = entry.get("response", {})
        
        # 提取基础URL
        url = request.get("url", "")
        parsed_url = urlparse(url)
        if parsed_url.netloc and "jianweisoftware.com" in parsed_url.netloc:
            self.parsed_data["base_url"] = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        # 提取认证Token
        headers = request.get("headers", [])
        for header in headers:
            if header.get("name") == "Authorization":
                auth_value = header.get("value", "")
                if auth_value.startswith("mob;"):
                    self.parsed_data["token"] = auth_value.replace("mob;", "")
        
        # 解析设备数据（来自profile接口）
        if "/api/home/profile" in url and response.get("status") == 200:
            self._parse_profile_response(response)
        
        # 解析控制请求和状态请求（提取项目代码）
        if ("/api/device/control" in url or "/api/device/states" in url) and request.get("method") == "POST":
            self._parse_control_request(request)
    
    def _parse_profile_response(self, response: Dict[str, Any]) -> None:
        """Parse the home profile response."""
        try:
            content = response.get("content", {})
            response_text = content.get("text", "")
            
            if response_text:
                data = json.loads(response_text)
                result = data.get("result", {})
                
                # 提取家庭信息
                home_info = result.get("home", {})
                if home_info:
                    self.parsed_data["home_info"] = home_info
                    self.parsed_data["device_sn"] = home_info.get("deviceSn")
                
                # 提取房间信息
                rooms = result.get("rooms", [])
                self.parsed_data["rooms"] = rooms
                
                # 提取设备信息
                devices = result.get("devices", [])
                self.parsed_data["devices"] = self._enrich_devices_with_rooms(devices, rooms)
                
        except Exception as err:
            _LOGGER.error(f"解析设备信息失败: {err}")
    
    def _parse_control_request(self, request: Dict[str, Any]) -> None:
        """Parse device control request to extract project code."""
        try:
            post_data = request.get("postData", {})
            if post_data:
                data = json.loads(post_data.get("text", "{}"))
                project_code = data.get("projectCode")
                if project_code and not self.parsed_data["project_code"]:
                    self.parsed_data["project_code"] = project_code
        except Exception as err:
            _LOGGER.debug(f"解析控制请求失败: {err}")
    
    def _enrich_devices_with_rooms(self, devices: List[Dict], rooms: List[Dict]) -> List[Dict]:
        """Enrich device data with room information."""
        # 创建房间ID到房间信息的映射
        room_map = {room["roomId"]: room for room in rooms}
        
        enriched_devices = []
        for device in devices:
            # 复制设备信息
            enriched_device = device.copy()
            
            # 添加房间详细信息
            room_id = device.get("roomId")
            if room_id and room_id in room_map:
                room = room_map[room_id]
                enriched_device["room_info"] = {
                    "roomId": room["roomId"],
                    "name": room["name"],
                    "parentName": room.get("parentName"),
                    "imageIcon": room.get("imageIcon"),
                    "sortIndex": room.get("sortIndex", 0),
                }
            
            # 生成友好的设备名称（包含房间信息）
            device_name = device.get("deviceName", "未知设备")
            room_name = device.get("roomName", "")
            if room_name and room_name not in device_name:
                enriched_device["friendly_name"] = f"{room_name}{device_name}"
            else:
                enriched_device["friendly_name"] = device_name
            
            # 添加设备类型描述
            type_map = {
                8: "灯具",
                12: "空调",
                14: "窗帘",
                16: "地暖",
                36: "新风设备",
            }
            device_type_id = device.get("typeId")
            enriched_device["device_type_name"] = type_map.get(device_type_id, f"类型{device_type_id}")
            
            enriched_devices.append(enriched_device)
        
        return enriched_devices
    
    def get_device_summary(self) -> str:
        """Get a summary of parsed devices."""
        if not self.parsed_data["devices"]:
            return "未发现设备"
        
        type_counts = {}
        room_counts = {}
        
        for device in self.parsed_data["devices"]:
            device_type = device.get("device_type_name", "未知")
            room_name = device.get("roomName", "未知房间")
            
            type_counts[device_type] = type_counts.get(device_type, 0) + 1
            room_counts[room_name] = room_counts.get(room_name, 0) + 1
        
        summary_parts = []
        
        # 设备类型统计
        type_summary = ", ".join([f"{type_name}: {count}个" for type_name, count in type_counts.items()])
        summary_parts.append(f"设备类型: {type_summary}")
        
        # 房间统计
        room_summary = ", ".join([f"{room_name}: {count}个" for room_name, count in room_counts.items()])
        summary_parts.append(f"房间分布: {room_summary}")
        
        return "; ".join(summary_parts)
    
    def validate_data(self) -> Tuple[bool, str]:
        """Validate the parsed data."""
        if not self.parsed_data["base_url"]:
            return False, "未找到服务器地址"
        
        if not self.parsed_data["token"]:
            return False, "未找到认证Token"
        
        if not self.parsed_data["device_sn"]:
            return False, "未找到设备序列号"
        
        if not self.parsed_data["devices"]:
            return False, "未找到任何设备"
        
        return True, "数据验证成功"


def parse_har_file(har_content: str) -> Dict[str, Any]:
    """Parse HAR file and return device configuration."""
    parser = HARParser(har_content)
    return parser.parse()


def validate_har_content(har_content: str) -> Tuple[bool, str]:
    """Validate HAR file content."""
    try:
        parser = HARParser(har_content)
        parser.parse()
        return parser.validate_data()
    except Exception as err:
        return False, str(err)

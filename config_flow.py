"""Config flow for HYQW Adapter integration."""
import json
import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BASE_URL,
    CONF_DEVICE_SN,
    CONF_PROJECT_CODE,
    DEFAULT_BASE_URL,
    DEFAULT_PROJECT_CODE,
    DOMAIN,
)
from .har_parser import parse_har_file, validate_har_content
from .area_manager import AreaManager

_LOGGER = logging.getLogger(__name__)


class HYQWAdapterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HYQW Adapter."""
    
    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle the initial step - directly go to HAR upload."""
        return await self.async_step_har_upload()

    async def async_step_har_upload(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle HAR file upload step."""
        errors: Dict[str, str] = {}
        
        if user_input is not None:
            try:
                har_content = user_input.get("抓包HAR文件内容", "").strip()
                if not har_content:
                    errors["抓包HAR文件内容"] = "请粘贴HAR文件内容"
                else:
                    # 解析HAR文件
                    parsed_data = parse_har_file(har_content)
                    
                    # 检查是否已经配置过同样的设备
                    device_sn = parsed_data["device_sn"]
                    await self.async_set_unique_id(device_sn)
                    self._abort_if_unique_id_configured()
                    
                    # 存储解析的数据以供后续步骤使用
                    self.parsed_har_data = parsed_data
                    
                    return await self.async_step_room_mapping()
                    
            except Exception as err:
                _LOGGER.error(f"解析HAR文件失败: {err}")
                errors["抓包HAR文件内容"] = f"HAR文件解析失败: {str(err)}"
        
        # 显示HAR文件上传表单
        data_schema = vol.Schema({
            vol.Required("抓包HAR文件内容"): str,
        })
        
        return self.async_show_form(
            step_id="har_upload",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "instructions": (
                    "请按以下步骤获取HAR文件：\n"
                    "1. 在手机上打开花语前湾APP\n"
                    "2. 开启抓包工具（如Surge）\n"
                    "3. 刷新设备列表\n"
                    "4. 导出HAR文件\n"
                    "5. 将HAR文件内容粘贴到下方文本框"
                )
            },
        )

    async def async_step_room_mapping(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Configure room to area mapping."""
        parsed_data = self.parsed_har_data
        rooms = parsed_data.get("rooms", [])
        
        if user_input is not None:
            # 保存房间映射
            room_mapping = {}
            for room in rooms:
                room_name = room["name"]
                if room.get("type") == 1:  # 只处理实际房间（非结构性房间）
                    field_key = f"{room_name}_区域映射"
                    mapped_area = user_input.get(field_key, "").strip()
                    # 只有选择了具体区域（非"不分配到任何区域"）才添加到映射中
                    if mapped_area and mapped_area != "不分配到任何区域":
                        room_mapping[room_name] = mapped_area
            
            self.room_area_mapping = room_mapping
            return await self.async_step_confirm_har()
        
        # 创建区域管理器
        area_manager = AreaManager(self.hass)
        
        # 获取现有区域列表，构建选择选项
        existing_areas = []
        for area in area_manager.area_reg.areas.values():
            existing_areas.append(area.name)
        
        # 构建区域选项列表（只包含已有区域）
        all_area_options = ["不分配到任何区域"]  # 空选项表示不分配
        if existing_areas:
            all_area_options.extend(sorted(existing_areas))
        
        # 构建表单数据结构
        data_schema_dict = {}
        room_list = []
        
        for room in rooms:
            if room.get("type") == 1:  # 只显示实际房间
                room_name = room["name"]
                room_id = room["roomId"]
                
                # 获取该房间的设备数量
                device_count = len([d for d in parsed_data.get("devices", []) 
                                  if d.get("roomId") == room_id])
                
                room_list.append(f"{room_name} ({device_count}个设备)")
                
                # 使用下拉选择框，默认选择"不分配到任何区域"
                field_key = f"{room_name}_区域映射"
                data_schema_dict[vol.Optional(field_key, default="不分配到任何区域")] = vol.In(all_area_options)
        
        data_schema = vol.Schema(data_schema_dict)
        
        return self.async_show_form(
            step_id="room_mapping",
            data_schema=data_schema,
            description_placeholders={
                "room_list": "\n".join(room_list),
                "instructions": (
                    "为每个房间选择对应的Home Assistant区域。\n"
                    "• 可以选择现有区域进行映射\n"
                    "• 选择\"不分配到任何区域\"跳过该房间\n"
                    "• 如需要新区域，请先在Home Assistant中创建\n"
                    "• 建议为每个房间分配合适的区域以便管理"
                )
            },
        )

    async def async_step_confirm_har(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Confirm the parsed HAR data and room mapping."""
        if user_input is not None:
            # 创建配置条目
            parsed_data = self.parsed_har_data
            home_info = parsed_data.get("home_info", {})
            
            return self.async_create_entry(
                title=home_info.get("name", "花语前湾转接器"),
                data={
                    CONF_BASE_URL: parsed_data["base_url"],
                    CONF_TOKEN: parsed_data["token"],
                    CONF_DEVICE_SN: parsed_data["device_sn"],
                    CONF_PROJECT_CODE: parsed_data.get("project_code", DEFAULT_PROJECT_CODE),
                    "har_devices": parsed_data["devices"],
                    "har_rooms": parsed_data["rooms"],
                    "har_home_info": parsed_data["home_info"],
                    "room_area_mapping": getattr(self, "room_area_mapping", {}),
                },
            )
        
        # 显示最终确认页面
        parsed_data = self.parsed_har_data
        home_info = parsed_data.get("home_info", {})
        device_count = len(parsed_data.get("devices", []))
        room_mapping = getattr(self, "room_area_mapping", {})
        
        # 生成设备和房间映射摘要
        mapping_summary = []
        for room_name, area_name in room_mapping.items():
            device_count_in_room = len([d for d in parsed_data.get("devices", []) 
                                      if d.get("roomName") == room_name])
            mapping_summary.append(f"📍 {room_name} → {area_name} ({device_count_in_room}个设备)")
        
        if not mapping_summary:
            mapping_summary.append("未配置房间映射")
        
        return self.async_show_form(
            step_id="confirm_har",
            data_schema=vol.Schema({}),
            description_placeholders={
                "home_name": home_info.get("name", "未知"),
                "home_address": home_info.get("address", "未知"),
                "device_sn": parsed_data.get("device_sn", "未知"),
                "device_count": str(device_count),
                "room_mapping_count": str(len(room_mapping)),
                "room_mapping": "\n".join(mapping_summary),
            },
        )


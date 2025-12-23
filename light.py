"""Support for HYQW Adapter lights."""
import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HYQWAdapterCoordinator
from .const import DEVICE_FUNCTIONS, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter lights."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 检查是否为灯具设备 (typeId == 8)
            if device.get("typeId") == 8:
                entities.append(HYQWAdapterLight(coordinator, device))
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterLight(CoordinatorEntity, LightEntity):
    """Representation of a HYQW Adapter light."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        self._attr_name = device["deviceName"]
        
        # 设置支持的功能 - 仅支持开关，不支持亮度调节
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._attr_name,
            "manufacturer": "花语前湾",
            "model": f"Type {self._device.get('typeId')}",
            "sw_version": self._device.get("projectCode", "Unknown"),
            "suggested_area": self._device.get("roomName"),
        }
    
    @property
    def is_on(self) -> bool:
        """Return if the light is on."""
        device_si = self._device.get("si")
        device_name = self._attr_name
        
        # 优先使用实时状态数据
        current_states = self._device.get("current_states", {})
        if 1 in current_states:  # fn=1 是开关状态
            is_on = current_states[1]["fv"] == 1
            _LOGGER.debug(f"Light {device_name} (si={device_si}) using current_states: {'ON' if is_on else 'OFF'} (fv={current_states[1]['fv']})")
            return is_on
        
        # 使用设备缓存状态
        fallback_state = self._device.get("state", 0) == 1 or self._device.get("is_on", False)
        _LOGGER.debug(f"Light {device_name} (si={device_si}) using fallback state: {'ON' if fallback_state else 'OFF'} (device.state={self._device.get('state')}, device.is_on={self._device.get('is_on')})")
        return fallback_state
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success
    
    @property
    def extra_state_attributes(self) -> dict:
        """Return entity specific state attributes."""
        attrs = {
            "device_id": self._device.get("deviceId"),
            "device_name": self._device.get("deviceName"),
            "si": self._device.get("si"),
            "room": self._device.get("roomName"),
        }
        
        # 添加processing状态
        if self.coordinator.is_entity_occupied(self.entity_id):
            attrs["processing"] = True
            attrs["status"] = "processing"
            # 可以在前端通过card-mod等插件使用这个状态添加动画效果
        
        return attrs
    
    def _update_device_state_immediately(self, target_state: int) -> None:
        """立即更新设备状态到目标值"""
        try:
            # 更新current_states中的开关状态
            if "current_states" not in self._device:
                self._device["current_states"] = {}
            
            self._device["current_states"][1] = {
                "fv": target_state,
                "st": self._device.get("st", 10101),
            }
            
            # 更新设备属性
            self._device["is_on"] = target_state == 1
            self._device["state"] = target_state
            
            # 使用调度器避免并发更新
            self.async_schedule_update_ha_state()
            
            device_name = self._attr_name
            device_si = self._device.get("si")
            state_text = "ON" if target_state == 1 else "OFF"
            _LOGGER.info(f"Light {device_name} (si={device_si}) state immediately updated to {state_text}")
            
        except Exception as err:
            _LOGGER.error(f"Failed to update light state immediately: {err}")
    
    def _schedule_delayed_sync(self) -> None:
        """安排2秒后的状态同步"""
        async def delayed_sync():
            try:
                await asyncio.sleep(2)
                _LOGGER.debug(f"Light {self._attr_name} starting delayed sync after 2 seconds")
                await self.coordinator.async_request_state_update()
                _LOGGER.debug(f"Light {self._attr_name} delayed sync completed")
            except Exception as err:
                _LOGGER.error(f"Light {self._attr_name} delayed sync failed: {err}")
        
        # 创建后台任务
        asyncio.create_task(delayed_sync())
    
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        # 灯具只支持开/关，忽略亮度参数
        success = await self.coordinator.async_control_device(
            device_id=self._device_id,
            st=self._device["st"],
            si=self._device["si"],
            fn=DEVICE_FUNCTIONS["light"]["turn_on"]["fn"],
            fv=DEVICE_FUNCTIONS["light"]["turn_on"]["fv"],
            entity_id=self.entity_id,  # 传递实体ID用于节流控制
        )
        
        if success:
            _LOGGER.debug(f"Light {self._attr_name} turn on command sent")
            # 1. 立即更新设备状态为目标值
            self._update_device_state_immediately(1)
            # 2. 安排2秒后的状态同步
            self._schedule_delayed_sync()
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        success = await self.coordinator.async_control_device(
            device_id=self._device_id,
            st=self._device["st"],
            si=self._device["si"],
            fn=DEVICE_FUNCTIONS["light"]["turn_off"]["fn"],
            fv=DEVICE_FUNCTIONS["light"]["turn_off"]["fv"],
            entity_id=self.entity_id,  # 传递实体ID用于节流控制
        )
        
        if success:
            _LOGGER.debug(f"Light {self._attr_name} turn off command sent")
            # 1. 立即更新设备状态为目标值
            self._update_device_state_immediately(0)
            # 2. 安排2秒后的状态同步
            self._schedule_delayed_sync()
    
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        old_device = self._device.copy()
        
        # 更新设备信息
        if self.coordinator.data and "devices" in self.coordinator.data:
            for device in self.coordinator.data["devices"]:
                if device["deviceId"] == self._device_id:
                    self._device = device
                    
                    # 记录设备数据更新
                    old_states = old_device.get("current_states", {})
                    new_states = device.get("current_states", {})
                    
                    if old_states != new_states:
                        _LOGGER.info(f"Light {self._attr_name} (si={device.get('si')}) device data updated")
                        _LOGGER.debug(f"  Old states: {old_states}")
                        _LOGGER.debug(f"  New states: {new_states}")
                    
                    break
        
        super()._handle_coordinator_update()
    
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        # 当实体添加到HA时，主动请求一次状态更新
        await self.coordinator.async_request_state_update()

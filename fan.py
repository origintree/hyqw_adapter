"""Support for HYQW Adapter fan devices."""
import logging
from typing import Any, Optional

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HYQWAdapterCoordinator
from .const import (
    DEVICE_FUNCTIONS,
    DEVICE_TYPES,
    DOMAIN,
    FAN_SPEEDS,
    FAN_SPEEDS_REVERSE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter fan devices."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 检查是否为风扇设备 (新风36)
            if device.get("typeId") == 36:
                entities.append(HYQWAdapterFan(coordinator, device))
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterFan(CoordinatorEntity, FanEntity):
    """Representation of a HYQW Adapter fan device."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the fan device."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._device_type = device.get("typeId")
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        # 只在初始化时设置一次名称，后续不再修改
        self._initial_name = device["deviceName"]
        self._attr_name = self._initial_name
        
        # 新风设备支持开关和风速调节
        self._attr_supported_features = (
            FanEntityFeature.TURN_ON | 
            FanEntityFeature.TURN_OFF | 
            FanEntityFeature.SET_SPEED
        )
        
        # 支持的风速模式
        self._attr_speed_count = 4  # 0-3档风速
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._initial_name,  # 使用初始名称，避免重置用户自定义名称
            "manufacturer": "花语前湾",
            "model": f"新风设备 (Type {self._device.get('typeId')})",
            "sw_version": self._device.get("projectCode", "Unknown"),
            "suggested_area": self._device.get("roomName"),
        }
    
    @property
    def is_on(self) -> bool:
        """Return if the fan is on."""
        current_states = self._device.get("current_states", {})
        
        # 检查电源状态 (fn1)
        if 1 in current_states:
            return current_states[1]["fv"] == 1
        else:
            # 如果没有fn1状态，检查设备的通用state属性
            return self._device.get("state", 0) == 1
    
    @property
    def percentage(self) -> Optional[int]:
        """Return the current speed as a percentage."""
        if not self.is_on:
            return 0
            
        current_states = self._device.get("current_states", {})
        
        # 检查风速状态 (fn3)
        if 3 in current_states:
            fan_speed = current_states[3]["fv"]
            # 将0-3档转换为百分比 (0档=0%, 1档=33%, 2档=67%, 3档=100%)
            return int((fan_speed / 3) * 100) if fan_speed > 0 else 0
        
        return 0
    
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
            "device_type": self._device_type,
            "si": self._device.get("si"),
            "room": self._device.get("roomName"),
        }
        
        # 新风设备使用立即控制，不经过节流总线
        attrs["control_mode"] = "immediate"
        
        return attrs
    
    def _update_device_state_immediately(self, fn: int, fv: int) -> None:
        """立即更新设备状态到目标值"""
        try:
            # 更新current_states中的状态
            if "current_states" not in self._device:
                self._device["current_states"] = {}
            
            self._device["current_states"][fn] = {
                "fv": fv,
                "st": self._device.get("st", 10101),
            }
            
            # 根据更新的功能更新设备属性
            if fn == 1:  # 电源状态
                self._device["is_on"] = fv == 1
                self._device["state"] = fv
            elif fn == 3:  # 风速设置
                self._device["fan_speed"] = fv
            
            # 使用调度器避免并发更新
            self.async_schedule_update_ha_state()
            
            device_name = self._attr_name
            device_si = self._device.get("si")
            _LOGGER.info(f"新风设备 {device_name} (si={device_si}) state immediately updated: fn{fn}={fv}")
            
        except Exception as err:
            _LOGGER.error(f"Failed to update fan device state immediately: {err}")
    
    async def async_turn_on(
        self,
        percentage: Optional[int] = None,
        preset_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        device_functions = DEVICE_FUNCTIONS.get("fan", {})
        
        # 开启电源
        if "turn_on" in device_functions:
            success = await self.coordinator.async_control_device_immediate(
                device_id=self._device_id,
                st=self._device["st"],
                si=self._device["si"],
                fn=device_functions["turn_on"]["fn"],
                fv=device_functions["turn_on"]["fv"],
                entity_id=self.entity_id,
            )
            
            if success:
                _LOGGER.info(f"Fan {self._attr_name} turned on")
                # 立即更新电源状态
                self._update_device_state_immediately(1, 1)
                
                # 如果指定了风速，设置风速
                if percentage is not None:
                    await self.async_set_percentage(percentage)
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        device_functions = DEVICE_FUNCTIONS.get("fan", {})
        
        if "turn_off" in device_functions:
            success = await self.coordinator.async_control_device_immediate(
                device_id=self._device_id,
                st=self._device["st"],
                si=self._device["si"],
                fn=device_functions["turn_off"]["fn"],
                fv=device_functions["turn_off"]["fv"],
                entity_id=self.entity_id,
            )
            
            if success:
                _LOGGER.info(f"Fan {self._attr_name} turned off")
                # 立即更新电源状态
                self._update_device_state_immediately(1, 0)
    
    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        if percentage == 0:
            await self.async_turn_off()
            return
        
        # 将百分比转换为0-3档风速
        # 0-25% = 1档, 26-50% = 2档, 51-75% = 3档, 76-100% = 3档
        if percentage <= 25:
            fan_speed = 1
        elif percentage <= 50:
            fan_speed = 2
        else:
            fan_speed = 3
        
        device_functions = DEVICE_FUNCTIONS.get("fan", {})
        
        if "set_fan_speed" in device_functions:
            success = await self.coordinator.async_control_device_immediate(
                device_id=self._device_id,
                st=self._device["st"],
                si=self._device["si"],
                fn=device_functions["set_fan_speed"]["fn"],
                fv=fan_speed,
                entity_id=self.entity_id,
            )
            
            if success:
                _LOGGER.info(f"Fan {self._attr_name} speed set to {fan_speed} (percentage: {percentage}%)")
                # 立即更新风速状态
                self._update_device_state_immediately(3, fan_speed)
    
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # 更新设备信息
        if self.coordinator.data and "devices" in self.coordinator.data:
            for device in self.coordinator.data["devices"]:
                if device["deviceId"] == self._device_id:
                    self._device = device
                    break
        super()._handle_coordinator_update()
    
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        # 当实体添加到HA时，主动请求一次状态更新
        await self.coordinator.async_request_state_update()
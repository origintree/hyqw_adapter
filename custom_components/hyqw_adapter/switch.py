"""Support for HYQW Adapter switches."""
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HYQWAdapterCoordinator
from .const import DEVICE_FUNCTIONS, DOMAIN
from .mqtt_entities import (
    MqttConnectionSwitch,
    MqttLocalBroadcastSwitch,
    MqttOptimisticEchoSwitch,
    MqttStartupEnableSwitch,
    ReplayEnabledSwitch,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter switches."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 检查是否为需要单独开关实体的设备
            # 注意：地暖(16)作为climate设备，新风(36)作为fan设备，不需要额外的开关
            # 如果将来需要其他类型的开关设备，在这里添加
            pass
    
    # 添加MQTT管理开关实体
    entities.extend([
        MqttStartupEnableSwitch(coordinator, config_entry),    # 默认启动开关
        MqttOptimisticEchoSwitch(coordinator, config_entry),   # 乐观回显开关
        MqttConnectionSwitch(coordinator, config_entry),       # 连接开关
        MqttLocalBroadcastSwitch(coordinator, config_entry),   # 本地广播开关
        ReplayEnabledSwitch(coordinator, config_entry),        # 报文重放模式
    ])
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a HYQW Adapter switch."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        # 只在初始化时设置一次名称，后续不再修改
        self._initial_name = device["deviceName"]
        self._attr_name = self._initial_name
        
        # 设置设备图标（为将来的开关设备保留）
        # 当前没有需要单独开关实体的设备
        self._attr_icon = "mdi:toggle-switch"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._initial_name,  # 使用初始名称，避免重置用户自定义名称
            "manufacturer": "花语前湾",
            "model": f"Type {self._device.get('typeId')}",
            "sw_version": self._device.get("projectCode", "Unknown"),
            "suggested_area": self._device.get("roomName"),
        }
    
    @property
    def is_on(self) -> bool:
        """Return if the switch is on."""
        # 优先使用实时状态数据
        current_states = self._device.get("current_states", {})
        if 1 in current_states:  # fn=1 是开关状态
            return current_states[1]["fv"] == 1
        return self._device.get("state", 0) == 1 or self._device.get("is_on", False)
    
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
        
        return attrs
    
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        success = await self.coordinator.async_control_device(
            device_id=self._device_id,
            st=self._device["st"],
            si=self._device["si"],
            fn=DEVICE_FUNCTIONS["switch"]["turn_on"]["fn"],
            fv=DEVICE_FUNCTIONS["switch"]["turn_on"]["fv"],
            entity_id=self.entity_id,  # 传递实体ID用于节流控制
        )
        
        if success:
            # 状态更新由coordinator处理，无需手动更新
            _LOGGER.debug(f"Switch {self._attr_name} turn on command sent")
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        success = await self.coordinator.async_control_device(
            device_id=self._device_id,
            st=self._device["st"],
            si=self._device["si"],
            fn=DEVICE_FUNCTIONS["switch"]["turn_off"]["fn"],
            fv=DEVICE_FUNCTIONS["switch"]["turn_off"]["fv"],
            entity_id=self.entity_id,  # 传递实体ID用于节流控制
        )
        
        if success:
            # 状态更新由coordinator处理，无需手动更新
            _LOGGER.debug(f"Switch {self._attr_name} turn off command sent")
    
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

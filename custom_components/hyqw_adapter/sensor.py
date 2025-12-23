"""Support for HYQW Adapter sensors."""
import logging
from typing import Any, Optional
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HYQWAdapterCoordinator
from .const import DOMAIN
from .mqtt_entities import (
    MqttStatusSensor,
    MqttStatsSensor,
    RecordStatusSensor,
    RecordCurrentDeviceSensor,
    RecordCurrentCommandSensor,
    RecordCurrentStateSensor,
    RecordOverallProgressSensor,
    RecordFailedCommandsSensor,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter sensors."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    
    # 添加API请求统计传感器
    entities.extend([
        HYQWAdapterRequestStatsSensor(coordinator, "total_requests", "总请求次数"),
        HYQWAdapterRequestStatsSensor(coordinator, "successful_requests", "成功请求次数"),
        HYQWAdapterRequestStatsSensor(coordinator, "failed_requests", "失败请求次数"),
        HYQWAdapterRequestStatsSensor(coordinator, "device_states_requests", "状态查询请求"),
        HYQWAdapterRequestStatsSensor(coordinator, "device_control_requests", "设备控制请求"),
        HYQWAdapterLastRequestTimeSensor(coordinator),
        HYQWAdapterLastRequestStatusSensor(coordinator),
    ])
    
    # 添加MQTT状态传感器
    entities.extend([
        MqttStatusSensor(coordinator, config_entry),    # MQTT状态
        MqttStatsSensor(coordinator, config_entry),     # MQTT统计
        RecordStatusSensor(coordinator, config_entry),  # 录制状态
        RecordCurrentDeviceSensor(coordinator, config_entry),
        RecordCurrentCommandSensor(coordinator, config_entry),
        RecordCurrentStateSensor(coordinator, config_entry),
        RecordOverallProgressSensor(coordinator, config_entry),
        RecordFailedCommandsSensor(coordinator, config_entry),
    ])
    
    # 添加轮询总线状态传感器
    if coordinator.polling_bus:
        entities.extend([
            HYQWAdapterPollingModeSensor(coordinator),
            HYQWAdapterPollingStatsSensor(coordinator, "long_polling_count", "长轮询次数"),
            HYQWAdapterPollingStatsSensor(coordinator, "short_polling_count", "短轮询次数"),
            HYQWAdapterPollingStatsSensor(coordinator, "mode_switches", "模式切换次数"),
        ])
    
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 为空调设备创建温度传感器
            if device.get("typeId") == 12:  # 空调
                entities.append(HYQWAdapterTemperatureSensor(coordinator, device))
            
            # 新风设备(typeId=36)现在作为fan设备，不需要额外的传感器
    
    # 注意：避免重复添加相同的MQTT管理传感器（已在上方添加）
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterTemperatureSensor(CoordinatorEntity, SensorEntity):
    """Representation of a HYQW Adapter temperature sensor."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}_temperature"
        # 只在初始化时设置一次名称，后续不再修改
        self._initial_name = f"{device['deviceName']} 温度"
        self._attr_name = self._initial_name
        
        # 设置传感器属性
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_icon = "mdi:thermometer"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._device["deviceName"],  # 温度传感器使用设备原始名称
            "manufacturer": "花语前湾",
            "model": f"Type {self._device.get('typeId')}",
            "sw_version": self._device.get("projectCode", "Unknown"),
            "suggested_area": self._device.get("roomName"),
        }
    
    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        # 首先检查从设备属性获取的当前温度
        if self._device.get("current_temperature"):
            return self._device.get("current_temperature")
        
        # 对于空调，检查fn5是否为当前温度
        if self._device.get("typeId") == 12:
            current_states = self._device.get("current_states", {})
            if 5 in current_states:
                temp_value = current_states[5]["fv"]
                if temp_value and temp_value > 100:
                    return float(temp_value) / 10
                elif temp_value:
                    return float(temp_value)
        
        return None
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success
    
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # 更新设备信息
        if self.coordinator.data and "devices" in self.coordinator.data:
            for device in self.coordinator.data["devices"]:
                if device["deviceId"] == self._device_id:
                    self._device = device
                    break
        super()._handle_coordinator_update()


# HYQWAdapterAirQualitySensor类已移除
# 因为新风设备不提供空气质量传感器功能


class HYQWAdapterRequestStatsSensor(CoordinatorEntity, SensorEntity):
    """Representation of a HYQW Adapter request statistics sensor."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, stat_type: str, display_name: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._stat_type = stat_type
        self._display_name = display_name
        self._attr_unique_id = f"{DOMAIN}_request_stats_{stat_type}"
        self._attr_name = f"花语前湾 {display_name}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:api"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "hyqw_adapter_api")},
            "name": "花语前湾 API",
            "manufacturer": "花语前湾",
            "model": "API Client",
        }
    
    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        stats = self.coordinator.get_request_stats()
        return stats.get(self._stat_type, 0)
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class HYQWAdapterLastRequestTimeSensor(CoordinatorEntity, SensorEntity):
    """Representation of a HYQW Adapter last request time sensor."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_request_last_time"
        self._attr_name = "花语前湾 最后请求时间"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:clock-outline"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "hyqw_adapter_api")},
            "name": "花语前湾 API",
            "manufacturer": "花语前湾",
            "model": "API Client",
        }
    
    @property
    def native_value(self) -> Optional[datetime]:
        """Return the state of the sensor."""
        stats = self.coordinator.get_request_stats()
        last_request_time = stats.get("last_request_time")
        if last_request_time:
            return datetime.fromtimestamp(last_request_time, tz=timezone.utc)
        return None
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class HYQWAdapterLastRequestStatusSensor(CoordinatorEntity, SensorEntity):
    """Representation of a HYQW Adapter last request status sensor."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_request_last_status"
        self._attr_name = "花语前湾 最后请求状态"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:check-circle-outline"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "hyqw_adapter_api")},
            "name": "花语前湾 API",
            "manufacturer": "花语前湾",
            "model": "API Client",
        }
    
    @property
    def native_value(self) -> Optional[str]:
        """Return the state of the sensor."""
        stats = self.coordinator.get_request_stats()
        return stats.get("last_request_status")
    
    @property
    def icon(self) -> str:
        """Return the icon for the sensor based on status."""
        status = self.native_value
        if status == "success":
            return "mdi:check-circle-outline"
        elif status and status.startswith("api_error"):
            return "mdi:alert-circle-outline"
        elif status and status.startswith("http_error"):
            return "mdi:web-remove"
        elif status == "timeout":
            return "mdi:clock-alert-outline"
        elif status and status.startswith("exception"):
            return "mdi:alert-outline"
        else:
            return "mdi:help-circle-outline"
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class HYQWAdapterPollingModeSensor(CoordinatorEntity, SensorEntity):
    """轮询模式传感器"""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_polling_mode"
        self._attr_name = "花语前湾 轮询模式"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:refresh-auto"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "hyqw_adapter_polling_bus")},
            "name": "花语前湾 轮询总线",
            "manufacturer": "花语前湾",
            "model": "Polling Bus",
        }
    
    @property
    def native_value(self) -> Optional[str]:
        """Return the state of the sensor."""
        if self.coordinator.polling_bus:
            return self.coordinator.polling_bus.current_mode
        return "disabled"
    
    @property
    def icon(self) -> str:
        """Return the icon for the sensor based on mode."""
        mode = self.native_value
        if mode == "short":
            return "mdi:refresh-circle"
        elif mode == "long":
            return "mdi:refresh-auto"
        else:
            return "mdi:refresh-off"
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class HYQWAdapterPollingStatsSensor(CoordinatorEntity, SensorEntity):
    """轮询统计传感器"""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, stat_type: str, display_name: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._stat_type = stat_type
        self._display_name = display_name
        self._attr_unique_id = f"{DOMAIN}_polling_stats_{stat_type}"
        self._attr_name = f"花语前湾 {display_name}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:chart-line"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, "hyqw_adapter_polling_bus")},
            "name": "花语前湾 轮询总线",
            "manufacturer": "花语前湾",
            "model": "Polling Bus",
        }
    
    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if self.coordinator.polling_bus:
            stats = self.coordinator.polling_bus.stats
            return stats.get(self._stat_type, 0)
        return 0
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success

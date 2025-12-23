"""Support for HYQW Adapter climate devices."""
import asyncio
import logging
from typing import Any, Optional

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HYQWAdapterCoordinator
from .const import (
    AC_MODES,
    AC_MODES_REVERSE,
    CLIMATE_FUNCTIONS,
    DEVICE_CONFIGS,
    DOMAIN,
    FAN_SPEEDS,
    FAN_SPEEDS_REVERSE,
)
from .temperature_sensor_binder import TemperatureSensorBinder

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter climate devices."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 检查是否为气候设备 (空调12、地暖16)
            if device.get("typeId") in [12, 16]:
                entities.append(HYQWAdapterClimate(coordinator, device))
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterClimate(CoordinatorEntity, ClimateEntity):
    """Representation of a HYQW Adapter climate device."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the climate device."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._device_type = device.get("typeId")
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        # 只在初始化时设置一次名称，后续不再修改
        self._initial_name = device["deviceName"]
        self._attr_name = self._initial_name
        
        # 获取设备配置
        self._config = DEVICE_CONFIGS.get(self._device_type, {})
        
        # 设置支持的功能
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        
        if self._config.get("min_temp") is not None:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        
        if self._config.get("supports_fan"):
            features |= ClimateEntityFeature.FAN_MODE
        
        self._attr_supported_features = features
        
        # 温度单位
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        
        # 设置支持的模式
        self._attr_hvac_modes = self._config.get("hvac_modes", [HVACMode.OFF])
        self._attr_fan_modes = self._config.get("fan_modes")
        
        # 温度范围
        self._attr_min_temp = self._config.get("min_temp")
        self._attr_max_temp = self._config.get("max_temp")
        self._attr_target_temperature_step = self._config.get("temp_step", 1)
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._initial_name,  # 使用初始名称，避免重置用户自定义名称
            "manufacturer": "花语前湾",
            "model": f"{self._config.get('name', 'Climate')} (Type {self._device.get('typeId')})",
            "sw_version": self._device.get("projectCode", "Unknown"),
            "suggested_area": self._device.get("roomName"),
        }
    
    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        current_states = self._device.get("current_states", {})
        
        # 首先检查电源状态 (fn1) - 电源关闭时一律返回OFF
        is_power_on = False
        if 1 in current_states:
            is_power_on = current_states[1]["fv"] == 1
        else:
            # 如果没有fn1状态，检查设备的通用state属性
            is_power_on = self._device.get("state", 0) == 1
        
        if not is_power_on:
            return HVACMode.OFF
        
        # 电源开启时，根据设备类型和具体模式状态返回HVAC模式
        if self._device_type == 12:  # 空调
            # 空调有独立的模式设置 (fn3)
            if 3 in current_states:
                mode_value = current_states[3]["fv"]
                mode_mapping = {
                    0: HVACMode.COOL,     # 制冷
                    1: HVACMode.HEAT,     # 制热  
                    2: HVACMode.FAN_ONLY, # 通风
                    3: HVACMode.DRY,      # 除湿
                }
                return mode_mapping.get(mode_value, HVACMode.COOL)
            # 如果没有模式状态，默认为制冷
            return HVACMode.COOL
            
        elif self._device_type == 16:  # 地暖
            # 地暖只有加热模式
            return HVACMode.HEAT
        
        # 未知设备类型，电源开启时默认返回COOL
        return HVACMode.COOL
    
    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        # 对于地暖设备，优先使用绑定的空调温度传感器
        if self._device_type == 16:
            # 检查是否有绑定的温度传感器
            if hasattr(self.coordinator, 'temperature_binder'):
                bound_sensor_id = self.coordinator.temperature_binder.get_bound_temperature_sensor(self._device_id)
                if bound_sensor_id:
                    # 从绑定的温度传感器获取温度值
                    bound_temp = self._get_bound_sensor_temperature(bound_sensor_id)
                    if bound_temp is not None:
                        return bound_temp
        
        # 首先检查从设备属性获取的当前温度
        if self._device.get("current_temperature"):
            return self._device.get("current_temperature")
        
        # 对于空调，检查fn5是否为当前温度
        if self._device_type == 12:
            current_states = self._device.get("current_states", {})
            if 5 in current_states:
                temp_value = current_states[5]["fv"]
                if temp_value and temp_value > 100:
                    return float(temp_value) / 10
                elif temp_value:
                    return float(temp_value)
        
        return None
    
    def _get_bound_sensor_temperature(self, sensor_entity_id: str) -> Optional[float]:
        """从绑定的温度传感器获取温度值."""
        try:
            # 获取传感器实体的状态
            sensor_state = self.hass.states.get(sensor_entity_id)
            if sensor_state and sensor_state.state not in ['unknown', 'unavailable']:
                try:
                    return float(sensor_state.state)
                except (ValueError, TypeError):
                    _LOGGER.debug(f"无法解析温度传感器 {sensor_entity_id} 的值: {sensor_state.state}")
                    return None
            else:
                _LOGGER.debug(f"温度传感器 {sensor_entity_id} 状态不可用")
                return None
        except Exception as err:
            _LOGGER.debug(f"获取绑定温度传感器 {sensor_entity_id} 温度失败: {err}")
            return None
    
    @property
    def target_temperature(self) -> Optional[float]:
        """Return the temperature we try to reach."""
        if self._attr_min_temp is None:
            return None
            
        current_states = self._device.get("current_states", {})
        
        # 检查温度设置状态 (fn2)
        if 2 in current_states:
            temp_value = current_states[2]["fv"]
            # 如果温度值大于100，可能是放大了10倍（如260=26.0°C）
            if temp_value and temp_value > 100:
                return float(temp_value) / 10
            elif temp_value and self._attr_min_temp <= temp_value <= self._attr_max_temp:
                return float(temp_value)
        
        # 默认温度
        if self._device_type == 12:  # 空调
            return 24.0
        elif self._device_type == 16:  # 地暖
            return 20.0
        
        return None
    
    @property
    def fan_mode(self) -> Optional[str]:
        """Return the fan setting."""
        if not self._config.get("supports_fan"):
            return None
            
        current_states = self._device.get("current_states", {})
        
        # 根据设备类型检查风速状态
        fn_key = 4 if self._device_type == 12 else 3  # 空调用fn4
        
        if fn_key in current_states:
            fan_value = current_states[fn_key]["fv"]
            return FAN_SPEEDS.get(fan_value, "auto")
        
        return "auto"
    
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
        
        # 气候设备使用立即控制，不经过节流总线
        attrs["control_mode"] = "immediate"
        
        # 对于地暖设备，添加温度传感器绑定信息
        if self._device_type == 16 and hasattr(self.coordinator, 'temperature_binder'):
            bound_sensor_id = self.coordinator.temperature_binder.get_bound_temperature_sensor(self._device_id)
            if bound_sensor_id:
                attrs["bound_temperature_sensor"] = bound_sensor_id
                # 获取绑定传感器的当前温度
                bound_temp = self._get_bound_sensor_temperature(bound_sensor_id)
                if bound_temp is not None:
                    attrs["bound_sensor_temperature"] = bound_temp
        
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
            elif fn == 2 and self._device_type in [12, 16]:  # 温度设置
                if fv and fv > 100:
                    self._device["target_temperature"] = fv / 10
                else:
                    self._device["target_temperature"] = fv
            elif fn == 3:  # 模式设置(空调)
                if self._device_type == 12:
                    self._device["hvac_mode"] = fv
            elif fn == 4 and self._device_type == 12:  # 空调风速
                self._device["fan_speed"] = fv
            
            # 使用调度器避免并发更新
            self.async_schedule_update_ha_state()
            
            device_name = self._attr_name
            device_si = self._device.get("si")
            device_type_name = {12: "空调", 16: "地暖"}.get(self._device_type, "Climate")
            _LOGGER.info(f"{device_type_name} {device_name} (si={device_si}) state immediately updated: fn{fn}={fv}")
            
        except Exception as err:
            _LOGGER.error(f"Failed to update climate device state immediately: {err}")
    
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        device_functions = CLIMATE_FUNCTIONS.get(self._device_type, {})
        current_states = self._device.get("current_states", {})
        
        # 获取当前电源状态
        current_power_on = False
        if 1 in current_states:
            current_power_on = current_states[1]["fv"] == 1
        
        if hvac_mode == HVACMode.OFF:
            # 设置为OFF：只需关闭电源 (fn1, fv0)
            if "turn_off" in device_functions:
                if current_power_on:  # 只有在当前开启时才发送关闭命令
                    success = await self.coordinator.async_control_device_immediate(
                        device_id=self._device_id,
                        st=self._device["st"],
                        si=self._device["si"],
                        fn=device_functions["turn_off"]["fn"],
                        fv=device_functions["turn_off"]["fv"],
                        entity_id=self.entity_id,
                    )
                    if success:
                        _LOGGER.info(f"Climate {self._attr_name} turned off")
                        # 立即更新电源状态
                        self._update_device_state_immediately(1, 0)
                else:
                    _LOGGER.debug(f"Climate {self._attr_name} already off, skipping")
        else:
            # 设置为工作模式：需要分两步操作
            
            # 第一步：确保设备电源开启 (fn1, fv1)
            power_success = True
            if not current_power_on and "turn_on" in device_functions:
                power_success = await self.coordinator.async_control_device_immediate(
                    device_id=self._device_id,
                    st=self._device["st"],
                    si=self._device["si"],
                    fn=device_functions["turn_on"]["fn"],
                    fv=device_functions["turn_on"]["fv"],
                    entity_id=self.entity_id,
                )
                if power_success:
                    _LOGGER.info(f"Climate {self._attr_name} turned on")
                    # 立即更新电源状态
                    self._update_device_state_immediately(1, 1)
            
            # 第二步：设置工作模式（仅对空调）
            if power_success and self._device_type == 12 and "set_mode" in device_functions:
                mode_mapping = {
                    HVACMode.COOL: 0,     # 制冷
                    HVACMode.HEAT: 1,     # 制热
                    HVACMode.FAN_ONLY: 2, # 通风
                    HVACMode.DRY: 3,      # 除湿
                }
                if hvac_mode in mode_mapping:
                    # 检查当前模式，避免重复设置
                    current_mode = current_states.get(3, {}).get("fv")
                    target_mode = mode_mapping[hvac_mode]
                    
                    if current_mode != target_mode:
                        mode_success = await self.coordinator.async_control_device_immediate(
                            device_id=self._device_id,
                            st=self._device["st"],
                            si=self._device["si"],
                            fn=device_functions["set_mode"]["fn"],
                            fv=target_mode,
                            entity_id=self.entity_id,
                        )
                        if mode_success:
                            mode_name = {0: "制冷", 1: "制热", 2: "通风", 3: "除湿"}.get(target_mode, str(target_mode))
                            _LOGGER.info(f"Climate {self._attr_name} mode set to {mode_name}")
                            # 立即更新模式状态
                            self._update_device_state_immediately(3, target_mode)
                    else:
                        _LOGGER.debug(f"Climate {self._attr_name} mode already {hvac_mode}, skipping")
            
            # 对于地暖，只需要开启电源即可
            elif power_success and self._device_type == 16:
                _LOGGER.info(f"地暖 {self._attr_name} set to {hvac_mode}")
    
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
            
        # 检查温度范围
        if self._attr_min_temp is not None and temperature < self._attr_min_temp:
            temperature = self._attr_min_temp
        if self._attr_max_temp is not None and temperature > self._attr_max_temp:
            temperature = self._attr_max_temp
            
        device_functions = CLIMATE_FUNCTIONS.get(self._device_type, {})
        
        if "set_temperature" in device_functions:
            success = await self.coordinator.async_control_device_immediate(
                device_id=self._device_id,
                st=self._device["st"],
                si=self._device["si"],
                fn=device_functions["set_temperature"]["fn"],
                fv=int(temperature),
                entity_id=self.entity_id,
            )
            
            if success:
                _LOGGER.info(f"Climate {self._attr_name} temperature set to {temperature}")
                # 立即更新温度状态
                self._update_device_state_immediately(2, int(temperature))
    
    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        if not self._config.get("supports_fan"):
            return
            
        fan_speed = FAN_SPEEDS_REVERSE.get(fan_mode)
        if fan_speed is None:
            return
            
        device_functions = CLIMATE_FUNCTIONS.get(self._device_type, {})
        
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
                _LOGGER.info(f"Climate {self._attr_name} fan mode set to {fan_mode}")
                # 立即更新风速状态
                fn_key = 4 if self._device_type == 12 else 3  # 空调用fn4
                self._update_device_state_immediately(fn_key, fan_speed)
    
    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self.async_set_hvac_mode(HVACMode.COOL)
    
    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self.async_set_hvac_mode(HVACMode.OFF)
    
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

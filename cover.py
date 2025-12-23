"""Support for HYQW Adapter covers (curtains)."""
import asyncio
import logging
import time
from typing import Any, Optional

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
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
    """Set up HYQW Adapter covers."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = []
    if coordinator.data and "devices" in coordinator.data:
        for device in coordinator.data["devices"]:
            # 检查是否为窗帘设备 (typeId == 14)
            if device.get("typeId") == 14:
                entities.append(HYQWAdapterCover(coordinator, device))
    
    if entities:
        async_add_entities(entities)


class HYQWAdapterCover(CoordinatorEntity, CoverEntity):
    """Representation of a HYQW Adapter cover."""
    
    def __init__(self, coordinator: HYQWAdapterCoordinator, device: dict) -> None:
        """Initialize the cover."""
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["deviceId"]
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        # 只在初始化时设置一次名称，后续不再修改
        self._initial_name = device["deviceName"]
        self._attr_name = self._initial_name
        
        # 设置设备类型 - 纱帘和布帘都是左右开的窗帘
        self._attr_device_class = CoverDeviceClass.CURTAIN
        
        # 设置支持的功能 - 明确支持所有基本控制功能
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        
        # 本地状态记忆
        self._local_position = 0  # 当前位置 (0-100)
        self._target_position = None  # 目标位置
        self._moving_state = "stopped"  # stopped, opening, closing, moving
        self._movement_start_time = None
        self._movement_start_position = 0
        self._movement_duration = 8.0  # 全开全关需要6秒
        self._update_task = None
    
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
    def is_closed(self) -> Optional[bool]:
        """Return if the cover is closed."""
        return self._get_current_position() == 0
    
    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        return self._moving_state == "opening"
    
    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        return self._moving_state == "closing"
    
    @property
    def current_cover_position(self) -> Optional[int]:
        """Return current position of cover."""
        return self._get_current_position()
    
    def _get_current_position(self) -> int:
        """Get current position with animation support."""
        if self._moving_state == "stopped":
            return self._local_position
        
        if self._movement_start_time is None:
            return self._local_position
        
        # 计算当前位置
        elapsed = time.time() - self._movement_start_time
        if self._target_position is not None:
            distance = self._target_position - self._movement_start_position
            duration = abs(distance) * self._movement_duration / 100
            
            if elapsed >= duration:
                # 运动完成
                self._local_position = self._target_position
                self._moving_state = "stopped"
                self._target_position = None
                self._movement_start_time = None
                return self._local_position
            else:
                # 运动中
                progress = elapsed / duration
                current_position = self._movement_start_position + distance * progress
                return int(current_position)
        
        return self._local_position
    
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
        
        # 窗帘设备使用立即控制，不经过节流总线
        attrs["control_mode"] = "immediate"
        attrs["local_position"] = self._local_position
        attrs["target_position"] = self._target_position
        attrs["moving_state"] = self._moving_state
        
        return attrs
    
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        # 使用正确的控制码：fn=1, fv=1 表示打开
        success = await self.coordinator.async_control_device_immediate(
            device_id=self._device_id,
            st=self._get_control_st(),
            si=self._device["si"],
            fn=1,  # fn=1 是控制指令
            fv=1,  # fv=1 是打开
            entity_id=self.entity_id,
        )
        
        if success:
            self._start_movement(100, "opening")
            _LOGGER.info(f"Cover {self._attr_name} open command sent - starting movement to 100%")
    
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        # 使用正确的控制码：fn=1, fv=0 表示关闭
        success = await self.coordinator.async_control_device_immediate(
            device_id=self._device_id,
            st=self._get_control_st(),
            si=self._device["si"],
            fn=1,  # fn=1 是控制指令
            fv=0,  # fv=0 是关闭
            entity_id=self.entity_id,
        )
        
        if success:
            self._start_movement(0, "closing")
            _LOGGER.info(f"Cover {self._attr_name} close command sent - starting movement to 0%")
    
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        # 使用正确的控制码：fn=1, fv=2 表示停止
        success = await self.coordinator.async_control_device_immediate(
            device_id=self._device_id,
            st=self._get_control_st(),
            si=self._device["si"],
            fn=1,  # fn=1 是控制指令
            fv=2,  # fv=2 是停止
            entity_id=self.entity_id,
        )
        
        if success:
            self._stop_movement()
            _LOGGER.info(f"Cover {self._attr_name} stop command sent - stopping at current position")
    
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        position = kwargs[ATTR_POSITION]
        
        # 使用正确的控制码：fn=2, fv=位置百分比 表示设置开度
        success = await self.coordinator.async_control_device_immediate(
            device_id=self._device_id,
            st=self._get_control_st(),
            si=self._device["si"],
            fn=2,  # fn=2 是控制开度
            fv=position,  # fv是位置百分比
            entity_id=self.entity_id,
        )
        
        if success:
            self._start_movement(position, "moving")
            _LOGGER.info(f"Cover {self._attr_name} position set to {position}% - starting movement")
    
    def _get_control_st(self) -> int:
        """Get the control status type for this device."""
        # 根据抓包数据，窗帘控制使用 st=20201
        return 20201
    
    def _start_movement(self, target_position: int, moving_state: str) -> None:
        """Start cover movement to target position."""
        self._target_position = target_position
        self._movement_start_position = self._local_position
        self._movement_start_time = time.time()
        self._moving_state = moving_state
        
        # 取消现有更新任务
        if self._update_task:
            self._update_task.cancel()
        
        # 启动更新任务
        self._update_task = asyncio.create_task(self._update_position_loop())
    
    def _stop_movement(self) -> None:
        """Stop cover movement and update position."""
        # 计算当前位置
        current_pos = self._get_current_position()
        self._local_position = current_pos
        self._target_position = None
        self._moving_state = "stopped"
        self._movement_start_time = None
        
        # 取消更新任务
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None
        
        # 使用调度器更新状态
        self.async_schedule_update_ha_state()
    
    async def _update_position_loop(self) -> None:
        """Update position during movement with smooth animation."""
        try:
            update_counter = 0
            while self._moving_state != "stopped":
                await asyncio.sleep(0.01)  # 每50ms检查一次，提高动画流畅度
                
                # 提高状态更新频率：每150ms更新一次状态，避免卡顿
                update_counter += 1
                if update_counter >= 2:  # 3 * 50ms = 150ms
                    self.async_schedule_update_ha_state()
                    update_counter = 0
                
                # 检查是否到达目标位置
                if self._target_position is not None:
                    current_pos = self._get_current_position()
                    if current_pos == self._target_position:
                        self._local_position = self._target_position
                        self._moving_state = "stopped"
                        self._target_position = None
                        self._movement_start_time = None
                        # 最终位置更新
                        self.async_schedule_update_ha_state()
                        _LOGGER.debug(f"Cover {self._attr_name} reached target position {self._local_position}%")
                        break
        except asyncio.CancelledError:
            _LOGGER.debug(f"Cover {self._attr_name} position update loop cancelled")
            pass
        finally:
            self._update_task = None
    
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
    
    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from hass."""
        # 清理更新任务
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None
        await super().async_will_remove_from_hass()

"""Support for HYQW Adapter button entities."""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HYQWAdapterCoordinator
from .const import DOMAIN
from .mqtt_entities import (
    MqttApplyAndReconnectButton,
    MqttResetStatsButton,
    StartCurtainFullRecordButton,
    StartACFullRecordButton,
    StartFloorFullRecordButton,
    StartFreshAirFullRecordButton,
    StartLightFullRecordButton,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter button entities."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = [
        MqttApplyAndReconnectButton(coordinator, config_entry),  # 应用配置并重连
        MqttResetStatsButton(coordinator, config_entry),         # 重置统计
        StartCurtainFullRecordButton(coordinator, config_entry), # 开始窗帘全量录制
        StartACFullRecordButton(coordinator, config_entry),      # 开始空调全量录制
        StartFloorFullRecordButton(coordinator, config_entry),   # 开始地暖全量录制
        StartFreshAirFullRecordButton(coordinator, config_entry),# 开始新风全量录制
        StartLightFullRecordButton(coordinator, config_entry),   # 开始灯具全量录制
    ]
    
    async_add_entities(entities)

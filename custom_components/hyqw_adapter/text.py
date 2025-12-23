"""Support for HYQW Adapter text entities."""
import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HYQWAdapterCoordinator
from .const import DOMAIN
from .mqtt_entities import (
    MqttHostText,
    MqttUsernameText, 
    MqttPasswordText,
    MqttClientIdText,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter text entities."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = [
        MqttHostText(coordinator, config_entry),        # 服务器地址
        MqttUsernameText(coordinator, config_entry),    # 用户名
        MqttPasswordText(coordinator, config_entry),    # 密码
        MqttClientIdText(coordinator, config_entry),    # 客户端ID
    ]
    
    async_add_entities(entities)

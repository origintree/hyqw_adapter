"""Support for HYQW Adapter select entities."""
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HYQWAdapterCoordinator
from .const import DOMAIN
from .mqtt_entities import MqttFallbackIntervalSelect

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HYQW Adapter select entities."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities = [
        MqttFallbackIntervalSelect(coordinator, config_entry),  # 兜底巡检间隔
    ]
    
    async_add_entities(entities)

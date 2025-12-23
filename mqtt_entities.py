"""MQTT管理实体 - MQTT Management Entities for HYQW Adapter"""
import logging
from typing import Any, Dict, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import Entity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN, 
    MQTT_CONFIG,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME, 
    CONF_MQTT_PASSWORD,
    CONF_MQTT_CLIENT_ID,
    CONF_MQTT_STARTUP_ENABLE,
    CONF_MQTT_OPTIMISTIC_ECHO,
    CONF_MQTT_FALLBACK_INTERVAL,
)
from .const import CONF_REPLAY_ENABLED, CONF_MQTT_LOCAL_BROADCAST_ENABLED
from .replay_recorder import start_ac_full, start_floor_full, start_freshair_full, start_light_full

_LOGGER = logging.getLogger(__name__)


async def async_setup_mqtt_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置MQTT管理实体"""
    entities = []
    
    
    # MQTT配置文本实体
    entities.append(MqttHostText(coordinator, config_entry))
    entities.append(MqttUsernameText(coordinator, config_entry))
    entities.append(MqttPasswordText(coordinator, config_entry))
    entities.append(MqttClientIdText(coordinator, config_entry))
    
    # MQTT选择实体
    entities.append(MqttFallbackIntervalSelect(coordinator, config_entry))
    
    # 回放开关与录制控制（开关实体现在在switch.py中注册）
    entities.append(StartCurtainFullRecordButton(coordinator, config_entry))
    entities.append(RecordStatusSensor(coordinator, config_entry))
    entities.append(RecordCurrentDeviceSensor(coordinator, config_entry))
    entities.append(RecordCurrentCommandSensor(coordinator, config_entry))
    entities.append(RecordCurrentStateSensor(coordinator, config_entry))
    entities.append(RecordOverallProgressSensor(coordinator, config_entry))
    entities.append(StartACFullRecordButton(coordinator, config_entry))
    entities.append(StartFloorFullRecordButton(coordinator, config_entry))
    entities.append(StartFreshAirFullRecordButton(coordinator, config_entry))
    entities.append(StartLightFullRecordButton(coordinator, config_entry))
    
    # MQTT操作按钮
    entities.append(MqttApplyAndReconnectButton(coordinator, config_entry))
    entities.append(MqttResetStatsButton(coordinator, config_entry))
    
    # MQTT状态传感器
    entities.append(MqttStatusSensor(coordinator, config_entry))
    entities.append(MqttStatsSensor(coordinator, config_entry))
    
    # 实体名称设置按钮
    entities.append(SetEntityNameButton(coordinator, config_entry))
    
    async_add_entities(entities)
    _LOGGER.info(f"已添加{len(entities)}个MQTT管理实体")


class MqttBaseEntity(Entity):
    """MQTT实体基类"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        """初始化基类"""
        super().__init__()
        self.coordinator = coordinator
        self.config_entry = config_entry
        self.hass = coordinator.hass
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_mqtt")},
            name="HYQW MQTT管理",
            manufacturer="HYQW Adapter",
            model="MQTT Gateway",
            # 移除 via_device 引用以避免引用不存在的设备
            # via_device=(DOMAIN, config_entry.entry_id),
        )
        self._attr_entity_category = EntityCategory.CONFIG
    
    @property
    def mqtt_gateway(self):
        """获取MQTT网关实例"""
        return getattr(self.coordinator, 'mqtt_gateway', None)
    
    @property
    def state_sync_router(self):
        """获取状态同步路由器实例"""
        return getattr(self.coordinator, 'state_sync_router', None)
    
    def _get_option(self, key: str, default: Any = None) -> Any:
        """从配置选项中获取值"""
        return self.config_entry.options.get(key, default)
    
    async def _set_option(self, key: str, value: Any) -> None:
        """设置配置选项值"""
        new_options = dict(self.config_entry.options)
        new_options[key] = value
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=new_options
        )


class MqttConnectionSwitch(MqttBaseEntity, SwitchEntity):
    """MQTT连接开关"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "08 MQTT连接"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_connection"
        self._attr_icon = "mdi:mqtt"
    
    @property
    def is_on(self) -> bool:
        """返回开关状态"""
        if self.mqtt_gateway:
            return self.mqtt_gateway.is_connected
        return False
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """返回额外状态属性"""
        if not self.mqtt_gateway:
            return {"error": "MQTT网关未初始化"}
        
        status = self.mqtt_gateway.get_status()
        return {
            "host": status.get("host"),
            "port": status.get("port"),
            "client_id": status.get("client_id"),
            "topics": status.get("topics", []),
            "last_error": status.get("stats", {}).get("last_error"),
            "messages_received": status.get("stats", {}).get("messages_received", 0),
            "uptime_seconds": status.get("uptime_seconds"),
        }
    
    async def async_turn_on(self, **kwargs) -> None:
        """开启MQTT连接"""
        if not self.mqtt_gateway:
            _LOGGER.error("MQTT网关未初始化")
            return
        
        _LOGGER.info("手动启动MQTT连接")
        
        # 配置MQTT连接参数
        host = self._get_option(CONF_MQTT_HOST)
        port = self._get_option(CONF_MQTT_PORT, MQTT_CONFIG["default_port"])
        username = self._get_option(CONF_MQTT_USERNAME)
        password = self._get_option(CONF_MQTT_PASSWORD)
        client_id = self._get_option(CONF_MQTT_CLIENT_ID)
        
        _LOGGER.info(f"手动MQTT配置参数 - 主机:{host}, 端口:{port}, 用户名:{username}, 客户端ID:'{client_id}'")
        
        if not host:
            _LOGGER.error("MQTT服务器地址未配置")
            return
        
        self.mqtt_gateway.configure(host, port, username, password, client_id)
        
        success = await self.mqtt_gateway.start()
        if success and self.state_sync_router:
            await self.state_sync_router.use_mqtt_mode()
            _LOGGER.info("MQTT连接已开启，切换到MQTT模式")
    
    async def async_turn_off(self, **kwargs) -> None:
        """关闭MQTT连接"""
        if not self.mqtt_gateway:
            return
        
        await self.mqtt_gateway.stop()
        if self.state_sync_router:
            await self.state_sync_router.use_polling_mode()
            _LOGGER.info("MQTT连接已关闭，切换到轮询模式")


class MqttHostText(MqttBaseEntity, TextEntity):
    """MQTT服务器地址文本实体"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "01 MQTT服务器"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_host"
        self._attr_icon = "mdi:server"
    
    @property
    def native_value(self) -> str:
        """返回当前值"""
        return self._get_option(CONF_MQTT_HOST, "")
    
    async def async_set_value(self, value: str) -> None:
        """设置值"""
        await self._set_option(CONF_MQTT_HOST, value)


class MqttUsernameText(MqttBaseEntity, TextEntity):
    """MQTT用户名文本实体"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "02 MQTT用户名"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_username"
        self._attr_icon = "mdi:account"
    
    @property
    def native_value(self) -> str:
        """返回当前值"""
        return self._get_option(CONF_MQTT_USERNAME, "")
    
    async def async_set_value(self, value: str) -> None:
        """设置值"""
        await self._set_option(CONF_MQTT_USERNAME, value)


class MqttPasswordText(MqttBaseEntity, TextEntity):
    """MQTT密码文本实体"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "03 MQTT密码"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_password"
        self._attr_icon = "mdi:key"
        self._attr_mode = "password"  # 密码模式
    
    @property
    def native_value(self) -> str:
        """返回当前值"""
        return self._get_option(CONF_MQTT_PASSWORD, "")
    
    async def async_set_value(self, value: str) -> None:
        """设置值"""
        await self._set_option(CONF_MQTT_PASSWORD, value)


class MqttClientIdText(MqttBaseEntity, TextEntity):
    """MQTT客户端ID文本实体"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "04 MQTT客户端ID"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_client_id"
        self._attr_icon = "mdi:identifier"
    
    @property
    def native_value(self) -> str:
        """返回当前值"""
        return self._get_option(CONF_MQTT_CLIENT_ID, "")
    
    async def async_set_value(self, value: str) -> None:
        """设置值"""
        await self._set_option(CONF_MQTT_CLIENT_ID, value)


class MqttFallbackIntervalSelect(MqttBaseEntity, SelectEntity):
    """MQTT兜底巡检间隔选择实体"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "05 MQTT兜底巡检间隔"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_fallback_interval"
        self._attr_icon = "mdi:timer"
        self._attr_options = list(MQTT_CONFIG["fallback_check_intervals"].keys())
    
    @property
    def current_option(self) -> str:
        """返回当前选项"""
        interval = self._get_option(CONF_MQTT_FALLBACK_INTERVAL, MQTT_CONFIG["default_fallback_interval"])
        
        # 根据间隔值找到对应的选项
        for option, value in MQTT_CONFIG["fallback_check_intervals"].items():
            if value == interval:
                return option
        return "10m"  # 默认值
    
    async def async_select_option(self, option: str) -> None:
        """选择选项"""
        interval = MQTT_CONFIG["fallback_check_intervals"].get(option, MQTT_CONFIG["default_fallback_interval"])
        await self._set_option(CONF_MQTT_FALLBACK_INTERVAL, interval)
        
        # 更新路由器配置
        if self.state_sync_router:
            self.state_sync_router.configure_fallback(interval)


class MqttStartupEnableSwitch(MqttBaseEntity, SwitchEntity):
    """MQTT默认启动开关"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "06 MQTT默认启动"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_startup_enable"
        self._attr_icon = "mdi:power-on"
    
    @property
    def is_on(self) -> bool:
        """返回开关状态"""
        return self._get_option(CONF_MQTT_STARTUP_ENABLE, MQTT_CONFIG["default_startup_enable"])
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """返回额外状态属性"""
        return {
            "description": "控制插件启动时是否默认启用MQTT连接",
            "default_value": MQTT_CONFIG["default_startup_enable"],
            "current_value": self.is_on,
        }
    
    async def async_turn_on(self, **kwargs) -> None:
        """开启默认启动"""
        await self._set_option(CONF_MQTT_STARTUP_ENABLE, True)
        _LOGGER.info("MQTT默认启动已开启")
    
    async def async_turn_off(self, **kwargs) -> None:
        """关闭默认启动"""
        await self._set_option(CONF_MQTT_STARTUP_ENABLE, False)
        _LOGGER.info("MQTT默认启动已关闭")


class MqttOptimisticEchoSwitch(MqttBaseEntity, SwitchEntity):
    """MQTT乐观回显开关"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "07 MQTT乐观回显"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_optimistic_echo"
        self._attr_icon = "mdi:flash"
    
    @property
    def is_on(self) -> bool:
        """返回开关状态"""
        return self._get_option(CONF_MQTT_OPTIMISTIC_ECHO, MQTT_CONFIG["default_optimistic_echo"])
    
    async def async_turn_on(self, **kwargs) -> None:
        """开启乐观回显"""
        await self._set_option(CONF_MQTT_OPTIMISTIC_ECHO, True)
        if self.state_sync_router:
            self.state_sync_router.set_optimistic_echo(True)
    
    async def async_turn_off(self, **kwargs) -> None:
        """关闭乐观回显"""
        await self._set_option(CONF_MQTT_OPTIMISTIC_ECHO, False)
        if self.state_sync_router:
            self.state_sync_router.set_optimistic_echo(False)


class MqttLocalBroadcastSwitch(MqttBaseEntity, SwitchEntity):
    """MQTT本地广播开关：启用后定期向配置的主题发送毫秒时间戳"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "09 启用本地广播"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_local_broadcast"
        self._attr_icon = "mdi:bullhorn"
        self._attr_available = True

    @property
    def is_on(self) -> bool:
        return self._get_option(CONF_MQTT_LOCAL_BROADCAST_ENABLED, False)
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """返回额外状态属性"""
        attrs = {
            "description": f"启用后每{MQTT_CONFIG['local_broadcast_interval']}s向{MQTT_CONFIG['local_broadcast_topic']}发送毫秒时间戳",
            "topic": MQTT_CONFIG['local_broadcast_topic'],
            "interval": f"{MQTT_CONFIG['local_broadcast_interval']}秒",
        }
        
        if self.mqtt_gateway:
            attrs["mqtt_connected"] = self.mqtt_gateway.is_connected
            if self.is_on and self.mqtt_gateway.is_connected:
                attrs["status"] = "正在广播"
            elif self.is_on and not self.mqtt_gateway.is_connected:
                attrs["status"] = "等待MQTT连接"
            else:
                attrs["status"] = "未启用"
        else:
            attrs["status"] = "MQTT网关未初始化"
            
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 在实体加载时，依据当前配置同步网关状态
        if self.mqtt_gateway:
            self.mqtt_gateway.set_local_broadcast_enabled(self.is_on)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_option(CONF_MQTT_LOCAL_BROADCAST_ENABLED, True)
        if self.mqtt_gateway:
            self.mqtt_gateway.set_local_broadcast_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_option(CONF_MQTT_LOCAL_BROADCAST_ENABLED, False)
        if self.mqtt_gateway:
            self.mqtt_gateway.set_local_broadcast_enabled(False)

class ReplayEnabledSwitch(MqttBaseEntity, SwitchEntity):
    """回放模式开关"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "10 报文重放模式"
        self._attr_unique_id = f"{config_entry.entry_id}_replay_enabled"
        self._attr_icon = "mdi:script-text-outline"

    @property
    def is_on(self) -> bool:
        return self._get_option(CONF_REPLAY_ENABLED, False)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_option(CONF_REPLAY_ENABLED, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_option(CONF_REPLAY_ENABLED, False)


class StartCurtainFullRecordButton(MqttBaseEntity, ButtonEntity):
    """开始窗帘全量穷举录制"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "22 开始窗帘全量录制"
        self._attr_unique_id = f"{config_entry.entry_id}_start_curtain_record"
        self._attr_icon = "mdi:curtains"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await self.coordinator.replay_recorder.start_curtain_full()
        except Exception as err:
            _LOGGER.error(f"启动窗帘录制失败: {err}")


class StartACFullRecordButton(MqttBaseEntity, ButtonEntity):
    """开始空调全量录制"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "23 开始空调全量录制"
        self._attr_unique_id = f"{config_entry.entry_id}_start_ac_record"
        self._attr_icon = "mdi:air-conditioner"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await start_ac_full(self.coordinator.replay_recorder)
        except Exception as err:
            _LOGGER.error(f"启动空调录制失败: {err}")


class StartFloorFullRecordButton(MqttBaseEntity, ButtonEntity):
    """开始地暖全量录制"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "24 开始地暖全量录制"
        self._attr_unique_id = f"{config_entry.entry_id}_start_floor_record"
        self._attr_icon = "mdi:radiator"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await start_floor_full(self.coordinator.replay_recorder)
        except Exception as err:
            _LOGGER.error(f"启动地暖录制失败: {err}")


class StartFreshAirFullRecordButton(MqttBaseEntity, ButtonEntity):
    """开始新风全量录制"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "25 开始新风全量录制"
        self._attr_unique_id = f"{config_entry.entry_id}_start_freshair_record"
        self._attr_icon = "mdi:fan"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await start_freshair_full(self.coordinator.replay_recorder)
        except Exception as err:
            _LOGGER.error(f"启动新风录制失败: {err}")


class StartLightFullRecordButton(MqttBaseEntity, ButtonEntity):
    """开始灯具全量录制"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "26 开始灯具全量录制"
        self._attr_unique_id = f"{config_entry.entry_id}_start_light_record"
        self._attr_icon = "mdi:lightbulb-on"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await start_light_full(self.coordinator.replay_recorder)
        except Exception as err:
            _LOGGER.error(f"启动灯具录制失败: {err}")


class RecordStatusSensor(MqttBaseEntity, SensorEntity):
    """录制状态传感器"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "92 录制状态"
        self._attr_unique_id = f"{config_entry.entry_id}_record_status"
        self._attr_icon = "mdi:progress-clock"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # 订阅状态变化
        try:
            self.coordinator.replay_recorder.add_status_listener(self._schedule_update)
        except Exception:
            pass

    @property
    def native_value(self) -> str:
        running = self.coordinator.replay_recorder.is_running() if hasattr(self.coordinator, 'replay_recorder') else False
        return "运行中" if running else "空闲"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        try:
            return {
                "text": self.coordinator.replay_recorder.get_status_text(),
                **self.coordinator.replay_recorder.get_status(),
            }
        except Exception:
            return {}

    def _schedule_update(self) -> None:
        try:
            self.async_schedule_update_ha_state()
        except Exception:
            pass


class _BaseRecordDiagSensor(MqttBaseEntity, SensorEntity):
    """录制诊断传感器基类（挂到MQTT管理设备）"""
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        # 订阅状态变化
        try:
            self.coordinator.replay_recorder.add_status_listener(self._schedule_update)
        except Exception:
            pass
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _schedule_update(self) -> None:
        try:
            self.async_schedule_update_ha_state()
        except Exception:
            pass


class RecordCurrentDeviceSensor(_BaseRecordDiagSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "93 录制-当前设备"
        self._attr_unique_id = f"{config_entry.entry_id}_record_current_device"
        self._attr_icon = "mdi:tag"

    @property
    def native_value(self) -> str:
        try:
            s = self.coordinator.replay_recorder.get_status()
            return s.get("current_device") or "-"
        except Exception:
            return "-"


class RecordCurrentCommandSensor(_BaseRecordDiagSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "94 录制-请求指令"
        self._attr_unique_id = f"{config_entry.entry_id}_record_current_command"
        self._attr_icon = "mdi:code-tags"

    @property
    def native_value(self) -> str:
        try:
            s = self.coordinator.replay_recorder.get_status()
            fn = s.get("current_fn")
            fv = s.get("current_fv")
            idx = s.get("current_cmd_index", 0)
            total = s.get("current_cmd_total", 0)
            return f"fn={fn},fv={fv} ( {idx} / {total} )"
        except Exception:
            return "-"


class RecordCurrentStateSensor(_BaseRecordDiagSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "95 录制-当前状态"
        self._attr_unique_id = f"{config_entry.entry_id}_record_current_state"
        self._attr_icon = "mdi:information-outline"

    @property
    def native_value(self) -> str:
        try:
            s = self.coordinator.replay_recorder.get_status()
            return s.get("current_state") or "-"
        except Exception:
            return "-"


class RecordOverallProgressSensor(_BaseRecordDiagSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "96 录制-总进度"
        self._attr_unique_id = f"{config_entry.entry_id}_record_overall_progress"
        self._attr_icon = "mdi:progress-check"

    @property
    def native_value(self) -> str:
        try:
            s = self.coordinator.replay_recorder.get_status()
            processed = s.get("processed_devices", 0)
            total = s.get("total_devices", 0)
            return f"{processed} / {total}"
        except Exception:
            return "0 / 0"


class RecordFailedCommandsSensor(_BaseRecordDiagSensor):
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "97 录制-失败指令"
        self._attr_unique_id = f"{config_entry.entry_id}_record_failed_commands"
        self._attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self) -> str:
        try:
            failed_commands = self.coordinator.replay_recorder.replay.get_failed_commands()
            return str(len(failed_commands))
        except Exception:
            return "0"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        try:
            failed_commands = self.coordinator.replay_recorder.replay.get_failed_commands()
            return {
                "failed_commands": failed_commands,
                "count": len(failed_commands),
            }
        except Exception:
            return {"failed_commands": [], "count": 0}


class MqttApplyAndReconnectButton(MqttBaseEntity, ButtonEntity):
    """MQTT应用配置并重连按钮"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "20 MQTT应用配置并重连"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_apply_reconnect"
        self._attr_icon = "mdi:refresh"
        self._attr_available = True
        self._attr_entity_category = EntityCategory.CONFIG
    
    async def async_press(self) -> None:
        """按钮被按下"""
        if not self.mqtt_gateway:
            _LOGGER.error("MQTT网关未初始化")
            return
        
        _LOGGER.info("手动触发MQTT重连")
        
        # 重新配置MQTT参数
        host = self._get_option(CONF_MQTT_HOST)
        port = self._get_option(CONF_MQTT_PORT, MQTT_CONFIG["default_port"])
        username = self._get_option(CONF_MQTT_USERNAME)
        password = self._get_option(CONF_MQTT_PASSWORD)
        client_id = self._get_option(CONF_MQTT_CLIENT_ID)
        
        if not host:
            _LOGGER.error("MQTT服务器地址未配置")
            return
        
        self.mqtt_gateway.configure(host, port, username, password, client_id)
        
        # 执行重连
        success = await self.mqtt_gateway.reconnect()
        
        if success and self.state_sync_router:
            await self.state_sync_router.use_mqtt_mode()
            _LOGGER.info("MQTT重连成功，已切换到MQTT模式")
        elif not success and self.state_sync_router:
            await self.state_sync_router.use_polling_mode()
            _LOGGER.warning("MQTT重连失败，已切换到轮询模式")


class MqttResetStatsButton(MqttBaseEntity, ButtonEntity):
    """MQTT重置统计按钮"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "21 MQTT重置统计"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_reset_stats"
        self._attr_icon = "mdi:counter"
        self._attr_available = True
        self._attr_entity_category = EntityCategory.CONFIG
    
    async def async_press(self) -> None:
        """按钮被按下"""
        if self.mqtt_gateway:
            self.mqtt_gateway.reset_stats()
        
        if self.state_sync_router:
            self.state_sync_router.reset_stats()
        
        _LOGGER.info("MQTT统计信息已重置")


class MqttStatusSensor(MqttBaseEntity, SensorEntity):
    """MQTT状态传感器"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "90 MQTT状态"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_status"
        self._attr_icon = "mdi:information"
        # 传感器应该使用 DIAGNOSTIC 而不是 CONFIG
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
    
    @property
    def native_value(self) -> str:
        """返回传感器值"""
        if not self.mqtt_gateway:
            return "未初始化"
        
        if self.mqtt_gateway.is_connected:
            return "已连接"
        else:
            return "未连接"
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """返回额外状态属性"""
        if not self.mqtt_gateway or not self.state_sync_router:
            return {}
        
        mqtt_status = self.mqtt_gateway.get_status()
        router_status = self.state_sync_router.get_status()
        
        return {
            "current_mode": router_status.get("current_mode"),
            "mqtt_active": router_status.get("mqtt_active"),
            "polling_active": router_status.get("polling_active"),
            "fallback_running": router_status.get("fallback_running"),
            "optimistic_echo": router_status.get("optimistic_echo_enabled"),
            "last_message_time": mqtt_status.get("stats", {}).get("last_message_time"),
            "connection_attempts": mqtt_status.get("stats", {}).get("connection_attempts", 0),
            "messages_received": mqtt_status.get("stats", {}).get("messages_received", 0),
        }


class MqttStatsSensor(MqttBaseEntity, SensorEntity):
    """MQTT统计传感器"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "91 MQTT统计"
        self._attr_unique_id = f"{config_entry.entry_id}_mqtt_stats"
        self._attr_icon = "mdi:chart-line"
        # 传感器应该使用 DIAGNOSTIC 而不是 CONFIG
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
    
    @property
    def native_value(self) -> int:
        """返回传感器值（消息总数）"""
        if not self.mqtt_gateway:
            return 0
        
        status = self.mqtt_gateway.get_status()
        return status.get("stats", {}).get("messages_received", 0)
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """返回额外状态属性"""
        if not self.mqtt_gateway or not self.state_sync_router:
            return {}
        
        mqtt_stats = self.mqtt_gateway.get_status().get("stats", {})
        router_stats = self.state_sync_router.get_status().get("stats", {})
        
        return {
            "mqtt_messages_parsed": mqtt_stats.get("messages_parsed", 0),
            "mqtt_parse_errors": mqtt_stats.get("parse_errors", 0),
            "mqtt_reconnect_count": mqtt_stats.get("reconnect_count", 0),
            "router_mode_switches": router_stats.get("mode_switches", 0),
            "router_mqtt_updates": router_stats.get("mqtt_state_updates", 0),
            "router_polling_updates": router_stats.get("polling_state_updates", 0),
            "router_fallback_checks": router_stats.get("fallback_checks", 0),
        }


class SetEntityNameButton(MqttBaseEntity, ButtonEntity):
    """设置实体名称按钮"""
    
    def __init__(self, coordinator, config_entry: ConfigEntry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "30 设置实体名称"
        self._attr_unique_id = f"{config_entry.entry_id}_set_entity_name"
        self._attr_icon = "mdi:rename-box"
        self._attr_entity_category = EntityCategory.CONFIG
    
    async def async_press(self) -> None:
        """按钮被按下 - 显示设置实体名称的说明"""
        _LOGGER.info("=" * 60)
        _LOGGER.info("实体名称设置说明")
        _LOGGER.info("=" * 60)
        _LOGGER.info("插件已修复实体名称重置问题，现在实体名称只在初始化时设置一次。")
        _LOGGER.info("")
        _LOGGER.info("设置实体名称的方法：")
        _LOGGER.info("1. 在Home Assistant中，进入 设置 -> 设备与服务")
        _LOGGER.info("2. 找到对应的设备，点击设备名称")
        _LOGGER.info("3. 在设备详情页面，可以修改设备名称和实体名称")
        _LOGGER.info("4. 或者使用服务调用：")
        _LOGGER.info("   - 服务: hyqw_adapter.set_entity_name")
        _LOGGER.info("   - 参数: entity_id: 'light.xxx', name: '新名称'")
        _LOGGER.info("")
        _LOGGER.info("注意：修改后的名称不会被插件重置，可以安全使用。")
        _LOGGER.info("=" * 60)

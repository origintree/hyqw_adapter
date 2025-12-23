"""HYQW Adapter integration for Home Assistant.

Author: 花语前湾业主
Website: https://lbfnote.com
"""
import asyncio
import logging
from datetime import timedelta
import copy
from typing import Dict, List, Optional, Any

import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, PLATFORMS, POLLING_CONFIG, MQTT_CONFIG,
    CONF_MQTT_HOST, CONF_MQTT_PORT, CONF_MQTT_USERNAME, 
    CONF_MQTT_PASSWORD, CONF_MQTT_CLIENT_ID, CONF_MQTT_STARTUP_ENABLE,
    CONF_MQTT_OPTIMISTIC_ECHO, CONF_MQTT_FALLBACK_INTERVAL
)
from .const import CONF_REPLAY_ENABLED
from .area_manager import AreaManager
from .polling_bus import PollingBus
from .state_manager import StateManager
from .throttled_action_bus import ThrottledActionBus
from .mqtt_gateway import MqttGateway
from .replay_manager import ReplayManager
from .replay_recorder import ReplayRecorder
from .state_sync_router import StateSyncRouter
from .mqtt_entities import async_setup_mqtt_entities
from .temperature_sensor_binder import TemperatureSensorBinder

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HYQW Adapter from a config entry."""
    coordinator = HYQWAdapterCoordinator(hass, entry)
    
    # 如果有HAR数据，先设置到coordinator中
    if "har_devices" in entry.data:
        coordinator._har_data = {
            "home": entry.data.get("har_home_info", {}),
            "rooms": entry.data.get("har_rooms", []),
            "devices": entry.data.get("har_devices", []),
        }
    
    # 设置区域管理器
    if "room_area_mapping" in entry.data:
        coordinator.area_manager = AreaManager(hass)
        coordinator.area_manager.set_room_mapping(entry.data["room_area_mapping"])
        
        # 确保所有映射的区域都存在
        await coordinator.area_manager.ensure_areas_exist(entry.data["room_area_mapping"])
    
    # 设置温度传感器绑定管理器
    coordinator.temperature_binder = TemperatureSensorBinder(hass)
    
    await coordinator.async_config_entry_first_refresh()
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # 设置完成后，分配设备到区域
    if hasattr(coordinator, 'area_manager'):
        await _assign_devices_to_areas(coordinator)
    
    # 处理温度传感器绑定（延迟，以确保实体注册完成）
    if hasattr(coordinator, 'temperature_binder') and coordinator.data:
        async def delayed_binding():
            try:
                await asyncio.sleep(5)
                await _process_temperature_sensor_binding(coordinator)
            except Exception as err:
                _LOGGER.error(f"延迟处理温度传感器绑定失败: {err}")
        asyncio.create_task(delayed_binding())
    
    # 注册实体名称设置服务
    await _register_entity_naming_service(hass, coordinator)
    
    # 注册温度传感器绑定服务
    await _register_temperature_binding_service(hass, coordinator)
    
    return True


async def _assign_devices_to_areas(coordinator: "HYQWAdapterCoordinator") -> None:
    """Assign devices to their corresponding areas."""
    if not hasattr(coordinator, 'area_manager') or not coordinator.data:
        return
    
    devices = coordinator.data.get("devices", [])
    area_manager = coordinator.area_manager
    
    # 延迟执行以确保设备注册表已更新
    async def delayed_assignment():
        try:
            await asyncio.sleep(2)  # 等待设备注册完成
            
            for device in devices:
                try:
                    room_name = device.get("roomName")
                    device_id = str(device.get("deviceId"))
                    
                    if room_name and device_id:
                        success = await area_manager.assign_device_to_area(device_id, room_name)
                        if success:
                            _LOGGER.info(f"设备 {device.get('deviceName')} 已分配到区域")
                        else:
                            _LOGGER.debug(f"设备 {device.get('deviceName')} 区域分配跳过")
                except Exception as err:
                    _LOGGER.error(f"设备 {device.get('deviceName', 'Unknown')} 区域分配失败: {err}")
                    continue
        except Exception as err:
            _LOGGER.error(f"延迟区域分配任务失败: {err}")
    
    # 创建后台任务
    asyncio.create_task(delayed_assignment())


async def _process_temperature_sensor_binding(coordinator: "HYQWAdapterCoordinator") -> None:
    """处理温度传感器绑定."""
    try:
        # 延迟执行以确保所有实体都已创建
        await asyncio.sleep(3)
        
        devices = coordinator.data.get("devices", [])
        if not devices:
            _LOGGER.debug("没有设备数据，跳过温度传感器绑定")
            return
        
        # 处理温度传感器绑定
        coordinator.temperature_binder.process_all_devices(devices)
        
        # 记录绑定摘要
        binding_summary = coordinator.temperature_binder.get_binding_summary()
        if binding_summary:
            _LOGGER.info(f"温度传感器绑定完成，共绑定 {len(binding_summary)} 个地暖设备:")
            for floor_heating_name, sensor_name in binding_summary.items():
                _LOGGER.info(f"  - {floor_heating_name} -> {sensor_name}")
        else:
            _LOGGER.info("没有找到需要绑定的地暖设备")
            
    except Exception as err:
        _LOGGER.error(f"温度传感器绑定处理失败: {err}")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: HYQWAdapterCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    # 停止状态同步路由器（包含轮询总线和MQTT网关）
    if hasattr(coordinator, 'state_sync_router'):
        await coordinator.state_sync_router.stop()
    
    # 停止MQTT网关
    if hasattr(coordinator, 'mqtt_gateway'):
        await coordinator.mqtt_gateway.stop()
    
    # 停止节流操作总线
    await coordinator.throttled_action_bus.stop()
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok


class HYQWAdapterCoordinator(DataUpdateCoordinator):
    """Class to manage fetching HYQW Adapter data."""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.session = async_get_clientsession(hass)
        
        # 从配置中获取认证信息
        self.base_url = entry.data["base_url"]
        self.token = entry.data["token"]
        self.device_sn = entry.data["device_sn"]
        self.project_code = entry.data.get("project_code") or "SH-485-V22"  # fallback to default
        
        # 初始化轮询配置
        self.polling_config = POLLING_CONFIG.copy()
        
        # 初始化状态管理器
        self.state_manager = StateManager()
        
        # 初始化轮询总线（必须启用）
        self.polling_bus = PollingBus(
            config=self.polling_config,
            state_query_callback=self._query_device_states_via_bus
        )
        
        # 初始化MQTT网关
        self.mqtt_gateway = MqttGateway(
            project_code=self.project_code,
            device_sn=self.device_sn,
            state_callback=self._handle_mqtt_states,
            fallback_callback=self._fetch_device_states_raw
        )
        # 回放/录制管理（先创建manager，稍后异步加载文件，再创建recorder）
        self.replay_manager = ReplayManager(hass, self.project_code, self.device_sn, self.mqtt_gateway)
        # 异步加载存储
        hass.async_create_task(self.replay_manager.async_load())
        # 仅在manager存在时构造recorder（recorder内部不再在构造时访问coordinator.replay_recorder）
        self.replay_recorder = ReplayRecorder(self, self.replay_manager)
        
        # 初始化状态同步路由器
        self.state_sync_router = StateSyncRouter(
            state_manager=self.state_manager,
            polling_bus=self.polling_bus,
            update_callback=self._handle_router_update
        )
        
        # 设置兜底巡检回调
        self.state_sync_router.set_fallback_callback(self._fetch_device_states_raw)
        
        # 初始化节流操作总线
        self.throttled_action_bus = ThrottledActionBus(
            execute_callback=self._execute_device_control,
            polling_bus=self.polling_bus,
            router=self.state_sync_router,  # 传入路由器引用
            wait_time=0.2  # 异步等待200ms
        )
        
        # 兼容性：保留原有统计信息
        self._request_stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "last_request_time": None,
            "last_request_status": None,
            "device_states_requests": 0,
            "device_control_requests": 0,
        }
        
        # 其他属性
        self._last_update_time = None
        self._empty_states_count = 0
        
        # 异步锁，防止状态更新并发冲突
        self._update_lock = asyncio.Lock()
        
        # 设置更新间隔作为轮询总线的备用
        update_interval = timedelta(seconds=self.polling_config["long_polling_interval"])
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
    
    async def _async_update_data(self):
        """Fetch data from HYQW Adapter API."""
        # 如果有HAR数据，优先使用HAR数据（用于初始化）
        if hasattr(self, '_har_data') and self._har_data:
            # 初始化后，删除HAR数据，后续使用实时API
            har_data = self._har_data
            delattr(self, '_har_data')
            
            # 设置初始数据
            self.data = har_data
            
            # 初始化状态管理器
            if "devices" in har_data:
                self.state_manager.update_devices_info(har_data["devices"])
            
            # 初始化路由器配置
            await self._initialize_router_config()
            
            # 启动节流操作总线
            await self.throttled_action_bus.start()
            _LOGGER.info("节流操作总线已启动")
            
            return self.data
        
        # 初始化路由器配置（无HAR数据模式）
        await self._initialize_router_config()
        
        # 确保节流操作总线启动
        if not self.throttled_action_bus._is_running:
            await self.throttled_action_bus.start()
            _LOGGER.info("节流操作总线已启动（无HAR数据模式）")
        
        # 返回更新后的数据结构
        if self.data:
            updated_data = copy.deepcopy(self.data)
            return updated_data
        
        return {}
    
    async def _initialize_router_config(self) -> None:
        """初始化状态同步路由器配置"""
        try:
            # 从配置选项中读取MQTT设置
            startup_enable = self.entry.options.get(CONF_MQTT_STARTUP_ENABLE, MQTT_CONFIG["default_startup_enable"])
            optimistic_echo = self.entry.options.get(CONF_MQTT_OPTIMISTIC_ECHO, MQTT_CONFIG["default_optimistic_echo"])
            fallback_interval = self.entry.options.get(CONF_MQTT_FALLBACK_INTERVAL, MQTT_CONFIG["default_fallback_interval"])
            
            _LOGGER.info(f"MQTT启动配置检查 - startup_enable: {startup_enable}, 默认值: {MQTT_CONFIG['default_startup_enable']}")
            
            # 配置路由器
            self.state_sync_router.set_optimistic_echo(optimistic_echo)
            self.state_sync_router.configure_fallback(fallback_interval)
            
            # 决定启动模式
            if startup_enable:
                # 尝试启动MQTT模式
                host = self.entry.options.get(CONF_MQTT_HOST)
                if host:
                    port = self.entry.options.get(CONF_MQTT_PORT, MQTT_CONFIG["default_port"])
                    username = self.entry.options.get(CONF_MQTT_USERNAME)
                    password = self.entry.options.get(CONF_MQTT_PASSWORD)
                    client_id = self.entry.options.get(CONF_MQTT_CLIENT_ID)
                    
                    _LOGGER.info(f"初始化MQTT配置 - 主机:{host}, 端口:{port}, 用户名:{username}, 客户端ID:'{client_id}'")
                    self.mqtt_gateway.configure(host, port, username, password, client_id)
                    
                    success = await self.mqtt_gateway.start()
                    if success:
                        await self.state_sync_router.use_mqtt_mode()
                        _LOGGER.info("启动时启用MQTT模式成功")
                        return
                    else:
                        _LOGGER.warning("启动时启用MQTT模式失败，回退到轮询模式")
                else:
                    _LOGGER.warning("启动时启用MQTT但未配置服务器地址，使用轮询模式")
            
            # 启动轮询模式
            await self.state_sync_router.use_polling_mode()
            _LOGGER.info("启动时使用轮询模式")
            
        except Exception as err:
            _LOGGER.error(f"初始化路由器配置失败: {err}")
            # 出错时默认启用轮询模式
            await self.state_sync_router.use_polling_mode()
        
        # 启动时执行一次兜底巡检同步所有状态
        await self._perform_startup_fallback_check()
    
    async def _perform_startup_fallback_check(self) -> None:
        """启动时执行一次兜底巡检同步所有状态"""
        try:
            _LOGGER.info("执行启动时兜底巡检同步")
            
            # 获取当前所有设备状态
            states_data = await self._fetch_device_states_raw()
            if states_data:
                # 通过路由器处理兜底巡检状态数据
                await self.state_sync_router.handle_fallback_states(states_data)
                _LOGGER.info(f"启动时兜底巡检完成，同步了{len(states_data)}个设备状态")
            else:
                _LOGGER.warning("启动时兜底巡检 - 获取状态数据为空")
                
        except Exception as err:
            _LOGGER.error(f"启动时兜底巡检失败: {err}")
    
    async def _handle_mqtt_states(self, states: List[Dict]) -> None:
        """处理MQTT状态数据回调"""
        try:
            await self.state_sync_router.handle_mqtt_states(states)
        except Exception as err:
            _LOGGER.error(f"处理MQTT状态数据失败: {err}")
    
    async def _handle_router_update(self, changes: Dict) -> None:
        """处理路由器状态更新回调"""
        try:
            # 使用锁保护，避免与其他状态更新冲突
            async with self._update_lock:
                updated_data = copy.deepcopy(self.data)
                if updated_data and "devices" in updated_data:
                    for device in updated_data["devices"]:
                        si = device.get("si")
                        if si in changes.get("changed_devices", set()):
                            device_states = self.state_manager.get_device_state(si)
                            if device_states:
                                device["current_states"] = device_states
                
                # 在主线程调度 set_updated_data，避免从MQTT线程触发
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(lambda: self.async_set_updated_data(updated_data))
        except Exception as err:
            _LOGGER.error(f"处理路由器更新失败: {err}")

    async def _fetch_profile(self) -> None:
        """Fetch profile once to 'warm up' backend when states keep empty."""
        try:
            async with async_timeout.timeout(10):
                url = f"{self.base_url}/smart-api/api/home/profile"
                headers = {
                    "Authorization": f"mob;{self.token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "Accept-Language": "zh-Hans-HK;q=1",
                    "User-Agent": "GT_SmartHome/1.0 (iPhone; iOS 26.0; Scale/3.00)",
                    "Accept-Encoding": "gzip, deflate",
                }
                async with self.session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        prof = await resp.json()
                        _LOGGER.debug(f"Profile warm-up success: code={prof.get('code')}")
                    else:
                        _LOGGER.debug(f"Profile warm-up HTTP {resp.status}")
        except Exception as err:
            _LOGGER.debug(f"Profile warm-up failed: {err}")
    
    async def _query_device_states_via_bus(self):
        """轮询总线回调：查询设备状态"""
        try:
            states_data = await self._fetch_device_states_raw()
            if states_data:
                # 通过路由器处理轮询状态数据
                await self.state_sync_router.handle_polling_states(states_data)
            else:
                _LOGGER.warning("轮询总线查询 - 获取状态数据为空")
                
        except Exception as err:
            _LOGGER.error(f"轮询总线状态查询失败: {err}", exc_info=True)
    
    
    async def _fetch_device_states_raw(self) -> Optional[List[Dict]]:
        """获取原始设备状态数据"""
        # 第一次调用时验证配置
        if self._last_update_time is None:
            self.validate_config_with_har()
        
        # 更新请求统计
        import time
        self._request_stats["total_requests"] += 1
        self._request_stats["device_states_requests"] += 1
        self._request_stats["last_request_time"] = time.time()
        
        try:
            async with async_timeout.timeout(10):
                url = f"{self.base_url}/smart-api/api/device/states"
                headers = {
                    "Authorization": f"mob;{self.token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "Accept-Language": "zh-Hans-HK;q=1",
                    "User-Agent": "GT_SmartHome/1.0 (iPhone; iOS 26.0; Scale/3.00)",
                    "Accept-Encoding": "gzip, deflate",
                }
                payload = {
                    "projectCode": self.project_code,
                    "deviceSn": self.device_sn,
                }
                
                _LOGGER.debug(f"Requesting device states from {url}")
                
                async with self.session.post(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("code") == 0:
                            self._request_stats["successful_requests"] += 1
                            self._request_stats["last_request_status"] = "success"
                            states_data = data.get("result", [])

                            if not states_data:
                                _LOGGER.warning("States API返回空结果")
                                # 启动期重试机制
                                if self._last_update_time is None:
                                    for attempt in range(2):
                                        await asyncio.sleep(0.5)
                                        async with self.session.post(url, headers=headers, json=payload) as retry_resp:
                                            if retry_resp.status == 200:
                                                retry_data = await retry_resp.json()
                                                if retry_data.get("code") == 0 and retry_data.get("result"):
                                                    states_data = retry_data.get("result")
                                                    _LOGGER.debug(f"重试成功，获取到{len(states_data)}个状态")
                                                    break

                            if states_data:
                                self._last_update_time = asyncio.get_event_loop().time()
                                self._empty_states_count = 0
                            else:
                                self._empty_states_count += 1
                                # 连续空结果时尝试刷新profile
                                if self._empty_states_count in (3, 10):
                                    try:
                                        await self._fetch_profile()
                                    except Exception:
                                        pass

                            return states_data
                        else:
                            self._request_stats["failed_requests"] += 1
                            self._request_stats["last_request_status"] = f"api_error_{data.get('code')}"
                            _LOGGER.error(f"States API错误: {data.get('message')}")
                    else:
                        self._request_stats["failed_requests"] += 1
                        self._request_stats["last_request_status"] = f"http_error_{response.status}"
                        _LOGGER.error(f"HTTP错误: {response.status}")
                        
        except asyncio.TimeoutError:
            self._request_stats["failed_requests"] += 1
            self._request_stats["last_request_status"] = "timeout"
            _LOGGER.warning("状态查询超时")
        except Exception as err:
            self._request_stats["failed_requests"] += 1
            self._request_stats["last_request_status"] = f"exception_{type(err).__name__}"
            _LOGGER.error(f"状态查询异常: {err}")
        
        return None
    
    def _process_device_states(self, states_data: list):
        """处理设备状态数据 (已弃用，现使用StateManager)
        
        此方法保留仅为兼容性考虑，现在推荐使用StateManager。
        """
        _LOGGER.warning("_process_device_states已弃用，请使用StateManager")
        
        # 简化的处理逻辑，主要用于兼容性
        if states_data:
            has_changes, changes = self.state_manager.process_state_update(states_data)
            if has_changes:
                _LOGGER.info(f"兼容模式状态更新 - {len(changes.get('changed_devices', set()))}个设备有变化")
    
    def _update_device_properties(self, device: dict):
        """Update device properties based on current states."""
        current_states = device.get("current_states", {})
        
        # 更新通用状态
        if 1 in current_states:  # fn=1 通常是开关状态
            device["is_on"] = current_states[1]["fv"] == 1
        
        # 根据设备类型更新特定属性
        type_id = device.get("typeId")
        
        if type_id == 8:  # 灯具
            if 2 in current_states:  # fn=2 通常是亮度
                device["brightness"] = current_states[2]["fv"]
        
        elif type_id == 12:  # 空调
            if 2 in current_states:  # fn=2 温度设置
                temp_value = current_states[2]["fv"]
                if temp_value and temp_value > 100:
                    device["target_temperature"] = temp_value / 10
                else:
                    device["target_temperature"] = temp_value
            if 3 in current_states:  # fn=3 模式设置
                device["hvac_mode"] = current_states[3]["fv"]
            if 4 in current_states:  # fn=4 风力设置
                device["fan_speed"] = current_states[4]["fv"]
            if 5 in current_states:  # fn=5 当前温度
                current_temp_value = current_states[5]["fv"]
                if current_temp_value and current_temp_value > 100:
                    device["current_temperature"] = current_temp_value / 10
                else:
                    device["current_temperature"] = current_temp_value
        
        elif type_id == 16:  # 地暖
            if 2 in current_states:  # fn=2 温度设置
                temp_value = current_states[2]["fv"]
                if temp_value and temp_value > 100:
                    device["target_temperature"] = temp_value / 10
                else:
                    device["target_temperature"] = temp_value
        
        elif type_id == 36:  # 新风
            if 3 in current_states:  # fn=3 风力设置
                device["fan_speed"] = current_states[3]["fv"]
        
        elif type_id == 14:  # 窗帘
            if 2 in current_states:  # fn=2 是位置控制
                device["position"] = current_states[2]["fv"]
            
            # 根据开关状态和位置更新窗帘状态
            if 1 in current_states:
                control_state = current_states[1]["fv"]
                if control_state == 2:  # 停止
                    device["moving_state"] = "stopped"
                elif control_state == 1:  # 打开
                    device["moving_state"] = "opening"
                elif control_state == 0:  # 关闭
                    device["moving_state"] = "closing"
    
    async def _execute_device_control(self, device_id: int, st: int, si: int, fn: int, fv: int = 0):
        """执行设备控制操作（节流总线回调函数）"""
        # 更新请求统计
        import time
        self._request_stats["total_requests"] += 1
        self._request_stats["device_control_requests"] += 1
        self._request_stats["last_request_time"] = time.time()
        
        try:
            url = f"{self.base_url}/smart-api/api/device/control"
            headers = {
                "Authorization": f"mob;{self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "deviceSn": self.device_sn,
                "st": st,
                "si": si,
                "fn": fn,
                "fv": fv,
                "projectCode": self.project_code,
            }
            
            _LOGGER.info(f"控制设备: si={si}, fn={fn}, fv={fv}")
            
            async with async_timeout.timeout(10):
                async with self.session.post(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("code") == 0:
                            self._request_stats["successful_requests"] += 1
                            self._request_stats["last_request_status"] = "success"
                            _LOGGER.info(f"设备控制成功: {data.get('message')}")
                            
                            # 根据路由器状态决定乐观回显策略
                            should_optimistic_echo = False
                            should_trigger_polling = True
                            
                            if hasattr(self, 'state_sync_router'):
                                should_optimistic_echo = self.state_sync_router.is_optimistic_echo_enabled()
                                should_trigger_polling = self.state_sync_router.using_polling()
                            
                            # 乐观回显：立即更新状态管理器中的设备状态
                            if should_optimistic_echo:
                                self.state_manager.force_update_device(si, fn, fv)
                                _LOGGER.debug(f"乐观回显已更新设备状态: si={si}, fn={fn}, fv={fv}")
                            
                            # 轮询模式下触发短轮询
                            if should_trigger_polling:
                                self.polling_bus.set_short_polling_mode()
                                _LOGGER.info(f"已触发短轮询模式，持续{self.polling_config['short_polling_duration']}秒")
                            else:
                                _LOGGER.debug("MQTT模式下跳过短轮询触发")
                            
                            return True
                        else:
                            self._request_stats["failed_requests"] += 1
                            self._request_stats["last_request_status"] = f"api_error_{data.get('code')}"
                            _LOGGER.error(f"设备控制失败: {data.get('message')}")
                            return False
                    else:
                        self._request_stats["failed_requests"] += 1
                        self._request_stats["last_request_status"] = f"http_error_{response.status}"
                        _LOGGER.error(f"HTTP错误 - 设备控制: {response.status}")
                        return False
                        
        except Exception as err:
            self._request_stats["failed_requests"] += 1
            self._request_stats["last_request_status"] = f"exception_{type(err).__name__}"
            _LOGGER.error(f"设备控制异常: {err}")
            return False
    
    async def async_control_device_direct(self, device_id: int, st: int, si: int, fn: int, fv: int = 0, entity_id: str = None):
        """直接控制设备（不通过节流总线，适用于窗帘、气候设备）
        
        直接执行控制命令并立即更新状态，但共用轮询总线同步状态
        """
        # 回放优先
        if self.entry.options.get(CONF_REPLAY_ENABLED, False):
            if self.replay_find_and_send(si, fn, fv):
                _LOGGER.info(f"回放命中: si={si}, fn={fn}, fv={fv}")
                return True
        if entity_id is None:
            entity_id = f"device_{device_id}"
        
        try:
            # 根据路由器状态决定策略
            should_optimistic_echo = False
            should_trigger_polling = True
            
            if hasattr(self, 'state_sync_router'):
                should_optimistic_echo = self.state_sync_router.is_optimistic_echo_enabled()
                should_trigger_polling = self.state_sync_router.using_polling()
            
            # 轮询模式下触发短轮询
            if should_trigger_polling:
                self.polling_bus.set_short_polling_mode()
            
            # 直接调用设备控制API
            success = await self._execute_device_control(device_id, st, si, fn, fv)
            
            if success:
                # 乐观回显：立即更新本地状态
                if should_optimistic_echo:
                    self.state_manager.force_update_device(si, fn, fv)
                    # 触发数据更新通知
                    self.async_set_updated_data(self.data)
                
                _LOGGER.info(f"设备直接控制成功: {entity_id} (fn={fn}, fv={fv})")
            else:
                _LOGGER.warning(f"设备直接控制失败: {entity_id}")
            
            return success
            
        except Exception as err:
            _LOGGER.error(f"设备直接控制异常: {entity_id} - {err}")
            return False
    
    async def async_control_device_immediate(self, device_id: int, st: int, si: int, fn: int, fv: int = 0, entity_id: str = None):
        """立即控制设备（专为气候设备和窗帘设计）
        
        特点：
        1. 不经过任何节流或延迟逻辑
        2. 立即发送控制指令
        3. 立即更新本地状态 
        4. 使用轮询总线进行状态同步
        """
        # 回放优先
        if self.entry.options.get(CONF_REPLAY_ENABLED, False):
            if self.replay_find_and_send(si, fn, fv):
                _LOGGER.info(f"回放命中: si={si}, fn={fn}, fv={fv}")
                return True
        if entity_id is None:
            entity_id = f"device_{device_id}"
        
        # 更新请求统计
        import time
        self._request_stats["total_requests"] += 1
        self._request_stats["device_control_requests"] += 1
        self._request_stats["last_request_time"] = time.time()
        
        try:
            url = f"{self.base_url}/smart-api/api/device/control"
            headers = {
                "Authorization": f"mob;{self.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "deviceSn": self.device_sn,
                "st": st,
                "si": si,
                "fn": fn,
                "fv": fv,
                "projectCode": self.project_code,
            }
            
            _LOGGER.info(f"立即控制设备: {entity_id} (si={si}, fn={fn}, fv={fv})")
            
            # 立即发送控制指令，不等待任何延迟
            async with async_timeout.timeout(10):
                async with self.session.post(url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("code") == 0:
                            self._request_stats["successful_requests"] += 1
                            self._request_stats["last_request_status"] = "success"
                            _LOGGER.info(f"设备立即控制成功: {entity_id} - {data.get('message')}")
                            
                            # 根据路由器状态决定策略
                            should_optimistic_echo = False
                            should_trigger_polling = True
                            
                            if hasattr(self, 'state_sync_router'):
                                should_optimistic_echo = self.state_sync_router.is_optimistic_echo_enabled()
                                should_trigger_polling = self.state_sync_router.using_polling()
                            
                            # 乐观回显：立即更新状态管理器中的设备状态
                            if should_optimistic_echo:
                                self.state_manager.force_update_device(si, fn, fv)
                                # 立即触发数据更新通知
                                self.async_set_updated_data(self.data)
                            
                            # 轮询模式下触发短轮询
                            if should_trigger_polling:
                                self.polling_bus.set_short_polling_mode()
                                _LOGGER.debug(f"已触发短轮询同步: {entity_id}")
                            else:
                                _LOGGER.debug(f"MQTT模式下跳过短轮询: {entity_id}")
                            
                            return True
                        else:
                            self._request_stats["failed_requests"] += 1
                            self._request_stats["last_request_status"] = f"api_error_{data.get('code')}"
                            _LOGGER.error(f"设备立即控制失败: {entity_id} - {data.get('message')}")
                            return False
                    else:
                        self._request_stats["failed_requests"] += 1
                        self._request_stats["last_request_status"] = f"http_error_{response.status}"
                        _LOGGER.error(f"HTTP错误 - 设备立即控制: {entity_id} - {response.status}")
                        return False
                        
        except Exception as err:
            self._request_stats["failed_requests"] += 1
            self._request_stats["last_request_status"] = f"exception_{type(err).__name__}"
            _LOGGER.error(f"设备立即控制异常: {entity_id} - {err}")
            return False
    
    async def async_control_device(self, device_id: int, st: int, si: int, fn: int, fv: int = 0, entity_id: str = None):
        """通过节流总线控制设备"""
        # 回放优先
        if self.entry.options.get(CONF_REPLAY_ENABLED, False):
            if self.replay_find_and_send(si, fn, fv):
                _LOGGER.info(f"回放命中: si={si}, fn={fn}, fv={fv}")
                return True
        if entity_id is None:
            entity_id = f"device_{device_id}"
        
        try:
            # 通过节流总线提交操作
            action_id = await self.throttled_action_bus.submit_action(
                device_id=device_id,
                st=st,
                si=si,
                fn=fn,
                fv=fv,
                entity_id=entity_id
            )
            
            _LOGGER.info(f"设备控制操作已提交到队列: {entity_id} (action_id={action_id})")
            return True
            
        except ValueError as err:
            _LOGGER.warning(f"设备控制请求被拒绝: {err}")
            return False
        except Exception as err:
            _LOGGER.error(f"提交设备控制操作失败: {err}")
            return False
    
    async def async_request_state_update(self):
        """Request an immediate state update (for manual refresh)."""
        # 使用锁保护，避免与其他状态更新冲突
        async with self._update_lock:
            await self.async_refresh()

    # ===== 回放相关API =====
    def replay_find_and_send(self, si: int, fn: int, fv: int) -> bool:
        """查找回放样本并通过MQTT发布，成功返回True。"""
        cmd = self.replay_manager.find_command(si, fn, fv)
        if not cmd:
            return False
        return self.replay_manager.replay(cmd["topic"], cmd["payload_hex"], qos=cmd.get("qos", 0))
    
    def validate_config_with_har(self):
        """Validate current config against expected values from HAR data."""
        expected_device_sn = "FB485V222024110500000377"
        expected_project_code = "SH-485-V22"
        expected_base_url = "http://gt.jianweisoftware.com"
        
        issues = []
        auto_fixed = []
        
        if self.device_sn != expected_device_sn:
            issues.append(f"DeviceSN mismatch: got '{self.device_sn}', expected '{expected_device_sn}'")
        
        # Auto-fix projectCode if it's None or wrong
        if self.project_code != expected_project_code:
            if self.project_code is None:
                _LOGGER.warning(f"ProjectCode was None, auto-fixing to '{expected_project_code}'")
                self.project_code = expected_project_code
                auto_fixed.append(f"ProjectCode: None -> '{expected_project_code}'")
            else:
                issues.append(f"ProjectCode mismatch: got '{self.project_code}', expected '{expected_project_code}'")
        
        if self.base_url != expected_base_url:
            issues.append(f"BaseURL mismatch: got '{self.base_url}', expected '{expected_base_url}'")
        
        if auto_fixed:
            _LOGGER.info(f"Auto-fixed configuration: {'; '.join(auto_fixed)}")
        
        if issues:
            _LOGGER.error(f"Configuration validation failed: {'; '.join(issues)}")
            return False
        else:
            _LOGGER.info("Configuration validation passed")
            return True
    
    def get_device_state(self, si: int, fn: int = None):
        """Get specific device state."""
        if si in self._device_states:
            if fn is None:
                return self._device_states[si]
            return self._device_states[si].get(fn)
    
    def get_request_stats(self):
        """Get API request statistics."""
        return self._request_stats.copy()
    
    # ===== 轮询总线管理方法 =====
    
    def get_polling_status(self) -> Dict[str, Any]:
        """获取轮询总线状态"""
        status = {
            "polling_bus_enabled": True,
            "config": self.polling_config.copy(),
            "state_manager_stats": self.state_manager.get_stats(),
            "polling_bus_status": self.polling_bus.get_status(),
        }
        
        return status
    
    async def async_stop_polling_bus(self) -> None:
        """停止轮询总线（保持兼容性）"""
        if hasattr(self, 'state_sync_router'):
            await self.state_sync_router.stop()
            _LOGGER.info("状态同步路由器已停止")
        else:
            await self.polling_bus.stop()
            _LOGGER.info("轮询总线已停止")
    
    def get_device_state(self, si: int, fn: int = None):
        """Get specific device state."""
        return self.state_manager.get_device_state(si, fn)
    
    # ===== 节流操作总线管理方法 =====
    
    def is_entity_occupied(self, entity_id: str) -> bool:
        """检查实体是否正在被节流总线处理"""
        return self.throttled_action_bus.is_device_occupied(entity_id)
    
    def get_entity_action_status(self, entity_id: str) -> Optional[str]:
        """获取实体的操作状态"""
        return self.throttled_action_bus.get_device_action_status(entity_id)
    
    def get_throttled_action_info(self) -> Dict[str, Any]:
        """获取节流操作总线信息"""
        return self.throttled_action_bus.get_queue_info()


async def _register_entity_naming_service(hass: HomeAssistant, coordinator: "HYQWAdapterCoordinator") -> None:
    """注册实体名称设置服务"""
    
    async def set_entity_name(call):
        """设置实体名称的服务"""
        entity_id = call.data.get("entity_id")
        name = call.data.get("name")
        
        if not entity_id or not name:
            _LOGGER.error("set_entity_name服务需要entity_id和name参数")
            return
        
        # 获取实体
        entity = hass.states.get(entity_id)
        if not entity:
            _LOGGER.error(f"未找到实体: {entity_id}")
            return
        
        # 检查是否是HYQW Adapter的实体
        if not entity_id.startswith(f"{DOMAIN}."):
            _LOGGER.error(f"实体 {entity_id} 不是HYQW Adapter的实体")
            return
        
        # 通过设备注册表更新实体名称
        device_registry = hass.helpers.device_registry.async_get(hass)
        entity_registry = hass.helpers.entity_registry.async_get(hass)
        
        # 获取实体注册信息
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry:
            _LOGGER.error(f"未找到实体注册信息: {entity_id}")
            return
        
        # 更新实体名称
        entity_registry.async_update_entity(entity_id, name=name)
        
        # 如果是主实体，也更新设备名称
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry:
            # 检查是否所有实体都是同一个设备
            device_entities = entity_registry.entities.get_entities_for_device(entity_entry.device_id)
            if len(device_entities) == 1:
                # 只有一个实体，更新设备名称
                device_registry.async_update_device(entity_entry.device_id, name=name)
                _LOGGER.info(f"已更新设备名称: {entity_id} -> {name}")
            else:
                _LOGGER.info(f"设备有多个实体，仅更新实体名称: {entity_id} -> {name}")
        else:
            _LOGGER.info(f"已更新实体名称: {entity_id} -> {name}")
    
    # 注册服务
    hass.services.async_register(
        DOMAIN,
        "set_entity_name",
        set_entity_name,
        schema=vol.Schema({
            vol.Required("entity_id"): str,
            vol.Required("name"): str,
        })
    )


async def _register_temperature_binding_service(hass: HomeAssistant, coordinator: "HYQWAdapterCoordinator") -> None:
    """注册温度传感器绑定服务"""
    
    async def process_temperature_binding(call):
        """手动处理温度传感器绑定的服务"""
        try:
            if not hasattr(coordinator, 'temperature_binder'):
                _LOGGER.error("温度传感器绑定管理器未初始化")
                return
            
            if not coordinator.data:
                _LOGGER.error("没有设备数据，无法处理温度传感器绑定")
                return
            
            devices = coordinator.data.get("devices", [])
            if not devices:
                _LOGGER.error("设备列表为空")
                return
            
            # 清除现有绑定
            coordinator.temperature_binder.clear_bindings()
            
            # 重新处理绑定
            coordinator.temperature_binder.process_all_devices(devices)
            
            # 记录绑定摘要
            binding_summary = coordinator.temperature_binder.get_binding_summary()
            if binding_summary:
                _LOGGER.info(f"手动温度传感器绑定完成，共绑定 {len(binding_summary)} 个地暖设备:")
                for floor_heating_name, sensor_name in binding_summary.items():
                    _LOGGER.info(f"  - {floor_heating_name} -> {sensor_name}")
            else:
                _LOGGER.info("没有找到需要绑定的地暖设备")
                
        except Exception as err:
            _LOGGER.error(f"手动温度传感器绑定失败: {err}")
    
    async def get_binding_status(call):
        """获取温度传感器绑定状态的服务"""
        try:
            if not hasattr(coordinator, 'temperature_binder'):
                _LOGGER.error("温度传感器绑定管理器未初始化")
                return
            
            binding_summary = coordinator.temperature_binder.get_binding_summary()
            if binding_summary:
                _LOGGER.info("当前温度传感器绑定状态:")
                for floor_heating_name, sensor_name in binding_summary.items():
                    _LOGGER.info(f"  - {floor_heating_name} -> {sensor_name}")
            else:
                _LOGGER.info("当前没有温度传感器绑定")
                
        except Exception as err:
            _LOGGER.error(f"获取温度传感器绑定状态失败: {err}")
    
    async def debug_entities(call):
        """调试实体信息的服务"""
        try:
            if not coordinator.data:
                _LOGGER.error("没有设备数据")
                return
            
            devices = coordinator.data.get("devices", [])
            from homeassistant.helpers import entity_registry
            entity_reg = entity_registry.async_get(hass)
            
            _LOGGER.info("=== 设备调试信息 ===")
            for device in devices:
                device_id = str(device.get("deviceId"))
                device_name = device.get("deviceName", "未知设备")
                device_type = device.get("typeId")
                room_name = device.get("roomName", "未知房间")
                
                _LOGGER.info(f"设备: {device_name} (ID: {device_id}, 类型: {device_type}, 房间: {room_name})")
                
                # 检查climate实体
                climate_entity_id = f"climate.hyqw_adapter_{device_id}"
                climate_entity = entity_reg.async_get(climate_entity_id)
                if climate_entity:
                    _LOGGER.info(f"  ✓ Climate实体存在: {climate_entity_id}")
                else:
                    _LOGGER.info(f"  ✗ Climate实体不存在: {climate_entity_id}")
                
                # 检查温度传感器实体（仅空调）
                if device_type == 12:
                    temp_entity_id = f"sensor.hyqw_adapter_{device_id}_temperature"
                    temp_entity = entity_reg.async_get(temp_entity_id)
                    if temp_entity:
                        _LOGGER.info(f"  ✓ 温度传感器实体存在: {temp_entity_id}")
                    else:
                        _LOGGER.info(f"  ✗ 温度传感器实体不存在: {temp_entity_id}")
                
        except Exception as err:
            _LOGGER.error(f"调试实体信息失败: {err}")
    
    # 注册服务
    try:
        hass.services.async_register(
            DOMAIN,
            "process_temperature_binding",
            process_temperature_binding
        )
        
        hass.services.async_register(
            DOMAIN,
            "get_binding_status",
            get_binding_status
        )
        
        hass.services.async_register(
            DOMAIN,
            "debug_entities",
            debug_entities
        )
        
        _LOGGER.info("已注册温度传感器绑定服务: hyqw_adapter.process_temperature_binding, hyqw_adapter.get_binding_status, hyqw_adapter.debug_entities")
    except Exception as err:
        _LOGGER.error(f"注册温度传感器绑定服务失败: {err}")

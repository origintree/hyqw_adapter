"""MQTT网关 - MQTT Gateway for HYQW Adapter"""
import asyncio
import json
import logging
import time
import threading
from typing import Callable, Dict, List, Optional, Any
from datetime import datetime

import paho.mqtt.client as mqtt

from .const import MQTT_CONFIG

_LOGGER = logging.getLogger(__name__)


class MqttGateway:
    """MQTT网关类
    
    负责：
    1. MQTT连接管理与重连
    2. 主题订阅与消息解析
    3. 状态数据标准化输出
    4. 连接状态监控与统计
    """
    
    def __init__(self, project_code: str, device_sn: str, state_callback: Callable[[List[Dict]], None], 
                 fallback_callback: Callable = None):
        """初始化MQTT网关
        
        Args:
            project_code: 项目代码，如 SH-485-V22
            device_sn: 设备序列号，如 FB485V222024110500000377
            state_callback: 状态更新回调函数，接收标准化状态数据列表
            fallback_callback: 兜底巡检回调函数，用于获取云服务器状态
        """
        self.project_code = project_code
        self.device_sn = device_sn
        self.state_callback = state_callback
        self.fallback_callback = fallback_callback
        
        # 连接配置
        self.host: Optional[str] = None
        self.port: int = MQTT_CONFIG["default_port"]
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.client_id: Optional[str] = None
        self.keepalive: int = MQTT_CONFIG["default_keepalive"]
        
        # 连接状态
        self.is_connected = False
        self.client: Optional[mqtt.Client] = None
        self.connection_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._should_reconnect = True
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        
        # 统计信息
        self.stats = {
            "connection_attempts": 0,
            "successful_connections": 0,
            "connection_failures": 0,
            "messages_received": 0,
            "messages_parsed": 0,
            "parse_errors": 0,
            "last_message_time": None,
            "last_error": None,
            "uptime_start": None,
            "reconnect_count": 0,
        }
        
        # 订阅主题（默认仅订阅状态上报）
        self.topics = [
            f"FMQ/{project_code}/{device_sn}/UPLOAD/2002"
        ]
        # 录制模式下需要临时订阅的下行控制主题
        self._down_topic = f"FMQ/{project_code}/{device_sn}/DOWN/2001"
        self._recording_downstream = False
        self._down_message_callback = None  # type: Optional[Callable[[str, bytes], None]]
        
        # 本地广播相关
        self._local_broadcast_enabled: bool = False
        self._local_broadcast_task: Optional[asyncio.Task] = None
        self._local_broadcast_interval_seconds: int = MQTT_CONFIG["local_broadcast_interval"]
        self._local_broadcast_topic: str = MQTT_CONFIG["local_broadcast_topic"]
        
        _LOGGER.info(f"MQTT网关初始化完成 - 项目:{project_code}, 设备:{device_sn}")
    
    def configure(self, host: str, port: int = None, username: str = None, 
                  password: str = None, client_id: str = None) -> None:
        """配置MQTT连接参数
        
        Args:
            host: MQTT服务器地址
            port: MQTT端口，默认1883
            username: 用户名
            password: 密码
            client_id: 客户端ID
        """
        # 处理主机地址，如果包含端口号则分离
        if ':' in host and not host.startswith('['):  # 不是IPv6地址
            host_parts = host.split(':')
            if len(host_parts) == 2:
                self.host = host_parts[0]
                # 如果配置中没有指定端口，使用主机地址中的端口
                if port is None:
                    try:
                        self.port = int(host_parts[1])
                    except ValueError:
                        self.port = MQTT_CONFIG["default_port"]
                        _LOGGER.warning(f"主机地址中的端口号无效: {host_parts[1]}，使用默认端口: {self.port}")
                else:
                    self.port = port
            else:
                self.host = host
                self.port = port or MQTT_CONFIG["default_port"]
        else:
            self.host = host
            self.port = port or MQTT_CONFIG["default_port"]
        
        self.username = username
        self.password = password
        self.client_id = client_id or f"hyqw_adapter_{self.device_sn}_{int(time.time())}"
        
        _LOGGER.info(f"MQTT配置已更新 - 服务器:{self.host}:{self.port}, 客户端ID:{self.client_id}")
    
    async def _cleanup_connection(self) -> None:
        """清理之前的连接"""
        if self.client:
            try:
                _LOGGER.debug("清理之前的MQTT连接")
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as err:
                _LOGGER.debug(f"清理MQTT连接时出现异常: {err}")
            finally:
                self.client = None
        
        self.is_connected = False
    
    async def start(self) -> bool:
        """启动MQTT连接
        
        Returns:
            bool: 是否成功启动连接
        """
        if not self.host:
            _LOGGER.error("MQTT服务器地址未配置")
            self.stats["last_error"] = "服务器地址未配置"
            return False
        
        if self.is_connected:
            _LOGGER.info("MQTT已连接，跳过启动")
            return True
        
        # 先清理之前的连接
        await self._cleanup_connection()
        
        try:
            _LOGGER.info(f"启动MQTT连接 - {self.host}:{self.port}, 客户端ID: {self.client_id}")
            # 记录主事件循环（若可用）
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                self._main_loop = None
            self.stats["connection_attempts"] += 1
            
            # 创建MQTT客户端
            _LOGGER.debug(f"创建MQTT客户端，客户端ID: '{self.client_id}'")
            if self.client_id:
                self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
            else:
                self.client = mqtt.Client(protocol=mqtt.MQTTv311)
            _LOGGER.debug(f"MQTT客户端已创建，实际客户端ID: '{self.client._client_id.decode()}'")
            
            # 设置回调函数
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.on_log = self._on_log
            
            # 设置认证信息
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)
                _LOGGER.debug(f"已设置MQTT认证 - 用户名: {self.username}")
            else:
                _LOGGER.debug("未设置MQTT认证信息")
            
            # 启动连接任务
            self.connection_task = asyncio.create_task(self._connection_loop())
            
            # 等待连接建立（最多10秒）
            for i in range(100):  # 10秒 = 100 * 0.1秒
                if self.is_connected:
                    break
                await asyncio.sleep(0.1)
                if i % 10 == 0:  # 每秒打印一次状态
                    _LOGGER.debug(f"等待MQTT连接建立... ({i/10:.1f}s)")
            
            if self.is_connected:
                self.stats["successful_connections"] += 1
                self.stats["uptime_start"] = datetime.now()
                self.stats["last_error"] = None
                _LOGGER.info("MQTT连接成功建立")
                return True
            else:
                self.stats["connection_failures"] += 1
                self.stats["last_error"] = "连接超时"
                _LOGGER.error("MQTT连接超时")
                return False
                
        except Exception as err:
            self.stats["connection_failures"] += 1
            self.stats["last_error"] = str(err)
            _LOGGER.error(f"MQTT连接启动失败: {err}")
            return False
    
    async def stop(self) -> None:
        """停止MQTT连接"""
        _LOGGER.info("停止MQTT连接")
        
        # 停止重连
        self._should_reconnect = False
        
        # 停止连接任务
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass
        
        # 停止重连任务
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        
        # 断开客户端
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as err:
                _LOGGER.debug(f"MQTT断开连接异常: {err}")
        # 停止本地广播任务
        if self._local_broadcast_task and not self._local_broadcast_task.done():
            try:
                self._local_broadcast_task.cancel()
            except Exception:
                pass
        self._local_broadcast_task = None
        
        self.is_connected = False
        self.client = None
        self.stats["uptime_start"] = None
        _LOGGER.info("MQTT连接已停止")

    # ===== 录制/回放辅助 =====
    def enable_downstream_recording(self, enabled: bool, on_message: Optional[Callable[[str, bytes], None]] = None) -> None:
        """启用/禁用下行报文(DOWN/2001)录制。
        on_message(topic, payload_bytes) 在收到任意 DOWN/2001 报文时被调用。
        """
        self._recording_downstream = enabled
        self._down_message_callback = on_message
        # 若已连接，则动态订阅/退订
        if self.client and self.is_connected:
            try:
                if enabled:
                    res = self.client.subscribe(self._down_topic)
                    if res[0] == mqtt.MQTT_ERR_SUCCESS:
                        _LOGGER.info(f"已订阅下行主题: {self._down_topic}")
                    else:
                        _LOGGER.error(f"订阅下行主题失败: {res[0]}")
                else:
                    self.client.unsubscribe(self._down_topic)
                    _LOGGER.info(f"已退订下行主题: {self._down_topic}")
            except Exception as err:
                _LOGGER.error(f"动态(退)订下行主题失败: {err}")

    def publish_raw(self, topic: str, payload: bytes, qos: int = 0) -> bool:
        """直接发布原始MQTT消息(用于回放)。"""
        if not self.client or not self.is_connected:
            _LOGGER.error("MQTT未连接，无法发布原始消息")
            return False
        try:
            info = self.client.publish(topic, payload, qos=qos, retain=False)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.info(f"已发布回放消息到 {topic}，长度: {len(payload)} bytes")
                return True
            _LOGGER.error(f"发布回放消息失败，rc={info.rc}")
            return False
        except Exception as err:
            _LOGGER.error(f"发布回放消息异常: {err}")
            return False
    
    async def reconnect(self) -> bool:
        """手动重连MQTT
        
        Returns:
            bool: 是否重连成功
        """
        _LOGGER.info("手动触发MQTT重连")
        await self.stop()
        await asyncio.sleep(0.5)  # 短暂等待
        return await self.start()
    
    async def _connection_loop(self) -> None:
        """连接循环，处理连接和消息"""
        try:
            _LOGGER.debug(f"尝试连接到MQTT服务器: {self.host}:{self.port}")
            
            # 连接到MQTT服务器
            result = self.client.connect(self.host, self.port, self.keepalive)
            if result != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.error(f"MQTT连接失败，错误码: {result}")
                self.stats["connection_failures"] += 1
                self.stats["last_error"] = f"连接失败，错误码: {result}"
                return
            
            # 启动网络循环
            self.client.loop_start()
            _LOGGER.debug("MQTT网络循环已启动")
            
            # 等待连接建立
            await asyncio.sleep(2)  # 增加等待时间
            
            if self.is_connected:
                _LOGGER.info("MQTT连接已建立，开始订阅主题")
                # 订阅主题
                for topic in self.topics:
                    result = self.client.subscribe(topic)
                    if result[0] == mqtt.MQTT_ERR_SUCCESS:
                        _LOGGER.info(f"已订阅主题: {topic}")
                    else:
                        _LOGGER.error(f"订阅主题失败: {topic}, 错误码: {result[0]}")
                
                # 保持连接循环运行
                while self._should_reconnect and self.is_connected:
                    await asyncio.sleep(1)
            else:
                _LOGGER.error("MQTT连接建立失败")
                self.stats["connection_failures"] += 1
                self.stats["last_error"] = "连接建立失败"
                        
        except asyncio.CancelledError:
            _LOGGER.debug("MQTT连接循环被取消")
        except Exception as err:
            self.stats["last_error"] = str(err)
            _LOGGER.error(f"MQTT连接循环出现异常: {err}")
        finally:
            if self.client:
                try:
                    self.client.loop_stop()
                except Exception as err:
                    _LOGGER.debug(f"停止MQTT网络循环时出现异常: {err}")
            self.is_connected = False
            _LOGGER.info("MQTT连接循环已结束")
    
    def _on_connect(self, client, userdata, flags, rc):
        """MQTT连接回调"""
        if rc == 0:
            self.is_connected = True
            _LOGGER.info("MQTT客户端连接成功")
            self.stats["successful_connections"] += 1
            self.stats["uptime_start"] = datetime.now()
            self.stats["last_error"] = None
            
            # 连接成功后立即执行状态同步
            if self.fallback_callback:
                # 始终通过线程切回主线程安全调度，避免在paho线程直接触碰事件循环
                def schedule_on_main_thread():
                    try:
                        loop = asyncio.get_running_loop()
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._perform_immediate_sync()))
                    except RuntimeError:
                        # 主循环尚未就绪，稍后再试
                        threading.Timer(1.0, schedule_on_main_thread).start()
                threading.Timer(0.1, schedule_on_main_thread).start()
            # 订阅必要主题
            try:
                # 永久订阅状态主题
                for topic in self.topics:
                    result = self.client.subscribe(topic)
                    if result[0] == mqtt.MQTT_ERR_SUCCESS:
                        _LOGGER.info(f"已订阅主题: {topic}")
                    else:
                        _LOGGER.error(f"订阅主题失败: {topic}, 错误码: {result[0]}")
                # 录制时订阅下行控制主题
                if self._recording_downstream:
                    res = self.client.subscribe(self._down_topic)
                    if res[0] == mqtt.MQTT_ERR_SUCCESS:
                        _LOGGER.info(f"已订阅下行主题: {self._down_topic}")
                    else:
                        _LOGGER.error(f"订阅下行主题失败: {res[0]}")
            except Exception as err:
                _LOGGER.error(f"连接后订阅主题失败: {err}")
            # 若启用本地广播，确保任务运行
            try:
                if getattr(self, "_local_broadcast_enabled", False):
                    # 通过主事件循环启动任务，避免在paho线程中直接创建
                    def _start_lb():
                        try:
                            loop = asyncio.get_running_loop()
                            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._local_broadcast_loop()))
                        except RuntimeError:
                            threading.Timer(1.0, _start_lb).start()
                    _start_lb()
            except Exception as err:
                _LOGGER.error(f"启动本地广播任务失败: {err}")
        else:
            self.is_connected = False
            # 详细的错误码说明
            error_messages = {
                1: "连接被拒绝 - 协议版本不正确",
                2: "连接被拒绝 - 客户端ID无效",
                3: "连接被拒绝 - 服务器不可用",
                4: "连接被拒绝 - 用户名或密码错误",
                5: "连接被拒绝 - 未授权"
            }
            error_msg = error_messages.get(rc, f"连接被拒绝 - 未知错误码: {rc}")
            _LOGGER.error(f"MQTT连接失败: {error_msg}")
            self.stats["connection_failures"] += 1
            self.stats["last_error"] = error_msg
    
    def _on_disconnect(self, client, userdata, rc):
        """MQTT断开连接回调"""
        self.is_connected = False
        if rc != 0:
            # 详细的断开连接错误码说明
            disconnect_messages = {
                1: "网络错误",
                2: "协议错误", 
                3: "连接丢失",
                4: "连接超时",
                5: "服务器关闭",
                6: "客户端错误",
                7: "连接被拒绝"
            }
            error_msg = disconnect_messages.get(rc, f"未知断开原因，错误码: {rc}")
            _LOGGER.warning(f"MQTT意外断开连接: {error_msg}")
            self.stats["last_error"] = f"意外断开连接: {error_msg}"
            # 启动重连（使用线程安全的方式）
            if self._should_reconnect:
                try:
                    loop = asyncio.get_running_loop()
                    # 如果有运行中的事件循环，使用call_soon_threadsafe，并在事件循环线程中做并发保护
                    def _schedule_reconnect():
                        # 仅允许存在一个重连任务
                        if self._reconnect_task and not self._reconnect_task.done():
                            _LOGGER.debug("已有自动重连任务在运行，跳过新建")
                            return
                        task = asyncio.create_task(self._auto_reconnect())
                        self._reconnect_task = task
                        # 任务完成后清理引用
                        def _clear_ref(_):
                            if self._reconnect_task is task:
                                self._reconnect_task = None
                        task.add_done_callback(_clear_ref)
                    loop.call_soon_threadsafe(_schedule_reconnect)
                except RuntimeError:
                    # 没有运行中的事件循环，记录警告但不启动重连
                    _LOGGER.warning("没有运行中的事件循环，无法启动自动重连")
        else:
            _LOGGER.info("MQTT正常断开连接")
    
    def _on_message(self, client, userdata, msg):
        """MQTT消息接收回调"""
        try:
            # 创建消息对象以保持兼容性
            class Message:
                def __init__(self, topic, payload):
                    self.topic = topic
                    self.payload = payload
            
            message = Message(msg.topic, msg.payload)

            # 录制期的下行报文：优先回调并直接返回（不进入JSON解析）
            if self._recording_downstream and msg.topic == self._down_topic and self._down_message_callback:
                try:
                    if self._main_loop and self._main_loop.is_running():
                        self._main_loop.call_soon_threadsafe(lambda: self._down_message_callback(msg.topic, msg.payload))
                    else:
                        self._down_message_callback(msg.topic, msg.payload)
                except Exception as err:
                    _LOGGER.error(f"处理下行录制回调失败: {err}")
                return

            # 其它消息：仅在主事件循环中处理，避免线程问题
            if self._main_loop and self._main_loop.is_running():
                self._main_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_message(message))
                )
            else:
                _LOGGER.debug("主事件循环未就绪，丢弃一条MQTT消息")
                
        except Exception as err:
            _LOGGER.error(f"MQTT消息回调处理失败: {err}")
            self.stats["parse_errors"] += 1
    
    def _on_log(self, client, userdata, level, buf):
        """MQTT日志回调"""
        if level == mqtt.MQTT_LOG_ERR:
            _LOGGER.error(f"MQTT错误: {buf}")
        elif level == mqtt.MQTT_LOG_WARNING:
            _LOGGER.warning(f"MQTT警告: {buf}")
        else:
            _LOGGER.debug(f"MQTT日志: {buf}")

    # ===== 本地广播支持 =====
    def set_local_broadcast_enabled(self, enabled: bool) -> None:
        """启用/禁用本地广播。
        启用后定期向配置的主题发送毫秒时间戳。
        """
        self._local_broadcast_enabled = enabled
        if enabled:
            # 确保任务运行
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._local_broadcast_loop()))
            except RuntimeError:
                # 若主循环未就绪，则稍后再试
                def _delayed_start():
                    try:
                        loop2 = asyncio.get_running_loop()
                        loop2.call_soon_threadsafe(lambda: asyncio.create_task(self._local_broadcast_loop()))
                    except RuntimeError:
                        threading.Timer(1.0, _delayed_start).start()
                threading.Timer(0.2, _delayed_start).start()
            _LOGGER.info("本地广播已启用")
        else:
            _LOGGER.info("本地广播已禁用")

    async def _local_broadcast_loop(self) -> None:
        """循环发送本地广播（毫秒时间戳）。
        注意：允许存在多个并行启动请求，但循环开始时会自我去重。
        """
        # 去重：若已有运行中的任务引用，直接返回（尽量避免重复任务）
        if getattr(self, "_local_broadcast_task", None) and not self._local_broadcast_task.done():
            return
        self._local_broadcast_task = asyncio.current_task()
        try:
            while getattr(self, "_local_broadcast_enabled", False):
                try:
                    if self.client and self.is_connected:
                        millis = int(time.time() * 1000)
                        payload = str(millis)
                        info = self.client.publish(self._local_broadcast_topic, payload, qos=0, retain=False)
                        if info.rc == mqtt.MQTT_ERR_SUCCESS:
                            _LOGGER.debug(f"已发送本地广播: {payload}")
                        else:
                            _LOGGER.warning(f"发送本地广播失败 rc={info.rc}")
                    else:
                        _LOGGER.debug("未连接MQTT，跳过一次本地广播")
                except Exception as err:
                    _LOGGER.error(f"本地广播发送异常: {err}")
                await asyncio.sleep(getattr(self, "_local_broadcast_interval_seconds", 15))
        except asyncio.CancelledError:
            pass
        finally:
            # 退出时清理引用
            if getattr(self, "_local_broadcast_task", None) is asyncio.current_task():
                self._local_broadcast_task = None
            _LOGGER.debug("本地广播任务结束")
    
    async def _auto_reconnect(self):
        """自动重连"""
        if not self._should_reconnect:
            return
        
        reconnect_intervals = MQTT_CONFIG["reconnect_intervals"]
        max_interval = MQTT_CONFIG["max_reconnect_interval"]
        
        for interval in reconnect_intervals:
            if not self._should_reconnect:
                break
            
            _LOGGER.info(f"MQTT自动重连，等待{interval}秒...")
            await asyncio.sleep(interval)
            
            if not self._should_reconnect:
                break
            
            try:
                if await self.start():
                    _LOGGER.info("MQTT自动重连成功")
                    self.stats["reconnect_count"] += 1
                    return
            except Exception as err:
                _LOGGER.error(f"MQTT自动重连失败: {err}")
        
        # 如果所有重连间隔都失败，使用最大间隔继续重连
        while self._should_reconnect:
            _LOGGER.info(f"MQTT重连失败，等待{max_interval}秒后重试...")
            await asyncio.sleep(max_interval)
            
            if not self._should_reconnect:
                break
            
            try:
                if await self.start():
                    _LOGGER.info("MQTT自动重连成功")
                    self.stats["reconnect_count"] += 1
                    return
            except Exception as err:
                _LOGGER.error(f"MQTT自动重连失败: {err}")
    
    async def _handle_message(self, message) -> None:
        """处理接收到的MQTT消息（异步版本）"""
        self.stats["messages_received"] += 1
        self.stats["last_message_time"] = datetime.now()
        
        topic = message.topic
        payload = message.payload.decode('utf-8')
        
        _LOGGER.debug(f"收到MQTT消息 - 主题:{topic}, 载荷:{payload}")
        
        try:
            # 解析JSON载荷
            data = json.loads(payload)
            
            # 提取状态数据
            if "payload" in data and isinstance(data["payload"], dict):
                payload_data = data["payload"]
                
                # 标准化状态数据
                state_data = self._normalize_state_data(payload_data)
                
                if state_data:
                    self.stats["messages_parsed"] += 1
                    _LOGGER.debug(f"解析状态数据: {state_data}")
                    
                    # 回调状态更新
                    if self.state_callback:
                        await self._safe_callback([state_data])
                else:
                    _LOGGER.debug("消息载荷不包含有效状态数据")
            else:
                _LOGGER.debug("消息格式不符合预期")
                
        except json.JSONDecodeError as err:
            _LOGGER.error(f"JSON解析失败: {err}")
            self.stats["parse_errors"] += 1
        except Exception as err:
            _LOGGER.error(f"消息处理异常: {err}")
            self.stats["parse_errors"] += 1
    
    def _handle_message_sync(self, message) -> None:
        """处理接收到的MQTT消息（同步版本）"""
        self.stats["messages_received"] += 1
        self.stats["last_message_time"] = datetime.now()
        
        topic = message.topic
        payload = message.payload.decode('utf-8')
        
        _LOGGER.debug(f"收到MQTT消息 - 主题:{topic}, 载荷:{payload}")
        
        try:
            # 解析JSON载荷
            data = json.loads(payload)
            
            # 提取状态数据
            if "payload" in data and isinstance(data["payload"], dict):
                payload_data = data["payload"]
                
                # 标准化状态数据
                state_data = self._normalize_state_data(payload_data)
                
                if state_data:
                    self.stats["messages_parsed"] += 1
                    _LOGGER.debug(f"解析状态数据: {state_data}")
                    
                    # 回调状态更新（同步调用）
                    if self.state_callback:
                        try:
                            if asyncio.iscoroutinefunction(self.state_callback):
                                # 如果是异步回调，尝试在新的事件循环中运行
                                try:
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    loop.run_until_complete(self._safe_callback([state_data]))
                                    loop.close()
                                except Exception as err:
                                    _LOGGER.error(f"异步回调执行失败: {err}")
                            else:
                                # 同步回调直接调用
                                self.state_callback([state_data])
                        except Exception as err:
                            _LOGGER.error(f"状态回调执行失败: {err}")
                else:
                    _LOGGER.debug("消息载荷不包含有效状态数据")
            else:
                _LOGGER.debug("消息格式不符合预期")
                
        except json.JSONDecodeError as err:
            _LOGGER.error(f"JSON解析失败: {err}")
            self.stats["parse_errors"] += 1
        except Exception as err:
            _LOGGER.error(f"消息处理异常: {err}")
            self.stats["parse_errors"] += 1
    
    def _normalize_state_data(self, payload_data: Dict) -> Optional[Dict]:
        """标准化状态数据
        
        Args:
            payload_data: 原始载荷数据
            
        Returns:
            标准化的状态数据或None
        """
        try:
            # 提取必需字段
            st = payload_data.get("st")
            si = payload_data.get("si")  
            fn = payload_data.get("fn")
            fv = payload_data.get("fv")
            
            # 验证必需字段
            if st is None or si is None or fn is None or fv is None:
                _LOGGER.debug(f"状态数据缺少必需字段: st={st}, si={si}, fn={fn}, fv={fv}")
                return None
            
            # 返回标准化数据
            return {
                "st": st,
                "si": si,
                "fn": fn,
                "fv": fv,
            }
            
        except Exception as err:
            _LOGGER.error(f"状态数据标准化失败: {err}")
            return None
    
    async def _perform_immediate_sync(self) -> None:
        """MQTT连接成功后立即执行状态同步"""
        if not self.fallback_callback:
            _LOGGER.debug("没有设置兜底巡检回调，跳过立即状态同步")
            return
        
        try:
            _LOGGER.info("MQTT连接成功，开始立即状态同步")
            
            # 调用兜底巡检回调获取云服务器状态
            if asyncio.iscoroutinefunction(self.fallback_callback):
                states = await self.fallback_callback()
            else:
                states = self.fallback_callback()
            
            if states:
                _LOGGER.info(f"立即状态同步完成，获取到{len(states)}个设备状态")
                
                # 通过状态回调处理同步的状态数据
                if self.state_callback:
                    await self._safe_callback(states)
            else:
                _LOGGER.warning("立即状态同步 - 获取状态数据为空")
                
        except Exception as err:
            _LOGGER.error(f"立即状态同步失败: {err}")
    
    async def _safe_callback(self, states: List[Dict]) -> None:
        """安全调用状态回调"""
        try:
            if asyncio.iscoroutinefunction(self.state_callback):
                await self.state_callback(states)
            else:
                self.state_callback(states)
        except Exception as err:
            _LOGGER.error(f"状态回调执行失败: {err}")
    
    def get_status(self) -> Dict[str, Any]:
        """获取MQTT网关状态"""
        status = {
            "connected": self.is_connected,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "topics": self.topics,
            "stats": self.stats.copy(),
        }
        
        if self.is_connected and self.stats["uptime_start"]:
            uptime = datetime.now() - self.stats["uptime_start"]
            status["uptime_seconds"] = uptime.total_seconds()
        
        return status
    
    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats.update({
            "connection_attempts": 0,
            "successful_connections": 0,
            "connection_failures": 0,
            "messages_received": 0,
            "messages_parsed": 0,
            "parse_errors": 0,
            "last_message_time": None,
            "reconnect_count": 0,
        })
        _LOGGER.info("MQTT网关统计信息已重置")

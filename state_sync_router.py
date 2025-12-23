"""状态同步路由器 - State Sync Router for HYQW Adapter"""
import asyncio
import logging
from typing import Callable, Dict, List, Optional, Any
from datetime import datetime

from .const import MQTT_CONFIG

_LOGGER = logging.getLogger(__name__)


class StateSyncRouter:
    """状态同步路由器
    
    负责：
    1. 管理MQTT和轮询模式的切换
    2. 统一状态数据入口和处理
    3. 兜底巡检管理
    4. 乐观回显策略控制
    """
    
    def __init__(self, state_manager, polling_bus, update_callback: Callable[[Dict], None]):
        """初始化状态同步路由器
        
        Args:
            state_manager: 状态管理器实例
            polling_bus: 轮询总线实例
            update_callback: 状态更新回调函数，用于触发coordinator数据更新
        """
        self.state_manager = state_manager
        self.polling_bus = polling_bus
        self.update_callback = update_callback
        
        # 路由模式
        self.current_mode = "polling"  # polling | mqtt
        self.mqtt_active = False
        self.polling_active = False
        
        # 兜底巡检
        self.fallback_interval = MQTT_CONFIG["default_fallback_interval"]  # 默认10分钟
        self.fallback_task: Optional[asyncio.Task] = None
        self.fallback_callback: Optional[Callable] = None
        
        # 乐观回显设置
        self.optimistic_echo_enabled = MQTT_CONFIG["default_optimistic_echo"]
        
        # 统计信息
        self.stats = {
            "mode_switches": 0,
            "mqtt_state_updates": 0,
            "polling_state_updates": 0,
            "fallback_checks": 0,
            "last_mode_switch": None,
            "last_state_update": None,
            "last_fallback_check": None,
        }
        
        _LOGGER.info("状态同步路由器初始化完成")
    
    def set_fallback_callback(self, callback: Callable) -> None:
        """设置兜底巡检回调函数"""
        self.fallback_callback = callback
        _LOGGER.debug("兜底巡检回调已设置")
    
    def configure_fallback(self, interval: int) -> None:
        """配置兜底巡检间隔
        
        Args:
            interval: 巡检间隔(秒)，0表示禁用
        """
        old_interval = self.fallback_interval
        self.fallback_interval = interval
        
        if old_interval != interval:
            _LOGGER.info(f"兜底巡检间隔已更新: {old_interval}s -> {interval}s")
            
            # 如果正在MQTT模式且兜底巡检在运行，重启兜底任务
            if self.current_mode == "mqtt" and self.fallback_task:
                asyncio.create_task(self._restart_fallback_task())
    
    def set_optimistic_echo(self, enabled: bool) -> None:
        """设置乐观回显开关"""
        old_enabled = self.optimistic_echo_enabled
        self.optimistic_echo_enabled = enabled
        
        if old_enabled != enabled:
            _LOGGER.info(f"乐观回显已{'启用' if enabled else '禁用'}")
    
    def is_optimistic_echo_enabled(self) -> bool:
        """检查乐观回显是否启用"""
        return self.optimistic_echo_enabled
    
    async def use_mqtt_mode(self) -> None:
        """切换到MQTT模式"""
        if self.current_mode == "mqtt":
            _LOGGER.debug("已在MQTT模式，跳过切换")
            return
        
        _LOGGER.info("切换到MQTT模式")
        old_mode = self.current_mode
        self.current_mode = "mqtt"
        self.mqtt_active = True
        
        # 停止轮询总线
        if self.polling_active:
            await self.polling_bus.stop()
            self.polling_active = False
            _LOGGER.info("已停止轮询总线")
        
        # 启动兜底巡检
        await self._start_fallback_task()
        
        # 立即执行一次状态同步
        await self._perform_immediate_sync()
        
        # 更新统计
        self.stats["mode_switches"] += 1
        self.stats["last_mode_switch"] = datetime.now()
        
        _LOGGER.info(f"模式切换完成: {old_mode} -> mqtt")
    
    async def use_polling_mode(self) -> None:
        """切换到轮询模式"""
        if self.current_mode == "polling":
            _LOGGER.debug("已在轮询模式，跳过切换")
            return
        
        _LOGGER.info("切换到轮询模式")
        old_mode = self.current_mode
        self.current_mode = "polling"
        self.mqtt_active = False
        
        # 停止兜底巡检
        await self._stop_fallback_task()
        
        # 启动轮询总线
        if not self.polling_active:
            if not self.polling_bus.is_running:
                await self.polling_bus.start()
            self.polling_active = True
            _LOGGER.info("已启动轮询总线")
        
        # 更新统计
        self.stats["mode_switches"] += 1
        self.stats["last_mode_switch"] = datetime.now()
        
        _LOGGER.info(f"模式切换完成: {old_mode} -> polling")
    
    def using_mqtt(self) -> bool:
        """检查是否正在使用MQTT模式"""
        return self.current_mode == "mqtt" and self.mqtt_active
    
    def using_polling(self) -> bool:
        """检查是否正在使用轮询模式"""
        return self.current_mode == "polling" and self.polling_active
    
    def get_current_mode(self) -> str:
        """获取当前模式"""
        return self.current_mode
    
    async def handle_mqtt_states(self, states: List[Dict]) -> None:
        """处理MQTT状态数据
        
        Args:
            states: MQTT接收到的状态数据列表
        """
        if not self.using_mqtt():
            _LOGGER.warning("收到MQTT状态数据，但当前不在MQTT模式")
            return
        
        _LOGGER.debug(f"路由器处理MQTT状态数据: {len(states)}条")
        
        try:
            # 使用状态管理器处理状态更新
            has_changes, changes = self.state_manager.process_state_update(states)
            
            if has_changes:
                self.stats["mqtt_state_updates"] += 1
                self.stats["last_state_update"] = datetime.now()
                
                _LOGGER.info(f"MQTT状态更新 - {len(changes.get('changed_devices', set()))}个设备有变化")
                
                # 触发coordinator数据更新
                await self._trigger_coordinator_update(changes)
            else:
                _LOGGER.debug("MQTT状态无变化")
                
        except Exception as err:
            _LOGGER.error(f"处理MQTT状态数据失败: {err}", exc_info=True)
    
    async def handle_polling_states(self, states: List[Dict]) -> None:
        """处理轮询状态数据
        
        Args:
            states: 轮询获取到的状态数据列表
        """
        if not self.using_polling():
            _LOGGER.debug("收到轮询状态数据，但当前不在轮询模式")
            return
        
        _LOGGER.debug(f"路由器处理轮询状态数据: {len(states)}条")
        
        try:
            # 使用状态管理器处理状态更新
            has_changes, changes = self.state_manager.process_state_update(states)
            
            if has_changes:
                self.stats["polling_state_updates"] += 1
                self.stats["last_state_update"] = datetime.now()
                
                _LOGGER.info(f"轮询状态更新 - {len(changes.get('changed_devices', set()))}个设备有变化")
                
                # 触发coordinator数据更新
                await self._trigger_coordinator_update(changes)
            else:
                _LOGGER.debug("轮询状态无变化")
                
        except Exception as err:
            _LOGGER.error(f"处理轮询状态数据失败: {err}", exc_info=True)
    
    async def handle_fallback_states(self, states: List[Dict]) -> None:
        """处理兜底巡检状态数据
        
        Args:
            states: 兜底巡检获取到的状态数据列表
        """
        if not self.using_mqtt():
            _LOGGER.debug("收到兜底巡检数据，但当前不在MQTT模式")
            return
        
        _LOGGER.debug(f"路由器处理兜底巡检数据: {len(states)}条")
        
        try:
            # 使用状态管理器处理状态更新
            has_changes, changes = self.state_manager.process_state_update(states)
            
            if has_changes:
                self.stats["fallback_checks"] += 1
                self.stats["last_fallback_check"] = datetime.now()
                
                _LOGGER.info(f"兜底巡检发现状态差异 - {len(changes.get('changed_devices', set()))}个设备有变化")
                
                # 触发coordinator数据更新
                await self._trigger_coordinator_update(changes)
            else:
                _LOGGER.debug("兜底巡检状态一致")
                
        except Exception as err:
            _LOGGER.error(f"处理兜底巡检数据失败: {err}", exc_info=True)
    
    async def _trigger_coordinator_update(self, changes: Dict) -> None:
        """触发coordinator数据更新"""
        try:
            if self.update_callback:
                if asyncio.iscoroutinefunction(self.update_callback):
                    await self.update_callback(changes)
                else:
                    self.update_callback(changes)
        except Exception as err:
            _LOGGER.error(f"触发coordinator更新失败: {err}")
    
    async def _start_fallback_task(self) -> None:
        """启动兜底巡检任务"""
        if self.fallback_interval <= 0:
            _LOGGER.debug("兜底巡检已禁用")
            return
        
        if self.fallback_task and not self.fallback_task.done():
            _LOGGER.debug("兜底巡检任务已在运行")
            return
        
        _LOGGER.info(f"启动兜底巡检任务 - 间隔{self.fallback_interval}秒")
        self.fallback_task = asyncio.create_task(self._fallback_loop())
    
    async def _stop_fallback_task(self) -> None:
        """停止兜底巡检任务"""
        if self.fallback_task and not self.fallback_task.done():
            self.fallback_task.cancel()
            try:
                await self.fallback_task
            except asyncio.CancelledError:
                pass
            _LOGGER.info("兜底巡检任务已停止")
        
        self.fallback_task = None
    
    async def _restart_fallback_task(self) -> None:
        """重启兜底巡检任务"""
        await self._stop_fallback_task()
        await self._start_fallback_task()
    
    async def _perform_immediate_sync(self) -> None:
        """立即执行状态同步"""
        if not self.fallback_callback:
            _LOGGER.debug("没有设置兜底巡检回调，跳过立即状态同步")
            return
        
        try:
            _LOGGER.info("路由器执行立即状态同步")
            
            # 调用兜底巡检回调获取云服务器状态
            if asyncio.iscoroutinefunction(self.fallback_callback):
                states = await self.fallback_callback()
            else:
                states = self.fallback_callback()
            
            if states:
                _LOGGER.info(f"路由器立即状态同步完成，获取到{len(states)}个设备状态")
                
                # 通过兜底巡检处理状态数据
                await self.handle_fallback_states(states)
            else:
                _LOGGER.warning("路由器立即状态同步 - 获取状态数据为空")
                
        except Exception as err:
            _LOGGER.error(f"路由器立即状态同步失败: {err}")
    
    async def _fallback_loop(self) -> None:
        """兜底巡检循环"""
        try:
            while True:
                await asyncio.sleep(self.fallback_interval)
                
                if self.using_mqtt() and self.fallback_callback:
                    try:
                        _LOGGER.debug("执行兜底巡检")
                        
                        if asyncio.iscoroutinefunction(self.fallback_callback):
                            states = await self.fallback_callback()
                        else:
                            states = self.fallback_callback()
                        
                        if states:
                            await self.handle_fallback_states(states)
                        
                        self.stats["last_fallback_check"] = datetime.now()
                        
                    except Exception as err:
                        _LOGGER.error(f"兜底巡检执行失败: {err}")
                else:
                    # 如果不在MQTT模式或没有回调，退出循环
                    break
                    
        except asyncio.CancelledError:
            _LOGGER.debug("兜底巡检循环被取消")
            raise
        except Exception as err:
            _LOGGER.error(f"兜底巡检循环异常: {err}")
    
    async def stop(self) -> None:
        """停止路由器"""
        _LOGGER.info("停止状态同步路由器")
        
        # 停止兜底巡检
        await self._stop_fallback_task()
        
        # 停止轮询总线
        if self.polling_active:
            await self.polling_bus.stop()
            self.polling_active = False
        
        self.mqtt_active = False
        _LOGGER.info("状态同步路由器已停止")
    
    def get_status(self) -> Dict[str, Any]:
        """获取路由器状态"""
        status = {
            "current_mode": self.current_mode,
            "mqtt_active": self.mqtt_active,
            "polling_active": self.polling_active,
            "optimistic_echo_enabled": self.optimistic_echo_enabled,
            "fallback_interval": self.fallback_interval,
            "fallback_running": self.fallback_task is not None and not self.fallback_task.done(),
            "stats": self.stats.copy(),
        }
        
        return status
    
    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats.update({
            "mode_switches": 0,
            "mqtt_state_updates": 0,
            "polling_state_updates": 0,
            "fallback_checks": 0,
            "last_mode_switch": None,
            "last_state_update": None,
            "last_fallback_check": None,
        })
        _LOGGER.info("路由器统计信息已重置")

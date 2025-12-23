"""智能轮询总线系统 - Intelligent Polling Bus System"""
import asyncio
import logging
import time
from typing import Callable, Dict, Any, Optional
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)


class PollingBus:
    """智能轮询总线类
    
    实现长短轮询的智能切换：
    - 长轮询：默认每n秒查询一次状态  
    - 短轮询：设备操作后的高频查询模式
    - 时间戳精确控制查询时机
    - 动态配置支持
    """
    
    def __init__(self, config: Dict[str, Any], state_query_callback: Callable):
        """初始化轮询总线
        
        Args:
            config: 轮询配置参数
            state_query_callback: 状态查询回调函数
        """
        self.config = config.copy()
        self.state_query_callback = state_query_callback
        
        # 轮询状态
        self.is_running = False
        self.current_mode = "long"  # long | short
        self.polling_task: Optional[asyncio.Task] = None
        
        # 时间戳管理
        self.last_operation_time = 0.0
        self.short_polling_end_time = 0.0  # c时间点
        self.next_poll_time = 0.0  # d时间点
        
        # 统计信息
        self.stats = {
            "long_polling_count": 0,
            "short_polling_count": 0,
            "mode_switches": 0,
            "last_poll_time": None,
            "current_mode": "long",
        }
        
        _LOGGER.info(f"轮询总线初始化完成 - 长轮询:{self.config['long_polling_interval']}s, "
                    f"短轮询:{self.config['short_polling_interval']}s, "
                    f"持续时间:{self.config['short_polling_duration']}s")
    
    
    async def start(self) -> None:
        """启动轮询总线"""
        if self.is_running:
            _LOGGER.warning("轮询总线已在运行中")
            return
        
        self.is_running = True
        current_time = time.time()
        
        # 初始化为长轮询模式
        self.current_mode = "long"
        self.next_poll_time = current_time + self.config["long_polling_interval"]
        
        # 启动轮询任务
        self.polling_task = asyncio.create_task(self._polling_loop())
        
        _LOGGER.info(f"轮询总线已启动 - 首次轮询将在{self.config['long_polling_interval']}秒后进行")
    
    async def stop(self) -> None:
        """停止轮询总线"""
        self.is_running = False
        
        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        
        _LOGGER.info("轮询总线已停止")
    
    def trigger_short_polling(self) -> None:
        """触发短轮询模式
        
        在用户主动操作设备后调用，启动高频查询模式
        """
        current_time = time.time()
        self.last_operation_time = current_time
        
        # 计算关键时间点
        a = self.config["short_polling_interval"]
        b = self.config["short_polling_duration"]
        
        self.next_poll_time = current_time + a  # d时间点 = t0 + a
        self.short_polling_end_time = current_time + b  # c时间点 = t0 + b
        
        # 切换到短轮询模式
        old_mode = self.current_mode
        self.current_mode = "short"
        
        if old_mode != "short":
            self.stats["mode_switches"] += 1
            _LOGGER.info(f"切换到短轮询模式 - 持续{b}秒, 每{a}秒查询一次, "
                        f"首次查询:{datetime.fromtimestamp(self.next_poll_time).strftime('%H:%M:%S')}, "
                        f"结束时间:{datetime.fromtimestamp(self.short_polling_end_time).strftime('%H:%M:%S')}")
        else:
            _LOGGER.debug(f"延长短轮询模式 - 新结束时间:{datetime.fromtimestamp(self.short_polling_end_time).strftime('%H:%M:%S')}")
    
    def extend_short_polling(self) -> None:
        """延长短轮询模式
        
        在短轮询期间如有其他操作，延长短轮询结束时间
        """
        if self.current_mode != "short":
            # 如果不在短轮询模式，触发短轮询
            self.trigger_short_polling()
            return
        
        current_time = time.time()
        self.last_operation_time = current_time
        
        # 更新结束时间点c
        b = self.config["short_polling_duration"]
        self.short_polling_end_time = current_time + b
        
        _LOGGER.debug(f"延长短轮询模式 - 新的结束时间:{datetime.fromtimestamp(self.short_polling_end_time).strftime('%H:%M:%S')}")
    
    def set_short_polling_mode(self) -> None:
        """设置短轮询模式（智能判断当前状态）
        
        统一的短轮询设置接口：
        - 如果当前不在短轮询模式，则启动短轮询
        - 如果已在短轮询模式，则延长持续时间
        """
        if self.current_mode == "short":
            # 已在短轮询模式，延长时间
            self.extend_short_polling()
            _LOGGER.debug("已在短轮询模式 - 延长持续时间")
        else:
            # 启动短轮询模式
            self.trigger_short_polling()
            _LOGGER.debug("启动短轮询模式")
    
    async def _polling_loop(self) -> None:
        """轮询主循环"""
        try:
            while self.is_running:
                current_time = time.time()
                
                # 等待到下次轮询时间
                sleep_time = self.next_poll_time - current_time
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                
                if not self.is_running:
                    break
                
                # 执行状态查询
                await self._execute_poll()
                
                # 计算下次轮询时间
                self._calculate_next_poll_time()
                
        except asyncio.CancelledError:
            _LOGGER.debug("轮询循环被取消")
            raise
        except Exception as err:
            _LOGGER.error(f"轮询循环异常: {err}", exc_info=True)
    
    async def _execute_poll(self) -> None:
        """执行状态查询"""
        current_time = time.time()
        
        try:
            # 更新统计信息
            if self.current_mode == "long":
                self.stats["long_polling_count"] += 1
                poll_type = "长轮询"
            else:
                self.stats["short_polling_count"] += 1
                poll_type = "短轮询"
            
            self.stats["last_poll_time"] = datetime.fromtimestamp(current_time)
            self.stats["current_mode"] = self.current_mode
            
            # 短轮询时显示详细信息
            if self.current_mode == "short":
                remaining_time = self.short_polling_end_time - current_time
                _LOGGER.info(f"执行{poll_type}查询 - 第{self.stats['short_polling_count']}次, "
                           f"剩余时间:{remaining_time:.1f}秒")
            else:
                _LOGGER.debug(f"执行{poll_type}查询 - 总计{self.stats['long_polling_count']}次")
            
            # 调用状态查询回调
            await self.state_query_callback()
            
        except Exception as err:
            _LOGGER.error(f"状态查询执行失败: {err}", exc_info=True)
    
    def _calculate_next_poll_time(self) -> None:
        """计算下次轮询时间"""
        current_time = time.time()
        
        if self.current_mode == "short":
            # 检查是否应该结束短轮询
            remaining_time = self.short_polling_end_time - current_time
            if remaining_time <= 0:
                # 短轮询结束，切换到长轮询
                self.current_mode = "long"
                self.next_poll_time = current_time + self.config["long_polling_interval"]
                
                self.stats["mode_switches"] += 1
                _LOGGER.info(f"短轮询结束，切换到长轮询模式 - "
                           f"下次查询:{datetime.fromtimestamp(self.next_poll_time).strftime('%H:%M:%S')}")
            else:
                # 继续短轮询
                self.next_poll_time = current_time + self.config["short_polling_interval"]
                _LOGGER.debug(f"继续短轮询 - 下次查询:{datetime.fromtimestamp(self.next_poll_time).strftime('%H:%M:%S')}, "
                            f"剩余时间:{remaining_time:.1f}秒")
        else:
            # 长轮询模式
            self.next_poll_time = current_time + self.config["long_polling_interval"]
            _LOGGER.debug(f"长轮询模式 - 下次查询:{datetime.fromtimestamp(self.next_poll_time).strftime('%H:%M:%S')}")
    
    def get_status(self) -> Dict[str, Any]:
        """获取轮询总线状态"""
        current_time = time.time()
        
        status = {
            "is_running": self.is_running,
            "current_mode": self.current_mode,
            "config": self.config.copy(),
            "stats": self.stats.copy(),
        }
        
        if self.is_running:
            status.update({
                "next_poll_in_seconds": max(0, self.next_poll_time - current_time),
                "next_poll_time": datetime.fromtimestamp(self.next_poll_time).strftime('%H:%M:%S'),
            })
            
            if self.current_mode == "short":
                status.update({
                    "short_polling_ends_in_seconds": max(0, self.short_polling_end_time - current_time),
                    "short_polling_end_time": datetime.fromtimestamp(self.short_polling_end_time).strftime('%H:%M:%S'),
                })
        
        return status
    
    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats.update({
            "long_polling_count": 0,
            "short_polling_count": 0,
            "mode_switches": 0,
            "last_poll_time": None,
        })
        _LOGGER.info("轮询统计信息已重置")

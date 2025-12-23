"""优化版操作节流总线系统 - Optimized Throttled Action Bus System

基于FIFO队列的设备操作管理，提供流畅的用户体验
"""
import asyncio
import logging
import time
from typing import Callable, Dict, Any, Optional
from datetime import datetime
from enum import Enum
from dataclasses import dataclass
from collections import deque

_LOGGER = logging.getLogger(__name__)


class ActionStatus(Enum):
    """操作状态枚举"""
    PENDING = "pending"      # 等待执行
    EXECUTING = "executing"  # 正在执行
    COMPLETED = "completed"  # 执行完成
    FAILED = "failed"       # 执行失败
    CANCELLED = "cancelled" # 被取消


@dataclass
class DeviceAction:
    """设备操作数据类"""
    device_id: int
    st: int
    si: int
    fn: int
    fv: int
    action_id: str
    entity_id: str  # Home Assistant实体ID
    timestamp: float
    status: ActionStatus = ActionStatus.PENDING
    result: Optional[bool] = None
    error_message: Optional[str] = None


class OptimizedThrottledActionBus:
    """优化版操作节流总线
    
    基于FIFO队列的设备操作管理：
    1. 点击设备时检查设备是否已在队列中，如果存在不允许添加
    2. 如果不存在，添加到队列尾部，设置设备状态为占用
    3. 队列size=1时立即执行，否则按顺序处理
    4. 执行流程：触发短轮询总线 -> 异步等待200ms -> 触发回调
    5. 回调完成后设置目标值，继续处理队列
    """
    
    def __init__(self, execute_callback: Callable, polling_bus, wait_time: float = 0.2, router=None):
        """初始化优化版操作节流总线
        
        Args:
            execute_callback: 实际执行操作的回调函数
            polling_bus: 短轮询总线实例
            wait_time: 异步等待时间（秒），默认0.2秒
            router: 状态同步路由器实例（可选）
        """
        self.execute_callback = execute_callback
        self.polling_bus = polling_bus
        self.router = router
        self.wait_time = wait_time
        
        # FIFO队列管理
        self._action_queue: deque = deque()
        self._executing_action: Optional[DeviceAction] = None
        
        # 设备占用状态 {entity_id: action_id}
        self._occupied_devices: Dict[str, str] = {}
        
        # 运行状态
        self._is_running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._processing_lock = asyncio.Lock()
        
        # 统计信息
        self._stats = {
            "total_actions": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "cancelled_actions": 0,
            "queue_max_length": 0,
            "average_wait_time": 0.0,
            "last_execution_time": None,
        }
        
        _LOGGER.info(f"优化版操作节流总线初始化完成 - 等待时间: {wait_time}秒, 路由器: {'已集成' if router else '未集成'}")
    
    async def start(self) -> None:
        """启动操作节流总线"""
        if self._is_running:
            _LOGGER.warning("操作节流总线已在运行中")
            return
        
        self._is_running = True
        _LOGGER.info("优化版操作节流总线已启动")
    
    async def stop(self) -> None:
        """停止操作节流总线"""
        self._is_running = False
        
        # 取消处理器任务
        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        # 取消所有排队的操作
        while self._action_queue:
            action = self._action_queue.popleft()
            action.status = ActionStatus.CANCELLED
            action.error_message = "节流总线已停止"
            self._stats["cancelled_actions"] += 1
        
        # 清空占用状态
        self._occupied_devices.clear()
        
        _LOGGER.info("优化版操作节流总线已停止")
    
    async def submit_action(self, device_id: int, st: int, si: int, fn: int, 
                          fv: int, entity_id: str) -> str:
        """提交设备操作到队列
        
        Args:
            device_id: 设备ID
            st: 状态类型
            si: 设备索引
            fn: 功能码
            fv: 功能值
            entity_id: Home Assistant实体ID
            
        Returns:
            str: 操作ID
            
        Raises:
            RuntimeError: 节流总线未运行
            ValueError: 设备已被占用
        """
        if not self._is_running:
            raise RuntimeError("节流总线未运行")
        
        # 检查设备是否已在队列中（占用状态检测）
        if entity_id in self._occupied_devices:
            existing_action_id = self._occupied_devices[entity_id]
            _LOGGER.warning(f"设备 {entity_id} 已存在于队列中: {existing_action_id}")
            raise ValueError(f"设备 {entity_id} 正在处理中，请稍后再试")
        
        # 生成操作ID
        action_id = f"action_{int(time.time() * 1000)}"
        
        # 创建操作对象
        action = DeviceAction(
            device_id=device_id,
            st=st,
            si=si,
            fn=fn,
            fv=fv,
            action_id=action_id,
            entity_id=entity_id,
            timestamp=time.time(),
        )
        
        # 添加到FIFO队列尾部
        self._action_queue.append(action)
        
        # 设置设备占用状态
        self._occupied_devices[entity_id] = action_id
        
        # 更新统计
        self._stats["total_actions"] += 1
        queue_length = len(self._action_queue)
        if queue_length > self._stats["queue_max_length"]:
            self._stats["queue_max_length"] = queue_length
        
        _LOGGER.info(f"设备操作已加入队列: {entity_id} (fn={fn}, fv={fv}) "
                    f"- 队列长度: {queue_length}")
        
        # 如果队列size=1，立即开始处理
        if queue_length == 1:
            _LOGGER.debug(f"队列size=1，立即开始处理操作: {action_id}")
            asyncio.create_task(self._process_queue())
        
        return action_id
    
    def is_device_occupied(self, entity_id: str) -> bool:
        """检查设备是否被占用"""
        return entity_id in self._occupied_devices
    
    def get_device_action_status(self, entity_id: str) -> Optional[str]:
        """获取设备的操作状态"""
        if entity_id not in self._occupied_devices:
            return None
        
        action_id = self._occupied_devices[entity_id]
        
        # 检查当前执行的操作
        if self._executing_action and self._executing_action.action_id == action_id:
            return "executing"
        
        # 检查队列中的操作
        for action in self._action_queue:
            if action.action_id == action_id:
                return "pending"
        
        return "unknown"
    
    async def _process_queue(self) -> None:
        """处理队列中的操作（使用锁防止并发处理）"""
        async with self._processing_lock:
            while self._action_queue and self._is_running:
                # 弹出队列头部的操作
                action = self._action_queue.popleft()
                self._executing_action = action
                action.status = ActionStatus.EXECUTING
                
                _LOGGER.info(f"开始处理操作: {action.entity_id} (fn={action.fn}, fv={action.fv})")
                
                # 执行操作流程
                await self._execute_action_flow(action)
                
                # 清理执行状态
                self._executing_action = None
                
                # 清除设备占用状态
                if action.entity_id in self._occupied_devices:
                    del self._occupied_devices[action.entity_id]
                    _LOGGER.debug(f"设备 {action.entity_id} 占用状态已清除")
    
    async def _execute_action_flow(self, action: DeviceAction) -> None:
        """执行单个操作的完整流程"""
        start_time = time.time()
        
        try:
            # 步骤1: 根据路由器状态决定是否触发短轮询
            should_trigger_polling = True
            if self.router and hasattr(self.router, 'using_polling'):
                should_trigger_polling = self.router.using_polling()
            
            if should_trigger_polling:
                _LOGGER.debug(f"触发短轮询总线: {action.entity_id}")
                if hasattr(self.polling_bus, 'set_short_polling_mode'):
                    self.polling_bus.set_short_polling_mode()
            else:
                _LOGGER.debug(f"MQTT模式下跳过短轮询触发: {action.entity_id}")
            
            # 步骤2: 异步等待200ms（可配置）
            _LOGGER.debug(f"异步等待 {self.wait_time}秒: {action.entity_id}")
            await asyncio.sleep(self.wait_time)
            
            # 步骤3: 触发执行回调
            _LOGGER.debug(f"触发执行回调: {action.entity_id}")
            success = await self.execute_callback(
                action.device_id, action.st, action.si, action.fn, action.fv
            )
            
            # 步骤4: 更新操作状态为目标值
            action.result = success
            if success:
                action.status = ActionStatus.COMPLETED
                self._stats["successful_actions"] += 1
                _LOGGER.info(f"操作执行成功: {action.entity_id}")
            else:
                action.status = ActionStatus.FAILED
                action.error_message = "执行回调返回False"
                self._stats["failed_actions"] += 1
                _LOGGER.warning(f"操作执行失败: {action.entity_id}")
        
        except Exception as err:
            action.status = ActionStatus.FAILED
            action.result = False
            action.error_message = str(err)
            self._stats["failed_actions"] += 1
            _LOGGER.error(f"操作执行异常: {action.entity_id} - {err}", exc_info=True)
        
        finally:
            # 更新等待时间统计
            wait_time = start_time - action.timestamp
            current_avg = self._stats["average_wait_time"]
            total_actions = self._stats["successful_actions"] + self._stats["failed_actions"]
            if total_actions > 0:
                self._stats["average_wait_time"] = (current_avg * (total_actions - 1) + wait_time) / total_actions
            
            self._stats["last_execution_time"] = datetime.now()
    
    def get_queue_info(self) -> Dict[str, Any]:
        """获取队列信息"""
        current_time = time.time()
        
        queue_info = []
        for action in self._action_queue:
            queue_info.append({
                "action_id": action.action_id,
                "entity_id": action.entity_id,
                "device_id": action.device_id,
                "si": action.si,
                "fn": action.fn,
                "fv": action.fv,
                "wait_time": current_time - action.timestamp,
                "status": action.status.value,
            })
        
        executing_info = None
        if self._executing_action:
            executing_info = {
                "action_id": self._executing_action.action_id,
                "entity_id": self._executing_action.entity_id,
                "device_id": self._executing_action.device_id,
                "si": self._executing_action.si,
                "fn": self._executing_action.fn,
                "fv": self._executing_action.fv,
                "execution_time": current_time - self._executing_action.timestamp,
                "status": self._executing_action.status.value,
            }
        
        return {
            "queue_length": len(self._action_queue),
            "queue_actions": queue_info,
            "executing_action": executing_info,
            "occupied_devices": list(self._occupied_devices.keys()),
            "stats": self._stats.copy(),
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self._stats.copy()
    
    def clear_stats(self) -> None:
        """清空统计信息"""
        self._stats.update({
            "total_actions": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "cancelled_actions": 0,
            "queue_max_length": 0,
            "average_wait_time": 0.0,
            "last_execution_time": None,
        })
        _LOGGER.info("统计信息已清空")


# 保持向后兼容的类别名
ThrottledActionBus = OptimizedThrottledActionBus
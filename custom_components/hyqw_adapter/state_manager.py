"""状态差分更新管理器 - State Differential Update Manager"""
import asyncio
import logging
import copy
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime

_LOGGER = logging.getLogger(__name__)


class StateManager:
    """状态差分更新管理器
    
    负责：
    1. 比较新旧状态差异
    2. 识别需要更新的设备
    3. 推送状态变化给监听者
    4. 维护状态缓存
    """
    
    def __init__(self):
        """初始化状态管理器"""
        # 设备状态缓存 {si: {fn: {fv: value, st: status, timestamp: time}}}
        self._device_states_cache = {}
        
        # 设备信息缓存 {device_id: device_info}
        self._devices_cache = {}
        
        # 状态变化监听者
        self._state_listeners = []
        
        # 统计信息
        self._stats = {
            "total_updates": 0,
            "devices_changed": 0,
            "functions_changed": 0,
            "last_update_time": None,
            "cached_devices": 0,
        }
        
        _LOGGER.info("状态差分管理器初始化完成")
    
    def add_state_listener(self, listener_callback) -> None:
        """添加状态变化监听者"""
        self._state_listeners.append(listener_callback)
        _LOGGER.debug(f"添加状态监听者，当前监听者数量: {len(self._state_listeners)}")
    
    def remove_state_listener(self, listener_callback) -> None:
        """移除状态变化监听者"""
        if listener_callback in self._state_listeners:
            self._state_listeners.remove(listener_callback)
            _LOGGER.debug(f"移除状态监听者，当前监听者数量: {len(self._state_listeners)}")
    
    def update_devices_info(self, devices: List[Dict]) -> None:
        """更新设备信息缓存"""
        old_count = len(self._devices_cache)
        
        for device in devices:
            device_id = device.get("deviceId")
            if device_id:
                self._devices_cache[device_id] = device.copy()
        
        new_count = len(self._devices_cache)
        self._stats["cached_devices"] = new_count
        
        if new_count != old_count:
            _LOGGER.info(f"设备信息缓存已更新: {old_count} -> {new_count} 个设备")
    
    def process_state_update(self, new_states_data: List[Dict]) -> Tuple[bool, Dict]:
        """处理状态更新数据
        
        Args:
            new_states_data: 新的状态数据列表
            
        Returns:
            Tuple[bool, Dict]: (是否有变化, 变化详情)
        """
        if not new_states_data:
            _LOGGER.debug("状态数据为空，跳过更新")
            return False, {}
        
        self._stats["total_updates"] += 1
        self._stats["last_update_time"] = datetime.now()
        
        # 组织新状态数据
        new_device_states = self._organize_states_data(new_states_data)
        
        # 比较状态差异
        changes = self._compare_states(new_device_states)
        
        if not changes["has_changes"]:
            _LOGGER.debug("状态未发生变化")
            return False, changes
        
        # 更新缓存
        self._update_states_cache(new_device_states, changes)
        
        # 更新设备属性
        updated_devices = self._update_device_properties(changes)
        
        # 推送状态变化
        self._notify_state_listeners(changes, updated_devices)
        
        # 更新统计信息
        self._stats["devices_changed"] += len(changes["changed_devices"])
        self._stats["functions_changed"] += sum(len(funcs) for funcs in changes["changed_functions"].values())
        
        _LOGGER.info(f"状态差分更新完成 - 设备变化:{len(changes['changed_devices'])}个, "
                    f"功能变化:{sum(len(funcs) for funcs in changes['changed_functions'].values())}个")
        
        return True, changes
    
    def _organize_states_data(self, states_data: List[Dict]) -> Dict[int, Dict[int, Dict]]:
        """组织状态数据按设备索引"""
        organized = {}
        
        for state in states_data:
            si = state.get("si")
            fn = state.get("fn")
            fv = state.get("fv")
            st = state.get("st")
            
            if si is not None and fn is not None:
                if si not in organized:
                    organized[si] = {}
                
                organized[si][fn] = {
                    "fv": fv,
                    "st": st,
                    "timestamp": datetime.now().timestamp()
                }
        
        _LOGGER.debug(f"组织状态数据完成 - {len(organized)}个设备, {len(states_data)}个状态点")
        return organized
    
    def _compare_states(self, new_states: Dict[int, Dict[int, Dict]]) -> Dict:
        """比较新旧状态差异"""
        changes = {
            "has_changes": False,
            "changed_devices": set(),
            "changed_functions": {},  # {si: {fn: (old_value, new_value)}}
            "new_devices": set(),
            "new_functions": {},  # {si: [fn]}
        }
        
        # 比较每个设备的状态
        for si, functions in new_states.items():
            device_changed = False
            device_function_changes = {}
            device_new_functions = []
            
            # 检查是否为新设备
            if si not in self._device_states_cache:
                changes["new_devices"].add(si)
                device_changed = True
                _LOGGER.debug(f"发现新设备: si={si}")
            
            # 比较每个功能的状态
            for fn, new_state in functions.items():
                new_fv = new_state["fv"]
                
                # 获取旧状态
                old_state = self._device_states_cache.get(si, {}).get(fn, {})
                old_fv = old_state.get("fv")
                
                # 检查是否为新功能
                if si not in self._device_states_cache or fn not in self._device_states_cache[si]:
                    device_new_functions.append(fn)
                    device_changed = True
                    _LOGGER.debug(f"设备si={si}新增功能: fn={fn}, fv={new_fv}")
                
                # 检查功能值是否变化
                elif old_fv != new_fv:
                    device_function_changes[fn] = (old_fv, new_fv)
                    device_changed = True
                    _LOGGER.debug(f"设备si={si}功能变化: fn={fn}, {old_fv} -> {new_fv}")
            
            # 记录设备级别的变化
            if device_changed:
                changes["has_changes"] = True
                changes["changed_devices"].add(si)
                
                if device_function_changes:
                    changes["changed_functions"][si] = device_function_changes
                
                if device_new_functions:
                    changes["new_functions"][si] = device_new_functions
        
        return changes
    
    def _update_states_cache(self, new_states: Dict[int, Dict[int, Dict]], changes: Dict) -> None:
        """更新状态缓存"""
        for si in changes["changed_devices"]:
            if si in new_states:
                if si not in self._device_states_cache:
                    self._device_states_cache[si] = {}
                
                # 更新变化的功能状态
                for fn, state_info in new_states[si].items():
                    self._device_states_cache[si][fn] = state_info.copy()
        
        _LOGGER.debug(f"状态缓存已更新 - 缓存设备数:{len(self._device_states_cache)}")
    
    def _update_device_properties(self, changes: Dict) -> List[Dict]:
        """更新设备属性"""
        updated_devices = []
        
        for si in changes["changed_devices"]:
            # 查找对应的设备信息
            device_info = None
            for device in self._devices_cache.values():
                if device.get("si") == si:
                    device_info = device
                    break
            
            if not device_info:
                _LOGGER.warning(f"未找到设备si={si}的信息")
                continue
            
            # 更新设备状态属性 - 合并而不是覆盖
            current_states = self._device_states_cache.get(si, {})
            
            # 如果设备已有current_states，则合并新状态
            if "current_states" in device_info:
                existing_states = device_info["current_states"]
                # 合并新状态到现有状态中
                for fn, state_info in current_states.items():
                    existing_states[fn] = state_info
                device_info["current_states"] = existing_states
            else:
                # 如果没有现有状态，直接设置
                device_info["current_states"] = current_states
            
            # 根据设备类型更新特定属性
            self._update_device_specific_properties(device_info, device_info["current_states"])
            
            updated_devices.append(device_info.copy())
        
        _LOGGER.debug(f"设备属性更新完成 - {len(updated_devices)}个设备")
        return updated_devices
    
    def _update_device_specific_properties(self, device: Dict, current_states: Dict) -> None:
        """更新设备特定属性"""
        type_id = device.get("typeId")
        
        # 通用开关状态
        if 1 in current_states:
            device["is_on"] = current_states[1]["fv"] == 1
        
        # 灯具设备 (typeId=8)
        if type_id == 8:
            if 2 in current_states:
                device["brightness"] = current_states[2]["fv"]
        
        # 空调设备 (typeId=12)
        elif type_id == 12:
            if 2 in current_states:
                temp_value = current_states[2]["fv"]
                if temp_value and temp_value > 100:
                    device["target_temperature"] = temp_value / 10
                else:
                    device["target_temperature"] = temp_value
            
            if 3 in current_states:
                device["hvac_mode"] = current_states[3]["fv"]
            
            if 4 in current_states:
                device["fan_speed"] = current_states[4]["fv"]
            
            if 5 in current_states:
                current_temp = current_states[5]["fv"]
                if current_temp and current_temp > 100:
                    device["current_temperature"] = current_temp / 10
                else:
                    device["current_temperature"] = current_temp
        
        # 地暖设备 (typeId=16)
        elif type_id == 16:
            if 2 in current_states:
                temp_value = current_states[2]["fv"]
                if temp_value and temp_value > 100:
                    device["target_temperature"] = temp_value / 10
                else:
                    device["target_temperature"] = temp_value
        
        # 新风设备 (typeId=36)
        elif type_id == 36:
            if 3 in current_states:
                device["fan_speed"] = current_states[3]["fv"]
        
        # 窗帘设备 (typeId=14)
        elif type_id == 14:
            if 2 in current_states:
                device["position"] = current_states[2]["fv"]
            
            if 1 in current_states:
                control_state = current_states[1]["fv"]
                if control_state == 2:
                    device["moving_state"] = "stopped"
                elif control_state == 1:
                    device["moving_state"] = "opening"
                elif control_state == 0:
                    device["moving_state"] = "closing"
    
    def _notify_state_listeners(self, changes: Dict, updated_devices: List[Dict]) -> None:
        """通知状态变化监听者"""
        if not self._state_listeners:
            return
        
        notification_data = {
            "changes": changes,
            "updated_devices": updated_devices,
            "timestamp": datetime.now(),
        }
        
        for listener in self._state_listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    # 异步监听者需要在事件循环中调用
                    asyncio.create_task(listener(notification_data))
                else:
                    listener(notification_data)
            except Exception as err:
                _LOGGER.error(f"通知状态监听者失败: {err}", exc_info=True)
        
        _LOGGER.debug(f"已通知{len(self._state_listeners)}个状态监听者")
    
    def get_device_state(self, si: int, fn: Optional[int] = None) -> Optional[Dict]:
        """获取设备状态"""
        if si not in self._device_states_cache:
            return None
        
        if fn is None:
            return self._device_states_cache[si].copy()
        
        return self._device_states_cache[si].get(fn, {}).copy()
    
    def get_all_states(self) -> Dict[int, Dict[int, Dict]]:
        """获取所有状态缓存"""
        return copy.deepcopy(self._device_states_cache)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self._stats.copy()
    
    def clear_cache(self) -> None:
        """清空状态缓存"""
        self._device_states_cache.clear()
        self._devices_cache.clear()
        
        self._stats.update({
            "total_updates": 0,
            "devices_changed": 0,
            "functions_changed": 0,
            "cached_devices": 0,
        })
        
        _LOGGER.info("状态缓存已清空")
    
    def force_update_device(self, si: int, fn: int, fv: Any) -> None:
        """强制更新设备状态（用于主动操作后立即更新）"""
        if si not in self._device_states_cache:
            self._device_states_cache[si] = {}
        
        old_fv = self._device_states_cache[si].get(fn, {}).get("fv")
        
        self._device_states_cache[si][fn] = {
            "fv": fv,
            "st": 10101,  # 默认状态码
            "timestamp": datetime.now().timestamp()
        }
        
        _LOGGER.info(f"强制更新设备状态: si={si}, fn={fn}, {old_fv} -> {fv}")
        
        # 查找并更新对应设备信息
        for device in self._devices_cache.values():
            if device.get("si") == si:
                # 合并状态而不是覆盖
                if "current_states" in device:
                    device["current_states"][fn] = self._device_states_cache[si][fn]
                else:
                    device["current_states"] = self._device_states_cache[si].copy()
                
                self._update_device_specific_properties(device, device["current_states"])
                
                # 通知监听者
                changes = {
                    "has_changes": True,
                    "changed_devices": {si},
                    "changed_functions": {si: {fn: (old_fv, fv)}},
                    "new_devices": set(),
                    "new_functions": {},
                }
                self._notify_state_listeners(changes, [device])
                break

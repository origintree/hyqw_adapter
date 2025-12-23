"""Constants for HYQW Adapter integration."""

DOMAIN = "hyqw_adapter"

# Platforms
PLATFORMS = ["light", "climate", "fan", "cover", "switch", "sensor", "text", "select", "button"]

# Configuration
CONF_BASE_URL = "base_url"
CONF_TOKEN = "token"
CONF_DEVICE_SN = "device_sn"
CONF_PROJECT_CODE = "project_code"

# Default values
DEFAULT_BASE_URL = "http://gt.jianweisoftware.com"
DEFAULT_PROJECT_CODE = "SH-485-V22"


# Polling Bus Configuration - 轮询总线配置
POLLING_CONFIG = {
    # 长轮询间隔(秒) - 默认状态查询频率
    "long_polling_interval": 15,
    
    # 短轮询间隔(秒) - 设备操作后的高频查询
    "short_polling_interval": 1,
    
    # 短轮询持续时间(秒) - 高频查询持续多长时间
    "short_polling_duration": 5,
}

# MQTT Configuration - MQTT配置
MQTT_CONFIG = {
    # 默认MQTT配置
    "default_port": 1883,
    "default_keepalive": 60,
    "reconnect_intervals": [1, 2, 5, 10],  # 重连间隔序列(秒)
    "max_reconnect_interval": 10,  # 最大重连间隔(秒)
    
    # 兜底巡检配置
    "fallback_check_intervals": {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "10m": 600,
        "20m": 1200, 
        "30m": 1800,
        "disabled": 0,
    },
    "default_fallback_interval": 600,  # 默认10分钟
    
    # 默认启用设置
    "default_startup_enable": False,  # 插件启动时是否默认启用MQTT
    "default_optimistic_echo": False,  # 默认乐观回显关闭
    
    # 本地广播配置
    "local_broadcast_interval": 15,  # 本地广播间隔(秒)
    "local_broadcast_topic": "SERVER/BROADCAST",  # 本地广播主题
}

# MQTT相关常量
CONF_MQTT_HOST = "mqtt_host"
CONF_MQTT_PORT = "mqtt_port"  
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_CLIENT_ID = "mqtt_client_id"
CONF_MQTT_STARTUP_ENABLE = "mqtt_startup_enable"
CONF_MQTT_OPTIMISTIC_ECHO = "mqtt_optimistic_echo"
CONF_MQTT_FALLBACK_INTERVAL = "mqtt_fallback_interval"
CONF_MQTT_LOCAL_BROADCAST_ENABLED = "mqtt_local_broadcast_enabled"

# Replay/Record 相关常量
CONF_REPLAY_ENABLED = "replay_enabled"
REPLAY_STORAGE_FILENAME = "hyqw_adapter_replay.yaml"

# Device types mapping from API
DEVICE_TYPES = {
    8: "light",      # 灯具
    12: "climate",   # 空调
    14: "cover",     # 窗帘
    16: "climate",   # 地暖 (作为climate设备)
    36: "fan",   # 新风 (作为fan设备)
}

# Device function codes
DEVICE_FUNCTIONS = {
    "light": {
        "turn_on": {"fn": 1, "fv": 1},
        "turn_off": {"fn": 1, "fv": 0},
        "brightness": {"fn": 2, "fv": None},  # fv will be brightness value
    },
    "cover": {
        "open": {"fn": 1, "fv": 1},
        "close": {"fn": 1, "fv": 0},
        "stop": {"fn": 1, "fv": 2},
        "set_position": {"fn": 2, "fv": None},  # fv will be position percentage
    },
    "switch": {
        "turn_on": {"fn": 1, "fv": 1},
        "turn_off": {"fn": 1, "fv": 0},
    },
    "fan": {
        "turn_on": {"fn": 1, "fv": 1},
        "turn_off": {"fn": 1, "fv": 0},
        "set_fan_speed": {"fn": 3, "fv": None},  # fn3风力设置, fv0自动/1微风/2大风/3强风
    }
}

# Climate device function codes by type
CLIMATE_FUNCTIONS = {
    # 空调 (typeId=12)
    12: {
        "turn_on": {"fn": 1, "fv": 1},      # fn开关, fv1打开
        "turn_off": {"fn": 1, "fv": 0},     # fn开关, fv0关闭
        "set_temperature": {"fn": 2, "fv": None},  # fn2温度设置, fv 18-29
        "set_mode": {"fn": 3, "fv": None},  # fn3模式设置, fv0制冷/1制热/2通风/3除湿
        "set_fan_speed": {"fn": 4, "fv": None},  # fn4风力设置, fv0自动/1微风/2大风/3强风
    },
    # 地暖 (typeId=16)
    16: {
        "turn_on": {"fn": 1, "fv": 1},      # fn1开关, fv1打开
        "turn_off": {"fn": 1, "fv": 0},     # fn1开关, fv0关闭
        "set_temperature": {"fn": 2, "fv": None},  # fn2温度设置, fv 5-35
    },
}

# Climate modes mapping (for air conditioner)
AC_MODES = {
    0: "cool",      # 制冷
    1: "heat",      # 制热
    2: "fan_only",  # 通风
    3: "dry",       # 除湿
}

AC_MODES_REVERSE = {v: k for k, v in AC_MODES.items()}

# Fan speed mapping
FAN_SPEEDS = {
    0: "auto",      # 自动风力
    1: "low",       # 微风
    2: "medium",    # 大风
    3: "high",      # 强风
}

FAN_SPEEDS_REVERSE = {v: k for k, v in FAN_SPEEDS.items()}

# Device specific configurations
DEVICE_CONFIGS = {
    # 空调配置
    12: {
        "name": "空调",
        "hvac_modes": ["off", "cool", "heat", "fan_only", "dry"],
        "fan_modes": ["auto", "low", "medium", "high"],
        "min_temp": 18,
        "max_temp": 29,
        "temp_step": 1,
        "supports_mode": True,
        "supports_fan": True,
    },
    # 地暖配置
    16: {
        "name": "地暖",
        "hvac_modes": ["off", "heat"],
        "fan_modes": None,
        "min_temp": 5,
        "max_temp": 35,
        "temp_step": 1,
        "supports_mode": False,
        "supports_fan": False,
    },
}

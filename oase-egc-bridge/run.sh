#!/usr/bin/with-contenv bashio
export CONTROLLER_IP="$(bashio::config 'controller_ip')"
export DEVICE_PASSWORD="$(bashio::config 'device_password')"
export POLL_INTERVAL="$(bashio::config 'poll_interval')"
export ENABLE_CONTROL="$(bashio::config 'enable_control')"
export MODE="$(bashio::config 'mode')"
export LOG_LEVEL="$(bashio::config 'log_level')"
# MQTT aus dem Supervisor-Service
if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  export MQTT_USER="$(bashio::services 'mqtt' 'username')"
  export MQTT_PASS="$(bashio::services 'mqtt' 'password')"
fi
exec python3 /app/oase_bridge.py

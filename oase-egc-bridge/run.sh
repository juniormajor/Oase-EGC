#!/usr/bin/with-contenv bashio
export CONTROLLER_IP="$(bashio::config 'controller_ip')"
export DEVICE_PASSWORD="$(bashio::config 'device_password')"
export POLL_INTERVAL="$(bashio::config 'poll_interval')"
export ENABLE_CONTROL="$(bashio::config 'enable_control')"
export MODE="$(bashio::config 'mode')"
export LOG_LEVEL="$(bashio::config 'log_level')"

if bashio::config.has_value 'mqtt_host'; then
  export MQTT_HOST="$(bashio::config 'mqtt_host')"
  export MQTT_PORT="$(bashio::config 'mqtt_port')"
  export MQTT_USER="$(bashio::config 'mqtt_user')"
  export MQTT_PASS="$(bashio::config 'mqtt_pass')"
  bashio::log.info "MQTT aus Add-on-Optionen: ${MQTT_HOST}:${MQTT_PORT}"
elif bashio::services.available 'mqtt'; then
  export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  export MQTT_USER="$(bashio::services 'mqtt' 'username')"
  export MQTT_PASS="$(bashio::services 'mqtt' 'password')"
  bashio::log.info "MQTT aus Supervisor-Service: ${MQTT_HOST}:${MQTT_PORT}"
else
  bashio::log.warning "Kein MQTT konfiguriert (weder Optionen noch Supervisor-Service)."
fi

exec python3 /app/oase_bridge.py

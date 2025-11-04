# Symlink Setup Instructions

This document provides the exact commands to update your Home Assistant configuration to use the entity definitions from this AppDaemon pyheat repository.

## Current Setup (PyScript)

Your pyscript version currently has symlinks like:
```
/opt/appdata/hass/homeassistant/yaml/input_boolean/pyheat_input_booleans.yaml 
  -> /home/pete/tmp/pyheat_pyscript/ha_yaml/pyheat_input_booleans.yaml
```

## New Setup (AppDaemon)

Replace these symlinks to point to the AppDaemon pyheat repository instead.

### Step 1: Remove old symlinks

```bash
cd /opt/appdata/hass/homeassistant/yaml

# Remove old pyscript symlinks
rm -f input_boolean/pyheat_input_booleans.yaml
rm -f input_select/pyheat_input_selects.yaml
rm -f input_number/pyheat_input_numbers.yaml
rm -f timer/pyheat_timers.yaml
rm -f input_datetime/pyheat_input_datetimes.yaml
rm -f input_text/pyheat_input_texts.yaml
rm -f template/pyheat_template_sensors.yaml
rm -f sensor/pyheat_mqtt_sensor.yaml
rm -f climate/pyheat_climate.yaml
```

### Step 2: Create new symlinks

```bash
cd /opt/appdata/hass/homeassistant/yaml

# Create symlinks to AppDaemon pyheat repo
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_booleans.yaml input_boolean/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_selects.yaml input_select/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_numbers.yaml input_number/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_timers.yaml timer/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_datetimes.yaml input_datetime/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_texts.yaml input_text/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_template_sensors.yaml template/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_mqtt_sensor.yaml sensor/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_climate.yaml climate/
```

### Step 3: Verify symlinks

```bash
ls -la /opt/appdata/hass/homeassistant/yaml/input_boolean/pyheat_input_booleans.yaml
ls -la /opt/appdata/hass/homeassistant/yaml/input_select/pyheat_input_selects.yaml
# etc...
```

Each should show:
```
lrwxrwxrwx ... /opt/appdata/hass/homeassistant/yaml/input_boolean/pyheat_input_booleans.yaml -> /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_booleans.yaml
```

### Step 4: Reload Home Assistant

After updating the symlinks, reload the relevant integrations in Home Assistant:
- Developer Tools → YAML → Reload Input Booleans
- Developer Tools → YAML → Reload Input Selects
- Developer Tools → YAML → Reload Input Numbers
- Developer Tools → YAML → Reload Timers
- Developer Tools → YAML → Reload Template Entities
- etc...

Or restart Home Assistant entirely.

## Benefits

With this setup:
- ✅ All entity definitions are version controlled in the AppDaemon pyheat repo
- ✅ Changes to entities are tracked with git commits
- ✅ Easy to sync between development and production environments
- ✅ Single source of truth for pyheat configuration
- ✅ Can safely archive/delete the pyscript project

## Verification

After setup, verify all entities exist:
```bash
# Check via Home Assistant API
source /opt/appdata/hass/homeassistant/.env.hass
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  "${HA_BASE_URL_LOCAL}/api/states" | \
  jq '.[] | select(.entity_id | contains("pyheat")) | .entity_id' | \
  sort
```

You should see all pyheat entities listed.

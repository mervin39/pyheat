# Home Assistant Entity Definitions

This directory contains the YAML configuration files for all Home Assistant helper entities used by PyHeat.

## Entities Included

- **pyheat_input_booleans.yaml** - Master enable and holiday mode toggles
- **pyheat_input_selects.yaml** - Per-room heating mode selectors (Auto/Manual/Off)
- **pyheat_input_numbers.yaml** - Manual setpoints and override targets for each room
- **pyheat_input_datetimes.yaml** - Datetime helpers for scheduling
- **pyheat_input_texts.yaml** - Text inputs for status messages
- **pyheat_timers.yaml** - Timers for override/boost functionality
- **pyheat_template_sensors.yaml** - Template sensors for derived values
- **pyheat_mqtt_sensor.yaml** - MQTT sensor definitions (if applicable)
- **pyheat_climate.yaml** - Climate entity configurations

## Installation

These files need to be included in your Home Assistant configuration. There are two approaches:

### Option 1: Symlink (Recommended for development)

Create symlinks from your Home Assistant config directory to these files:

```bash
# From your Home Assistant config directory
mkdir -p yaml/input_boolean
mkdir -p yaml/input_select
mkdir -p yaml/input_number
mkdir -p yaml/timer
mkdir -p yaml/input_datetime
mkdir -p yaml/input_text
mkdir -p yaml/template
mkdir -p yaml/sensor
mkdir -p yaml/climate

# Create symlinks
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_booleans.yaml yaml/input_boolean/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_selects.yaml yaml/input_select/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_numbers.yaml yaml/input_number/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_timers.yaml yaml/timer/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_datetimes.yaml yaml/input_datetime/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_input_texts.yaml yaml/input_text/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_template_sensors.yaml yaml/template/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_mqtt_sensor.yaml yaml/sensor/
ln -s /opt/appdata/appdaemon/conf/apps/pyheat/ha_yaml/pyheat_climate.yaml yaml/climate/
```

Then in your `configuration.yaml`, ensure you have:

```yaml
input_boolean: !include_dir_merge_named yaml/input_boolean
input_select: !include_dir_merge_named yaml/input_select
input_number: !include_dir_merge_named yaml/input_number
timer: !include_dir_merge_named yaml/timer
input_datetime: !include_dir_merge_named yaml/input_datetime
input_text: !include_dir_merge_named yaml/input_text
template: !include_dir_merge_list yaml/template
sensor: !include_dir_merge_list yaml/sensor
climate: !include_dir_merge_list yaml/climate
```

### Option 2: Direct Copy

Copy the files to your Home Assistant config directory and include them directly:

```yaml
input_boolean: !include pyheat_input_booleans.yaml
input_select: !include pyheat_input_selects.yaml
# ... etc
```

## Required Entities

PyHeat expects the following entities to exist in Home Assistant:

### Global Controls
- `input_boolean.pyheat_master_enable` - System on/off
- `input_boolean.pyheat_holiday_mode` - Holiday mode toggle
- `input_boolean.pyheat_boiler_actor` - Boiler control (virtual switch)

### Per-Room Controls (for each room: pete, abby, office, lounge, games, bathroom)
- `input_select.pyheat_{room}_mode` - Heating mode (Auto/Manual/Off)
- `input_number.pyheat_{room}_manual_setpoint` - Manual temperature target
- `input_number.pyheat_{room}_override_target` - Override/boost target
- `timer.pyheat_{room}_override` - Override/boost timer

### Status Entity
- `sensor.pyheat_status` - System status (published by AppDaemon app)

After adding these files, restart Home Assistant or reload the relevant integrations.

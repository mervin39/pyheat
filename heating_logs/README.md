# Heating Logs Data Dictionary

This directory contains daily CSV files with comprehensive heating system state logs for analysis and optimization.

## 📋 Purpose

These logs are **temporary data collection** for developing OpenTherm optimization algorithms. They capture the complete state of the heating system including:
- OpenTherm boiler sensors (temperatures, modulation, flame status)
- Domestic Hot Water (DHW) demand
- Room-by-room heating demand and valve positions
- Boiler finite state machine transitions

## 📁 File Structure

- **Filename format**: `YYYY-MM-DD.csv` (e.g., `2025-11-20.csv`)
- **Rotation**: Automatic daily rotation at midnight
- **Recovery**: Files automatically recreated if deleted while system is running

## 📊 Column Reference

### Timestamp & Metadata

| Column | Type | Description |
|--------|------|-------------|
| `date` | Date | Log entry date (YYYY-MM-DD format) |
| `time` | Time | Log entry time (HH:MM:SS format, 24-hour) |
| `trigger` | String | What caused this log entry (see Trigger Types below) |
| `trigger_val` | Mixed | Current value of whatever triggered this entry (for quick visual scanning) |

### OpenTherm Sensors

| Column | Type | Range | Description |
|--------|------|-------|-------------|
| `ot_flame` | Binary | on/off | Boiler flame status (burner active) |
| `ot_heating_temp` | Integer | 0-100°C | Flow temperature (water leaving boiler) - logged as whole degrees |
| `ot_return_temp` | Integer | 0-100°C | Return temperature (water returning to boiler) - logged as whole degrees |
| `ot_modulation` | Float | 0-100% | Boiler modulation level (burner power) |
| `ot_power` | Float | 0-100% | Current power consumption percentage |
| `ot_burner_starts` | Integer | Counter | Cumulative CH (Central Heating) burner starts since boiler installation |
| `ot_dhw_burner_starts` | Integer | Counter | Cumulative DHW burner starts since boiler installation |
| `ot_dhw` | Binary | on/off | Domestic Hot Water demand status |
| `ot_dhw_flow` | Binary | on/off | DHW flow rate status (on = water flowing, off = no flow) |
| `ot_climate_state` | String | heat/idle/off | OpenTherm climate entity state |
| `ot_setpoint_temp` | Float | 0-100°C | CH setpoint temperature (target flow temp), rounded to 2 decimal places |

### Boiler State (PyHeat FSM)

| Column | Type | Values | Description |
|--------|------|--------|-------------|
| `boiler_state` | String | idle/heating/pump_overrun/off | Current boiler finite state machine state |
| `pump_overrun_active` | Boolean | True/False | Whether pump overrun period is active (circulation after flame off) |

### System Aggregates

| Column | Type | Description |
|--------|------|-------------|
| `num_rooms_calling` | Integer | Count of rooms currently calling for heat |
| `total_valve_pct` | Integer | Sum of all TRV valve feedback positions (0-100% per room) |

### Per-Room Data

For each configured room (e.g., `pete`, `games`, `lounge`, `beth`, `main`), the following columns exist:

| Column Pattern | Type | Description |
|----------------|------|-------------|
| `{room}_temp` | Float | Current room temperature (°C), rounded to 2 decimal places |
| `{room}_target` | Float | Target temperature for this room (°C), rounded to 2 decimal places |
| `{room}_calling` | Boolean | True if room is calling for heat (temp below target - hysteresis) |
| `{room}_valve_fb` | Integer | TRV valve feedback position (0-100%) |
| `{room}_valve_cmd` | Integer | Last commanded valve position (0-100%) |
| `{room}_mode` | String | Room mode: `auto` (schedule), `manual`, or `off` |
| `{room}_override` | Boolean | True if temporary override timer is active |

**Room Column Order**: Columns are grouped by property type (all temps together, all targets together, etc.) for easier analysis.

## 🎯 Trigger Types

The `trigger` column indicates what event caused the log entry:

### OpenTherm Triggers
- `opentherm_flame` - Flame turned on/off
- `opentherm_heating_temp` - Flow temp changed by ≥1°C (whole degree)
- `opentherm_heating_return_temp` - Return temp changed by ≥1°C (whole degree)
- `opentherm_heating_setpoint_temp` - Setpoint changed (any change, manual control)
- `opentherm_modulation` - Modulation changed (any change, immediate log)
- `opentherm_dhw` - DHW demand state changed (on ↔ off)
- `opentherm_dhw_flow_rate` - DHW flow changed (zero ↔ nonzero transition only)

### Boiler State Triggers
- `boiler_state_change` - Boiler FSM state changed
- `boiler_*` - Various boiler-related state changes

### Room Triggers
- `{room}_calling` - Room calling status changed
- `{room}_valve_fb` - TRV valve feedback position changed
- `{room}_mode` - Room mode changed
- `{room}_override` - Override status changed

### Other Triggers
- `first_run` - Initial baseline log entry
- `recompute` - Periodic system recompute
- Various system events

## 📈 Logging Behavior

### Smart Filtering
Logs are written only when **significant changes** occur to reduce file size while capturing important events:

- ✅ **Always logged**: Boiler state changes, flame on/off, room calling changes, DHW state changes
- ✅ **Threshold-filtered**: Heating/return temps (≥1°C change), DHW flow (zero ↔ nonzero)
- ✅ **Immediate**: Manual controls (setpoint, modulation), mode changes, overrides
- ❌ **Filtered out**: Tiny temperature fluctuations, DHW flow rate changes between nonzero values

### Temperature Precision
- **Flow/return temps**: Logged as integers (whole degrees) to reduce log noise
- **Room temps**: Logged to 2 decimal places for accuracy
- **Setpoint temp**: Logged to 2 decimal places (manual control input)

### Data Quality
- **Empty values**: Shown as empty string if sensor unavailable/unknown
- **Boolean values**: Logged as `True`/`False`
- **Binary sensors**: Logged as `on`/`off`
- **Flush on write**: Data is immediately flushed to disk (no buffering)

## 🔍 Analysis Tips

### Useful Queries

**When does the boiler cycle?**
```bash
csvgrep -c ot_flame -m "on" 2025-11-20.csv | csvlook
```

**What's the modulation when heating each room?**
```bash
csvcut -c trigger,ot_modulation,pete_calling,games_calling,lounge_calling 2025-11-20.csv | csvlook
```

**DHW impact on heating:**
```bash
csvcut -c time,ot_dhw,ot_dhw_flow,ot_modulation,ot_heating_temp 2025-11-20.csv | csvlook
```

**Room calling patterns:**
```bash
csvcut -c time,num_rooms_calling,total_valve_pct,ot_modulation 2025-11-20.csv | csvlook
```

### Recommended Tools
- **csvkit** (csvlook, csvcut, csvgrep, csvstat) - Command-line CSV analysis
- **pandas** (Python) - Comprehensive data analysis
- **Excel/LibreOffice Calc** - Visual analysis and charting
- **R** - Statistical analysis

## 🗑️ Removal

This is a **temporary feature** for data collection. Once sufficient data is gathered:

1. Archive useful CSV files elsewhere
2. Disable logging by setting `ENABLE_HEATING_LOGS = False` in `constants.py`
3. Remove `heating_logger.py` module
4. Remove integration points from `app.py`
5. Delete `heating_logs/` directory

## 📝 Notes

- Files are **gitignored** - will not be committed to version control
- Daily files persist until manually deleted
- No disk space checks - monitor usage if logging long-term
- Logged in UTC timezone (same as AppDaemon)

---

**Generated by PyHeat Heating Logger v1.0**  
Last updated: 2025-11-20

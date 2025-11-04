# Pyheat Changelog

## [Unreleased]

### Added
- TRV setpoint locking: All TRVs locked to 5°C to force "always open" mode
- Non-blocking valve control using AppDaemon scheduler instead of time.sleep()
- Automatic TRV setpoint monitoring and correction (every 5 minutes)
- Initial PyHeat AppDaemon implementation
- Core heating logic with sensor fusion, target resolution, hysteresis
- Sequential TRV valve control with feedback confirmation (simplified to single command)
- Basic boiler control (on/off based on room demand)
- Configuration loading from rooms.yaml and schedules.yaml
- Status publishing to sensor.pyheat_status
- Callback infrastructure for helper entity changes
- Support for manual mode, schedule mode, and holiday mode

### Changed
- **Major simplification**: Valve control now only sends opening_degree command (not opening+closing)
- Eliminated blocking time.sleep() calls - now uses run_in() scheduler callbacks
- TRV control reduced from 4s per room to 2s per room (50% faster)
- No more AppDaemon callback timeout warnings during startup
- Room configuration now derives all TRV entities from climate entity

### Fixed
- AppDaemon thread blocking warnings eliminated
- TRV valve thrashing prevented through setpoint locking strategy

## [Previous - PyScript Version]
- Multiple issues with state consistency
- Insurmountable pyscript problems creating single, consistent state

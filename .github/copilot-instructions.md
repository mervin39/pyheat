# Copilot Workspace Instructions

**Scope:** Applies to all Copilot Chat in this repository.

If I ask you to implement code, you must make changes to the relevant files, don't just output the files in the chat window

## Home Assistant API access
If you need access to the Home Assistant API, you must first load the environment variables:
source /opt/appdata/hass/homeassistant/.env.hass
HA_BASE_URL, HA_BASE_URL_LOCAL, HA_BASE_URL_LAN and HA_TOKEN

## Home Assistant PyScript
If you are planning or writing pyscript, always check the documenation first at https://hacs-pyscript.readthedocs.io/en/latest/reference.html
Note that pyscript does not support generator expressions, so avoid using them in pyscript code.

## Home Assistant
Make sure to read the official documentation for Home Assistant before suggesting changes that involve Home Assistant features, configurations, or integrations.

Home Assistant documentation can be found at https://www.home-assistant.io/docs/
Home Assistant API documentation can be found at https://homeassistantapi.readthedocs.io/en/latest/


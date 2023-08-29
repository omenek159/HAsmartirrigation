"""The Smart Irrigation Integration."""
from datetime import timedelta
import datetime
import logging
import os
import asyncio
import statistics


from homeassistant.core import (
    callback,
)
from homeassistant.components.sensor import DOMAIN as PLATFORM
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, asyncio
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
)
from homeassistant.util.unit_system import METRIC_SYSTEM

from . import const
from .localize import localize
from .store import async_get_registry
from .panel import (
    async_register_panel,
    async_unregister_panel,
)
from .helpers import (check_time, convert_between, convert_mapping_to_metric, loadModules)
from .websockets import async_register_websockets

from .OWMClient import OWMClient

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    """Track states and offer events for sensors."""
    #this did not work. Users will have to reload the integration / i.e. restart HA if they make this change.
    #listener for core config changes (for unit changes)
    #hass.bus.listen("core_config_updated", handle_core_config_change)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Smart Irrigation from a config entry."""
    session = async_get_clientsession(hass)

    store = await async_get_registry(hass)
    #store OWM info in hass.data
    hass.data.setdefault(const.DOMAIN, {})
    hass.data[const.DOMAIN][const.CONF_USE_OWM]= entry.data.get(const.CONF_USE_OWM)
    if hass.data[const.DOMAIN][const.CONF_USE_OWM]:
        if const.CONF_OWM_API_KEY in entry.data:
            hass.data[const.DOMAIN][const.CONF_OWM_API_KEY] = entry.data.get(const.CONF_OWM_API_KEY).strip()
        hass.data[const.DOMAIN][const.CONF_OWM_API_VERSION] = entry.data.get(const.CONF_OWM_API_VERSION)

    #logic here is: if options are set that do not agree with the data settings, use the options
    #handle options flow data
    if const.CONF_USE_OWM in entry.options and entry.options.get(const.CONF_USE_OWM) != entry.data.get(const.CONF_USE_OWM):
        hass.data[const.DOMAIN][const.CONF_USE_OWM] = entry.options.get(const.CONF_USE_OWM)
        if const.CONF_OWM_API_KEY in entry.options:
            hass.data[const.DOMAIN][const.CONF_OWM_API_KEY] = entry.options.get(const.CONF_OWM_API_KEY).strip()
        hass.data[const.DOMAIN][const.CONF_OWM_API_VERSION] = entry.options.get(const.CONF_OWM_API_VERSION)

    coordinator = SmartIrrigationCoordinator(hass, session, entry, store)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(const.DOMAIN, coordinator.id)},
        name=const.NAME,
        model=const.NAME,
        sw_version=const.VERSION,
        manufacturer=const.MANUFACTURER,
    )

    hass.data[const.DOMAIN]["coordinator"] = coordinator
    hass.data[const.DOMAIN]["zones"]={}

    # make sure we capture the use_owm state
    store.async_update_config({const.CONF_USE_OWM: coordinator.use_OWM})

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=coordinator.id, data={})

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, PLATFORM)
    )

    #update listener for options flow
    entry.async_on_unload(entry.add_update_listener(options_update_listener))

    # Register the panel (frontend)
    await async_register_panel(hass)

    # Websocket support
    await async_register_websockets(hass)

    # Register custom services
    register_services(hass)

    # Finish up by setting factory defaults if needed for zones, mappings and modules
    await store.set_up_factory_defaults()

    return True



async def options_update_listener(
    hass, config_entry
):
    """Handle options update."""
    # copy the api key and version to the hass data
    hass.data[const.DOMAIN][const.CONF_USE_OWM] = config_entry.options.get(const.CONF_USE_OWM)
    if const.CONF_OWM_API_KEY in config_entry.options:
        hass.data[const.DOMAIN][const.CONF_OWM_API_KEY] = config_entry.options.get(const.CONF_OWM_API_KEY).strip()
    hass.data[const.DOMAIN][const.CONF_OWM_API_VERSION] = config_entry.options.get(const.CONF_OWM_API_VERSION)
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass, entry):
    """Unload Smart Irrigation config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[hass.config_entries.async_forward_entry_unload(entry, PLATFORM)]
        )
    )
    if not unload_ok:
        return False

    async_unregister_panel(hass)
    coordinator = hass.data[const.DOMAIN]["coordinator"]
    await coordinator.async_unload()
    return True


async def async_remove_entry(hass, entry):
    """Remove Smart Irrigation config entry."""
    async_unregister_panel(hass)
    coordinator = hass.data[const.DOMAIN]["coordinator"]
    await coordinator.async_delete_config()
    del hass.data[const.DOMAIN]


class SmartIrrigationCoordinator(DataUpdateCoordinator):
    """Define an object to hold Smart Irrigation device."""

    def __init__(self, hass, session, entry, store):
        """Initialize."""
        self.id = entry.unique_id
        self.hass = hass
        self.entry = entry
        self.store = store
        self.use_OWM = hass.data[const.DOMAIN][const.CONF_USE_OWM]
        if self.use_OWM:
            self._OWMClient = OWMClient(hass.data[const.DOMAIN][const.CONF_OWM_API_KEY],
                                       hass.data[const.DOMAIN][const.CONF_OWM_API_VERSION],
                                    self.hass.config.as_dict().get(CONF_LATITUDE),
                                    self.hass.config.as_dict().get(CONF_LONGITUDE))
        self._subscriptions = []

        self._subscriptions.append(
            async_dispatcher_connect(
                hass,
                const.DOMAIN + "_platform_loaded",
                self.setup_SmartIrrigation_entities,
            )
        )
        self._track_auto_calc_time_unsub = None
        self._track_auto_update_time_unsub = None
        self._track_sunrise_event_unsub = None
        # set up auto calc time and auto update time from data
        the_config = self.store.async_get_config()
        if the_config[const.CONF_AUTO_UPDATE_ENABLED]:
            hass.loop.create_task(self.set_up_auto_update_time(the_config))
        if the_config[const.CONF_AUTO_CALC_ENABLED]:
            hass.loop.create_task(self.set_up_auto_calc_time(the_config))


        #set up sunrise tracking
        self.register_start_event()

        super().__init__(hass, _LOGGER, name=const.DOMAIN)

    @callback
    def setup_SmartIrrigation_entities(self):
        zones = self.store.async_get_zones()
        #self.store.async_get_modules()
        #self.store.async_get_config()

        for zone in zones:
            # self.async_create_zone(zone)
            async_dispatcher_send(self.hass, const.DOMAIN + "_register_entity", zone)

    async def async_update_config(self, data):
        #handle auto calc changes
        await self.set_up_auto_calc_time(data)
        #handle auto update changes
        await self.set_up_auto_update_time(data)
        self.store.async_update_config(data)
        async_dispatcher_send(self.hass, const.DOMAIN + "_config_updated")

    async def set_up_auto_update_time(self,data):
        if data[const.CONF_AUTO_UPDATE_ENABLED]:
            #CONF_AUTO_UPDATE_SCHEDULE: minute, hour, day
            #CONF_AUTO_UPDATE_INTERVAL: X
            #CONF_AUTO_UPDATE_TIME: first update time
            if check_time(data[const.CONF_AUTO_UPDATE_TIME]):
                #first auto update time is valid
                #update only the actual changed value: auto update time
                timesplit = data[const.CONF_AUTO_UPDATE_TIME].split(":")
                if self._track_auto_update_time_unsub:
                    self._track_auto_update_time_unsub()
                self._track_auto_update_time_unsub = async_track_time_change(
                    self.hass,
                    self._async_track_update_time,
                    hour=timesplit[0],
                    minute=timesplit[1],
                    second=0
                )
                _LOGGER.info("Scheduled auto update first time update for {}".format(data[const.CONF_AUTO_UPDATE_TIME]))
            else:
                _LOGGER.warning("Schedule auto update time is not valid: {}".format(data[const.CONF_AUTO_UPDATE_TIME]))
                raise ValueError("Time is not a valid time")
        else:
            # remove all time trackers for auto update
            if self._track_auto_update_time_unsub:
                self._track_auto_update_time_unsub()
            self.store.async_update_config(data)

    async def set_up_auto_calc_time(self, data):
        if data[const.CONF_AUTO_CALC_ENABLED]:
            #make sure to unsub any existing and add for calc time
            if check_time(data[const.CONF_CALC_TIME]):
                #make sure we track this time and at that moment trigger the refresh of all modules of all zones that are on automatic
                timesplit = data[const.CONF_CALC_TIME].split(":")
                #unsubscribe from any existing track_time_changes
                if self._track_auto_calc_time_unsub:
                    self._track_auto_calc_time_unsub()
                self._track_auto_calc_time_unsub = async_track_time_change(
                    self.hass,
                    self._async_calculate_all,
                    hour=timesplit[0],
                    minute=timesplit[1],
                    second=0
                )
                _LOGGER.info("Scheduled auto calculate update for {}".format(data[const.CONF_CALC_TIME]))
            else:
                _LOGGER.warning("Schedule auto calculate time is not valid: {}".format(data[const.CONF_AUTO_UPDATE_TIME]))
                raise ValueError("Time is not a valid time")
        else:
            #remove all time trackers
            if self._track_auto_calc_time_unsub:
                self._track_auto_calc_time_unsub()
            self.store.async_update_config(data)

    async def _async_track_update_time(self, *args):
        #perform update once
        await self._async_update_all()
        #use async_track_time_interval
        data = self.store.async_get_config()
        the_time_delta = None
        interval = int(data[const.CONF_AUTO_UPDATE_INTERVAL])
        if data[const.CONF_AUTO_UPDATE_SCHEDULE] == const.CONF_AUTO_UPDATE_DAILY:
            # track time X days
            the_time_delta = timedelta(days=interval)
        elif data[const.CONF_AUTO_UPDATE_SCHEDULE] == const.CONF_AUTO_UPDATE_HOURLY:
            # track time X hours
            the_time_delta = timedelta(hours=interval)
        elif data[const.CONF_AUTO_UPDATE_SCHEDULE] == const.CONF_AUTO_UPDATE_MINUTELY:
            #track time X minutes
            the_time_delta = timedelta(minutes=interval)
        async_track_time_interval(self.hass, self._async_update_all,the_time_delta)
        _LOGGER.info("Scheduled auto update time interval for each {}".format(the_time_delta))

    async def _get_unique_mappings_for_automatic_zones(self, zones):
        mappings = []
        for zone in zones:
            if zone.get(const.ZONE_STATE)==const.ZONE_STATE_AUTOMATIC:
                mappings.append(zone.get(const.ZONE_MAPPING))
        #remove duplicates
        mappings = list(set(mappings))
        return mappings

    async def _async_update_all(self, *args):
        #update the sensor data for all mappings for all zones that are automatic here and store it.
        #in _async_calculate_all we need to read that data back and if there is none, we log an error, otherwise apply aggregate and use data
        _LOGGER.info("Updating sensor data for all automatic zones")
        zones = self.store.async_get_zones()
        mappings = await self._get_unique_mappings_for_automatic_zones(zones)
        #loop over the mappings and store sensor data
        for mapping_id in mappings:
            owm_in_mapping, sensor_in_mapping = self.check_mapping_sources(mapping_id = mapping_id)

            if sensor_in_mapping:
                mapping = self.store.async_get_mapping(mapping_id)
                sensor_values = self.build_sensor_values_for_mapping(mapping)

                #add the sensor value to the mappings sensor values
                mapping_sensor_values = mapping[const.MAPPING_DATA]
                mapping_sensor_values.append(sensor_values)
                changes = {"data": mapping_sensor_values}
                self.store.async_update_mapping(mapping_id,changes)

    async def apply_aggregates_to_mapping_data(self, mapping):
        if mapping.get(const.MAPPING_DATA):
            #get the keys for the mapping data entries.
            #if there are more than one values for a given key, apply configured aggregate
            data = mapping.get(const.MAPPING_DATA)
            data_by_sensor = {}
            resultdata = {}
            for d in data:
                if isinstance(d, dict):
                    for key,val in d.items():
                        if key not in data_by_sensor:
                            data_by_sensor[key] = [val]
                        else:
                            data_by_sensor[key].append(val)
            for key,d in data_by_sensor.items():
                if len(d) > 1:
                    #apply aggregate
                    aggregate = const.MAPPING_CONF_AGGREGATE_OPTIONS_DEFAULT
                    if mapping.get(const.MAPPING_MAPPINGS):
                        mappings = mapping.get(const.MAPPING_MAPPINGS)
                        if key in mappings:
                            aggregate = mappings[key].get(const.MAPPING_CONF_AGGREGATE, const.MAPPING_CONF_AGGREGATE_OPTIONS_DEFAULT)
                    if aggregate == const.MAPPING_CONF_AGGREGATE_AVERAGE:
                        resultdata[key] = statistics.mean(d)
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_FIRST:
                        resultdata[key] = d[0]
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_LAST:
                        resultdata[key] = d[len(d)-1]
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_MAXIMUM:
                        resultdata[key] = max(d)
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_MINIMUM:
                        resultdata[key] = min(d)
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_MEDIAN:
                        resultdata[key] = statistics.median(d)
                    elif aggregate == const.MAPPING_CONF_AGGREGATE_SUM:
                        resultdata[key] = sum(d)
                else:
                    resultdata[key] = d[0]
            return resultdata
        return None

    async def _async_calculate_all(self, *args):
        _LOGGER.info("Calculating all automatic zones")
        weatherdata = {}
        #get all zones that are in automatic and for all of those, loop over the unique list of mappings
        #are any modules using OWM / sensors?
        owm_in_mapping = False
        zones = self.store.async_get_zones()
        mappings = await self._get_unique_mappings_for_automatic_zones(zones)
        for mapping_id in mappings:
            o, s = self.check_mapping_sources(mapping_id = mapping_id)
            if o:
                owm_in_mapping = True
        #at least part of the data comes from OWM
        if self.use_OWM and owm_in_mapping:
            # data comes at least partly from owm
            weatherdata = await self.hass.async_add_executor_job(self._OWMClient.get_data)

        #loop over zones again and update weatherdata with mapping, then calculate
        for zone in zones:
            if zone.get(const.ZONE_STATE)==const.ZONE_STATE_AUTOMATIC:
                mapping_id = zone[const.ZONE_MAPPING]
                o_i_m, s_i_m = self.check_mapping_sources(mapping_id = mapping_id)
                # if using pyeto and using a forecast o_i_m needs to be set to true!
                modinst = self.getModuleInstanceByID(zone.get(const.ZONE_MODULE))
                if modinst and modinst.name=="PyETO" and modinst._forecast_days>0:
                    o_i_m=True

                mapping = self.store.async_get_mapping(mapping_id)
                mapping_weatherdata = {}
                #at least part of the data comes from OWM, so capture it
                if o_i_m:
                    mapping_weatherdata = weatherdata
                #if there is sensor data on the mapping, apply aggregates to it.
                sensor_values = None
                if mapping and mapping.get(const.MAPPING_DATA):
                    sensor_values = await self.apply_aggregates_to_mapping_data(mapping)
                precip_from_sensor = None
                sol_rad_from_sensor = None
                et_from_sensor = None
                ha_config_is_metric = self.hass.config.units is METRIC_SYSTEM
                if mapping_weatherdata or sensor_values:
                    if "daily" not in mapping_weatherdata:
                        mapping_weatherdata["daily"] = []
                        mapping_weatherdata["daily"].append({})
                    if sensor_values:
                        #loop over sensor values and put them in weatherdata in the right keys
                        mapping_weatherdata,precip_from_sensor, sol_rad_from_sensor,et_from_sensor = self.insert_sensor_values_in_weatherdata(mapping=mapping, sensor_values=sensor_values, weatherdata=mapping_weatherdata, ha_config_is_metric=ha_config_is_metric)
                    data = self.calculate_module(zone, mapping_weatherdata, precip_from_sensor=precip_from_sensor, sol_rad_from_sensor = sol_rad_from_sensor,et_data=et_from_sensor)

                    self.store.async_update_zone(zone.get(const.ZONE_ID), data)
                    async_dispatcher_send(
                        self.hass, const.DOMAIN + "_config_updated", zone.get(const.ZONE_ID)
                    )
                elif not mapping_weatherdata and not sensor_values:
                    # no data to calculate with!
                    _LOGGER.warning("Calculate for zone {} failed: no data available.".format(zone.get(const.ZONE_NAME)))
        #remove mapping data from all mappings used
        for mapping_id in mappings:
            #remove sensor data from mapping
            changes = {}
            changes[const.MAPPING_DATA] = []
            self.store.async_update_mapping(mapping_id, changes=changes)

    def getModuleInstanceByID(self, module_id):
        m = self.store.async_get_module(module_id)
        if m is None:
            return
        #load the module dynamically
        mods = loadModules(const.MODULE_DIR)
        modinst = None
        for mod in mods:
            if mods[mod]["class"] == m[const.MODULE_NAME]:
                themod = getattr(mods[mod]["module"], mods[mod]["class"])
                modinst = themod(self.hass, config=m["config"])
                break
        return modinst

    def calculate_module(self, zone,weatherdata, precip_from_sensor, sol_rad_from_sensor, et_data):
        mod_id = zone.get(const.ZONE_MODULE)
        m = self.store.async_get_module(mod_id)
        if m is None:
            return
        modinst = self.getModuleInstanceByID(mod_id)
        precip = 0
        bucket = zone.get(const.ZONE_BUCKET)
        data = {}
        data[const.ZONE_OLD_BUCKET]=round(bucket,1)
        explanation = ""


        if modinst:
            if m[const.MODULE_NAME] == "PyETO":
                # if we have precip info from a sensor we don't need to call OWM to get it.
                if precip_from_sensor is None:
                        precip = self._OWMClient.get_precipitation(weatherdata)
                else:
                    precip = precip_from_sensor
            if m[const.MODULE_NAME] == "PyETO":
                 delta = modinst.calculate(weather_data=weatherdata, precip=precip, sol_rad=sol_rad_from_sensor)
            elif m[const.MODULE_NAME] == "Penmon":
                delta = modinst.calculate(weather_data=weatherdata, sol_rad=sol_rad_from_sensor)
            elif m[const.MODULE_NAME] == "Static":
                delta = modinst.calculate()
            elif m[const.MODULE_NAME] == "Passthrough":
                delta = 0-modinst.calculate(et_data=et_data)
            data[const.ZONE_BUCKET] = round(bucket+delta,1)
            data[const.ZONE_DELTA] = round(delta,1)
        else:
            _LOGGER.error("Unknown module for zone {}".format(zone.get(const.ZONE_NAME)))
            return
        explanation = localize("module.calculation.explanation.module-returned-evapotranspiration-deficiency", self.hass.config.language)+" {}. ".format(data[const.ZONE_DELTA])
        explanation += localize("module.calculation.explanation.bucket-was", self.hass.config.language)+" {}".format(data[const.ZONE_OLD_BUCKET])
        explanation += ".<br/>"+localize("module.calculation.explanation.new-bucket-values-is", self.hass.config.language)+" ["
        explanation += localize("module.calculation.explanation.old-bucket-variable", self.hass.config.language)+"]+["
        explanation += localize("module.calculation.explanation.delta", self.hass.config.language)+"]={}+{}={}.<br/>".format(data[const.ZONE_OLD_BUCKET],data[const.ZONE_DELTA],data[const.ZONE_BUCKET])

        if data[const.ZONE_BUCKET] < 0:
            # calculate duration
            ha_config_is_metric = self.hass.config.units is METRIC_SYSTEM
            tput = zone.get(const.ZONE_THROUGHPUT)
            sz = zone.get(const.ZONE_SIZE)
            if not ha_config_is_metric:
                # throughput is in gpm and size is in sq ft since HA is not in metric, so we need to adjust those first!
                tput = convert_between(const.UNIT_GPM,const.UNIT_LPM,tput)
                sz = convert_between(const.UNIT_SQ_FT, const.UNIT_M2, sz)
            precipitation_rate = (tput*60)/sz
            #new version of calculation below - this is the old version from V1. Switching to the new version removes the need for ET values to be passed in!
            #water_budget = 1
            #if mod.maximum_et != 0:
            #    water_budget = round(abs(data[const.ZONE_BUCKET])/mod.maximum_et,2)
            #
            #base_schedule_index = (mod.maximum_et / precipitation_rate * 60)*60

            #duration = water_budget * base_schedule_index
            #new version (2.0): ART = W * BSI = ( |B| / ETpeak ) * ( ETpeak / PR * 3600 ) = |B| / PR * 3600 = ( ET - P ) / PR * 3600
            #so duration = |B| / PR * 3600
            duration = abs(data[const.ZONE_BUCKET])/precipitation_rate*3600
            explanation += localize("module.calculation.explanation.bucket-less-than-zero-irrigation-necessary", self.hass.config.language)+".<br/>"+localize("module.calculation.explanation.steps-taken-to-calculate-duration", self.hass.config.language)+":<br/>"
            # v1 only
            # explanation += "<ol><li>Water budget is defined as abs([bucket])/max(ET)={}</li>".format(water_budget)
            explanation += "<li>"+localize("module.calculation.explanation.precipitation-rate-defined-as",self.hass.config.language)+"["+localize("common.attributes.throughput",self.hass.config.language)+"]*60/["+localize("common.attributes.size",self.hass.config.language)+"]={}*60/{}={}</li>".format(zone.get(const.ZONE_THROUGHPUT),zone.get(const.ZONE_SIZE),round(precipitation_rate,1))
            # v1 only
            # explanation += "<li>The base schedule index is defined as (max(ET)/[precipitation rate]*60)*60=({}/{}*60)*60={}</li>".format(mod.maximum_et,precipitation_rate,round(base_schedule_index,1))
            # explanation += "<li>the duration is calculated as [water_budget]*[base_schedule_index]={}*{}={}</li>".format(water_budget,round(base_schedule_index,1),round(duration))
            explanation += "<li>"+localize("module.calculation.explanation.duration-is-calculated-as",self.hass.config.language)+" abs(["+localize("module.calculation.explanation.bucket",self.hass.config.language)+"])/["+localize("module.calculation.explanation.precipitation-rate-variable",self.hass.config.language)+"]*3600={}/{}*3600={}</li>".format(abs(data[const.ZONE_BUCKET]),round(precipitation_rate,1),round(duration))
            duration = zone.get(const.ZONE_MULTIPLIER) * duration
            explanation += "<li>"+localize("module.calculation.explanation.multiplier-is-applied",self.hass.config.language)+" {}, ".format(zone.get(const.ZONE_MULTIPLIER))
            explanation += localize("module.calculation.explanation.duration-after-multiplier-is",self.hass.config.language)+" {}</li>".format(round(duration))
            duration = round(zone.get(const.ZONE_LEAD_TIME)+duration)
            explanation += "<li>"+localize("module.calculation.explanation.lead-time-is-applied",self.hass.config.language)+" {}, ".format(zone.get(const.ZONE_LEAD_TIME))
            explanation += localize("module.calculation.explanation.duration-after-lead-time-is",self.hass.config.language)+" {}</li></ol>".format(duration)
        else:
            # no need to irrigate, set duration to 0
            duration = 0
            explanation += localize("module.calculation.explanation.bucket-larger-than-or-equal-to-zero-no-irrigation-necessary",self.hass.config.language)+" {}".format(duration)


        data[const.ZONE_DURATION] = duration
        data[const.ZONE_EXPLANATION] = explanation
        return data

    async def async_update_module_config(self, module_id: int=None, data: dict= {}):
        module_id = int(module_id)
        if const.ATTR_REMOVE in data:
            #delete a module
            res = self.store.async_get_module(module_id)
            if not res:
                return
            self.store.async_delete_module(module_id)
        elif self.store.async_get_module(module_id):
            # modify a module
            self.store.async_update_module(module_id, data)
            async_dispatcher_send(
                self.hass, const.DOMAIN + "_config_updated", module_id
            )
        else:
            # create a module
            self.store.async_create_module(data)
            self.store.async_get_config()

    async def async_update_mapping_config(self, mapping_id: int=None, data: dict= {}):
        mapping_id = int(mapping_id)
        if const.ATTR_REMOVE in data:
            #delete a mapping
            res = self.store.async_get_mapping(mapping_id)
            if not res:
                return
            self.store.async_delete_mapping(mapping_id)
        elif self.store.async_get_mapping(mapping_id):
            # modify a mapping
            self.store.async_update_mapping(mapping_id, data)
            async_dispatcher_send(
                self.hass, const.DOMAIN + "_config_updated", mapping_id
            )
        else:
            # create a mapping
            self.store.async_create_mapping(data)
            self.store.async_get_config()

    def check_mapping_sources(self, mapping_id):
        owm_in_mapping = False
        sensor_in_mapping = False
        if not mapping_id is None:
            mapping = self.store.async_get_mapping(mapping_id)
            for key, the_map in mapping[const.MAPPING_MAPPINGS].items():
                if not isinstance(the_map, str):
                    if the_map.get(const.MAPPING_CONF_SOURCE)==const.MAPPING_CONF_SOURCE_OWM:
                        owm_in_mapping = True
                    if the_map.get(const.MAPPING_CONF_SOURCE)==const.MAPPING_CONF_SOURCE_SENSOR:
                        sensor_in_mapping = True
        return owm_in_mapping, sensor_in_mapping

    def build_sensor_values_for_mapping(self, mapping):
        sensor_values = {}
        for key, the_map in mapping[const.MAPPING_MAPPINGS].items():
            if not isinstance(the_map, str):
                if the_map.get(const.MAPPING_CONF_SOURCE)==const.MAPPING_CONF_SOURCE_SENSOR and the_map.get(const.MAPPING_CONF_SENSOR):
                    #this mapping maps to a sensor, so retrieve its value from HA
                    if self.hass.states.get(the_map.get(const.MAPPING_CONF_SENSOR)):
                        val = float(self.hass.states.get(the_map.get(const.MAPPING_CONF_SENSOR)).state)
                        # add val to sensor values
                        sensor_values[key]= val
        return sensor_values

    def insert_sensor_values_in_weatherdata(self, mapping, sensor_values, weatherdata,ha_config_is_metric):
        precip_from_sensor = None
        sol_rad_from_sensor = None
        et_from_sensor = None
        for s, val in sensor_values.items():
            unit = mapping[const.MAPPING_MAPPINGS][s].get(const.MAPPING_CONF_UNIT)
            if s == const.MAPPING_DEWPOINT:
                weatherdata["daily"][0]["dew_point"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            if s == const.MAPPING_EVAPOTRANSPIRATION:
                et_from_sensor = convert_mapping_to_metric(val,s,unit,ha_config_is_metric)
            elif s == const.MAPPING_TEMPERATURE:
                if not "temp" in weatherdata["daily"][0]:
                    weatherdata["daily"][0]["temp"]={}
                weatherdata["daily"][0]["temp"]["day"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_HUMIDITY:
                weatherdata["daily"][0]["humidity"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_MAX_TEMP:
                if not "temp" in weatherdata["daily"][0]:
                    weatherdata["daily"][0]["temp"]={}
                weatherdata["daily"][0]["temp"]["max"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_MIN_TEMP:
                if not "temp" in weatherdata["daily"][0]:
                    weatherdata["daily"][0]["temp"]={}
                weatherdata["daily"][0]["temp"]["min"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_PRECIPITATION:
                precip_from_sensor = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_PRESSURE:
                weatherdata["daily"][0]["pressure"] = convert_mapping_to_metric(val, s,unit,ha_config_is_metric)
            elif s == const.MAPPING_SOLRAD:
                sol_rad_from_sensor = convert_mapping_to_metric(val,s,unit,ha_config_is_metric)
            elif s == const.MAPPING_WINDSPEED:
                weatherdata["daily"][0]["wind_speed"] = convert_mapping_to_metric(val,s,unit,ha_config_is_metric)
        return weatherdata, precip_from_sensor, sol_rad_from_sensor,et_from_sensor

    async def async_update_zone_config(self, zone_id: int = None, data: dict = {}):
        if not zone_id is None:
            zone_id = int(zone_id)
        if const.ATTR_REMOVE in data:
            # delete a zone
            res = self.store.async_get_zone(zone_id)
            if not res:
                return
            self.store.async_delete_zone(zone_id)
            await self.async_remove_entity(zone_id)
        elif const.ATTR_CALCULATE in data:
            if isinstance(data, dict):
                data.pop(const.ATTR_CALCULATE)
            #this should not retrieve new data from sensors!
            #calculate a zone
            res = self.store.async_get_zone(zone_id)
            _LOGGER.info("Calculating zone {}".format(res[const.ZONE_NAME]))
            if not res:
                return
            #call the calculate method on the module for the zone
            #build the weatherdata from OWM/sensors or a mix depending on the mapping
            weatherdata = {}
            sensor_values = {}
            #does the mapping use OWM?
            mapping_id = res[const.ZONE_MAPPING]
            owm_in_mapping, sensor_in_mapping = self.check_mapping_sources(mapping_id = mapping_id)
            # if using pyeto and using a forecast o_i_m needs to be set to true!
            modinst = self.getModuleInstanceByID(res.get(const.ZONE_MODULE))
            if modinst and modinst.name=="PyETO" and modinst._forecast_days>0:
                owm_in_mapping=True
            if self.use_OWM and owm_in_mapping:
                # data comes at least partly from owm
                weatherdata = await self.hass.async_add_executor_job(self._OWMClient.get_data)
            elif not self.use_OWM and owm_in_mapping:
                _LOGGER.error("Error calculating zone {}. Part of the data required came from OpenWeather but there is no OWM API configured. Either configure the OWM API, change your mapping sources or stop using forcasting on the PyETO module.".format(res[const.ZONE_NAME]))
                return
            mapping = self.store.async_get_mapping(mapping_id)

            #if there is sensor data on the mapping, apply aggregates to it.
            sensor_values = None
            if const.MAPPING_DATA in mapping and mapping.get(const.MAPPING_DATA):
                sensor_values = await self.apply_aggregates_to_mapping_data(mapping)
            precip_from_sensor = None
            sol_rad_from_sensor = None
            et_data = None
            ha_config_is_metric = self.hass.config.units is METRIC_SYSTEM
            if weatherdata or sensor_values:
                if "daily" not in weatherdata:
                    weatherdata["daily"] = []
                    weatherdata["daily"].append({})
                if sensor_values:
                    #loop over sensor values and put them in weatherdata in the right keys
                    weatherdata,precip_from_sensor, sol_rad_from_sensor,et_data = self.insert_sensor_values_in_weatherdata(mapping=mapping, sensor_values=sensor_values, weatherdata=weatherdata, ha_config_is_metric=ha_config_is_metric)
                data = self.calculate_module(res, weatherdata, precip_from_sensor=precip_from_sensor, sol_rad_from_sensor = sol_rad_from_sensor, et_data=et_data)
                #remove mapping sensor data
                changes = {}
                changes[const.MAPPING_DATA] = []
                self.store.async_update_mapping(mapping_id, changes=changes)
                self.store.async_update_zone(zone_id, data)
                async_dispatcher_send(
                    self.hass, const.DOMAIN + "_config_updated", zone_id)

                #make sure we fire the start event in time!
                self.register_start_event()
            elif not weatherdata and not sensor_values:
                    # no data to calculate with!
                    _LOGGER.warning("Calculate zone {} failed: no data available.".format(res[const.ZONE_NAME]))
        elif const.ATTR_CALCULATE_ALL in data:
            #calculate all zones
            _LOGGER.info("Calculating all zones");
            data.pop(const.ATTR_CALCULATE_ALL)
            await self._async_calculate_all()
            self.register_start_event()
        elif const.ATTR_UPDATE in data:
            #update sensor data for a zone
            res = self.store.async_get_zone(zone_id)
            _LOGGER.info("Updating zone {}".format(res[const.ZONE_NAME]))
            if not res:
                return
            sensor_values = {}
            mapping_id = res[const.ZONE_MAPPING]
            owm_in_mapping, sensor_in_mapping = self.check_mapping_sources(mapping_id = mapping_id)

            mapping = self.store.async_get_mapping(mapping_id)

            if sensor_in_mapping:
                sensor_values = self.build_sensor_values_for_mapping(mapping)

                #add the sensor value to the mappings sensor values
                mapping_sensor_values = mapping[const.MAPPING_DATA]
                mapping_sensor_values.append(sensor_values)
                changes = {"data": mapping_sensor_values}
                self.store.async_update_mapping(mapping_id,changes)
        elif const.ATTR_UPDATE_ALL in data:
            _LOGGER.info("Updating all zones.")
            await self._async_update_all()
        elif const.ATTR_SET_BUCKET in data:
            #set a bucket to a new value
            new_bucket_value = 0
            if const.ATTR_NEW_BUCKET_VALUE in data:
                new_bucket_value = data[const.ATTR_NEW_BUCKET_VALUE]
            data[const.ZONE_BUCKET] = new_bucket_value
            data.pop(const.ATTR_SET_BUCKET)
            self.store.async_update_zone(zone_id, data)
            async_dispatcher_send(
                self.hass, const.DOMAIN + "_config_updated", zone_id
            )
        elif self.store.async_get_zone(zone_id):
            # modify a zone
            entry = self.store.async_update_zone(zone_id, data)
            async_dispatcher_send(
                self.hass, const.DOMAIN + "_config_updated", zone_id
            )  # make sure to update the HA entity here by listening to this in sensor.py.
               # this should be called by changes from the UI (by user) or by a calculation module (updating a duration), which should be done in python
        else:
            # create a zone
            entry = self.store.async_create_zone(data)

            async_dispatcher_send(self.hass, const.DOMAIN + "_register_entity", entry)

            self.store.async_get_config()

    def register_start_event(self):
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is not None:
            sun_rise = sun_state.attributes.get("next_rising")
            if sun_rise is not None:
                try:
                    sun_rise = datetime.datetime.strptime(sun_rise, "%Y-%m-%dT%H:%M:%S.%f%z")
                except(ValueError):
                    sun_rise = datetime.datetime.strptime(sun_rise, "%Y-%m-%dT%H:%M:%S%z")
                total_duration = self.get_total_duration_all_enabled_zones()
                if total_duration > 0:
                    time_to_fire = sun_rise - datetime.timedelta(seconds=total_duration)
                    if self._track_sunrise_event_unsub:
                        self._track_sunrise_event_unsub()
                        self._track_sunrise_event_unsub = None
                    self._track_sunrise_event_unsub = async_track_point_in_time(
                        self.hass, self._fire_start_event, point_in_time=time_to_fire
                    )
                    event_to_fire = f"{const.DOMAIN}_{const.EVENT_IRRIGATE_START}"
                    _LOGGER.info("Start irrigation event {} will fire at {}".format(event_to_fire, time_to_fire))

    def get_total_duration_all_enabled_zones(self):
        total_duration = 0
        zones = self.store.async_get_zones()
        for zone in zones:
            if zone.get(const.ZONE_STATE)==const.ZONE_STATE_AUTOMATIC or zone.get(const.ZONE_STATE)==const.ZONE_STATE_MANUAL:
                total_duration = zone.get(const.ZONE_DURATION,0)
        return total_duration

    def _fire_start_event(self, *args):
        event_to_fire = f"{const.DOMAIN}_{const.EVENT_IRRIGATE_START}"
        self.hass.bus.fire(event_to_fire, {})
        _LOGGER.info("Fired start event: {}".format(event_to_fire))

    @callback
    def async_get_all_modules(self):
        """Get all ModuleEntries"""
        res = []
        mods = loadModules(const.MODULE_DIR)
        for mod in mods:
            m = getattr(mods[mod]["module"], mods[mod]["class"])
            s = m(self.hass, {})
            res.append({"name": s.name, "description":s.description,"config":s._config,"schema":s.schema_serialized()})

        return res

    async def async_remove_entity(self, zone_id: str):
        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        zone_id = int(zone_id)
        entity = self.hass.data[const.DOMAIN]["zones"][zone_id]
        entity_registry.async_remove(entity.entity_id)
        self.hass.data[const.DOMAIN]["zones"].pop(zone_id, None)

    async def async_unload(self):
        """remove all Smart Irrigation objects"""

        # remove zone entities
        zones = list(self.hass.data[const.DOMAIN]["zones"].keys())
        for zone in zones:
            await self.async_remove_entity(zone)

        # remove subscriptions for coordinator
        while len(self._subscriptions):
            self._subscriptions.pop()()

    async def async_delete_config(self):
        """wipe Smart Irrigation storage"""
        await self.store.async_delete()

    async def _async_set_all_buckets(self, val=0):
        """Sets all buckets to val"""
        zones = self.store.async_get_zones()
        data = {}
        data[const.ATTR_SET_BUCKET] = {}
        data[const.ATTR_NEW_BUCKET_VALUE] = val

        for zone in zones:
            await self.async_update_zone_config(zone_id=zone.get(const.ZONE_ID),data=data)

    async def handle_calculate_all_zones(self, call):
        """Calculate all zones."""
        _LOGGER.info("Calculate all zones service called.")
        await self._async_calculate_all()

    async def handle_calculate_zone(self, call):
        """Calculate specific zone."""
        if const.SERVICE_ENTITY_ID in call.data:
            for entity in call.data[const.SERVICE_ENTITY_ID]:
                _LOGGER.info("Calculate zone service called for zone {}.".format(entity))
                #find entity zone id and call calculate on the zone
                state = self.hass.states.get(entity)
                if state:
                    #find zone_id for zone with name
                    zone_id = state.attributes.get(const.ZONE_ID)
                    if not zone_id is None:
                        data = []
                        data.append(const.ATTR_CALCULATE)
                        await self.async_update_zone_config(zone_id=zone_id,data=data)

    async def handle_update_all_zones(self, call):
        """Update all zones."""
        _LOGGER.info("Update all zones service called.")
        await self._async_update_all()

    async def handle_update_zone(self, call):
        """Update specific zone."""
        if const.SERVICE_ENTITY_ID in call.data:
            for entity in call.data[const.SERVICE_ENTITY_ID]:
                _LOGGER.info("Update zone service called for zone {}.".format(entity))
                #find entity zone id and call update on the zone
                state = self.hass.states.get(entity)
                if state:
                    #find zone_id for zone with name
                    zone_id = state.attributes.get(const.ZONE_ID)
                    if not zone_id is None:
                        data = []
                        data.append(const.ATTR_UPDATE)
                        await self.async_update_zone_config(zone_id=zone_id,data=data)

    async def handle_reset_bucket(self, call):
        """Reset a specific zone bucket to 0"""
        if const.SERVICE_ENTITY_ID in call.data:
            for entity in call.data[const.SERVICE_ENTITY_ID]:
                _LOGGER.info("Reset bucket service called for zone {}.".format(entity))
                #find entity zone id and call calculate on the zone
                state = self.hass.states.get(entity)
                if state:
                    #find zone_id for zone with name
                    zone_id = state.attributes.get(const.ZONE_ID)
                    if not zone_id is None:
                        data = {}
                        data[const.ATTR_SET_BUCKET] = {}
                        data[const.ATTR_NEW_BUCKET_VALUE] = 0
                        await self.async_update_zone_config(zone_id=zone_id,data=data)

    async def handle_reset_all_buckets(self, call):
        """Reset all buckets to 0"""
        _LOGGER.info("Reset all buckets service called")
        await self._async_set_all_buckets(0)

    async def handle_set_bucket(self, call):
        """Reset a specific zone bucket to new value"""
        if const.SERVICE_ENTITY_ID in call.data and const.ATTR_NEW_BUCKET_VALUE in call.data:
            new_value = call.data[const.ATTR_NEW_BUCKET_VALUE]
            for entity in call.data[const.SERVICE_ENTITY_ID]:
                _LOGGER.info("Set bucket service called for zone {}, new value: {}.".format(entity, new_value))
                #find entity zone id and call calculate on the zone
                state = self.hass.states.get(entity)
                if state:
                    #find zone_id for zone with name
                    zone_id = state.attributes.get(const.ZONE_ID)
                    if not zone_id is None:
                        data = {}
                        data[const.ATTR_SET_BUCKET] = {}
                        data[const.ATTR_NEW_BUCKET_VALUE] = new_value
                        await self.async_update_zone_config(zone_id=zone_id,data=data)

    async def handle_set_all_buckets(self, call):
        """Reset all buckets to new value"""
        if const.ATTR_NEW_BUCKET_VALUE in call.data:
            new_value = call.data[const.ATTR_NEW_BUCKET_VALUE]
            _LOGGER.info("Reset all buckets service called, new value: {}".format(new_value))
            await self._async_set_all_buckets(new_value)

@callback
def register_services(hass):
    """Register services used by Smart Irrigation integration."""

    coordinator = hass.data[const.DOMAIN]["coordinator"]


    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_CALCULATE_ALL_ZONES,
        coordinator.handle_calculate_all_zones
    )
    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_CALCULATE_ZONE,
        coordinator.handle_calculate_zone
    )

    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_UPDATE_ALL_ZONES,
        coordinator.handle_update_all_zones
    )
    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_UPDATE_ZONE,
        coordinator.handle_update_zone
    )
    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_RESET_BUCKET,
        coordinator.handle_reset_bucket
    )

    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_RESET_ALL_BUCKETS,
        coordinator.handle_reset_all_buckets
    )

    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_SET_BUCKET,
        coordinator.handle_set_bucket
    )

    hass.services.async_register(
        const.DOMAIN,
        const.SERVICE_SET_ALL_BUCKETS,
        coordinator.handle_set_all_buckets
    )
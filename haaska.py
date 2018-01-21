#!/usr/bin/env python3.6
# coding: utf-8

# Copyright (c) 2015 Michael Auchter <a@phire.org>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import json
import logging
import operator
import requests
import colorsys
import datetime
import uuid
from requests.packages.urllib3.exceptions import InsecureRequestWarning
# Imports for v3 validation
#from validation import validate_message

# Disable warning about Insecure Request
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Setup logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

LIGHT_SUPPORT_COLOR_TEMP = 2
LIGHT_SUPPORT_RGB_COLOR = 16
LIGHT_SUPPORT_XY_COLOR = 64

DISPLAY_CATEGORIES = {
    'garage_door': 'SWITCH',
    'group': 'SWITCH',
    'input_boolean': 'SWITCH',
    'input_slider': 'SWITCH',
    'switch': 'SWITCH',
    'fan': 'SWITCH',
    'cover': 'SWITCH',
    'lock': 'SMARTLOCK',
    'script': 'ACTIVITY_TRIGGER',
    'scene': 'SCENE_TRIGGER',
    'light': 'LIGHT',
    'media_player': 'TV',
    'climate': 'THERMOSTAT',
    'alert': 'OTHER',
    'automation': 'ACTIVITY_TRIGGER'
}

ALEXA_INTERFACES = {
    'BrightnessController': {'directives': ['AdjustBrightness', 'SetBrightness']},
    'CameraStreamController': {'directives': ['InitializeCameraStreams']},
    'ColorController': {'directives': ['SetColor']},
    'ColorTemperatureController': {'directives': ['DecreaseColorTemperature', 'IncreaseColorTemperature', 'SetColorTemperature']},
    'InputController': {'directives': ['SelectInput']},
    'LockController': {'directives': ['Lock', 'Unlock']},
    'PercentageController': {'directives': ['SetPercentage', 'AdjustPercentage']},
    'PlaybackController': {'directives': ['FastForward', 'Next', 'Pause', 'Play', 'Previous', 'Rewind', 'StartOver', 'Stop']},
    'PowerController': {'directives': ['TurnOn', 'TurnOff']},
    'PowerLevelController': {'directives': ['SetPowerLevel', 'AdjustPowerLevel']},
    'Speaker': {'directives': ['SetVolume', 'AdjustVolume', 'SetMute']},
    'StepSpeaker': {'directives': ['AdjustVolume', 'SetMute']},
    'TemperatureSensor': {'directives': ['ReportState']},
    'ThermostatController': {'directives': ['SetTargetTemperature', 'AdjustTargetTemperature', 'SetThermostatMode']}
}

class HomeAssistant(object):
    def __init__(self, config):
        self.config = config
        self.url = config.url.rstrip('/')
        agent_str = 'Home Assistant Alexa Smart Home Skill - %s - %s'
        agent_fmt = agent_str % (os.environ['AWS_DEFAULT_REGION'],
                                 requests.utils.default_user_agent())
        self.session = requests.Session()
        self.session.headers = {'x-ha-access': config.password,
                                'content-type': 'application/json',
                                'User-Agent': agent_fmt}
        self.session.verify = config.ssl_verify

    def build_url(self, relurl):
        return '%s/%s' % (self.config.url, relurl)

    def get(self, relurl):
        r = self.session.get(self.build_url(relurl))
        r.raise_for_status()
        return r.json()

    def post(self, relurl, d, wait=False):
        read_timeout = None if wait else 0.01
        r = None
        try:
            logger.debug('HA post calling %s with %s', relurl, str(d))
            r = self.session.post(self.build_url(relurl),
                                  data=json.dumps(d),
                                  timeout=(None, read_timeout))
            r.raise_for_status()
        except requests.exceptions.ReadTimeout:
            # Allow response timeouts after request was sent
            logger.debug('HA post for %s sent without waiting for response',
                         relurl)
        return r


class ConnectedHomeCall(object):
    def __init__(self, namespace, name, ha, payload, endpoint, correlationToken):
        logger.debug('Building ConnectedHomeCall %s, %s, %s', namespace,
                     name, payload)
        self.namespace = namespace
        self.name = name
        if name == 'ReportState':
            self.response_name = 'StateReport'
            self.namespace = 'Alexa'
        elif name == 'SetTargetTemperature':
            self.response_name = 'Response'
            self.namespace = 'Alexa'
        else:
            self.response_name = self.name + '.Response'
        self.ha = ha
        self.payload = payload
        self.endpoint = endpoint
        self.entity = None
        self.context_properties = []
        if self.endpoint and ('endpointId' in self.endpoint):
            self.entity = mk_entity(ha, self.endpoint['endpointId']
                                    .replace(':', '.'))
        self.correlationToken = correlationToken

    class ConnectedHomeException(Exception):
        def __init__(self, name="DriverInternalError", payload={}):
            self.error_name = name
            self.payload = payload

    class ValueOutOfRangeError(ConnectedHomeException):
        def __init__(self, minValue, maxValue):
            self.error_name = 'ValueOutOfRangeError'
            self.payload = {'minimumValue': minValue, 'maximumValue': maxValue}

    def invoke(self, name):
        logger.debug('invoking ConnectedHomeCall %s %s', self.namespace, name)
        r = {'event': {}}
        try:
            r['event']['header'] = {'namespace': self.namespace,
                       'name': self.response_name,
                       'payloadVersion': '3',
                       'messageId': get_uuid(),
                       "correlationToken": self.correlationToken}
            
            payload = operator.attrgetter(name)(self)()
            if payload:
                r['event']['payload'] = payload
            else:
                r['event']['payload'] = {}

            if self.endpoint:
                r['event']['endpoint'] = {
                    "endpointId": self.endpoint['endpointId']
                    } 
                
            if self.context_properties:
                r['context'] = {"properties": self.context_properties }
            
            logger.debug('response payload: %s', str(r['event']['payload']))
        except ConnectedHomeCall.ConnectedHomeException as e:
            logger.exception('ConnectedHomeCall failed: %s, %s', e.error_name, e.payload)
            self.response_name = e.error_name
            r['event']['payload'] = e.payload
        except Exception:
            logger.exception('ConnectedHomeCall failed unexpectedly')
            self.response_name = 'DriverInternalError'
            r['event']['payload'] = {}

        return r


class Alexa(object):
    class ReportState(ConnectedHomeCall):
        def ReportState(self):
            if hasattr(self.entity, 'get_current_temperature'):
                state = self.ha.get('states/' + self.entity.entity_id)
                scale = get_temp_scale(state['attributes']['unit_of_measurement'])
                temperature = self.entity.get_current_temperature(state)
                self.context_properties.append({
                    "namespace": "Alexa.TemperatureSensor",
                    "name": "temperature",
                    "value": {
                        "value": temperature,
                        "scale": scale
                    },
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
            
            if hasattr(self.entity, 'get_temperature'):
                state = self.ha.get('states/' + self.entity.entity_id)
                scale = get_temp_scale(state['attributes']['unit_of_measurement'])
                temperature, mode = self.entity.get_temperature(state)
                self.context_properties.append({
                    "namespace": "Alexa.ThermostatController",
                    "name": "targetSetpoint",
                    "value": {
                        "value": temperature,
                        "scale": scale
                    },
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
                self.context_properties.append({
                    "namespace": "Alexa.ThermostatController",
                    "name": "thermostatMode",
                    "value": mode.upper(),
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
            
            if hasattr(self.entity, 'get_lock_state'):
                state = self.entity.get_lock_state().upper()
                self.context_properties.append({
                    "namespace": "Alexa.LockController",
                    "name": "lockState",
                    "value": state.upper(),
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
            
            if (hasattr(self.entity, 'turn_on') or hasattr(self.entity, 'turn_off')) and not hasattr(self.entity, 'get_temperature'):
                state = self.ha.get('states/' + self.entity.entity_id)
                unit = state.get('state')
                self.context_properties.append({
                    "namespace": "Alexa.PowerController",
                    "name": "powerState",
                    "value": unit.upper(),
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
            
            if hasattr(self.entity, 'get_percentage'):
                state = self.ha.get('states/' + self.entity.entity_id)
                #unit = state.get('state')
                val = self.entity.get_percentage()
                self.context_properties.append({
                    "namespace": "Alexa.PercentageController",
                    "name": "percentage",
                    "value": val,
                    "timeOfSample": get_utc_timestamp(),
                    "uncertaintyInMilliseconds": 200
                })
                
            # Report EndpointHealth for ALL items
            self.context_properties.append({
                "namespace": "Alexa.EndpointHealth",
                "name": "connectivity",
                "value": {
                    "value": "OK"
                },
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class Discovery(ConnectedHomeCall):
        def Discover(self):
            try:
                return {'endpoints': discover_appliances(self.ha)}
            except Exception:
                logger.exception('v3 DiscoverAppliancesRequest failed')

    class PowerController(ConnectedHomeCall):
        def TurnOn(self):
            self.entity.turn_on()
            self.context_properties.append({
                "namespace": "Alexa.PowerController",
                "name": "powerState",
                "value": "ON",
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

        def TurnOff(self):
            self.entity.turn_off()
            self.context_properties.append({
                "namespace": "Alexa.PowerController",
                "name": "powerState",
                "value": "OFF",
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class BrightnessController(ConnectedHomeCall):
        def AdjustBrightness(self):
            delta = self.payload['brightnessDelta']
            val = self.entity.get_percentage()
            val += delta
            if val < 0.0:
                val = 0
            elif val >= 100.0:
                val = 100.0
            self.entity.set_percentage(val)
            self.context_properties.append({
                "namespace": "Alexa.BrightnessController",
                "name": "brightness",
                "value": val,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
            
        def SetBrightness(self):
            percentage = self.payload['brightness']
            self.entity.set_percentage(percentage)
            self.context_properties.append({
                "namespace": "Alexa.BrightnessController",
                "name": "brightness",
                "value": percentage,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class PercentageController(ConnectedHomeCall):
        def SetPercentage(self):
            percentage = self.payload['percentage']
            self.entity.set_percentage(percentage)
            self.context_properties.append({
                "namespace": "Alexa.PercentageController",
                "name": "percentage",
                "value": percentage,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

        def AdjustPercentage(self):
            delta = self.payload['percentageDelta']
            val = self.entity.get_percentage()
            val += delta
            if val < 0.0:
                val = 0
            elif val >= 100.0:
                val = 100.0
            self.entity.set_percentage(val)
            self.context_properties.append({
                "namespace": "Alexa.PercentageController",
                "name": "percentage",
                "value": val,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class ColorTemperatureController(ConnectedHomeCall):
        def DecreaseColorTemperature(self):
            current = self.entity.get_color_temperature()
            new = current - 500
            self.entity.set_color_temperature(new)
            self.context_properties.append({
                "namespace": "Alexa.ColorTemperatureController",
                "name": "colorTemperatureInKelvin",
                "value": new,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

        def IncreaseColorTemperature(self):
            current = self.entity.get_color_temperature()
            new = current + 500
            self.entity.set_color_temperature(new)
            self.context_properties.append({
                "namespace": "Alexa.ColorTemperatureController",
                "name": "colorTemperatureInKelvin",
                "value": new,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

        def SetColorTemperature(self):
            temp = self.payload['colorTemperatureInKelvin']
            self.entity.set_color_temperature(temp)
            self.context_properties.append({
                "namespace": "Alexa.ColorTemperatureController",
                "name": "colorTemperatureInKelvin",
                "value": temp,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class PowerLevelController(ConnectedHomeCall):
        def AdjustPowerLevel(self):
            percentage = self.payload['powerLevel']
            self.entity.set_percentage(percentage)
            self.context_properties.append({
                "namespace": "Alexa.PowerLevelController",
                "name": "powerLevel",
                "value": percentage,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

        def SetPowerLevel(self):
            delta = self.payload['powerLevelDelta']
            val = self.entity.get_percentage()
            val += delta
            if val < 0.0:
                val = 0
            elif val >= 100.0:
                val = 100.0
            self.entity.set_percentage(val)
            self.context_properties.append({
                "namespace": "Alexa.PowerLevelController",
                "name": "powerLevel",
                "value": val,
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class ThermostatController(ConnectedHomeCall):
        def SetTargetTemperature(self):
            state = self.ha.get('states/' + self.entity.entity_id)
            unit = state['attributes']['unit_of_measurement']
            scale = get_temp_scale(state['attributes']['unit_of_measurement'])
            min_temp = convert_temp(state['attributes']['min_temp'], unit)
            max_temp = convert_temp(state['attributes']['max_temp'], unit)
            temperature, mode = self.entity.get_temperature(state)
            
            new_temp = float(self.payload['targetSetpoint']['value'])
            
            if new_temp > max_temp or new_temp < min_temp:
                raise ConnectedHomeCall.ValueOutOfRangeError(min_temp,max_temp)
                    
            # Only 4 allowed values for mode in this response
            if mode not in ['AUTO', 'COOL', 'ECO', 'HEAT']:
                current = self.entity.get_current_temperature(state)
                mode = 'COOL' if current >= new_temp else 'HEAT'
            
            self.entity.set_temperature(new_temp, mode.lower(), state)
            
            self.context_properties.append({
                "namespace": "Alexa.ThermostatController",
                "name": "targetSetpoint",
                "value": {
                    "value": new_temp,
                    "scale": scale
                },
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
            self.context_properties.append({
                "namespace": "Alexa.ThermostatController",
                "name": "thermostatMode",
                "value": mode.upper(),
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
            
        def AdjustTargetTemperature(self):
            state = self.ha.get('states/' + self.entity.entity_id)
            unit = state['attributes']['unit_of_measurement']
            scale = get_temp_scale(state['attributes']['unit_of_measurement'])
            min_temp = convert_temp(state['attributes']['min_temp'], unit)
            max_temp = convert_temp(state['attributes']['max_temp'], unit)
            temperature, mode = self.entity.get_temperature(state)
        
            new_temp = op(temperature,float(self.payload['targetSetpointDelta']['value']))
            # Clamp the allowed temperature for relative adjustments
            if temperature != max_temp and temperature != min_temp:
                if new_temp < min_temp:
                    new_temp = min_temp
                elif new_temp > max_temp:
                    new_temp = max_temp
        
            if new_temp > max_temp or new_temp < min_temp:
                raise ConnectedHomeCall.ValueOutOfRangeError(min_temp,max_temp)
        
            # Only 4 allowed values for mode in this response
            if mode not in ['AUTO', 'COOL', 'ECO', 'HEAT']:
                current = self.entity.get_current_temperature(state)
                mode = 'COOL' if current >= new_temp else 'HEAT'
            
            self.entity.set_temperature(new_temp, mode.lower(), state)
                            
            self.context_properties.append({
                "namespace": "Alexa.ThermostatController",
                "name": "targetSetpoint",
                "value": {
                    "value": new_temp,
                    "scale": scale
                },
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
            self.context_properties.append({
                "namespace": "Alexa.ThermostatController",
                "name": "thermostatMode",
                "value": mode.upper(),
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
            
        def SetThermostatMode(self):
            mode = self.payload['thermostatMode']
            
            if mode in ['AUTO', 'COOL', 'ECO', 'HEAT']:
                self.entity.turn_on
            else:
                self.entity.turn_off
            
            self.context_properties.append({
                "namespace": "Alexa.ThermostatController",
                "name": "thermostatMode",
                "value": mode.upper(),
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class TemperatureSensor(ConnectedHomeCall):
        def ReportState(self):
            state = self.ha.get('states/' + self.entity.entity_id)
            scale = get_temp_scale(state['attributes']['unit_of_measurement'])
            temperature = self.entity.get_current_temperature(state)
            self.context_properties.append({
                "namespace": "Alexa.TemperatureSensor",
                "name": "temperature",
                "value": {
                    "value": temperature,
                    "scale": scale
                },
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

    class LockController(ConnectedHomeCall):
        def Lock(self):
            self.entity.set_lock_state(self.payload["lockState"])
            self.context_properties.append({
                "namespace": "Alexa.LockController",
                "name": "lockState",
                "value": "LOCKED",
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })
        
        def Unlock(self):
            self.entity.set_lock_state(self.payload["lockState"])
            self.context_properties.append({
                "namespace": "Alexa.LockController",
                "name": "lockState",
                "value": "UNOCKED",
                "timeOfSample": get_utc_timestamp(),
                "uncertaintyInMilliseconds": 200
            })

def invoke(namespace, name, ha, payload, endpoint, correlationToken):
    class allowed(object):
        Alexa = Alexa
    if namespace == 'Alexa' and name == 'ReportState':
        namespace = namespace + '.' + name
    
    make_class = operator.attrgetter(namespace)
    logger.debug('Calling invoke %s, %s, %s, %s, %s, %s', namespace, name, ha,
                 payload, endpoint, correlationToken)
    obj = make_class(allowed)(namespace, name, ha, payload, endpoint, correlationToken)
    return obj.invoke(name)

def discover_appliances(ha):
    def entity_domain(x):
        return x['entity_id'].split('.', 1)[0]

    def is_supported_entity(x):
        return entity_domain(x) in ha.config.exposed_domains

    def is_exposed_entity(x):
        attr = x['attributes']
        if 'haaska_hidden' in attr:
            return not attr['haaska_hidden']
        elif 'hidden' in attr:
            return not attr['hidden']
        else:
            return ha.config.expose_by_default

    def mk_appliance(x):
        features = 0
        if 'supported_features' in x['attributes']:
            features = x['attributes']['supported_features']
        entity = mk_entity(ha, x['entity_id'], features)
        o = {}
        # this needs to be unique and has limitations on allowed characters ("^[a-zA-Z0-9_\\-=#;:?@&]*$"):
        o['endpointId'] = x['entity_id'].replace('.', ':')
        o['manufacturerName'] = 'Unknown'
        if 'haaska_name' in x['attributes']:
            o['friendlyName'] = x['attributes']['haaska_name']
        else:
            o['friendlyName'] = x['attributes']['friendly_name']
            suffix = ha.config.entity_suffixes[entity_domain(x)]
            if suffix != '':
                o['friendlyName'] += ' ' + suffix

        if 'haaska_desc' in x['attributes']:
            o['description'] = x['attributes']['haaska_desc']
        else:
            o['description'] = 'Home Assistant ' + \
                entity_domain(x).replace('_', ' ').title()

        o['displayCategories'] = [DISPLAY_CATEGORIES[entity_domain(x)]]
 
        o['capabilities'] = entity.get_capabilities()
 
        return o

    states = ha.get('states')
    return [mk_appliance(x) for x in states if is_supported_entity(x) and
            is_exposed_entity(x)]

def supported_features(payload):
    try:
        details = 'additionalApplianceDetails'
        return payload['appliance'][details]['supported_features']
    except Exception:

        return 0

def convert_temp(temp, from_unit=u'°C', to_unit=u'°C'):
    if temp is None or from_unit == to_unit:
        return temp
    if from_unit == u'°C':
        return temp * 1.8 + 32
    else:
        return (temp - 32) / 1.8
        
def get_temp_scale(unit):
    if unit == u'°C':
        return 'CELSIUS'
    else:
        return 'FAHRENHEIT'

def get_utc_timestamp():
    #return datetime.datetime.utcnow().isoformat()
    return datetime.datetime.strftime(datetime.datetime.utcnow(), "%Y-%m-%dT%H:%M:%S.%f")[:-4] + "Z"

def get_uuid():
    return str(uuid.uuid4())

def mk_entity(ha, entity_id, supported_features=0):
    entity_domain = entity_id.split('.', 1)[0]
    logger.debug('Making entity w/ domain: %s', entity_domain)
    return DOMAINS[entity_domain](ha, entity_id, supported_features)


class Entity(object):
    def __init__(self, ha, entity_id, supported_features):
        self.ha = ha
        self.entity_id = entity_id.replace(':', '.')
        self.supported_features = supported_features
        self.entity_domain = self.entity_id.split('.', 1)[0]

    def _call_service(self, service, data={}):
        data['entity_id'] = self.entity_id
        self.ha.post('services/' + service, data)

    def get_model_name(self):
        return None

    def get_capabilities(self):
        capabilities = []
        capabilities.append(
            {
                "type": "AlexaInterface",
                "interface": "Alexa",
                "version": "3"
            })

        if hasattr(self, 'turn_on') or hasattr(self, 'turn_off'):
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.PowerController",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "powerState"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })
        if hasattr(self, 'set_percentage') or hasattr(self, 'get_percentage'):
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.PercentageController",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "percentage"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.BrightnessController",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "brightness"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })

        if hasattr(self, 'get_current_temperature') or hasattr(
                                           self, 'get_temperature'):
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.TemperatureSensor",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "temperature"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })

        if hasattr(self, 'set_temperature'):
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.ThermostatController",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "targetSetpoint"
                            },
                            {
                                "name": "thermostatMode"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })
        
        if hasattr(self, 'get_lock_state') or hasattr(self, 'set_lock_state'):
            capabilities.append(
                {
                    "type": "AlexaInterface",
                    "interface": "Alexa.LockController",
                    "version": "3",
                    "properties": {
                        "supported": [
                            {
                                "name": "lockState"
                            }
                        ],
                        "proactivelyReported": False,
                        "retrievable": True
                    }
                })

        if self.entity_domain == "light":
            if self.supported_features & LIGHT_SUPPORT_RGB_COLOR:
                capabilities.append(
                    {
                        "type": "AlexaInterface",
                        "interface": "Alexa.ColorController",
                        "version": "3",
                        "properties": {
                            "supported": [
                                {
                                    "name": "color"
                                }
                            ],
                            "proactivelyReported": False,
                            "retrievable": True
                        }
                    })
            if self.supported_features & LIGHT_SUPPORT_COLOR_TEMP:
                capabilities.append(
                    {
                        "type": "AlexaInterface",
                        "interface": "Alexa.ColorTemperatureController",
                        "version": "3",
                        "properties": {
                            "supported": [
                                {
                                    "name": "colorTemperatureInKelvin"
                                }
                            ],
                            "proactivelyReported": False,
                            "retrievable": True
                        }
                    })

        capabilities.append(
            {
                "type": "AlexaInterface",
                "interface": "Alexa.EndpointHealth",
                "version": "3",
                "properties": {
                    "supported": [
                        {
                            "name": "connectivity"
                        }
                    ],
                    "proactivelyReported": False,
                    "retrievable": True
                }
            })            

        return capabilities


class ToggleEntity(Entity):
    def turn_on(self):
        self._call_service('homeassistant/turn_on')

    def turn_off(self):
        self._call_service('homeassistant/turn_off')


class InputSliderEntity(Entity):
    def get_percentage(self):
        state = self.ha.get('states/' + self.entity_id)
        value = float(state['state'])
        minimum = state['attributes']['min']
        maximum = state['attributes']['max']
        adjusted = value - minimum

        return (adjusted * 100.0 / (maximum - minimum))

    def set_percentage(self, val):
        state = self.ha.get('states/' + self.entity_id)
        minimum = state['attributes']['min']
        maximum = state['attributes']['max']
        step = state['attributes']['step']
        scaled = val * (maximum - minimum) / 100.0
        rounded = step * round(scaled / step)
        adjusted = rounded + minimum

        self._call_service('input_slider/select_value', {'value': adjusted})


class GarageDoorEntity(ToggleEntity):
    def turn_on(self):
        self._call_service('garage_door/open')

    def turn_off(self):
        self._call_service('garage_door/close')


class CoverEntity(ToggleEntity):
    def turn_on(self):
        self._call_service('cover/open_cover')

    def turn_off(self):
        self._call_service('cover/close_cover')


class LockEntity(Entity):
    def set_lock_state(self, state):
        if state == "LOCKED":
            self._call_service('lock/lock')
        elif state == "UNLOCKED":
            self._call_service('lock/unlock')

    def get_lock_state(self):
        state = self.ha.get('states/' + self.entity_id)
        return state['state']


class ScriptEntity(ToggleEntity):
    def turn_off(self):
        self.turn_on()


class SceneEntity(ToggleEntity):
    def turn_off(self):
        self.turn_on()


class LightEntity(ToggleEntity):
    def get_percentage(self):
        state = self.ha.get('states/' + self.entity_id)
        current_brightness = state['attributes']['brightness']
        return (current_brightness / 255.0) * 100.0

    def set_percentage(self, val):
        brightness = (val / 100.0) * 255.0
        self._call_service('light/turn_on', {'brightness': brightness})

    def get_color_temperature(self):
        state = self.ha.get('states/' + self.entity_id)
        current_temperature = state['attributes']['color_temp']
        return (1000000 / current_temperature)

    def set_color(self, hue, saturation, brightness):
        rgb = [int(round(i * 255)) for i in colorsys.hsv_to_rgb(hue / 360.0,
                                                                saturation,
                                                                brightness)]
        self._call_service('light/turn_on', {'rgb_color': rgb})

    def set_color_temperature(self, val):
        self._call_service('light/turn_on',
                           {'color_temp': (1000000 / val)})


class MediaPlayerEntity(ToggleEntity):
    def get_percentage(self):
        state = self.ha.get('states/' + self.entity_id)
        vol = state['attributes']['volume_level']
        return vol * 100.0

    def set_percentage(self, val):
        vol = val / 100.0
        self._call_service('media_player/volume_set', {'volume_level': vol})


class ClimateEntity(Entity):
    def turn_on(self):
        state = self.ha.get('states/' + self.entity_id)
        current = self.get_current_temperature(state)
        temperature, mode = self.get_temperature(state)
        # I think logic should change here, maybe
        # based on Hive heating
        # auto = schedule - doesn't turn on heating
        # heat = manual - heating is turned on
        # off = off
        
        if temperature is None:
            mode = 'auto'
        else:
            if 'cool' in state['attributes']['operation_list']:
                mode = 'cool' if current >= temperature else 'heat'
            else:
                mode = 'heat'
        self._call_service('climate/set_operation_mode',
                           {'operation_mode': mode})

    def turn_off(self):
        self._call_service('climate/set_operation_mode',
                           {'operation_mode': 'off'})

    def get_current_temperature(self, state=None):
        if not state:
            state = self.ha.get('states/' + self.entity_id)
        return convert_temp(
            state['attributes']['current_temperature'],
            state['attributes']['unit_of_measurement'])

    def get_temperature(self, state=None):
        if not state:
            state = self.ha.get('states/' + self.entity_id)
        temperature = convert_temp(
            state['attributes']['temperature'],
            state['attributes']['unit_of_measurement'])
        mode = state['state'].replace('idle', 'off').upper()
        return (temperature, mode)

    def set_temperature(self, val, mode=None, state=None):
        if not state:
            state = self.ha.get('states/' + self.entity_id)
        temperature = convert_temp(
            val,
            to_unit=state['attributes']['unit_of_measurement'])
        data = {'temperature': temperature}
        if mode:
            data['operation_mode'] = mode
        self._call_service('climate/set_temperature', data)


class FanEntity(ToggleEntity):
    def get_percentage(self):
        state = self.ha.get('states/' + self.entity_id)
        speed = state['attributes']['speed']
        if speed == "off":
            return 0
        elif speed == "low":
            return 33
        elif speed == "medium":
            return 66
        elif speed == "high":
            return 100

    def set_percentage(self, val):
        speed = "off"
        if val <= 33:
            speed = "low"
        elif val <= 66:
            speed = "medium"
        elif val <= 100:
            speed = "high"
        self._call_service('fan/set_speed', {'speed': speed})

DOMAINS = {
    'garage_door': GarageDoorEntity,
    'group': ToggleEntity,
    'input_boolean': ToggleEntity,
    'input_slider': InputSliderEntity,
    'switch': ToggleEntity,
    'fan': FanEntity,
    'cover': CoverEntity,
    'lock': LockEntity,
    'script': ScriptEntity,
    'scene': SceneEntity,
    'light': LightEntity,
    'media_player': MediaPlayerEntity,
    'climate': ClimateEntity,
    'alert': ToggleEntity,
    'automation': ToggleEntity
}


class Configuration(object):
    def __init__(self, filename=None, optsDict=None):
        self._json = {}
        if filename is not None:
            with open(filename) as f:
                self._json = json.load(f)

        if optsDict is not None:
            self._json = optsDict

        opts = {}
        opts['url'] = self.get(['url', 'ha_url'],
                               default='http://localhost:8123/api')
        opts['ssl_verify'] = self.get(['ssl_verify', 'ha_cert'], default=True)
        opts['password'] = self.get(['password', 'ha_passwd'], default='')
        opts['exposed_domains'] = \
            sorted(self.get(['exposed_domains', 'ha_allowed_entities'],
                            default=DOMAINS.keys()))

        default_entity_suffixes = {'group': 'Group', 'scene': 'Scene'}
        opts['entity_suffixes'] = {domain: '' for domain in DOMAINS.keys()}
        opts['entity_suffixes'].update(self.get(['entity_suffixes'],
                                       default=default_entity_suffixes))

        opts['expose_by_default'] = self.get(['expose_by_default'],
                                             default=True)
        opts['debug'] = self.get(['debug'], default=False)
        self.opts = opts

    def __getattr__(self, name):
        return self.opts[name]

    def get(self, keys, default):
        for key in keys:
            if key in self._json:
                return self._json[key]
        return default

    def dump(self):
        return json.dumps(self.opts, indent=2, separators=(',', ': '))

def request_handler(request, context):
    #Main Lambda handler.
    #Only expects v3 requests (as we are only user) so no neeed to handle v2 requests
    try:
        config = Configuration('config.json')
        if config.debug:
            logger.setLevel(logging.DEBUG)
        
        ha = HomeAssistant(config)
        
        logger.debug('Directive:')
        logger.debug(json.dumps(request, indent=4, sort_keys=True))
        
        directive = request['directive']
        name = directive['header']['name']
        namespace = directive['header']['namespace']
        payload = directive.get('payload')
        endpoint = directive.get('endpoint')
        header = directive['header']
        correlationToken = header.get('correlationToken')
        
        logger.debug('calling request_handler for %s, payload: %s', name,
                 str({k: v for k, v in payload.items()
                    if k != u'accessToken'}))
        
        response = invoke(namespace, name, ha, payload, endpoint, correlationToken)
        
        logger.debug("Response:")
        logger.debug(json.dumps(response, indent=4, sort_keys=True))
        
        logger.debug("Validate response")
        #validate_message(request, response)
        
        return response
        
    except ValueError as error:
        logger.error(error)
        raise

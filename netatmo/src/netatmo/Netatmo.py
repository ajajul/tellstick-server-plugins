# -*- coding: utf-8 -*-

from base import Application, Plugin, implements, mainthread, configuration
from web.base import IWebRequestHandler, WebResponseRedirect, WebResponseJson, Server
from telldus import DeviceManager, Sensor
from telldus.web import IWebReactHandler, ConfigurationReactComponent
from threading import Thread
from pkg_resources import resource_filename
import rauth, json, time

class NetatmoModule(Sensor):
	def __init__(self, _id, name, type):
		super(NetatmoModule,self).__init__()
		self._localId = _id
		self.batteryLevel = None
		self._type = type
		self.setName(name)

	def battery(self):
		return self.batteryLevel

	def localId(self):
		return self._localId

	def model(self):
		return self._type

	def typeString(self):
		return 'netatmo'

class NetatmoConfiguration(ConfigurationReactComponent):
	def __init__(self):
		super(NetatmoConfiguration,self).__init__(component='netatmo', defaultValue={}, readable=False)
		self.activated = False

	def serialize(self):
		retval = super(NetatmoConfiguration,self).serialize()
		retval['activated'] = self.activated
		return retval

@configuration(
	oauth = NetatmoConfiguration()
)
class Netatmo(Plugin):
	implements(IWebRequestHandler)
	implements(IWebReactHandler)

	supportedTypes = {
		'Temperature': (Sensor.TEMPERATURE, Sensor.SCALE_TEMPERATURE_CELCIUS),
		'Humidity': (Sensor.HUMIDITY, Sensor.SCALE_HUMIDITY_PERCENT),
		#'CO2': (Sensor.UNKNOWN, Sensor.SCALE_UNKNOWN),
		#'Noise':,
		'Pressure': (Sensor.BAROMETRIC_PRESSURE, Sensor.SCALE_BAROMETRIC_PRESSURE_KPA),
		'Rain': (Sensor.RAINRATE, Sensor.SCALE_RAINRATE_MMH),
		'sum_rain_24': (Sensor.RAINTOTAL, Sensor.SCALE_RAINTOTAL_MM),
		'WindAngle': (Sensor.WINDDIRECTION, Sensor.SCALE_WIND_DIRECTION),
		'WindStrength': (Sensor.WINDAVERAGE, Sensor.SCALE_WIND_VELOCITY_MS),
		'GustStrength': (Sensor.WINDGUST, Sensor.SCALE_WIND_VELOCITY_MS),
	}
	products = {
#		'NAMain': {}  # Base station
		'NAModule1': {'batteryMax': 6000, 'batteryMin': 3600},  # Outdoor module
		'NAModule4': {'batteryMax': 6000, 'batteryMin': 4200},  # Additional indoor module
		'NAModule3': {'batteryMax': 6000, 'batteryMin': 3600},  # Rain gauge
		'NAModule2': {'batteryMax': 6000, 'batteryMin': 3950},  # Wind gauge
#		'NAPlug': {},  # Thermostat relay/plug
#		'NATherm1': {},  # Thermostat module
	}

	def __init__(self):
		self.deviceManager = DeviceManager(self.context)
		self.sensors = {}
		self.loaded = False
		self.clientId = ''
		self.clientSecret = ''
		config = self.config('oauth')
		self.accessToken = config.get('accessToken', '')
		self.refreshToken = config.get('refreshToken', '')
		if self.accessToken is not '':
			self.configuration['oauth'].activated = True
		self.tokenTTL = config.get('tokenTTL', 0)
		Application().registerScheduledTask(self.__requestNewValues, minutes=10, runAtOnce=True)

	def getReactComponents(self):
		return {
			'netatmo': {
				'title': 'Netatmo',
				'script': 'netatmo/netatmo.js',
			}
		}

	def matchRequest(self, plugin, path):
		if plugin != 'netatmo':
			return False
		if path in ['activate', 'code', 'logout']:
			return True
		return False

	def handleRequest(self, plugin, path, params, request, **kwargs):
		# Web requests
		if path in ['activate', 'code']:
			service = rauth.OAuth2Service(
				client_id=self.clientId,
				client_secret=self.clientSecret,
				access_token_url='https://api.netatmo.net/oauth2/token',
				authorize_url='https://api.netatmo.net/oauth2/authorize'
			)
			if path == 'activate':
				params = {'redirect_uri': '%s/netatmo/code' % request.base(),
				          'response_type': 'code'}
				url = service.get_authorize_url(**params)
				return WebResponseJson({'url': url})
			if path == 'code':
				data = {'code': params['code'],
				        'grant_type': 'authorization_code',
				        'redirect_uri': '%s/netatmo/code' % request.base()
				}
				session = service.get_auth_session(data=data, decoder=self.__decodeAccessToken)
				return WebResponseRedirect('%s/plugins?settings=netatmo' % request.base())
		if path == 'logout':
			self.accessToken = ''
			self.refreshToken = ''
			self.tokenTTL = 0
			self.setConfig('oauth', {
				'accessToken': self.accessToken,
				'refreshToken': self.refreshToken,
				'tokenTTL': self.tokenTTL,
			})
			self.configuration['oauth'].activated = False
			return WebResponseJson({'success': True})
		return None

	def __addUpdateDevice(self, data):
		if data['_id'] not in self.sensors:
			sensor = NetatmoModule(data['_id'], data['module_name'], data['type'])
			self.deviceManager.addDevice(sensor)
			self.sensors[data['_id']] = sensor
		else:
			sensor = self.sensors[data['_id']]
		for dataType in Netatmo.supportedTypes:
			if dataType not in data['dashboard_data']:
				continue
			valueType, scale = Netatmo.supportedTypes[dataType]
			value = data['dashboard_data'][dataType]
			if dataType == 'WindStrength' or dataType == 'GustStrength':
				value = round(value / 3.6, 2)  # Data is reported in km/h, we want m/s
			elif dataType == 'Pressure':
				value = round(value/10.0)  # Data is reported in mbar, we want kPa
			sensor.setSensorValue(valueType, value, scale)
		if 'battery_vp' in data and data['type'] in Netatmo.products:
			product = Netatmo.products[data['type']]
			battery = 1.0*max(min(data['battery_vp'], product['batteryMax']), product['batteryMin'])
			sensor.batteryLevel = int((battery - product['batteryMin'])/(product['batteryMax'] - product['batteryMin'])*100)

	@mainthread
	def __parseValues(self, data):
		if 'body' not in data:
			return
		body = data['body']
		if 'devices' not in body:
			return
		devices = body['devices']
		for device in devices:
			self.__addUpdateDevice(device)
			for module in device['modules']:
				self.__addUpdateDevice(module)
		if self.loaded == False:
			self.loaded = True
			self.deviceManager.finishedLoading('netatmo')

	def __requestNewValues(self):
		if self.accessToken == '':
			return
		def backgroundTask():
			service = rauth.OAuth2Service(
				client_id=self.clientId,
				client_secret=self.clientSecret,
				access_token_url='https://api.netatmo.net/oauth2/token'
			)
			if time.time() > self.tokenTTL:
				session = self.__requestSession(service)
			else:
				session = rauth.OAuth2Session(self.clientId,self.clientSecret,access_token=self.accessToken,service=service)
			response = session.get('https://api.netatmo.com/api/getstationsdata')
			data = response.json()
			if 'error' in data and data['error']['code'] in [2, 3]:
				# Token is expired. Request new
				session = self.__requestSession(service)
				response = session.get('https://api.netatmo.com/api/getstationsdata')
				data = response.json()
			self.__parseValues(data)
		Thread(target=backgroundTask).start()

	def __requestSession(self, service):
		data = {'grant_type': 'refresh_token',
		        'refresh_token': self.refreshToken}
		session = service.get_auth_session(data=data, decoder=self.__decodeAccessToken)
		return session

	def __decodeAccessToken(self, data):
		response = json.loads(data)
		self.accessToken = response['access_token']
		self.refreshToken = response['refresh_token']
		self.tokenTTL = int(time.time()) + response['expires_in']
		self.setConfig('oauth', {
			'accessToken': self.accessToken,
			'refreshToken': self.refreshToken,
			'tokenTTL': self.tokenTTL,
		})
		self.configuration['oauth'].activated = True
		return response

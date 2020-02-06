#!/usr/bin/env python
# -*- coding: utf-8 -*-

#fix teleinfo import. If not specified import local one that does not contain Parser and other stuff
#this is fixed in python3.X
from __future__ import absolute_import
import logging
import time
from raspiot.utils import CommandError, MissingParameter, InvalidParameter
from raspiot.libs.internals.task import Task
from raspiot.raspiot import RaspIotModule
from raspiot.utils import CATEGORIES
import glob
import teleinfo
from teleinfo import Parser
from teleinfo.hw_vendors import UTInfo2

__all__ = [u'Teleinfo']

class Teleinfo(RaspIotModule):
    """
    Teleinfo application.
    Returns current France Enedis electricity provider.
    This application uses MicroTeleinfo dongle built by Charles Hallard (http://hallard.me/utinfo/)

    Note:
        python library: https://github.com/demikl/python-teleinfo
        Teleinfo protocol description: https://www.planete-domotique.com/blog/2010/03/30/la-teleinformation-edf/
    """
    MODULE_AUTHOR = u'Cleep'
    MODULE_VERSION = u'1.0.0'
    MODULE_PRICE = 0
    MODULE_DEPS = []
    MODULE_CATEGORY = CATEGORIES.SERVICE
    MODULE_DESCRIPTION = u'Gets house power consumption infos from Enedis electric power meter'
    MODULE_LONGDESCRIPTION = u'This application reads data from Enedis Linky (or older compatible version) house power meter and make<br>\
                             possible to monitor your house power consumption usage and build charts on some useful indicators.'
    MODULE_TAGS = [u'electricity', u'power', u'teleinfo', u'enedis', u'edf', u'erdf', u'engie']
    MODULE_COUNTRY = u'fr'
    MODULE_URLINFO = u'https://github.com/tangb/cleepmod-teleinfo'
    MODULE_URLHELP = u'https://github.com/tangb/cleepmod-teleinfo/wiki'
    MODULE_URLSITE = u'https://www.enedis.fr/'
    MODULE_URLBUGS = u'https://github.com/tangb/cleepmod-teleinfo/issues'

    MODULE_CONFIG_FILE = u'teleinfo.conf'
    DEFAULT_CONFIG = {
        u'previousconsoheurespleines': None,
        u'previousconsoheurescreuses': None,
    }

    TELEINFO_TASK_DELAY = 60 # seconds
    USB_PATH = u'/dev/serial/by-id/'
    VA_FACTOR = 220

    def __init__(self, bootstrap, debug_enabled):
        """
        Constructor

        Args:
            bootstrap (dict): bootstrap objects
            debug_enabled (bool): flag to set debug level to logger
        """
        #init
        self.port = None #init here due to get_config overwritting
        RaspIotModule.__init__(self, bootstrap, debug_enabled)

        #members
        self.teleinfo_task = None
        self.__teleinfo_uuid = None
        self.__teleinfo_parser = None
        self.__last_conso_heures_creuses = 0
        self.__last_conso_heures_pleines = 0

        #events
        self.power_update_event = self._get_event('teleinfo.power.update')
        self.consumption_update_event = self._get_event('teleinfo.consumption.update')
        
    def _get_config(self):
        """
        Returns module configuration
        It just returns port value

        Returns:
            dict: configuration
        """
        return {
            u'port': self.port
        }

    def _configure(self):
        """
        Configure module
        """
        #add openweathermap device
        if self._get_device_count()==0:
            self._add_device({
                u'type': u'teleinfo',
                u'name': u'Teleinfo',
                u'lastupdate': None,
                u'power': 0,
                u'mode': None,
            })

        #get device uuid
        devices = self.get_module_devices()
        if len(devices)==1:
            self.__teleinfo_uuid = devices.keys()[0]
        else:
            #supposed to have only one device!
            raise Exception(u'Teleinfo should handle only one device')

        #configure hardware
        if self.__configure_hardware():
            #update values at startup
            self.last_raw = self._get_teleinfo_raw_data()

        #start teleinfo task
        self._start_teleinfo_task()

    def __configure_hardware(self):
        """
        Configure hardware
        Search device (device path) and initialize parser

        Returns:
            bool: True if hardware configured successfully, False otherwise
        """
        #scan dongle port
        devices = [path for path in [f for f in glob.glob(self.USB_PATH + u'*')] if path.find(u'TINFO')>0]
        self.logger.debug('Found devices: %s' % devices)
        if len(devices)==0:
            self.logger.warn(u'No device found, teleinfo disabled')
            return False

        #get only one device
        self.port = devices[0]
        self.logger.info(u'Using device "%s" as teleinfo port' % self.port)

        #init parser
        try:
            self.__teleinfo_parser = Parser(UTInfo2(port=self.port))
            return True

        except:
            self.logger.exception(u'Fatal error initializing teleinfo parser. Are you using µTeleinf dongle?')
            return False

    def _stop(self):
        """
        Stop module
        """
        self._stop_teleinfo_task()

    def _start_teleinfo_task(self):
        """
        Start teleinfo task
        """
        if self.teleinfo_task is None:
            self.teleinfo_task = Task(self.TELEINFO_TASK_DELAY, self._teleinfo_task, self.logger)
            self.teleinfo_task.start()

    def _stop_teleinfo_task(self):
        """
        Stop weather task
        """
        if self.teleinfo_task is not None:
            self.teleinfo_task.stop()

    def _restart_teleinfo_task(self):
        """
        Restart teleinfo task
        """
        self._stop_teleinfo_task()
        self._start_teleinfo_task()

    def event_received(self, event):
        """ 
        Event received from bus

        Params:
            event (MessageRequest): event data
        """
        self.logger.trace(u'Event received %s' % event)
        if event[u'event']==u'parameters.time.now':
            #compute and send consumption event once a day at midnight
            #previous day consumption is stored in configuration file to take care of device reboot
            if event[u'params'][u'hour']==0 and event[u'params'][u'minute']==0:
                config = self._get_config()
                params = {
                    u'heurescreuses': config[u'previousconsoheurescreuses'] - self.__last_conso_heures_creuses,
                    u'heurespleines': config[u'previousconsoheurespleines'] - self.__last_conso_heures_pleines,
                }

                #send consumption event
                self.logger.trace(u'Send consumption update event with params: %s' % params)
                self.consumption_update_event.send(params=params, device_id=self.__teleinfo_uuid)

                #save last power consumption in config file
                self.logger.info(u'Save last power consumption of the day')
                self._update_config({
                    u'previousconsoheurescreuses', self.__last_conso_heures_creuses,
                    u'previousconsoheurespleines', self.__last_conso_heures_pleines,
                })

    def _teleinfo_task(self):
        """
        Teleinfo task reads data from dongle and store current consumption.
        It also emit power event.
        """
        try:
            if self.port:
                self.logger.trace(u'Update teleinfo')

                #read teleinfo data
                self.last_raw = self._get_teleinfo_raw_data()
                self.logger.debug(u'Raw teleinfo: %s' % self.last_raw)

                #compute some values
                if self.last_raw:
                    #power consumption
                    keys = set(self.last_raw.keys())
                    if set([u'HCHC', u'HCHP']).issubset(keys):
                        #handle heures creuses/pleines
                        self.logger.trace(u'Handle heures creuses/pleines')
                        self.__last_conso_heures_creuses = int(self.last_raw[u'HCHC'])
                        self.__last_conso_heures_pleines = int(self.last_raw[u'HCHP'])
                    elif set([u'EJPHN', u'EJPHPM']).issubset(keys):
                        #handle EJP
                        self.logger.trace(u'Handle EJP')
                        self.__last_conso_heures_creuses = int(self.last_raw[u'EJPHN'])
                        self.__last_conso_heures_pleines = int(self.last_raw[u'EJPHPM'])
                    elif set([u'BBRHCJB', u'BBRHPJB', u'BBRHCJW', u'BBRHPJW', u'BBRHCJR', u'BBRHPJR']).issubset(keys):
                        #handle Tempo
                        self.logger.trace(u'Handle Tempo')
                        self.__last_conso_heures_creuses = int(self.last_raw[u'BBRHCJB']) + int(self.last_raw[u'BBRHCJW']) + int(self.last_raw[u'BBRHCJR'])
                        self.__last_conso_heures_pleines = int(self.last_raw[u'BBRHPJB']) + int(self.last_raw[u'BBRHPJW']) + int(self.last_raw[u'BBRHPJR'])
                    elif set([u'BASE']).issubset(keys):
                        #handle Base
                        self.logger.trace(u'Handle Base')
                        self.__last_conso_heures_creuses = int(self.last_raw[u'BASE'])
                        self.__last_conso_heures_pleines = 0
                    else:
                        self.logger.warn(u'No consumption value in raw data %s' % self.last_raw)

                    #intensity
                    if set([u'PAPP']).issubset(keys):
                        params = {
                            u'lastupdate': int(time.time()),
                            u'power': int(self.last_raw[u'PAPP']) * self.VA_FACTOR,
                            u'currentmode': self.last_raw[u'PTEC'] if u'PTEC' in self.last_raw else None,
                            u'nextmode': self.last_raw[u'DEMAIN'] if u'DEMAIN' in self.last_raw else None,
                        }
                        
                        #and emit events
                        self.logger.trace(u'Send power update event with params: %s' % params)
                        self.power_update_event.send(params=params, device_id=self.__teleinfo_uuid)
                    else:
                        self.logger.warn(u'No intensity value in raw data %s' % self.last_raw)

        except Exception as e:
            self.logger.exception(u'Exception during teleinfo task:')

    def _get_teleinfo_raw_data(self):
        """
        Get teleinfo raw data from power meter

        Returns:
            dict: raw teleinfo data or empty if no dongle not connected
        """
        if self.__teleinfo_parser:
            return self.__teleinfo_parser.get_frame()

        return {}

    def get_teleinfo(self):
        """
        Return latest teleinfo data

        Returns:
            dict: dict of teleinfo data according to user subscription (can be empty if no dongle configured)
        """
        return self.last_raw

#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
# Copyright 2011-2014 Marcus Popp                          marcus@popp.mx
# Copyright 2016- Christian Straßburg
#########################################################################
#  This file is part of SmartHomeNG
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG.  If not, see <http://www.gnu.org/licenses/>.
##########################################################################

import gc  # noqa
import logging
import time
import datetime
import calendar
import sys
import traceback
import threading
import os  # noqa
import random
import types  # noqa
import subprocess  # noqa
from lib.model.smartplugin import SmartPlugin

import dateutil.relativedelta
from dateutil.relativedelta import MO, TU, WE, TH, FR, SA, SU
from dateutil.tz import tzutc

logger = logging.getLogger(__name__)

class PriorityQueue:

    def __init__(self):
        self.queue = []
        self.lock = threading.Lock()

    def insert(self, priority, data):
        self.lock.acquire()
        lo = 0
        hi = len(self.queue)
        while lo < hi:
            mid = (lo + hi) // 2
            if priority < self.queue[mid][0]:
                hi = mid
            else:
                lo = mid + 1
        self.queue.insert(lo, (priority, data))
        self.lock.release()

    def get(self):
        self.lock.acquire()
        try:
            return self.queue.pop(0)
        except IndexError:
            raise
        finally:
            self.lock.release()

    def qsize(self):
        return len(self.queue)


class Scheduler(threading.Thread):

    _workers = []
    _worker_num = 5
    _worker_max = 20
    _worker_delta = 60  # wait 60 seconds before adding another worker thread
    _scheduler = {}
    _runq = PriorityQueue()
    _triggerq = PriorityQueue()

    def __init__(self, smarthome):
        threading.Thread.__init__(self, name='Scheduler')
        logger.info('Init Scheduler')
        self._sh = smarthome
        self._lock = threading.Lock()
        self._runc = threading.Condition()

    def run(self):
        self.alive = True
        logger.debug("creating {0} workers".format(self._worker_num))
        for i in range(self._worker_num):
            self._add_worker()
        while self.alive:
            now = self._sh.now()
            if self._runq.qsize() > len(self._workers):
                delta = now - self._last_worker
                if delta.seconds > self._worker_delta:
                    if len(self._workers) < self._worker_max:
                        self._add_worker()
                    else:
                        logger.error("Needing more worker threads than the specified maximum of {0}!".format(self._worker_max))
                        tn = {}
                        for t in threading.enumerate():
                            tn[t.name] = tn.get(t.name, 0) + 1
                        logger.info('Threads: ' + ', '.join("{0}: {1}".format(k, v) for (k, v) in list(tn.items())))
                        self._add_worker()
            while self._triggerq.qsize() > 0:
                try:
                    (dt, prio), (name, obj, by, source, dest, value) = self._triggerq.get()
                except Exception as e:
                    logger.warning("Trigger queue exception: {0}".format(e))
                    break

                if dt < now:  # run it
                    self._runc.acquire()
                    self._runq.insert(prio, (name, obj, by, source, dest, value))
                    self._runc.notify()
                    self._runc.release()
                else:  # put last entry back and break while loop
                    self._triggerq.insert((dt, prio), (name, obj, by, source, dest, value))
                    break
            if not self._lock.acquire(timeout=1):
                logger.critical("Scheduler: Deadlock!")
                continue
            for name in self._scheduler:
                task = self._scheduler[name]
                if task['next'] is not None:
                    if task['next'] < now:
                        self._runc.acquire()
                        self._runq.insert(task['prio'], (name, task['obj'], 'Scheduler', None, None, task['value']))
                        self._runc.notify()
                        self._runc.release()
                        task['next'] = None
                    else:
                        continue
                elif not task['active']:
                    continue
                else:
                    if task['cron'] is None and task['cycle'] is None:
                        continue
                    else:
                        self._next_time(name)
            self._lock.release()
            time.sleep(0.5)

    def stop(self):
        self.alive = False

    def trigger(self, name, obj=None, by='Logic', source=None, value=None, dest=None, prio=3, dt=None):
        if obj is None:
            if name in self._scheduler:
                obj = self._scheduler[name]['obj']
            else:
                logger.warning("Logic name not found: {0}".format(name))
                return
        if name in self._scheduler:
            if not self._scheduler[name]['active']:
                logger.debug("Logic '{0}' deactivated. Ignoring trigger from {1} {2}".format(name, by, source))
                return
        if dt is None:
            logger.debug("Triggering {0} - by: {1} source: {2} dest: {3} value: {4}".format(name, by, source, dest, str(value)[:40]))
            self._runc.acquire()
            self._runq.insert(prio, (name, obj, by, source, dest, value))
            self._runc.notify()
            self._runc.release()
        else:
            if not isinstance(dt, datetime.datetime):
                logger.warning("Trigger: Not a valid timezone aware datetime for {0}. Ignoring.".format(name))
                return
            if dt.tzinfo is None:
                logger.warning("Trigger: Not a valid timezone aware datetime for {0}. Ignoring.".format(name))
                return
            logger.debug("Triggering {0} - by: {1} source: {2} dest: {3} value: {4} at: {5}".format(name, by, source, dest, str(value)[:40], dt))
            self._triggerq.insert((dt, prio), (name, obj, by, source, dest, value))

    def remove(self, name):
        self._lock.acquire()
        if name in self._scheduler:
            del(self._scheduler[name])
        self._lock.release()

    def return_next(self, name):
        if name in self._scheduler:
            return self._scheduler[name]['next']

    def add(self, name, obj, prio=3, cron=None, cycle=None, value=None, offset=None, next=None):
        self._lock.acquire()
        if isinstance(cron, str):
            cron = [cron, ]
        if isinstance(cron, list):
            _cron = {}
            for entry in cron:
                desc, __, _value = entry.partition('=')
                desc = desc.strip()
                if _value == '':
                    _value = None
                else:
                    _value = _value.strip()
                if desc.startswith('init'):
                    offset = 5  # default init offset
                    desc, op, seconds = desc.partition('+')
                    if op:
                        offset += int(seconds)
                    else:
                        desc, op, seconds = desc.partition('-')
                        if op:
                            offset -= int(seconds)
                    value = _value
                    next = self._sh.now() + datetime.timedelta(seconds=offset)
                else:
                    _cron[desc] = _value
            if _cron == {}:
                cron = None
            else:
                cron = _cron
        if isinstance(cycle, int):
            cycle = {cycle: None}
        elif isinstance(cycle, str):
            cycle, __, _value = cycle.partition('=')
            try:
                cycle = int(cycle.strip())
            except Exception:
                logger.warning("Scheduler: invalid cycle entry for {0} {1}".format(name, cycle))
                return
            if _value != '':
                _value = _value.strip()
            else:
                _value = None
            cycle = {cycle: _value}
        if cycle is not None and offset is None:  # spread cycle jobs
                offset = random.randint(10, 15)
        # change name for multi instance plugins 
        if obj.__class__.__name__ == 'method':
            if isinstance(obj.__self__, SmartPlugin):
                if obj.__self__.get_instance_name() != '':
                    name = name +'_'+ obj.__self__.get_instance_name()
                    logger.debug("Scheduler: Name changed by adding plugin instance name to: " + name)
        self._scheduler[name] = {'prio': prio, 'obj': obj, 'cron': cron, 'cycle': cycle, 'value': value, 'next': next, 'active': True}
        if next is None:
            self._next_time(name, offset)
        self._lock.release()

    def get( self, name):
        if name in self._scheduler:
            return self._scheduler[name]
        else:
            return None

    def change(self, name, **kwargs):
        if name in self._scheduler:
            for key in kwargs:
                if key in self._scheduler[name]:
                    if key == 'cron':
                        if isinstance(kwargs[key], str):
                            _cron = {}
                            for entry in kwargs[key].split('|'):
                                desc, __, _value = entry.partition('=')
                                desc = desc.strip()
                                if _value == '':
                                    _value = None
                                else:
                                    _value = _value.strip()
                                _cron[desc] = _value
                            if _cron == {}:
                                kwargs[key] = None
                            else:
                                kwargs[key] = _cron
                    elif key == 'active':
                        if kwargs['active'] and not self._scheduler[name]['active']:
                            logger.info("Activating logic: {0}".format(name))
                        elif not kwargs['active'] and self._scheduler[name]['active']:
                            logger.info("Deactivating logic: {0}".format(name))
                    self._scheduler[name][key] = kwargs[key]
                else:
                    logger.warning("Attribute {0} for {1} not specified. Could not change it.".format(key, name))
            if self._scheduler[name]['active'] is True:
                if 'cycle' in kwargs or 'cron' in kwargs:
                    self._next_time(name)
            else:
                self._scheduler[name]['next'] = None
        else:
            logger.warning("Could not change {0}. No logic/method with this name found.".format(name))

    def _next_time(self, name, offset=None):
        job = self._scheduler[name]
        if None == job['cron'] == job['cycle']:
            self._scheduler[name]['next'] = None
            return
        next_time = None
        value = None
        now = self._sh.now()
        now = now.replace(microsecond=0)
        if job['cycle'] is not None:
            cycle = list(job['cycle'].keys())[0]
            value = job['cycle'][cycle]
            if offset is None:
                offset = cycle
            next_time = now + datetime.timedelta(seconds=offset)
        if job['cron'] is not None:
            for entry in job['cron']:
                ct = self._crontab(entry)
                if next_time is not None:
                    if ct < next_time:
                        next_time = ct
                        value = job['cron'][entry]
                else:
                    next_time = ct
                    value = job['cron'][entry]
        self._scheduler[name]['next'] = next_time
        self._scheduler[name]['value'] = value
        if name not in ['Connections', 'series', 'SQLite dump']:
            logger.debug("{0} next time: {1}".format(name, next_time))

    def __iter__(self):
        for job in self._scheduler:
            yield job

    def _add_worker(self):
        self._last_worker = self._sh.now()
        t = threading.Thread(target=self._worker)
        t.start()
        self._workers.append(t)
        if len(self._workers) > self._worker_num:
            logger.info("Adding worker thread. Total: {0}".format(len(self._workers)))
            tn = {}
            for t in threading.enumerate():
                tn[t.name] = tn.get(t.name, 0) + 1
            logger.info('Threads: ' + ', '.join("{0}: {1}".format(k, v) for (k, v) in list(tn.items())))

    def _worker(self):
        while self.alive:
            self._runc.acquire()
            self._runc.wait(timeout=1)
            try:
                prio, (name, obj, by, source, dest, value) = self._runq.get()
            except IndexError:
                continue
            finally:
                self._runc.release()
            self._task(name, obj, by, source, dest, value)

    def _task(self, name, obj, by, source, dest, value):
        threading.current_thread().name = name
        logger = logging.getLogger(name)
        if obj.__class__.__name__ == 'Logic':
            trigger = {'by': by, 'source': source, 'dest': dest, 'value': value}  # noqa
            logic = obj  # noqa
            sh = self._sh  # noqa
            try:
                exec(obj.bytecode)
            except SystemExit:
                # ignore exit() call from logic.
                pass
            except Exception as e:
                tb = sys.exc_info()[2]
                tb = traceback.extract_tb(tb)[-1]
                logger.exception("Logic: {0}, File: {1}, Line: {2}, Method: {3}, Exception: {4}".format(name, tb[0], tb[1], tb[2], e))
        elif obj.__class__.__name__ == 'Item':
            try:
                if value is not None:
                    obj(value, caller="Scheduler")
            except Exception as e:
                logger.exception("Item {0} exception: {1}".format(name, e))
        else:  # method
            try:
                if value is None:
                    obj()
                else:
                    obj(**value)
            except Exception as e:
                logger.exception("Method {0} exception: {1}".format(name, e))
        threading.current_thread().name = 'idle'

    def _crontab(self, crontab):
        try:
            # process sunrise/sunset
            for entry in crontab.split('<'):
                if entry.startswith('sun'):
                    return self._sun(crontab)
            next_event = self._parse_month(crontab)  # this month
            if not next_event:
                next_event = self._parse_month(crontab, next_month=True)  # next month
            return next_event
        except Exception as e:
            logger.error('Error parsing crontab "{}": {}'.format(crontab, e))
            return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)

    def _parse_month(self, crontab, next_month=False):
        now = self._sh.now()
        minute, hour, day, wday = crontab.split(' ')
        # evaluate the crontab strings
        minute_range = self._range(minute, 00, 59)
        hour_range = self._range(hour, 00, 23)
        if not next_month:
            mdays = calendar.monthrange(now.year, now.month)[1]
        elif now.month == 12:
            mdays = calendar.monthrange(now.year + 1, 1)[1]
        else:
            mdays = calendar.monthrange(now.year, now.month + 1)[1]
        if wday == '*' and day == '*':
            day_range = self._day_range('0, 1, 2, 3, 4, 5, 6')
        elif wday != '*' and day == '*':
            day_range = self._day_range(wday)
        elif wday != '*' and day != '*':
            day_range = self._day_range(wday)
            day_range = day_range + self._range(day, 0o1, mdays)
        else:
            day_range = self._range(day, 0o1, mdays)
        # combine the different ranges
        event_range = sorted([str(day) + '-' + str(hour) + '-' + str(minute) for minute in minute_range for hour in hour_range for day in day_range])
        if next_month:  # next month
            next_event = event_range[0]
            next_time = now + dateutil.relativedelta.relativedelta(months=+1)
        else:  # this month
            now_str = now.strftime("%d-%H-%M")
            next_event = self._next(lambda event: event > now_str, event_range)
            if not next_event:
                return False
            next_time = now
        day, hour, minute = next_event.split('-')
        return next_time.replace(day=int(day), hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def _next(self, f, seq):
        for item in seq:
            if f(item):
                return item
        return False

    def _sun(self, crontab):
        if not self._sh.sun:  # no sun object created
            logger.warning('No latitude/longitude specified. You could not use sunrise/sunset as crontab entry.')
            return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)
        # find min/max times
        tabs = crontab.split('<')
        if len(tabs) == 1:
            smin = None
            cron = tabs[0].strip()
            smax = None
        elif len(tabs) == 2:
            if tabs[0].startswith('sun'):
                smin = None
                cron = tabs[0].strip()
                smax = tabs[1].strip()
            else:
                smin = tabs[0].strip()
                cron = tabs[1].strip()
                smax = None
        elif len(tabs) == 3:
            smin = tabs[0].strip()
            cron = tabs[1].strip()
            smax = tabs[2].strip()
        else:
            logger.error('Wrong syntax: {0}. Should be [H:M<](sunrise|sunset)[+|-][offset][<H:M]'.format(crontab))
            return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)

        doff = 0  # degree offset
        moff = 0  # minute offset
        tmp, op, offs = cron.rpartition('+')
        if op:
            if offs.endswith('m'):
                moff = int(offs.strip('m'))
            else:
                doff = float(offs)
        else:
            tmp, op, offs = cron.rpartition('-')
            if op:
                if offs.endswith('m'):
                    moff = -int(offs.strip('m'))
                else:
                    doff = -float(offs)

        if cron.startswith('sunrise'):
            next_time = self._sh.sun.rise(doff, moff)
        elif cron.startswith('sunset'):
            next_time = self._sh.sun.set(doff, moff)
        else:
            logger.error('Wrong syntax: {0}. Should be [H:M<](sunrise|sunset)[+|-][offset][<H:M]'.format(crontab))
            return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)

        now = self._sh.now()
        if smin is not None:
            h, sep, m = smin.partition(':')
            try:
                dmin = next_time.replace(hour=int(h), minute=int(m), second=0, tzinfo=self._sh.tzinfo())
            except Exception:
                logger.error('Wrong syntax: {0}. Should be [H:M<](sunrise|sunset)[+|-][offset][<H:M]'.format(crontab))
                return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)
            if dmin > next_time:
                next_time = dmin
        if smax is not None:
            h, sep, m = smax.partition(':')
            try:
                dmax = next_time.replace(hour=int(h), minute=int(m), second=0, tzinfo=self._sh.tzinfo())
            except Exception:
                logger.error('Wrong syntax: {0}. Should be [H:M<](sunrise|sunset)[+|-][offset][<H:M]'.format(crontab))
                return datetime.datetime.now(tzutc()) + dateutil.relativedelta.relativedelta(years=+10)
            if dmax < next_time:
                if dmax < now:
                    dmax = dmax + datetime.timedelta(days=1)
                next_time = dmax
        return next_time

    def _range(self, entry, low, high):
        result = []
        item_range = []

        # Check for multiple items and process each item recursively
        if ',' in entry:
            for item in entry.split(','):
                result.extend(self._range(item, low, high))

        # Check for intervals, e.g. "*/2", "9-17/2"
        elif '/' in entry:
             spec_range, interval = entry.split('/')
             logger.error('Cron spec interval {} {}'.format(entry, interval))
             result = self._range(spec_range, low, high)[::int(interval)]

        # Check for numeric ranges, e.g. "9-17"
        elif '-' in entry:
             spec_low, spec_high = entry.split('-')
             result = self._range('*', int(spec_low), int(spec_high))

        # Process single item
        else:
            if entry == '*':
                item_range = list(range(low, high + 1))
            else:
                item = int(entry)
                if item > high:  # entry above range
                    item = high  # truncate value to highest possible
                item_range.append(item)
            for entry in item_range:
                result.append('{:02d}'.format(entry))

        return result

    def _day_range(self, days):
        now = datetime.date.today()
        wdays = [MO, TU, WE, TH, FR, SA, SU]
        result = []
        for day in days.split(','):
            wday = wdays[int(day)]
            # add next weekday occurence
            day = now + dateutil.relativedelta.relativedelta(weekday=wday)
            result.append(day.strftime("%d"))
            # safety add-on if weekday equals todays weekday
            day = now + dateutil.relativedelta.relativedelta(weekday=wday(+2))
            result.append(day.strftime("%d"))
        return result

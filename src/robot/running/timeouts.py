#  Copyright 2008-2011 Nokia Siemens Networks Oyj
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import ctypes
import os
import threading

import time
import thread

from robot import utils
from robot.utils.robotthread import ThreadedRunner
from robot.errors import TimeoutError, DataError, FrameworkError


class _Timeout(object):

    def __init__(self, timeout=None, message='', variables=None):
        self.string = timeout or ''
        self.message = message
        self.secs = -1
        self.starttime = -1
        self.error = None
        if variables:
            self.replace_variables(variables)

    @property
    def type(self):
        return type(self).__name__.replace('Timeout', ' timeout')

    @property
    def active(self):
        return self.starttime > 0

    def replace_variables(self, variables):
        try:
            self.string = variables.replace_string(self.string)
            if not self.string or self.string.upper() == 'NONE':
                return
            self.secs = utils.timestr_to_secs(self.string)
            self.string = utils.secs_to_timestr(self.secs)
            self.message = variables.replace_string(self.message)
        except (DataError, ValueError), err:
            self.secs = 0.000001 # to make timeout active
            self.error = 'Setting %s failed: %s' % (self.type.lower(), unicode(err))

    def start(self):
        if self.secs > 0:
            self.starttime = time.time()

    def time_left(self):
        if not self.active:
            return -1
        elapsed = time.time() - self.starttime
        # Timeout granularity is 1ms. Without rounding some timeout tests fail
        # intermittently on Windows, probably due to threading.Event.wait().
        return round(self.secs - elapsed, 3)

    def timed_out(self):
        return self.active and self.time_left() <= 0

    def __str__(self):
        return self.string

    def __cmp__(self, other):
        return cmp(not self.active, not other.active) \
            or cmp(self.time_left(), other.time_left())

    def run(self, runnable, args=None, kwargs=None):
        if self.error:
            raise DataError(self.error)
        if not self.active:
            raise FrameworkError('Timeout is not active')
        timeout = self.time_left()
        if timeout <= 0:
            raise TimeoutError(self.get_message())
        return self._execute_with_timeout(timeout, runnable, args, kwargs)

    def _execute_with_timeout(self, timeout, runnable, args, kwargs):
        raise NotImplementedError(self.__class__)

    def get_message(self):
        if not self.active:
            return '%s not active.' % self.type
        if not self.timed_out():
            return '%s %s active. %s seconds left.' % (self.type, self.string,
                                                       self.time_left())
        return self._get_timeout_error()

    def _get_timeout_error(self):
        if self.message:
            return self.message
        return '%s %s exceeded.' % (self.type, self.string)


try:

    from signal import setitimer, signal, SIGALRM, ITIMER_REAL

    class TimeoutWithSignaling(_Timeout):

        def _execute_with_timeout(self, timeout, runnable, args, kwargs):
            self._start_timer(timeout)
            try:
                return runnable(*(args or ()), **(kwargs or {}))
            finally:
                self._stop_timer()

        def _start_timer(self, timeout):
            signal(SIGALRM, self._raise_timeout_error)
            setitimer(ITIMER_REAL, timeout)

        def _raise_timeout_error(self, *args):
            raise TimeoutError(self._get_timeout_error())

        def _stop_timer(self):
            setitimer(ITIMER_REAL, 0)

    _Timeout = TimeoutWithSignaling

except ImportError:

    if os.name == 'nt':

        class TimeoutError(TimeoutError):
            global_message = ''

            def __unicode__(self):
                return TimeoutError.global_message

        class TimeoutSignaler(object):

            def __init__(self, timeout, timeout_error):
                self._runner_thread_id = thread.get_ident()
                TimeoutError.global_message = timeout_error
                self._timer = threading.Timer(timeout, self)
                self._timeout_occurred = False

            def start(self):
                self._timer.start()

            def cancel(self):
                self._timer.cancel()
                # In case timeout has occurred but the exception has not yet been
                # thrown we need to do this to ensure that the exception
                # is not thrown in an unsafe location
                if self._timeout_occurred:
                    self._cancel_exception()
                    raise TimeoutError()

            def __call__(self):
                self._timeout_occurred = True
                return_code = self._try_to_raise_timeout_error_in_runner_thread()
                while return_code > 1:
                    self._cancel_exception()
                    time.sleep(0) # yield so that other threads will get time
                    return_code = self._try_to_raise_timeout_error_in_runner_thread()

            def _try_to_raise_timeout_error_in_runner_thread(self):
                return ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    self._runner_thread_id,
                    ctypes.py_object(TimeoutError))

            def _cancel_exception(self):
                ctypes.pythonapi.PyThreadState_SetAsyncExc(self._runner_thread_id, None)


        class TimeoutWithTimerThrowingException(_Timeout):

            def _execute_with_timeout(self, timeout, runnable, args, kwargs):
                self._enable_timeout(timeout)
                try:
                    return runnable(*(args or ()), **(kwargs or {}))
                finally:
                    self._disable_timeout()

            def _enable_timeout(self, timeout):
                self._signaler = TimeoutSignaler(timeout, self._get_timeout_error())
                self._signaler.start()

            def _disable_timeout(self):
                self._signaler.cancel()


        _Timeout = TimeoutWithTimerThrowingException

    else:

        class TimeoutWithThread(_Timeout):

            def _execute_with_timeout(self, timeout, runnable, args, kwargs):
                runner = ThreadedRunner(runnable, args, kwargs)
                if runner.run_in_thread(timeout):
                    return runner.get_result()
                try:
                    runner.stop_thread()
                except:
                    raise TimeoutError('Stopping keyword after %s failed: %s'
                                       % (self.type.lower(), utils.get_error_message()))
                raise TimeoutError(self._get_timeout_error())

        _Timeout = TimeoutWithThread


class TestTimeout(_Timeout):
    _keyword_timeouted = False

    def set_keyword_timeout(self, timeout_occurred):
        self._keyword_timeouted = self._keyword_timeouted or timeout_occurred

    def any_timeout_occurred(self):
        return self.timed_out() or self._keyword_timeouted


class KeywordTimeout(_Timeout):
    pass

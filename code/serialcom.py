"""hardware.py - 

Simple module for communicating with ComPort firmware written for AVR328p.

Usage:
  hardware.py test [--dev=DEV ] [--test] [--submit_to=SUBMIT_TO] [--redishost=REDISHOST]
  hardware.py 1wire [--dev=DEV ] [--test] [--submit_to=SUBMIT_TO] [--redishost=REDISHOST]
  hardware.py run [--dev=DEV] [--local] [--submit_to=SUBMIT_TO] [--redishost=REDISHOST]
  hardware.py (-h | --help)

Options:
  -h, --help
  --dev=DEV              [default: /dev/arduino]
  --run=RUN              [default: True]
  --submit_to=SUBMIT_TO  [default: 127.0.0.1]
  --redishost=REDISHOST  [default: 127.0.0.1]

"""

# Python 
import threading
import time
import re
import json
import sys
from time import sleep
from datetime import datetime
import subprocess
from json import dumps, loads

# pip install
import serial
import redis

from docopt import docopt
from redislog import handlers, logger    # pip install python-redis-log

##########################################################################################
# Global definitions
TIMEOUT  = 2
EXCHANGE = 'ComPort'

#l = logger.RedisLogger('ablib.hw.sermon')
#l.addHandler(handlers.RedisHandler.to("log:sermon", host='localhost', port=6379, password=''))

def get_host_ip():
    shell_raw = subprocess.check_output(['hostname', '-I'])
    shell_parsed  = re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",shell_raw)
    if shell_parsed:
        return shell_parsed.group()
    else:
        return '127.0.0.1'

class Message(object):
    """
    Class for defining validating and handling messages send between system components
    """

    def __init__(self, from_host=get_host_ip(), to='', msg=''):
        self.from_host = from_host
        self.to        = to
        self.msg       = msg

    def __str__(self):
        return "Message(FROM: %s, TO: %s, MSG: %s)" % (self.from_host, self.to, str(self.msg))

    def as_jsno(self):
        data = {"FROM" : self.from_host, "TO" : self.to, "MSG" : self.msg}
        return dumps(data)

    def as_json(self):
        data = {"FROM" : self.from_host, "TO" : self.to, "MSG" : self.msg}
        return dumps(data)

    def decode(self, msg):
        data_dict = loads(msg)
        self.from_host = data_dict['FROM']
        self.to        = data_dict['TO']
        self.msg       = data_dict['MSG']
        return data_dict

##########################################################################################
class SerialRedisCom(object):
    re_data        = re.compile(r'(?:<)(?P<cmd>\d+)(?:>)(.*)(?:<\/)(?P=cmd)(?:>)', re.DOTALL)
    re_next_cmd    = re.compile("(?:<)(\d+)(?:>\{\"cmd\":\")")

    redis_pub_channel = 'data'
    
    def __init__(self,
                 port = '/dev/ttyUSB0',
                 packet_timeout=1,
                 baudrate=115200,       
                 bytesize=8,    
                 parity='N',    
                 stopbits=1, 
                 xonxoff=0,             
                 rtscts=0,              
                 writeTimeout=None,     
                 dsrdtr=None,
                 host='127.0.0.1',
                 run=True):
        
        self.buffer  = ''
        self.last_read_line = ''
        self.serial    = serial.Serial(port, baudrate, bytesize, parity, stopbits, packet_timeout, xonxoff, rtscts, writeTimeout, dsrdtr)
        self.signature = "{0:s}:{1:s}".format(get_host_ip(), self.serial.port)
        
        self.redis_send_key = self.signature+'-send'
        self.redis_read_key = self.signature+'-read'
        self.redis = redis.Redis(host=host)
        
        logger_name = 'sermon.py:{}'.format(self.signature)
        if sys.stdout.isatty():        
        #    self.log   = Logger(logger_name)
        #else:            
            self.log   = logger.RedisLogger(logger_name)
            self.log.addHandler(handlers.RedisHandler.to("log", host='localhost', port=6379))

        self.log.level = 1
        self.alive = False
        self._reader_alive = False

        # TODO add checking for redis presence and connection
        if self.redis.ping():
            # Register the new instance with the redis exchange
            if not self.redis.sismember(EXCHANGE,self.signature):
                self.redis.sadd(EXCHANGE,self.signature)
        else:
            pass
        
        if run:
            self.run()

    def __del__(self):
        self.log.debug("About to delete the object")
        self.close()
        time.sleep(1)
        self.log.debug("Closing serial interface")
        self.serial.close()

        if self.serial.closed:
            self.log.error("The serial connection still appears to be open")
        else:
            self.log.debug("The serial connection is closed")
        self.log.debug("Object deleted")

        if self.redis.sismember('ComPort',self.signature):
            self.redis.srem('ComPort',self.signature)
    
    # Thread control
    def run(self):
        self.log.debug('run()')
        self._start_reader()
        self._start_listner()

    def _start_reader(self):
        """Start reader thread"""
        self.log.debug("Start serial port reader thread")
        self.alive           = True
        self._reader_alive   = True
        self.receiver_thread = threading.Thread(target=self.reader)
        self.receiver_thread.setDaemon(True)
        self.receiver_thread.start()

    def _stop_reader(self):
        """Stop reader thread only, wait for clean exit of thread"""
        self.log.debug("Stop reader thread only, wait for clean exit of thread")
        self._reader_alive = False
        self.receiver_thread.join()

    def _start_listner(self):
        self.log.debug("Start redis sub channel and listen for commands send via redis")
        self._redis_subscriber_alive = True
        self.redis_subscriber_thread = threading.Thread(target=self.cmd_via_redis_subscriber)
        self.redis_subscriber_thread.setDaemon(True)
        self.redis_subscriber_thread.start()

    def cmd_via_redis_subscriber(self):
        self.log.debug('cmd_via_redis_subscriber(channel={})'.format(self.signature))
        self.pubsub    = self.redis.pubsub()
        self.pubsub.subscribe(self.signature)
        
        while self._redis_subscriber_alive:
            try:
                for item in self.pubsub.listen():
                    if item['data'] == "unsubscribe":
                        self.pubsub.unsubscribe()
                        self.log.info("unsubscribed and finished")
                        break
                    else:
                        cmd = item['data']
                        if isinstance(cmd,str):
                            self.log.debug(cmd)
                            self.send(item['data'])
                        else:
                            self.log.debug(cmd)
            except Exception as E:
                error_msg =  'error: {}'.format(E.message)
                self.log.error(error_msg)
        
        self.pubsub.unsubscribe()      
        self.log.debug('end of cmd_via_redis_subscriber()')

    def stop(self):
        self.alive = False

    def join(self, transmit_only=False):
        self.transmitter_thread.join()
        if not transmit_only:
            self.receiver_thread.join()
        
    def start_thread(self):
        '''
        Open the serial serial bus to be read. This starts the listening
        thread.
        '''

        self.log.debug('start_thread()')
        self.serial.flushInput()
        self.running.set()
        self.start()
        
    def open(self):
        if not self.serial.isOpen():
            self.serial.open()
        return self.serial.isOpen()
    
    def send(self, data, CR=True):
        '''Send command to the serial port
        '''
        if len(data) == 0:               
            return
        self.log.debug("send(cmd=%s)" % data)
        # Automatically append \n by default, but allow the user to send raw characters as well
        if CR:
            if (data[-1] == "\n"):
                pass            
            else:
                data += "\n"
            
        if self.open():
            try:
                self.serial.write(data)
                serial_error = 0
            except:
                serial_error = 1
        else:
            serial_error = 2
        self.redis.set(self.redis_send_key,data)
        return serial_error
    
    def read(self, waitfor=''):
        '''
        reads the data by waiting until new comport is found in the buffer and result can be read from the redis server
        '''

        serial_data = ''
        done = False
        to = time.clock()
        while time.clock() - to < TIMEOUT and not done:
            if self.alive and self._reader_alive:
                serial_data = self.redis.get(self.redis_read_key)
                done = waitfor in self.buffer and isinstance(serial_data,str)
            else:
                self.read_serial_data()
                done = waitfor in self.buffer and isinstance(serial_data,str)

        if not done:
            self.log.debug("read() did not find waitfor {:s} in self.buffer".format(waitfor))

        self.redis.delete(self.redis_read_key)
        return [done, serial_data]

    def query(self,cmd, **kwargs):
        """
        sends cmd to the controller and waits until waitfor is found in the buffer.
        """
        
        waitfor = kwargs.get('waitfor','')
        tag     = kwargs.get('tag','')
        json    = kwargs.get('json',1)
        delay   = kwargs.get('delay',0.01)

        if len(waitfor) < 1:
            next_cmd_num = self.re_next_cmd.findall(self.buffer)
            if len(next_cmd_num) > 0:
                waitfor = '<{:d}>{:s}"cmd":"'.format(int(next_cmd_num[0])+1,"{")

        self.log.debug('query(cmd=%s, waitfor=%s, tag=%s,json=%d, delay=%d):' % \
            (cmd, waitfor, tag, json, delay))

        self.send(cmd)
        time.sleep(delay)        
        query_data = self.read(waitfor=waitfor)
        if query_data[0]:
            try:
                query_data[1] = sjson.loads(query_data[1])
            except:
                query_data[0] = False
        return query_data

    def close(self):
        '''
        Close the listening thread.
        '''
        self.log.debug('close() - closing the worker thread')
        self.alive = False
        self._reader_alive = False
        self._redis_subscriber_alive = False
        self.receiver_thread.join()

    def reader(self):
        '''
        Run is the function that runs in the new thread and is called by        
        '''
        
        try:
            self.log.debug('Starting the listner thread')
            Msg = Message(self.signature)

            while self.alive and self._reader_alive:
                bytes_in_waiting = self.serial.inWaiting()
                
                if bytes_in_waiting:
                    new_data = self.serial.read(bytes_in_waiting)
                    self.buffer = self.buffer + new_data
                else:
                    sleep(0.1)


                crlf_index = self.buffer.find('\r\n')

                if crlf_index > -1:
                    line = self.buffer[0:crlf_index]
                    temp = self.re_data.findall(line)
                    self.log.debug('read line: ' + line)

                    if len(temp):
                        final_data = dict()
                        timestamp = datetime.now().strftime('%Y-%m-%d-%H:%M:%S')
                        final_data['timestamp'] = timestamp
                        final_data['raw']       = line
                        try:
                            final_data.update({'cmd_number' : sjson.loads(temp[0][0])})
                            final_data.update(sjson.loads(temp[0][1]))
                            self.log.debug('.....updated final_data')

                        except Exception as E:
                            final_data.update({'cmd_number' : -1})
                            error_msg = {'timestamp' : timestamp, 'from': self.signature, 'source' : 'ComPort', 'function' : 'def run() - inner', 'error' : E.message}
                            Msg.msg = error_msg
                            self.log.error(Msg.msg)

                        Msg.msg = final_data
                        self.log.debug("final_data={}".format(final_data))
                        self.redis.publish(self.redis_pub_channel, Msg.as_jsno())                        
                        self.redis.set(self.redis_read_key,Msg.as_jsno())
                        self.buffer = self.buffer[crlf_index+2:]                        
                    else:
                        self.buffer = ''
                        self.send('Z')
                        self.log.debug('.....reseting command number')
                        pass

        except Exception as E:
            error_msg = {'source' : 'ComPort', 'function' : 'def run() - outter', 'error' : E.message}
            self.log.error("Exception occured, within the run function: %s" % E.message)
        
        self.log.debug('Exiting run() function')

    def read_serial_data(self):

        Msg = Message(self.signature)
        bytes_in_waiting = self.serial.inWaiting()                
        if bytes_in_waiting:
            new_data = self.serial.read(bytes_in_waiting)
            self.buffer = self.buffer + new_data
        else:
            sleep(0.1)

        crlf_index = self.buffer.find('\r\n')

        if crlf_index > -1:
            self.last_read_line = self.buffer[0:crlf_index]            
            temp = self.re_data.findall(self.last_read_line)
            self.log.debug('read self.last_read_line: ' + self.last_read_line)

            if len(temp):
                final_data = dict()
                timestamp = datetime.now().strftime('%Y-%m-%d-%H:%M:%S')
                final_data['timestamp'] = timestamp
                final_data['raw']       = self.last_read_line
                try:
                    final_data.update({'cmd_number' : sjson.loads(temp[0][0])})
                    final_data.update(sjson.loads(temp[0][1]))
                    self.log.debug('.....updated final_data')

                except Exception as E:
                    final_data.update({'cmd_number' : -1})
                    error_msg = {'timestamp' : timestamp, 'from': self.signature, 'source' : 'ComPort', 'function' : 'def run() - inner', 'error' : E.message}
                    Msg.msg = error_msg
                    self.log.error(Msg.msg)

                Msg.msg = final_data
                self.log.debug("final_data={}".format(final_data))
                self.redis.publish(self.redis_pub_channel, Msg.as_jsno())
                #self.log.debug('.....publish to :' + self.redis_pub_channel)
                self.redis.set(self.redis_read_key,Msg.as_jsno())                
                #self.log.debug('.....empty buffer')
                self.buffer = self.buffer[crlf_index+2:]
            else:
                self.buffer = ''
                self.send('Z')
                self.log.debug('.....reseting command number')
                pass
            
class SimpleCom(object):
    
    def __init__(self,
                 port = '/dev/ttyUSB0',
                 packet_timeout=1,
                 baudrate=115200):
        
        self.buffer         = ''
        self.last_read_line = ''
        self.serial    = serial.Serial(port, baudrate)
        self.signature = "{0:s}:{1:s}".format(get_host_ip(), self.serial.port)
        
        logger_name = 'sermon.py:{}'.format(self.signature)
        if sys.stdout.isatty():
            self.log   = logger.RedisLogger(logger_name)
            self.log.addHandler(handlers.RedisHandler.to("log", host='localhost', port=6379))

        self.log.level = 1

    def __del__(self):
        self.log.debug("About to delete the object")        
        self.log.debug("Closing serial interface")
        self.serial.close()

        if self.serial.closed:
            self.log.error("The serial connection still appears to be open")
        else:
            self.log.debug("The serial connection is closed")
        self.log.debug("Object deleted")
        
    def open(self):
        if not self.serial.isOpen():
            self.serial.open()
        return self.serial.isOpen()
    
    def send(self, data, CR=True):
        '''Send command to the serial port
        '''
        if len(data) == 0:               
            return
        self.log.debug("send(cmd=%s)" % data)
        # Automatically append \n by default, but allow the user to send raw characters as well
        if CR:
            if (data[-1] == "\n"):
                pass            
            else:
                data += "\n"
            
        if self.open():
            try:
                self.serial.write(data)
                serial_error = 0
            except:
                serial_error = 1
        else:
            serial_error = 2
        return serial_error
    
    def read(self, waitfor=''):
        '''
        Run is the function that runs in the new thread and is called by        
        '''

        output = ''
        
        try:
            self.log.debug('Starting the listner thread')
            bytes_in_waiting = self.serial.inWaiting()
                
            if bytes_in_waiting:
                new_data = self.serial.read(bytes_in_waiting)
                self.buffer = self.buffer + new_data
            else:
                sleep(0.1)

            crlf_index = self.buffer.find('\r\n')

            if crlf_index > -1:
                output = self.buffer[0:crlf_index]
                self.buffer = self.buffer[crlf_index+2:]

        except Exception as E:
            error_msg = {'source' : 'ComPort', 'function' : 'def run() - outter', 'error' : E.message}
            self.log.error("Exception occured, within the run function: %s" % E.message)
        
        self.log.debug('Exiting run() function')
        return output

############################################################################################

def main(**kwargs):
    try:
        while True:
            sleep(0.1)
            pass
    except KeyboardInterrupt:
        pass    

if __name__ == '__main__':
    pass
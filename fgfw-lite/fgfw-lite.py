#!/usr/bin/env python
# coding: UTF-8
#
# FGFW_Lite.py A Proxy Server help go around the Great Firewall
#
# Copyright (C) 2012-2017 Jiang Chao <sgzz.cj@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <http://www.gnu.org/licenses>.

from __future__ import absolute_import, print_function, division

import sys
import os
import glob

gevent = None
try:
    import gevent
    import gevent.socket
    import gevent.server
    import gevent.queue
    import gevent.monkey
    gevent.monkey.patch_all(subprocess=True, Event=True)
    gevent.get_hub().threadpool_size = 20
except ImportError:
    sys.stderr.write('Warning: gevent not found! Using thread instead...\n')
except TypeError:
    gevent.monkey.patch_all()
    sys.stderr.write('Warning: Please update gevent to the latest 1.0 version!\n')
import subprocess
import shlex
import time
import re
import errno
import atexit
import base64
import json
import random
import select
import socket
import logging
import traceback
try:
    from cStringIO import StringIO
except ImportError:
    try:
        from StringIO import StringIO
    except ImportError:
        from io import BytesIO as StringIO
from threading import Thread

sys.dont_write_bytecode = True
WORKINGDIR = '/'.join(os.path.dirname(os.path.abspath(__file__).replace('\\', '/')).split('/')[:-1])
os.chdir(WORKINGDIR)
sys.path.append(os.path.dirname(os.path.abspath(__file__).replace('\\', '/')))
if sys.platform.startswith('win'):
    sys.path += glob.glob('%s/Python27/*.egg' % WORKINGDIR)

import config
from util import parse_hostport, is_connection_dropped, extract_server_name
from connection import create_connection
from encrypt import BufEmptyError, InvalidTag
from resolver import TCP_Resolver
from parent_proxy import ParentProxy
from httputil import read_response_line, read_headers, read_header_data, httpconn_pool, parse_headers
try:
    import urllib.request as urllib2
    import urllib.parse as urlparse
    urlquote = urlparse.quote
    urlunquote = urlparse.unquote
    from socketserver import ThreadingMixIn
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ipaddress import ip_address
except ImportError:
    import urllib2
    import urlparse
    urlquote = urllib2.quote
    urlunquote = urllib2.unquote
    from SocketServer import ThreadingMixIn
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from ipaddr import IPAddress as ip_address

try:
    from _manager import on_finish
except ImportError:
    def on_finish(hdlr):
        pass

__version__ = '4.21.2'

NetWorkIOError = (IOError, OSError, BufEmptyError, InvalidTag)
DEFAULT_TIMEOUT = 5
FAKEGIF = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;'


class ClientError(OSError):
    pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True, level=1, conf=None):
        self.proxy_level = level
        self.conf = conf
        self.logger = logging.getLogger(str(server_address[1]))
        self.logger.setLevel(logging.INFO)
        hdr = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(name)s:%(levelname)s %(message)s',
                                      datefmt='%H:%M:%S')
        hdr.setFormatter(formatter)
        self.logger.addHandler(hdr)
        self.logger.info('starting server at %s:%s, level %d' % (server_address[0], server_address[1], level))
        HTTPServer.__init__(self, server_address, RequestHandlerClass)


class HTTPRequestHandler(BaseHTTPRequestHandler):
    HTTPCONN_POOL = httpconn_pool()

    def __init__(self, request, client_address, server):
        self.conf = server.conf
        self.logger = server.logger
        self.traffic_count = [0, 0]  # [read from client, write to client]
        BaseHTTPRequestHandler.__init__(self, request, client_address, server)

    def _quote_html(self, html):
        return html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header('Connection', 'keep_alive')
        self.send_header("Content-Length", '0')
        self.end_headers()

    def log_message(self, format, *args):
        pass

    def finish(self):
        """make python2 BaseHTTPRequestHandler happy"""
        try:
            BaseHTTPRequestHandler.finish(self)
        except NetWorkIOError as e:
            if e[0] not in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE):
                raise

    def send_error(self, code, message=None):
        """Send and log an error reply. """
        try:
            short, long = self.responses[code]
        except KeyError:
            short, long = '???', '???'
        if message is None:
            message = short
        explain = long
        # using _quote_html to prevent Cross Site Scripting attacks (see bug #1100201)
        content = (self.error_message_format %
                   {'code': code, 'message': self._quote_html(message), 'explain': explain})
        self.send_response(code, message)
        self.send_header("Content-Type", self.error_content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Connection', 'keep_alive')
        self.end_headers()
        if self.command != 'HEAD' and code >= 200 and code not in (204, 304):
            self._wfile_write(content.encode('UTF-8'))
        self.logger.error('%s %s ' % (self.path, code))

    def write(self, code=200, msg=None, ctype=None):
        if msg is None:
            msg = b''
        if not isinstance(msg, bytes):
            msg = msg.encode('UTF-8')
        self.send_response(code)
        if ctype:
            self.send_header('Content-type', ctype)
        self.send_header('Content-Length', str(len(msg)))
        self.send_header('Connection', 'keep_alive')
        self.end_headers()
        if self.command != 'HEAD' and code >= 200 and code not in (204, 304):
            self._wfile_write(msg)

    def connection_recv(self, size):
        try:
            data = self.connection.recv(size)
            self.traffic_count[0] += len(data)
            return data
        except NetWorkIOError as e:
            raise ClientError(e.errno, e.strerror or repr(e))

    def rfile_read(self, size=-1):
        try:
            data = self.rfile.read(size)
            self.traffic_count[0] += len(data)
            return data
        except NetWorkIOError as e:
            raise ClientError(e.errno, e.strerror or repr(e))

    def rfile_readline(self, size=-1):
        try:
            data = self.rfile.readline(size)
            self.traffic_count[0] += len(data)
            return data
        except NetWorkIOError as e:
            raise ClientError(e.errno, e.strerror)

    def _wfile_write(self, data):
        self.retryable = False
        try:
            self.traffic_count[1] += len(data)
            return self.wfile.write(data)
        except NetWorkIOError as e:
            raise ClientError(e.errno, e.strerror)


class ProxyHandler(HTTPRequestHandler):
    server_version = "FW-Lite/" + __version__
    protocol_version = "HTTP/1.1"
    bufsize = 8192
    timeout = 60

    def __init__(self, request, client_address, server):
        self.ssrealip = None
        self.shortpath = ''
        self.ppname = ''
        self.retryable = True
        HTTPRequestHandler.__init__(self, request, client_address, server)

    def setup(self):
        HTTPRequestHandler.setup(self)
        self.traffic_count = [0, 0]  # [read from client, write to client]

    def handle_one_request(self):
        self._proxylist = None
        self.remotesoc = None
        self.retryable = True
        self.rbuffer = []  # client read buffer: store request body, ssl handshake package for retry. no pop method.
        self.wbuffer = []  # client write buffer: read only once, not used in connect method
        self.wbuffer_size = 0
        self.shortpath = None
        self.failed_parents = []
        self.path = ''
        self.noxff = False
        self.count = 0
        self.traffic_count = [0, 0]  # [read from client, write to client]
        self.logmethod = self.logger.info
        self.connection_port = self.connection.getpeername()[1]
        self.logger.debug('enter handle_one_request: %d' % self.connection_port)
        try:
            HTTPRequestHandler.handle_one_request(self)
        except NetWorkIOError as e:
            if e.errno in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE):
                self.close_connection = 1
            else:
                raise
        finally:
            if self.path:
                self.logger.debug(self.shortpath or self.path + ' finished: %d' % self.connection_port)
                self.logger.debug('upload: %d, download %d' % tuple(self.traffic_count))
            if self.remotesoc:
                self.remotesoc.close()
            on_finish(self)

    def getparent(self):
        if self._proxylist is None:
            self._proxylist = self.conf.GET_PROXY.get_proxy(self.path, self.requesthost, self.command, self.rip, self.server.proxy_level)
            self.logger.debug(repr(self._proxylist) + str(self.connection_port))
        if not self._proxylist:
            self.ppname = ''
            self.pproxy = None
            return 1
        self.pproxy = self._proxylist.pop(0)
        self.ppname = self.pproxy.name

    def do_GET(self):
        if isinstance(self.path, bytes):
            self.path = self.path.decode('latin1')
        if self.path.lower().startswith('ftp://'):
            return self.send_error(400)

        if self.path == '/pac':
            if self.headers['Host'].startswith(self.conf.local_ip):
                return self.write(msg=self.conf.PAC, ctype='application/x-ns-proxy-autoconfig')

        # transparent proxy
        if self.path.startswith('/'):
            if 'Host' not in self.headers:
                return self.send_error(403)
            self.path = 'http://%s%s' % (self.headers['Host'], self.path)

        # fix request
        if self.path.startswith('http://http://'):
            self.path = self.path[7:]

        parse = urlparse.urlparse(self.path)

        self.shortpath = '%s://%s%s%s%s' % (parse.scheme, parse.netloc, parse.path.split(':')[0], '?' if parse.query else '', ':' if ':' in parse.path else '')

        # redirector
        new_url = self.conf.GET_PROXY.redirect(self)
        if new_url:
            self.logger.debug('redirect %s, %s %s' % (new_url, self.command, self.shortpath or self.path))
            if new_url.isdigit() and 400 <= int(new_url) < 600:
                return self.send_error(int(new_url))
            elif new_url.lower() == 'return':
                # request handled by redirector, return
                self.logger.info('{} {} {} return'.format(self.command, self.shortpath or self.path, self.client_address[0]))
                return
            elif new_url.lower() == 'reset':
                self.close_connection = 1
                self.logger.info('{} {} {} reset'.format(self.command, self.shortpath or self.path, self.client_address[0]))
                return
            elif new_url.lower() == 'adblock':
                self.logger.info('{} {} {} adblock'.format(self.command, self.shortpath or self.path, self.client_address[0]))
                return self.write(msg=FAKEGIF, ctype='image/gif')
            elif all(u in self.conf.parentlist.dict.keys() for u in new_url.split()):
                self._proxylist = [self.conf.parentlist.get(u) for u in new_url.split()]
                # random.shuffle(self._proxylist)
            else:
                self.logger.info('redirect {} {}'.format(self.shortpath or self.path, new_url))
                return self.redirect(new_url)

        parse = urlparse.urlparse(self.path)

        # gather info
        if 'Host' not in self.headers:
            self.logger.warning('"Host" not in self.headers')
            self.requesthost = parse_hostport(parse.netloc, 80)
        else:
            if not self.headers['Host'].startswith(parse_hostport(parse.netloc, 80)[0]):
                self.logger.warning('Host and URI mismatch! %s %s' % (self.path, self.headers['Host']))
                # self.headers['Host'] = parse.netloc
            self.requesthost = parse_hostport(self.headers['Host'], 80)

        # self.shortpath = '%s://%s%s%s%s' % (parse.scheme, parse.netloc, parse.path.split(':')[0], '?' if parse.query else '', ':' if ':' in parse.path else '')
        self.rip = self.conf.resolver.get_ip_address(self.requesthost[0])

        if self.rip.is_loopback:
            if ip_address(self.client_address[0]).is_loopback:
                if self.requesthost[1] in range(self.conf.listen[1], self.conf.listen[1] + self.conf.profile_num):
                    return self.api(parse)
            else:
                return self.send_error(403)

        if str(self.rip) == self.connection.getsockname()[0]:
            if self.requesthost[1] in range(self.conf.listen[1], self.conf.listen[1] + len(self.conf.userconf.dget('fgfwproxy', 'profile', '134'))):
                if self.conf.userconf.dgetbool('fgfwproxy', 'remoteapi', False):
                    return self.api(parse)
                return self.send_error(403)

        if self.conf.xheaders:
            iplst = [client_ip.strip() for client_ip in self.headers.get('X-Forwarded-For', '').split(',') if client_ip.strip()]
            if not ip_address(self.client_address[0]).is_loopback:
                iplst.append(self.client_address[0])
            self.headers['X-Forwarded-For'] = ', '.join(iplst)

        if self.noxff and 'X-Forwarded-For' in self.headers:
            del self.headers['X-Forwarded-For']

        for h in ['Proxy-Connection', 'Proxy-Authenticate']:
            if h in self.headers:
                del self.headers[h]

        self._do_GET()

    def _do_GET(self, retry=False):
        try:
            if retry:
                if self.remotesoc:
                    try:
                        self.remotesoc.close()
                    except Exception:
                        pass
                    self.remotesoc = None
                self.failed_parents.append(self.ppname)
                self.count += 1
                if self.count > 10:
                    self.logger.error('for some strange reason retry time exceeded 10, pls check!')
                    return
            if not self.retryable:
                self.close_connection = 1
                self.conf.GET_PROXY.notify(self.command, self.shortpath, self.requesthost, False, self.failed_parents, self.ppname)
                return
            if self.getparent():
                self.conf.GET_PROXY.notify(self.command, self.shortpath, self.requesthost, False, self.failed_parents, self.ppname)
                return self.send_error(504)

            iplist = None
            if self.pproxy.name == 'direct' and self.requesthost[0] in self.conf.HOSTS and not self.failed_parents:
                iplist = self.conf.HOSTS.get(self.requesthost[0])
                self._proxylist.insert(0, self.pproxy)
            self.set_timeout()
            self.remotesoc = self._http_connect_via_proxy(self.requesthost, iplist)
            if hasattr(self.remotesoc, 'name'):
                self.ppname = self.remotesoc.name
            self.remotesoc.settimeout(self.rtimeout)
            self.wbuffer = []
            self.wbuffer_size = 0
            # prep request header
            s = []
            if self.pproxy.proxy.startswith('http'):
                s.append('%s %s %s\r\n' % (self.command, self.path, self.request_version))
                if self.pproxy.username:
                    a = '%s:%s' % (self.pproxy.username, self.pproxy.password)
                    s.append('Proxy-Authorization: Basic %s' % base64.b64encode(a.encode()))
            else:
                s.append('%s /%s %s\r\n' % (self.command, '/'.join(self.path.split('/')[3:]), self.request_version))
            # Does the client want to close connection after this request?
            conntype = self.headers.get('Connection', "")
            if self.request_version >= "HTTP/1.1":
                self.close_connection = 'close' in conntype.lower()
            else:
                self.close_connection = 'keep_alive' in conntype.lower()
            if 'Upgrade' in self.headers:
                self.close_connection = True
                self.logger.warning('Upgrade header found! (%s)' % self.headers['Upgrade'])
                # del self.headers['Upgrade']
            else:
                # always try to keep connection alive
                self.headers['Connection'] = 'keep_alive'

            for k, v in self.headers.items():
                if isinstance(v, bytes):
                    v = v.decode('latin1')
                s.append("%s: %s\r\n" % ("-".join([w.capitalize() for w in k.split("-")]), v))
            s.append("\r\n")
            data = ''.join(s).encode('latin1')
            # send request header
            self.remotesoc.sendall(data)
            self.traffic_count[0] += len(data)
            remoterfile = self.remotesoc.makefile('rb', 0)
            # Expect
            skip = False
            if 'Expect' in self.headers:
                try:
                    response_line, protocol_version, response_status, response_reason = read_response_line(remoterfile)
                except Exception as e:
                    # TODO: probably the server don't handle Expect well.
                    self.logger.warning('read response line error: %r' % e)
                else:
                    if response_status == 100:
                        hdata = read_header_data(remoterfile)
                        self._wfile_write(response_line + hdata)
                    else:
                        skip = True
            # send request body
            if not skip:
                content_length = int(self.headers.get('Content-Length', 0))
                if self.headers.get("Transfer-Encoding") and self.headers.get("Transfer-Encoding") != "identity":
                    if self.rbuffer:
                        self.remotesoc.sendall(b''.join(self.rbuffer))
                    flag = 1
                    req_body_len = 0
                    while flag:
                        trunk_lenth = self.rfile_readline()
                        if self.retryable:
                            self.rbuffer.append(trunk_lenth)
                            req_body_len += len(trunk_lenth)
                        self.remotesoc.sendall(trunk_lenth)
                        trunk_lenth = int(trunk_lenth.strip(), 16) + 2
                        flag = trunk_lenth != 2
                        data = self.rfile_read(trunk_lenth)
                        if self.retryable:
                            self.rbuffer.append(data)
                            req_body_len += len(data)
                        self.remotesoc.sendall(data)
                        if req_body_len > 102400:
                            self.retryable = False
                            self.rbuffer = []
                elif content_length > 0:
                    if content_length > 102400:
                        self.retryable = False
                    if self.rbuffer:
                        s = b''.join(self.rbuffer)
                        content_length -= len(s)
                        self.remotesoc.sendall(s)
                    while content_length:
                        data = self.rfile_read(min(self.bufsize, content_length))
                        if not data:
                            break
                        content_length -= len(data)
                        if self.retryable:
                            self.rbuffer.append(data)
                        self.remotesoc.sendall(data)
                # read response line
                timelog = time.clock()
                response_line, protocol_version, response_status, response_reason = read_response_line(remoterfile)
                rtime = time.clock() - timelog
            # read response headers
            while response_status == 100:
                hdata = read_header_data(remoterfile)
                self._wfile_write(response_line + hdata)
                response_line, protocol_version, response_status, response_reason = read_response_line(remoterfile)
            header_data, response_header = read_headers(remoterfile)
            # check response headers
            conntype = response_header.get('Connection', "")
            if protocol_version >= b"HTTP/1.1":
                remote_close = 'close' in conntype.lower()
            else:
                remote_close = 'keep_alive' in conntype.lower()
            if 'Upgrade' in response_header:
                self.close_connection = remote_close = True
            if "Content-Length" in response_header:
                if "," in response_header["Content-Length"]:
                    # Proxies sometimes cause Content-Length headers to get
                    # duplicated.  If all the values are identical then we can
                    # use them but if they differ it's an error.
                    pieces = re.split(r',\s*', response_header["Content-Length"])
                    if any(i != pieces[0] for i in pieces):
                        raise ValueError("Multiple unequal Content-Lengths: %r" %
                                         response_header["Content-Length"])
                    response_header["Content-Length"] = pieces[0]
                content_length = int(response_header["Content-Length"])
            else:
                content_length = None

            if response_status in (301, 302) and self.conf.GET_PROXY.bad302(response_header.get('Location')):
                raise IOError(0, 'Bad 302!')

            self.wfile_write(response_line)
            self.wfile_write(header_data)
            # read response body
            if self.command == 'HEAD' or response_status in (204, 205, 304):
                pass
            elif response_header.get("Transfer-Encoding") and response_header.get("Transfer-Encoding") != "identity":
                flag = 1
                while flag:
                    trunk_lenth = remoterfile.readline()
                    self.wfile_write(trunk_lenth)
                    trunk_lenth = int(trunk_lenth.strip(), 16) + 2
                    flag = trunk_lenth != 2
                    while trunk_lenth:
                        data = self.remotesoc.recv(min(self.bufsize, trunk_lenth))
                        # self.logger.info('chunk data received %d %s' % (len(data), self.path))
                        trunk_lenth -= len(data)
                        self.wfile_write(data)
            elif content_length is not None:
                while content_length:
                    data = self.remotesoc.recv(min(self.bufsize, content_length))
                    if not data:
                        raise IOError(0, 'remote socket closed')
                    # self.logger.info('content_length data received %d %s' % (len(data), self.path))
                    content_length -= len(data)
                    self.wfile_write(data)
            else:
                # websocket?
                self.close_connection = 1
                self.retryable = False
                self.wfile_write()
                fd = [self.connection, self.remotesoc]
                while fd:
                    ins, _, _ = select.select(fd, [], [], 60)
                    if not ins:
                        break
                    if self.connection in ins:
                        data = self.connection_recv(self.bufsize)
                        if data:
                            self.remotesoc.sendall(data)
                        else:
                            fd.remove(self.connection)
                            self.remotesoc.shutdown(socket.SHUT_WR)
                    if self.remotesoc in ins:
                        data = self.remotesoc.recv(self.bufsize)
                        if data:
                            # self.logger.info('ws data received %d %s' % (len(data), self.path))
                            self._wfile_write(data)
                        else:
                            fd.remove(self.remotesoc)
                            self.connection.shutdown(socket.SHUT_WR)
            self.wfile_write()
            self.conf.GET_PROXY.notify(self.command, self.shortpath, self.requesthost, True if response_status < 400 else False, self.failed_parents, self.ppname, rtime)
            self.pproxy.log(self.requesthost[0], rtime)
            if remote_close or is_connection_dropped([self.remotesoc]):
                try:
                    if hasattr(self.remotesoc, 'pooled'):
                        if not self.remotesoc.pooled:
                            self.remotesoc.close()
                    else:
                        self.remotesoc.close()
                except Exception:
                    pass
            else:
                self.HTTPCONN_POOL.put((self.client_address, self.requesthost), self.remotesoc, self.ppname if '(pooled)' in self.ppname else (self.ppname + '(pooled)'))
            self.remotesoc = None
            if self.close_connection:
                self.connection.close()
        except ClientError as e:
            raise
        except NetWorkIOError as e:
            return self.on_GET_Error(e)

    def on_GET_Error(self, e):
        if self.ppname:
            self.logger.warning('{} {} via {} failed: {}'.format(self.command, self.shortpath, self.ppname, repr(e)))
            self.pproxy.log(self.requesthost[0], 10)
            return self._do_GET(True)
        self.conf.GET_PROXY.notify(self.command, self.shortpath, self.requesthost, False, self.failed_parents, self.ppname)
        return self.send_error(504)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_TRACE = do_GET

    def do_CONNECT(self):
        self.close_connection = 1
        if isinstance(self.path, bytes):
            self.path = self.path.decode('latin1')

        self.wfile.write(self.protocol_version.encode() + b" 200 Connection established\r\n\r\n")

        data = self.connection_recv(4)

        if self.path.endswith(':80') and data in (b'GET ', b'POST'):
            # it's a http request, start parsing
            request_line = data + self.rfile.readline()
            self.requestline = request_line.rstrip(b'\r\n')
            words = self.requestline.split()
            if len(words) == 3:
                command, path, version = words
                if version[:5] != 'HTTP/':
                    return
                try:
                    base_version_number = version.split('/', 1)[1]
                    version_number = base_version_number.split(".")
                    # RFC 2145 section 3.1 says there can be only one "." and
                    #   - major and minor numbers MUST be treated as
                    #      separate integers;
                    #   - HTTP/2.4 is a lower version than HTTP/2.13, which in
                    #      turn is lower than HTTP/12.3;
                    #   - Leading zeros MUST be ignored by recipients.
                    if len(version_number) != 2:
                        raise ValueError
                    version_number = int(version_number[0]), int(version_number[1])
                except (ValueError, IndexError):
                    return
                if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                    self.close_connection = 0
            else:
                return
            self.command, self.path, self.request_version = command, path, version

            # get headers
            header_data = []
            while True:
                line = self.rfile_readline()
                header_data.append(line)
                if line in (b'\r\n', b'\n', b'\r'):  # header ends with a empty line
                    break
                if not line:
                    raise IOError(0, 'remote socket closed')
            self.header_data = b''.join(header_data)
            self.headers = parse_headers(self.header_data)

            conntype = self.headers.get('Connection', "")
            if conntype.lower() == 'close':
                self.close_connection = 1
            elif (conntype.lower() == 'keep-alive' and
                  self.protocol_version >= "HTTP/1.1"):
                self.close_connection = 0
            return self.do_GET()

        elif data.startswith(b'\x16\x03'):
            # parse SNI
            data = data + self.connection_recv(8196)
            try:
                server_name = extract_server_name(data)
                self.logger.debug('sni: %s' % server_name)
                self.logger.debug('path: %s' % self.path)
                if server_name and server_name not in self.path:
                    host, _, port = self.path.partition(':')
                    self.path = '%s:%s' % (server_name, port)
            except Exception:
                pass

        self.requesthost = parse_hostport(self.path)

        self.rbuffer.append(data)

        # redirector
        new_url = self.conf.GET_PROXY.redirect(self)
        if new_url:
            self.logger.debug('redirect %s, %s %s' % (new_url, self.command, self.path))
            if new_url.isdigit() and 400 <= int(new_url) < 600:
                self.logger.info('{} {} {} send error {}'.format(self.command, self.shortpath or self.path, self.client_address[0], new_url))
                return
            elif new_url.lower() in ('reset', 'adblock', 'return'):
                self.logger.info('{} {} {} reset'.format(self.command, self.shortpath or self.path, self.client_address[0]))
                return
            elif all(u in self.conf.parentlist.dict.keys() for u in new_url.split()):
                self._proxylist = [self.conf.parentlist.get(u) for u in new_url.split()]
                # random.shuffle(self._proxylist)

        self.rip = self.conf.resolver.get_ip_address(self.requesthost[0])

        if self.rip.is_loopback:
            if ip_address(self.client_address[0]).is_loopback:
                if self.requesthost[1] in range(self.conf.listen[1], self.conf.listen[1] + self.conf.profile_num):
                    # prevent loop
                    return
            else:
                return
        self._do_CONNECT()

    def _do_CONNECT(self, retry=False):
        self.logger.debug('_do_CONNECT: %d' % self.connection_port)
        if retry:
            self.failed_parents.append(self.ppname)
            self.pproxy.log(self.requesthost[0], 10)
        if self.remotesoc:
            self.remotesoc.close()
        if not self.retryable or self.getparent():
            self.conf.GET_PROXY.notify(self.command, self.path, self.path, False, self.failed_parents, self.ppname)
            return
        iplist = None
        if self.pproxy.name == 'direct' and self.requesthost[0] in self.conf.HOSTS and not self.failed_parents:
            iplist = self.conf.HOSTS.get(self.requesthost[0])
            self._proxylist.insert(0, self.pproxy)
        self.set_timeout()
        try:
            self.remotesoc = self._connect_via_proxy(self.requesthost, iplist, tunnel=True)
            # self.remotesoc.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if hasattr(self.remotesoc, 'name'):
                self.ppname = self.remotesoc.name
        except NetWorkIOError as e:
            self.logger.warning('%s %s via %s failed on connect! %r' % (self.command, self.path, self.ppname, e))
            return self._do_CONNECT(True)
        self.logger.debug('%s connected' % self.path)
        count = 0
        if self.rbuffer:
            self.logger.debug('write rbuffer')
            self.remotesoc.sendall(b''.join(self.rbuffer))
            count = 1
            timelog = time.clock()
        rtime = 0
        fds = [self.connection, self.remotesoc]
        while self.retryable:
            try:
                reason = ''
                (ins, _, _) = select.select(fds, [], [], self.conf.timeout * 2)
                if not ins:
                    self.logger.debug('timeout, break, stage 0: %d' % self.connection_port)
                    reason = 'timeout'
                    break
                if self.connection in ins:
                    self.logger.debug('data from client, stage 0: %d' % self.connection_port)
                    data = self.connection_recv(self.bufsize)
                    if not data:
                        self.logger.debug('client closed, stage 0: %d' % self.connection_port)
                        reason = 'client closed'
                        self.remotesoc.shutdown(socket.SHUT_WR)
                        fds.remove(self.connection)
                        break
                    self.remotesoc.sendall(data)
                    # Now remotesoc is connected, set read timeout
                    self.remotesoc.settimeout(self.rtimeout)
                    count += 1
                    timelog = time.clock()
                    if self.retryable:
                        self.rbuffer.append(data)
                if self.remotesoc in ins:
                    self.logger.debug('data from remote, stage 0: %d' % self.connection_port)
                    data = self.remotesoc.recv(self.bufsize)
                    if not data:  # remote connection closed
                        # gonna retry, do not close connection
                        self.logger.debug('remote closed, stage 0: %d' % self.connection_port)
                        reason = 'remote closed'
                        fds.remove(self.remotesoc)
                        break
                    rtime = time.clock() - timelog
                    self._wfile_write(data)
            except NetWorkIOError as e:
                self.logger.warning('do_CONNECT error: %r on %s %s, stage 0: %d' % (e, reason, count, self.connection_port))
                break
        if self.retryable:
            reason = reason or "don't know why"
            if reason != 'client closed':
                self.logger.warning('%s %s via %s failed! %s. retry... %d' % (self.command, self.path, self.ppname, reason, self.connection_port))
                return self._do_CONNECT(True)
            else:
                self.logger.warning('%s %s via %s failed! %s %d' % (self.command, self.path, self.ppname, reason, self.connection_port))
                self.conf.GET_PROXY.notify(self.command, self.path, self.requesthost, True, self.failed_parents, self.ppname, rtime)
                return
        # not retryable, clear rbuffer
        self.rbuffer = []
        self.conf.GET_PROXY.notify(self.command, self.path, self.requesthost, True, self.failed_parents, self.ppname, rtime)
        self.pproxy.log(self.requesthost[0], rtime)
        self.logger.debug('%s response time %.3fs' % (self.requesthost[0], rtime))
        self.logger.debug('start forwarding... %d' % len(fds))
        """forward socket"""
        try:
            while fds:
                ins, _, _ = select.select(fds, [], [], 60)
                if not ins:
                    self.logger.debug('tcp forwarding timed out: %d' % self.connection.getpeername()[1])
                    break
                if self.connection in ins:
                    data = self.connection_recv(self.bufsize)
                    if data:
                        self.logger.debug('read from client %d, %d' % (len(data), self.connection_port))
                        self.remotesoc.sendall(data)
                    else:
                        self.logger.debug('client closed: %d' % self.connection_port)
                        fds.remove(self.connection)
                        try:
                            self.remotesoc.shutdown(socket.SHUT_WR)
                        except NetWorkIOError:
                            pass
                if self.remotesoc in ins:
                    try:
                        data = self.remotesoc.recv(self.bufsize)
                    except NetWorkIOError:
                        data = b''
                    if data:
                        self.logger.debug('read from remote %d, %d' % (len(data), self.connection_port))
                        self._wfile_write(data)
                    else:
                        self.logger.debug('remote closed: %d' % self.connection_port)
                        fds.remove(self.remotesoc)
                        try:
                            self.connection.shutdown(socket.SHUT_WR)
                        except NetWorkIOError:
                            pass
            self.logger.debug('forward completed successfully: %d' % self.connection_port)
        except socket.timeout:
            self.logger.debug('socket.timeout error: %d' % self.connection_port)
            pass
        except ClientError:
            pass
        except NetWorkIOError as e:
            self.logger.info('NetWorkIOError, code %r, %d' % (e.args[0], self.connection_port))
            self.logger.info(traceback.format_exc())
            pass
        finally:
            if hasattr(self.remotesoc, 'pooled') and not self.remotesoc.pooled:
                try:
                    self.remotesoc.close()
                except NetWorkIOError:
                    pass
                self.remotesoc = None

    def on_conn_log(self):
        self.logmethod('{} {} via {}. {}'.format(self.command, self.shortpath or self.path, self.ppname, self.client_address[1]))

    def wfile_write(self, data=None):
        if data is None:
            self.retryable = False
        if self.retryable and data:
            self.wbuffer.append(data)
            self.wbuffer_size += len(data)
            if self.wbuffer_size > 102400:
                self.retryable = False
                self.remotesoc.settimeout(10)
        else:
            if self.wbuffer:
                self._wfile_write(b''.join(self.wbuffer))
                self.wbuffer = []
            if data:
                self._wfile_write(data)

    def set_timeout(self):
        if self._proxylist:
            if self.ppname == 'direct':
                self.rtimeout = self.conf.timeout
                self.ctimeout = self.conf.timeout
            else:
                self.rtimeout = min(2 ** len(self.failed_parents) + self.conf.timeout - 1, 10)
                self.ctimeout = min(2 ** len(self.failed_parents) + self.conf.timeout - 1, 10)
        else:
            self.ctimeout = self.rtimeout = 10

    def _http_connect_via_proxy(self, netloc, iplist):
        if not self.failed_parents:
            result = self.HTTPCONN_POOL.get((self.client_address, self.requesthost))
            if result:
                self._proxylist.insert(0, self.conf.parentlist.get(self.ppname))
                sock, self.ppname = result
                self.on_conn_log()
                return sock
        return self._connect_via_proxy(netloc, iplist)

    def _connect_via_proxy(self, netloc, iplist=None, tunnel=False):
        self.on_conn_log()
        return create_connection(netloc, ctimeout=self.ctimeout, iplist=iplist, parentproxy=self.pproxy, tunnel=tunnel)

    def api(self, parse):
        '''
        path: supported command
        /api/localrule: GET POST DELETE
        '''
        self.logger.debug('{} {}'.format(self.command, self.path))
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 102400:
            return
        body = StringIO()
        while content_length:
            data = self.rfile_read(min(self.bufsize, content_length))
            if not data:
                return
            content_length -= len(data)
            body.write(data)
        body = body.getvalue()
        if parse.path == '/api/localrule' and self.command == 'GET':
            data = json.dumps([(rule, self.conf.GET_PROXY.local.expire[rule]) for rule in self.conf.GET_PROXY.local.rules])
            return self.write(200, data, 'application/json')
        elif parse.path == '/api/localrule' and self.command == 'POST':
            'accept a json encoded tuple: (str rule, int exp)'
            rule, exp = json.loads(body)
            result = self.conf.GET_PROXY.add_temp(rule, exp)
            self.write(400 if result else 201, result, 'application/json')
            return self.conf.stdout()
        elif parse.path.startswith('/api/localrule/') and self.command == 'DELETE':
            try:
                rule = base64.urlsafe_b64decode(parse.path[15:].encode('latin1')).decode()
                expire = self.conf.GET_PROXY.local.remove(rule)
                self.write(200, json.dumps([rule, expire]), 'application/json')
                return self.conf.stdout()
            except Exception as e:
                self.logger.error(traceback.format_exc())
                return self.send_error(404, repr(e))
        elif parse.path == '/api/redirector' and self.command == 'GET':
            data = json.dumps([(index, rule[0].rule, rule[1]) for index, rule in enumerate(self.conf.REDIRECTOR.redirlst)])
            return self.write(200, data, 'application/json')
        elif parse.path == '/api/redirector' and self.command == 'POST':
            'accept a json encoded tuple: (str rule, str dest)'
            rule, dest = json.loads(body)
            self.conf.GET_PROXY.add_redirect(rule, dest)
            self.write(200, data, 'application/json')
            return self.conf.stdout()
        elif parse.path.startswith('/api/redirector/') and self.command == 'DELETE':
            try:
                rule = urlparse.parse_qs(parse.query).get('rule', [''])[0]
                if rule:
                    assert base64.urlsafe_b64decode(rule).decode() == self.conf.REDIRECTOR.redirlst[int(parse.path[16:])][0].rule
                rule, dest = self.conf.REDIRECTOR.redirlst.pop(int(parse.path[16:]))
                self.write(200, json.dumps([int(parse.path[16:]), rule.rule, dest]), 'application/json')
                return self.conf.stdout()
            except Exception as e:
                return self.send_error(404, repr(e))
        elif parse.path == '/api/parent' and self.command == 'GET':
            data = [(p.name, ('%s://%s:%s' % (p.scheme, p.hostname, p.port)) if p.proxy else '', p.httppriority) for k, p in self.conf.parentlist.dict.items()]
            data = sorted(data, key=lambda item: item[0])
            data = json.dumps(sorted(data, key=lambda item: item[2]))
            return self.write(200, data, 'application/json')
        elif parse.path == '/api/parent' and self.command == 'POST':
            'accept a json encoded tuple: (str rule, str dest)'
            name, proxy = json.loads(body)
            if proxy.startswith('ss://') and self.conf.userconf.has_option('parents', 'shadowsocks_0'):
                self.conf.userconf.remove_option('parents', 'shadowsocks_0')
            self.conf.parentlist.remove('shadowsocks_0')
            self.conf.addparentproxy(name, proxy)
            self.conf.userconf.set('parents', name, proxy)
            self.conf.confsave()
            self.write(200, data, 'application/json')
            return self.conf.stdout()
        elif parse.path.startswith('/api/parent/') and self.command == 'DELETE':
            try:
                self.conf.parentlist.remove(parse.path[12:])
                if self.conf.userconf.has_option('parents', parse.path[12:]):
                    self.conf.userconf.remove_option('parents', parse.path[12:])
                    self.conf.confsave()
                self.write(200, parse.path[12:], 'application/json')
                return self.conf.stdout()
            except Exception as e:
                return self.send_error(404, repr(e))
        elif parse.path == '/api/gfwlist' and self.command == 'GET':
            return self.write(200, json.dumps(self.conf.userconf.dgetbool('fgfwproxy', 'gfwlist', True)), 'application/json')
        elif parse.path == '/api/gfwlist' and self.command == 'POST':
            self.conf.userconf.set('fgfwproxy', 'gfwlist', '1' if json.loads(body) else '0')
            self.conf.confsave()
            self.write(200, data, 'application/json')
            return self.conf.stdout()
        elif parse.path == '/api/autoupdate' and self.command == 'GET':
            return self.write(200, json.dumps(self.conf.userconf.dgetbool('FGFW_Lite', 'autoupdate', True)), 'application/json')
        elif parse.path == '/api/autoupdate' and self.command == 'POST':
            self.conf.userconf.set('FGFW_Lite', 'autoupdate', '1' if json.loads(body) else '0')
            self.conf.confsave()
            self.write(200, data, 'application/json')
            return self.conf.stdout()
        elif parse.path == '/api/remotedns' and self.command == 'POST':
            'accept a json encoded tuple: (str host, str server)'
            try:
                host, server = json.loads(body)
                server = [parse_hostport(server.encode(), 53)]
                port = self.conf.listen[1]
                proxy = ParentProxy('foo', 'http://127.0.0.1:%d' % port)
                resolver = TCP_Resolver(server, proxy)
                result = resolver.resolve(host)
                result = [r[1] for r in result]
                self.write(200, json.dumps(result), 'application/json')
            except Exception:
                result = traceback.format_exc()
                self.write(200, json.dumps(result.split()), 'application/json')
        elif parse.path == '/' and self.command == 'GET':
            return self.write(200, 'Hello World !', 'text/html')
        self.send_error(404)


def updater(conf):
    time.sleep(10)

    logger = logging.getLogger('updater')
    logger.setLevel(logging.INFO)
    hdr = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(name)s:%(levelname)s %(message)s',
                                  datefmt='%H:%M:%S')
    hdr.setFormatter(formatter)
    logger.addHandler(hdr)
    while 1:
        lastupdate = conf.version.dgetfloat('Update', 'LastUpdate', 0)

        if time.time() - lastupdate > conf.UPDATE_INTV * 60 * 60:
            try:
                update(conf, logger, auto=True)
            except Exception:
                logger.error(traceback.format_exc())
        time.sleep(3600 + random.randint(0, 600))


def update(conf, logger, auto=False):
    if auto and not conf.userconf.dgetbool('FGFW_Lite', 'autoupdate'):
        return
    gfwlist_url = conf.userconf.dget('fgfwproxy', 'gfwlist_url', 'https://raw.githubusercontent.com/v3aqb/gfwlist/master/gfwlist.txt')
    if 'googlecode' in gfwlist_url:
        conf.userconf.set('fgfwproxy', 'gfwlist_url', 'https://raw.githubusercontent.com/v3aqb/gfwlist/master/gfwlist.txt')
        conf.confsave()

    filelist = [(gfwlist_url, './fgfw-lite/gfwlist.txt'), ]

    adblock_url = conf.userconf.dget('fgfwproxy', 'adblock_url', '')
    if adblock_url:
        filelist.append((adblock_url, './fgfw-lite/adblock.txt'))

    for url, path in filelist:
        etag = conf.version.dget('Update', path.replace('./', '').replace('/', '-'), '')
        req = urllib2.Request(url)
        if etag:
            req.add_header('If-None-Match', etag)
        try:
            r = urllib2.urlopen(req)
        except Exception as e:
            if isinstance(e, urllib2.HTTPError):
                logger.info('%s NOT updated: %s' % (path, e.reason))
            else:
                logger.info('%s NOT updated: %r' % (path, e))
        else:
            data = r.read()
            if r.getcode() == 200 and data:
                with open(path, 'wb') as localfile:
                    localfile.write(data)
                etag = r.info().getheader('ETag')
                if etag:
                    conf.version.set('Update', path.replace('./', '').replace('/', '-'), etag)
                    conf.confsave()
                logger.info('%s Updated.' % path)
            else:
                logger.info('{} NOT updated: {}'.format(path, str(r.getcode())))
    branch = conf.userconf.dget('FGFW_Lite', 'branch', 'master')
    count = 0
    try:
        r = json.loads(urllib2.urlopen('https://github.com/v3aqb/fwlite/raw/%s/fgfw-lite/update.json' % branch).read())
    except Exception as e:
        logger.info('read update.json failed: %r' % e)
    else:
        import hashlib
        update = {}
        success = 1
        for path, v, in r.items():
            try:
                if v == conf.version.dget('Update', path.replace('./', '').replace('/', '-'), ''):
                    logger.debug('{} Not Modified'.format(path))
                    continue
                logger.info('Update: Downloading %s...' % path)
                fdata = urllib2.urlopen('https://github.com/v3aqb/fwlite/raw/%s%s' % (branch, path[1:])).read()
                h = hashlib.new("sha256", fdata).hexdigest()
                if h != v:
                    logger.warning('%s NOT updated: hash mismatch. %s %s' % (path, h, v))
                    success = 0
                    break
                update[path] = (fdata, h)
                logger.info('%s Downloaded.' % path)
            except Exception as e:
                success = 0
                logger.error('update failed: %r\n%s' % (e, traceback.format_exc()))
                break
        if success:
            for path, v in update.items():
                try:
                    fdata, h = v
                    if not os.path.isdir(os.path.dirname(path)):
                        os.mkdir(os.path.dirname(path))
                    with open(path, 'wb') as localfile:
                        localfile.write(fdata)
                    logger.info('%s Updated.' % path)
                    conf.version.set('Update', path.replace('./', '').replace('/', '-'), h)
                    if not path.endswith(('txt', 'ini')):
                        count += 1
                except Exception:
                    sys.stderr.write(traceback.format_exc() + '\n')
                    sys.stderr.flush()
        else:
            logger.error('update failed!')
        conf.version.set('Update', 'LastUpdate', str(time.time()))
    conf.confsave()
    if not conf.GUI:
        for item in subprocess_handler.ITEMS:
            item.restart()
    conf.GET_PROXY.config()
    if count:
        logger.info('Update Completed, %d file Updated.' % count)
    if conf.userconf.dget('FGFW_Lite', 'updatecmd', ''):
        subprocess.Popen(shlex.split(conf.userconf.dget('FGFW_Lite', 'updatecmd', '')))


class subprocess_handler(object):
    """docstring for subprocess_handler"""
    ITEMS = []
    logger = logging.getLogger('subprocess_handler')
    logger.setLevel(logging.INFO)
    hdr = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(name)s:%(levelname)s %(message)s',
                                  datefmt='%H:%M:%S')
    hdr.setFormatter(formatter)
    logger.addHandler(hdr)

    def __init__(self):
        subprocess_handler.ITEMS.append(self)
        self.subpobj = None
        self.cmd = ''
        self.cwd = ''
        self.pid = None
        self.filelist = []
        self.enable = True
        self.start()

    def config(self):
        pass

    def start(self):
        try:
            self.config()
            if self.enable:
                self.logger.info('starting %s' % self.cmd)
                self.subpobj = subprocess.Popen(shlex.split(self.cmd), cwd=self.cwd, stdin=subprocess.PIPE)
                self.pid = self.subpobj.pid
        except Exception:
            sys.stderr.write(traceback.format_exc() + '\n')
            sys.stderr.flush()

    def restart(self):
        self.stop()
        self.start()

    def stop(self):
        try:
            self.subpobj.terminate()
        except Exception:
            pass
        finally:
            self.subpobj = None


@atexit.register
def atexit_do():
    for item in subprocess_handler.ITEMS:
        item.stop()


def main():
    if gevent:
        s = 'FWLite %s with gevent %s' % (__version__, gevent.__version__)
    else:
        s = 'FWLite %s without gevent' % __version__
    conf = config.conf
    logger = logging.getLogger('FW_Lite')
    logger.setLevel(logging.INFO)
    hdr = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(name)s:%(levelname)s %(message)s',
                                  datefmt='%H:%M:%S')
    hdr.setFormatter(formatter)
    logger.addHandler(hdr)

    logger.info(s)

    d = {'http': '127.0.0.1:%d' % conf.listen[1], 'https': '127.0.0.1:%d' % conf.listen[1]}
    urllib2.install_opener(urllib2.build_opener(urllib2.ProxyHandler(d)))
    for i, level in enumerate(list(conf.userconf.dget('fgfwproxy', 'profile', '13'))):
        server = ThreadingHTTPServer((conf.listen[0], conf.listen[1] + i), ProxyHandler, conf=conf, level=int(level))
        t = Thread(target=server.serve_forever)
        t.start()

    for _, val in conf.userconf.items('port_forward'):
        proxy, local, remote = re.match(r'(\S+) (\S+) (\S+)', val).groups()
        if local.isdigit():
            local = '127.0.0.1:' + local
        if remote.isdigit():
            remote = '127.0.0.1:' + remote
        local = parse_hostport(local)
        remote = parse_hostport(remote)
        from tcp_tunnel import tcp_tunnel
        server = tcp_tunnel(proxy, remote, local)
        t = Thread(target=server.serve_forever)
        t.start()

    t = Thread(target=updater, args=(conf, ))
    t.daemon = True
    t.start()

    time.sleep(3)
    conf.stdout()
    t.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

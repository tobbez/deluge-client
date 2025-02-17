import logging
import socket
import ssl
import struct
import warnings
import zlib
import io
import os
import platform
from functools import wraps
from threading import local as thread_local
from .rencode import dumps, loads


DEFAULT_LINUX_CONFIG_DIR_PATH = '~/.config/deluge'
RPC_RESPONSE = 1
RPC_ERROR = 2
RPC_EVENT = 3

MESSAGE_HEADER_SIZE = 5
READ_SIZE = 10

logger = logging.getLogger(__name__)


class DelugeClientException(Exception):
    """Base exception for all deluge client exceptions"""


class ConnectionLostException(DelugeClientException):
    pass


class CallTimeoutException(DelugeClientException):
    pass


class InvalidHeaderException(DelugeClientException):
    pass


class FailedToReconnectException(DelugeClientException):
    pass


class RemoteException(DelugeClientException):
    pass


class DelugeRPCClient(object):
    timeout = 20

    def __init__(self, host, port, username, password, decode_utf8=False, automatic_reconnect=True):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

        self.deluge_protocol_version = None

        self.decode_utf8 = decode_utf8
        if not self.decode_utf8:
            warnings.warn('Using `decode_utf8=False` is deprecated, please set it to True.'
                          'The argument will be removed in a future release where it will be always True', DeprecationWarning)

        self.automatic_reconnect = automatic_reconnect

        self.request_id = 1
        self.connected = False

        # Insecure context without remote certificate verification
        self._ssl_context = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

        self._create_socket()

    def _create_socket(self, ssl_version=None):
        if ssl_version is not None:
            self._socket = self._ssl_context.wrap_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ssl_version=ssl_version)
        else:
            self._socket = self._ssl_context.wrap_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        self._socket.settimeout(self.timeout)

    def connect(self):
        """
        Connects to the Deluge instance
        """
        self._connect()
        logger.debug('Connected to Deluge, detecting daemon version')
        self._detect_deluge_version()
        logger.debug('Daemon version >= 2.0.0 detected, logging in')
        result = self.call('daemon.login', self.username, self.password, client_version='deluge-client')
        logger.debug('Logged in with value %r' % result)
        self.connected = True

    def _connect(self):
        logger.info('Connecting to %s:%s' % (self.host, self.port))
        try:
            self._socket.connect((self.host, self.port))
        except ssl.SSLError as e:
            # Note: have not verified that we actually get errno 258 for this error
            if (hasattr(ssl, 'PROTOCOL_SSLv3') and
                    (getattr(e, 'reason', None) == 'UNSUPPORTED_PROTOCOL' or e.errno == 258)):
                logger.warning('Was unable to ssl handshake, trying to force SSLv3 (insecure)')
                self._create_socket(ssl_version=ssl.PROTOCOL_SSLv3)
                self._socket.connect((self.host, self.port))
            else:
                raise

    def disconnect(self):
        """
        Disconnect from deluge
        """
        if self.connected:
            self._socket.close()
            self._socket = None
            self.connected = False

    def _detect_deluge_version(self):
        # Only support deluge version 2, RPC protocol version 1
        self._send_call(1, 'daemon.info')
        try:
            result = self._socket.recv(1)
        except TimeoutError:
            raise Exception('Unsupported remote daemon version, or remote daemon stopped responding')
        if ord(result[:1]) == 1:
            self.deluge_protocol_version = 1
            # If we need the specific version of deluge 2, this is it.
            daemon_version = self._receive_response(1, partial_data=result)
        else:
            # Currently (deluge 2.0.x, 2.1.x), the deluge daemon silently
            # ignores calls with a protocol version different than the
            # server's.
            #
            # As long as that logic stays the same, and the client is
            # connecting to an actual deluge instance (and not some other
            # service), the following statement should not be reached.
            raise Exception(f'Received unsupported deluge protocol version {result!r}')

    def _send_call(self, protocol_version, method, *args, **kwargs):
        self.request_id += 1
        if method == 'daemon.login':
            debug_args = list(args)
            if len(debug_args) >= 2:
                debug_args[1] = '<password hidden>'
            logger.debug('Calling reqid %s method %r with args:%r kwargs:%r' % (self.request_id, method, debug_args, kwargs))
        else:
            logger.debug('Calling reqid %s method %r with args:%r kwargs:%r' % (self.request_id, method, args, kwargs))

        req = ((self.request_id, method, args, kwargs), )
        req = zlib.compress(dumps(req))

        self._socket.send(struct.pack('!BI', protocol_version, len(req)))
        self._socket.send(req)

    def _receive_response(self, protocol_version, partial_data=b''):
        expected_bytes = None
        data = partial_data
        while True:
            try:
                d = self._socket.recv(READ_SIZE)
            except ssl.SSLError:
                raise CallTimeoutException()

            if len(d) == 0:
                # With the socket in blocking mode, recv should only return
                # zero bytes if there was an error.
                #
                # Without this, the client would get stuck in an infinite loop
                # if the connection was lost after a request was sent but
                # before the response could be read.
                raise ConnectionLostException()

            data += d
            if expected_bytes is None:
                if len(data) < 5:
                    continue

                header = data[:MESSAGE_HEADER_SIZE]
                data = data[MESSAGE_HEADER_SIZE:]

                if ord(header[:1]) != protocol_version:
                    raise InvalidHeaderException(
                        'Expected protocol version ({}) as first byte in reply'.format(protocol_version)
                    )

                expected_bytes = struct.unpack('!I', header[1:])[0]

            if len(data) >= expected_bytes:
                data = zlib.decompress(data)
                break

        data = list(loads(data, decode_utf8=self.decode_utf8))
        msg_type = data.pop(0)
        request_id = data.pop(0)

        if msg_type == RPC_ERROR:
            exception_type, exception_msg, _, traceback = data
            # On deluge 2, exception arguments are sent as tuple
            if self.decode_utf8:
                exception_msg = ', '.join(exception_msg)
            else:
                exception_msg = b', '.join(exception_msg)

            if self.decode_utf8:
                exception = type(str(exception_type), (RemoteException, ), {})
                exception_msg = '%s\n%s' % (exception_msg,
                                            traceback)
            else:
                exception = type(str(exception_type.decode('utf-8', 'ignore')), (RemoteException, ), {})
                exception_msg = '%s\n%s' % (exception_msg.decode('utf-8', 'ignore'),
                                            traceback.decode('utf-8', 'ignore'))
            raise exception(exception_msg)
        elif msg_type == RPC_RESPONSE:
            retval = data[0]
            return retval

    def reconnect(self):
        """
        Reconnect
        """
        self.disconnect()
        self._create_socket()
        self.connect()

    def call(self, method, *args, **kwargs):
        """
        Calls an RPC function
        """
        tried_reconnect = False
        for _ in range(2):
            try:
                self._send_call(self.deluge_protocol_version, method, *args, **kwargs)
                return self._receive_response(self.deluge_protocol_version)
            except (socket.error, ConnectionLostException, CallTimeoutException):
                if self.automatic_reconnect:
                    if tried_reconnect:
                        raise FailedToReconnectException()
                    else:
                        try:
                            self.reconnect()
                        except (socket.error, ConnectionLostException, CallTimeoutException):
                            raise FailedToReconnectException()

                    tried_reconnect = True
                else:
                    raise

    def __getattr__(self, item):
        return RPCCaller(self.call, item)

    def __enter__(self):
        """Connect to client while using with statement."""
        self.connect()
        return self

    def __exit__(self, type, value, traceback):
        """Disconnect from client at end of with statement."""
        self.disconnect()


class RPCCaller(object):
    def __init__(self, caller, method=''):
        self.caller = caller
        self.method = method

    def __getattr__(self, item):
        return RPCCaller(self.caller, self.method+'.'+item)

    def __call__(self, *args, **kwargs):
        return self.caller(self.method, *args, **kwargs)


class LocalDelugeRPCClient(DelugeRPCClient):
    """Client with auto discovery for the default local credentials"""
    def __init__(
        self,
        host='127.0.0.1',
        port=58846,
        username='',
        password='',
        decode_utf8=True,
        automatic_reconnect=True
    ):
        if (
            host in ('localhost', '127.0.0.1', '::1') and
            not username and not password
        ):
            username, password = self._get_local_auth()

        super(LocalDelugeRPCClient, self).__init__(
            host, port, username, password, decode_utf8, automatic_reconnect
        )

    def _cache_thread_local(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not hasattr(wrapper.cache, 'result'):
                wrapper.cache.result = func(*args, **kwargs)
            return wrapper.cache.result

        wrapper.cache = thread_local()
        return wrapper

    @_cache_thread_local
    def _get_local_auth(self):
        auth_path = local_username = local_password = ''
        os_family = platform.system()

        if 'Windows' in os_family or 'CYGWIN' in os_family:
            app_data_path = os.environ.get('APPDATA')
            auth_path = os.path.join(app_data_path, 'deluge', 'auth')
        elif 'Linux' in os_family:
            config_path = os.path.expanduser(DEFAULT_LINUX_CONFIG_DIR_PATH)
            auth_path = os.path.join(config_path, 'auth')

        if os.path.exists(auth_path):
            with io.open(auth_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line or line.startswith('#'):
                        continue

                    auth_data = line.split(':')
                    if len(auth_data) < 2:
                        continue

                    username, password = auth_data[:2]
                    if username == 'localclient':
                        local_username, local_password = username, password
                        break

        return local_username, local_password

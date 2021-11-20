# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

""" Transport layer fuctionality using python sockets. """
from __future__ import print_function
from abc import ABC, ABCMeta, abstractmethod
import ssl
import socket
import time
import threading
from importlib import util
from typing import Dict, Tuple
from hyper import HTTP20Connection


from utils.logger import raw_network_logging as RAW_LOGGING
from engine.errors import TransportLayerException
from restler_settings import ConnectionSettings
from restler_settings import Settings
from engine.transport_layer.response import *
if util.find_spec("test_servers"):
    from test_servers.test_socket import TestSocket

DELIM = "\r\n\r\n"
UTF8 = 'utf-8'


class HttpSock(object):
    """
    Proxy to return the correct socket object.
    """
    def __init__(self, connection_settings: Dict) -> None:
        if Settings().use_http2:
            self._subject = Http2Sock(connection_settings)
        else:
            self._subject = HttpRawSock(connection_settings)

    def sendRecv(self, message: str, req_timeout_sec: int) -> Tuple[bool, str]:
        """ Sends a specified request to the server and waits for a response

        @param message: Message to be sent.
        @type message : Str
        @param req_timeout_sec: The time, in seconds, to wait for request to complete
        @type req_timeout_sec : Int

        @return:
            False if failure, True if success
            Response if True returned, Error if False returned
        @rtype : Tuple (Bool, String)

        """
        return self._subject.sendRecv(message, req_timeout_sec)


class BaseSocket(object, metaclass=ABCMeta):
    __last_request_sent_time = time.time()
    __req_sem = threading.Semaphore()

    @abstractmethod
    def __init__(self, connection_settings: Dict) -> None:
        self.connection_settings = connection_settings

        host = Settings().host
        self.target_ip = connection_settings.target_ip or self.host
        self.target_port = connection_settings.target_port or 433

        self.connection_settings = connection_settings

        self.ignore_decoding_failures = Settings().ignore_decoding_failures
        
    
    @abstractmethod
    def __del__(self):
        pass

    @abstractmethod
    def sendRecv(self, message: str, req_timeout_sec: int) -> Tuple[bool, str]:
        pass


    def _get_method_from_message(self, message):
        end_of_method_idx = message.find(" ")
        method_name = message[0:end_of_method_idx]
        return method_name

    def _get_payload_from_message(self, message):
        # FIXME: really not a safe way of doing this...
        body_separator = '\r\n\r\n'
        payload_index = message.find(body_separator)
        body = message[payload_index+len(body_separator):]
        return body

    def _get_uri_segment_from_message(self, message):
        segment_start_index = message.find(' ')
        segment_end_index = message[segment_start_index+1:].find(' ') + segment_start_index+1
        segment = message[segment_start_index+1:segment_end_index]
        return segment
        
        


class Http2Sock(BaseSocket):
    def __init__(self, connection_settings: Dict) -> None:
        """ Initializes a socket object using hyper.

        @param connection_settings: The connection settings for this socket
        @type  connection_settings: ConnectionSettings

        @return: None
        @rtype : None

        """
        super().__init__(connection_settings)

        self.client = HTTP20Connection(
            host=self.target_ip,
            port=self.target_port,
            secure=self.connection_settings.use_ssl
        )


    def sendRecv(self, message: str, req_timeout_sec: int) -> Tuple[bool, str]:
        super().sendRecv(message, req_timeout_sec)
        method = self._get_method_from_message(message) 
        message_body = self._get_payload_from_message(message)
        uri_segment = self._get_uri_segment_from_message(message) 

        print(message)

        self.client.request(
            method, 
            url=uri_segment,
            body=bytes(message_body, 'utf-8'),  #TODO: allow other encodings
            #headers=  # TODO: implement header support
        )

        response = self.client.get_response()
        response_msg = response.read()

        # TODO: make it compatible with subsequent RESTler components.
        
        return (True, response_msg)


    def __del__(self):
        pass


class HttpRawSock(BaseSocket):
    __last_request_sent_time = time.time()
    __request_sem = threading.Semaphore()

    def __init__(self, connection_settings):
        """ Initializes a socket object using low-level python socket objects.

        @param connection_settings: The connection settings for this socket
        @type  connection_settings: ConnectionSettings

        @return: None
        @rtype : None

        """
        super().__init__(connection_settings)

        try:
            self._sock = None

            if Settings().request_throttle_ms:
                self._request_throttle_sec = Settings().request_throttle_ms/1000.0
            else:
                self._request_throttle_sec = None

            if Settings().use_test_socket:
                self._sock = TestSocket(Settings().test_server)
            elif self.connection_settings.use_ssl:
                if self.connection_settings.disable_cert_validation:
                    context = ssl._create_unverified_context()
                else:
                    context = ssl.create_default_context()
                if Settings().client_certificate_path:
                    context.load_cert_chain(
                        certfile = Settings().client_certificate_path,
                        keyfile = Settings().client_certificate_key_path,
                    )
                      
                with socket.create_connection((self.target_ip, self.target_port)) as sock:                  
                    self._sock = context.wrap_socket(sock, server_hostname=self.host)

            else:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.connect((self.target_ip, self.target_port or 80))
        except Exception as error:
            raise TransportLayerException(f"Exception Creating Socket: {error!s}")


    def __init__(self, connection_settings):
        """ Initializes a socket object using low-level python socket objects.

        @param connection_settings: The connection settings for this socket
        @type  connection_settings: ConnectionSettings

        @return: None
        @rtype : None

        """
        self._request_throttle_sec = (float)(Settings().request_throttle_ms/1000.0)\
            if Settings().request_throttle_ms else None

        self.connection_settings = connection_settings

        self.ignore_decoding_failures = Settings().ignore_decoding_failures
        self._connected = False
        self._sock = None

    def __del__(self):
        """ Destructor - Closes socket

        """
        if self._sock:
            self._closeSocket()

    def _get_method_from_message(self, message):
        end_of_method_idx = message.find(" ")
        method_name = message[0:end_of_method_idx]
        return method_name

    def sendRecv(self, message, req_timeout_sec, reconnect=False):
        """ Sends a specified request to the server and waits for a response

        @param message: Message to be sent.
        @type message : Str
        @param req_timeout_sec: The time, in seconds, to wait for request to complete
        @type req_timeout_sec : Int

        @return:
            False if failure, True if success
            Response if True returned, Error if False returned
        @rtype : Tuple (Bool, String)

        """

        try:
            if reconnect or not self._connected:
                if reconnect:
                    if self._sock:
                        self._closeSocket()
                self.set_up_connection()
                self._connected = True

            self._sendRequest(message)
            if not Settings().use_test_socket:
                http_method_name = self._get_method_from_message(message)
                received_response = self._recvResponse(req_timeout_sec, http_method_name)
                if not received_response and not reconnect:
                    # Re-connect and try again, since this may be due to the connection being closed.
                    RAW_LOGGING("Empty response received.  Re-creating connection and re-trying.")
                    return self.sendRecv(message, req_timeout_sec, reconnect=True)

                response = HttpResponse(received_response)
            else:
                response = self._sock.recv()
            RAW_LOGGING(f'Received: {response.to_str!r}\n')

            return (True, response)
        except TransportLayerException as error:
            response = HttpResponse(str(error).strip('"\''))
            if 'timed out' in str(error):
                response._status_code = TIMEOUT_CODE
                RAW_LOGGING(f"Reached max req_timeout_sec of {req_timeout_sec}.")
            elif self._contains_connection_closed(str(error)):
                response._status_code = CONNECTION_CLOSED_CODE
                RAW_LOGGING(f"Connection error: {error!s}")
                if not reconnect:
                    RAW_LOGGING("Re-creating connection and re-trying.")
                    return self.sendRecv(message, req_timeout_sec, reconnect=True)
            else:
                RAW_LOGGING(f"Unknown error: {error!s}")
                if not reconnect:
                    RAW_LOGGING("Re-creating connection and re-trying.")
                    return self.sendRecv(message, req_timeout_sec, reconnect=True)
            return (False, response)

    def _contains_connection_closed(self, error_str):
        """ Returns whether or not the error string contains a connection closed error

        @param error_str: The error string to check for connection closed error
        @type  error_str: Str

        @return: True if the error string contains the connection closed error
        @rtype : Bool

        """
        # WinError 10054 occurs when the server terminates the connection and RESTler
        # is being run from a Windows system.
        # Errno 104 occurs when the server terminates the connection and RESTler
        # is being run from a Linux system.
        connection_closed_strings = [
                # Windows
                '[WinError 10054]',
                '[WinError 10053]',
                # Linux
                '[Errno 104]'
            ]
        return any(filter(lambda x : x in error_str, connection_closed_strings))

    def _sendRequest(self, message):
        """ Sends message via current instance of socket object.

        @param message: Message to be sent.
        @type message : Str

        @return: None
        @rtype : None

        """
        def _get_end_of_header(message):
            return message.index(DELIM)

        def _get_start_of_body(message):
            return _get_end_of_header(message) + len(DELIM)

        def _append_to_header(message, content):
            header = message[:_get_end_of_header(message)] + "\r\n" + content + DELIM
            return header + message[_get_start_of_body(message):]

        if "Content-Length: " not in message:
            contentlen = len(message[_get_start_of_body(message):])
            message = _append_to_header(message, f"Content-Length: {contentlen}")
        if self.connection_settings.include_user_agent:
            message = _append_to_header(message, f"User-Agent: restler/{Settings().version}")

        # Attempt to throttle the request if necessary
        self._begin_throttle_request()

        try:
            RAW_LOGGING(f'Sending: {message!r}\n')
            self._sock.sendall(message.encode(UTF8))
        except Exception as error:
            raise TransportLayerException(f"Exception Sending Data: {error!s}")
        finally:
            self._end_throttle_request()

    def _begin_throttle_request(self):
        """ Will attempt to throttle a request by comparing the last time
        a request was sent to the throttle time (if any).

        @return: None
        @rtype : None

        """
        if self._request_throttle_sec:
            HttpSock.__request_sem.acquire()
            elapsed = time.time() - HttpSock.__last_request_sent_time
            throttle_time_remaining = self._request_throttle_sec - elapsed
            if throttle_time_remaining > 0:
                time.sleep(throttle_time_remaining)

    def _end_throttle_request(self):
        """ Will release the throttle lock (if held), so another
        request can be sent.

        Sets last_request_sent_time

        @return: None
        @rtype : None

        """
        if self._request_throttle_sec:
            HttpSock.__last_request_sent_time = time.time()
            HttpSock.__request_sem.release()

    def _recvResponse(self, req_timeout_sec, method_name):
        """ Reads data from socket object.

        @param req_timeout_sec: The time, in seconds, to wait for request to complete
        @type req_timeout_sec : Int

        @return: Data received on current socket.
        @rtype : Str

        """
        global DELIM
        data = ''

        def decode_buf (buf):
            try:
                return buf.decode(UTF8)
            except Exception as ex:
                if self.ignore_decoding_failures:
                    RAW_LOGGING(f'Failed to decode data due to {ex}. \
                        Trying again while ignoring offending bytes.')
                    return buf.decode(UTF8, "ignore")
                else:
                    raise

        # get the data of header (and maybe more)
        bytes_received = 0
        while DELIM not in data:
            try:
                self._sock.settimeout(req_timeout_sec)
                buf = self._sock.recv(2**20)
                bytes_received += len(buf)
            except Exception as error:
                raise TransportLayerException(f"Exception: {error!s}")

            if len(buf) == 0:
                return data

            data += decode_buf(buf)

        # Handle chunk encoding
        chuncked_encoding = False
        if 'Transfer-Encoding: chunked\r\n' in data or\
            'transfer-encoding: chunked\r\n' in data:
            # Handle corner case of single-chunk data transfer. Without this
            # check, the next recv call on _sock will hang until timeout.
            if data.endswith(DELIM):
                return data
            chuncked_encoding = True
        if chuncked_encoding:
            while True:
                try:
                    buf = self._sock.recv(2**20)
                    bytes_received += len(buf)
                except Exception as error:
                    raise TransportLayerException(f"Exception: {error!s}")

                if len(buf) == 0:
                    return data

                data += decode_buf(buf)

                if data.endswith(DELIM):
                    return data

        header_len = data.index(DELIM) + len(DELIM)

        if data[:12] in ["HTTP/1.1 204", "HTTP/1.1 304"]:
            content_len = 0
        elif method_name.upper() == "HEAD":
            content_len = 0
        else:
            try:
                data_lower = data.lower()
                content_len = data_lower.split("content-length: ")[1]
                content_len = int(content_len.split('\r\n')[0])
            except Exception as error:
                content_len = 2**20

        bytes_remain = content_len - bytes_received + header_len


        # get rest of socket data
        while bytes_remain > 0:
            try:
                buf = self._sock.recv(2**20)
            except Exception as error:
                raise TransportLayerException(f"Exception: {error!s}")

            if len(buf) == 0:
                return data

            bytes_remain -= len(buf)
            data += decode_buf(buf)

        return data

    def _closeSocket(self):
        """ Closes open socket object.

        @return: None
        @rtype : None

        """
        try:
            self._sock.close()
        except Exception as error:
                raise TransportLayerException(f"Exception: {error!s}")

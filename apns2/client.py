import collections
import json
import logging
from enum import Enum

from hyper import HTTP20Connection
from hyper.tls import init_context

from .errors import ConnectionFailed, exception_class_for_reason


class NotificationPriority(Enum):
    Immediate = '10'
    Delayed = '5'

RequestStream = collections.namedtuple('RequestStream', ['stream_id', 'token'])
Notification = collections.namedtuple('Notification', ['token', 'payload'])

DEFAULT_APNS_PRIORITY = NotificationPriority.Immediate
CONCURRENT_STREAMS_SAFETY_MAXIMUM = 1000
MAX_CONNECTION_RETRIES = 3

logger = logging.getLogger(__name__)


class APNsClient(object):
    def __init__(self, cert_file, use_sandbox=False, use_alternative_port=False, proto=None, json_encoder=None, password=None):
        server = 'api.development.push.apple.com' if use_sandbox else 'api.push.apple.com'
        port = 2197 if use_alternative_port else 443
        ssl_context = init_context()
        ssl_context.load_cert_chain(cert_file, password=password)
        self.__connection = HTTP20Connection(server, port, ssl_context=ssl_context, force_proto=proto or 'h2')
        self.__json_encoder = json_encoder
        self.__max_concurrent_streams = None
        self.__previous_server_max_concurrent_streams = None

    def send_notification(self, token_hex, notification, topic, priority=NotificationPriority.Immediate,
                          expiration=None):
        stream_id = self.send_notification_async(token_hex, notification, topic, priority, expiration)
        result = self.get_notification_result(stream_id)
        if result != 'Success':
            raise exception_class_for_reason(result)

    def send_notification_async(self, token_hex, notification, topic, priority=NotificationPriority.Immediate,
                                expiration=None):
        json_str = json.dumps(notification.dict(), cls=self.__json_encoder, ensure_ascii=False, separators=(',', ':'))
        json_payload = json_str.encode('utf-8')

        headers = {'apns-topic': topic}
        if priority != DEFAULT_APNS_PRIORITY:
            headers['apns-priority'] = priority.value

        if expiration is not None:
            headers['apns-expiration'] = '%d' % expiration

        url = '/3/device/{}'.format(token_hex)
        stream_id = self.__connection.request('POST', url, json_payload, headers)
        return stream_id

    def get_notification_result(self, stream_id):
        with self.__connection.get_response(stream_id) as response:
            if response.status == 200:
                return 'Success'
            else:
                raw_data = response.read().decode('utf-8')
                data = json.loads(raw_data)
                return data['reason']

    def send_notification_batch(self, notifications, topic, priority=NotificationPriority.Immediate, expiration=None):
        '''
        Send a notification to a list of tokens in batch. Instead of sending a synchronous request
        for each token, send multiple requests concurrently. This is done on the same connection,
        using HTTP/2 streams (one request per stream).

        APNs allows many streams simultaneously, but the number of streams can vary depending on
        server load. This method reads the SETTINGS frame sent by the server to figure out the
        maximum number of concurrent streams. Typically, APNs reports a maximum of 500.

        The function returns a dictionary mapping each token to its result. The result is "Success"
        if the token was sent successfully, or the string returned by APNs in the 'reason' field of
        the response, if the token generated an error.
        '''
        notification_iterator = iter(notifications)
        next_notification = next(notification_iterator, None)
        # Make sure we're connected to APNs, so that we receive and process the server's SETTINGS
        # frame before starting to send notifications.
        self.connect()

        results = {}
        open_streams = collections.deque()
        # Loop on the tokens, sending as many requests as possible concurrently to APNs.
        # When reaching the maximum concurrent streams limit, wait for a response before sending
        # another request.
        while len(open_streams) > 0 or next_notification is not None:
            # Update the max_concurrent_streams on every iteration since a SETTINGS frame can be
            # sent by the server at any time.
            self.update_max_concurrent_streams()
            if self.should_send_notification(next_notification, open_streams):
                logger.info('Sending to token %s', next_notification.token)
                stream_id = self.send_notification_async(next_notification.token, next_notification.payload, topic,
                                                         priority, expiration)
                open_streams.append(RequestStream(stream_id, next_notification.token))

                next_notification = next(notification_iterator, None)
                if next_notification is None:
                    # No tokens remaining. Proceed to get results for pending requests.
                    logger.info('Finished sending all tokens, waiting for pending requests.')
            else:
                # We have at least one request waiting for response (otherwise we would have either
                # sent new requests or exited the while loop.) Wait for the first outstanding stream
                # to return a response.
                pending_stream = open_streams.popleft()
                result = self.get_notification_result(pending_stream.stream_id)
                logger.info('Got response for %s: %s', pending_stream.token, result)
                results[pending_stream.token] = result

        return results

    def should_send_notification(self, notification, open_streams):
        return notification is not None and len(open_streams) < self.__max_concurrent_streams

    def update_max_concurrent_streams(self):
        # Get the max_concurrent_streams setting returned by the server.
        # The max_concurrent_streams value is saved in the H2Connection instance that must be
        # accessed using a with statement in order to acquire a lock.
        # pylint: disable=protected-access
        with self.__connection._conn as connection:
            max_concurrent_streams = connection.remote_settings.max_concurrent_streams

        if max_concurrent_streams == self.__previous_server_max_concurrent_streams:
            # The server hasn't issued an updated SETTINGS frame.
            return

        self.__previous_server_max_concurrent_streams = max_concurrent_streams
        # Handle and log unexpected values sent by APNs, just in case.
        if max_concurrent_streams > CONCURRENT_STREAMS_SAFETY_MAXIMUM:
            logger.warning('APNs max_concurrent_streams too high (%s), resorting to default maximum (%s)',
                           max_concurrent_streams, CONCURRENT_STREAMS_SAFETY_MAXIMUM)
            self.__max_concurrent_streams = CONCURRENT_STREAMS_SAFETY_MAXIMUM
        elif max_concurrent_streams < 1:
            logger.warning('APNs reported max_concurrent_streams less than 1 (%s), using value of 1',
                           max_concurrent_streams)
            self.__max_concurrent_streams = 1
        else:
            logger.info('APNs set max_concurrent_streams to %s', max_concurrent_streams)
            self.__max_concurrent_streams = max_concurrent_streams

    def connect(self):
        '''
        Establish a connection to APNs. If already connected, the function does nothing. If the
        connection fails, the function retries up to MAX_CONNECTION_RETRIES times.
        '''
        retries = 0
        while retries < MAX_CONNECTION_RETRIES:
            try:
                self.__connection.connect()
                logger.info('Connected to APNs')
                return
            except Exception:  # pylint: disable=broad-except
                retries += 1
                logger.exception('Failed connecting to APNs (attempt %s of %s)', retries, MAX_CONNECTION_RETRIES)

        raise ConnectionFailed()

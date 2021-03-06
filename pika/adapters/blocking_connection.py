"""Implement a blocking, procedural style connection adapter on top of the
asynchronous core.

"""
import logging
import socket
import time

from pika import callback
from pika import channel
from pika import exceptions
from pika import spec
from pika import utils
from pika.adapters import base_connection

LOGGER = logging.getLogger(__name__)


class BlockingConnection(base_connection.BaseConnection):
    """The BlockingConnection adapter is meant for simple implementations where
    you want to have blocking behavior. The behavior layered on top of the
    async library. Because of the nature of AMQP there are a few callbacks
    one needs to do, even in a blocking implementation. These include receiving
    messages from Basic.Deliver, Basic.GetOk, and Basic.Return.

    """
    WRITE_TO_READ_RATIO = 1000
    DO_HANDSHAKE = True
    SOCKET_CONNECT_TIMEOUT = .25
    SOCKET_TIMEOUT_THRESHOLD = 12
    SOCKET_TIMEOUT_CLOSE_THRESHOLD = 3
    SOCKET_TIMEOUT_MESSAGE = "Timeout exceeded, disconnected"

    def add_timeout(self, deadline, callback):
        """Add the callback to the IOLoop timer to fire after deadline
        seconds.

        :param int deadline: The number of seconds to wait to call callback
        :param method callback: The callback method
        :rtype: str

        """
        timeout_id = '%.8f' % time.time()
        self._timeouts[timeout_id] = {'deadline': deadline + time.time(),
                                      'method': callback}
        return timeout_id

    def channel(self, channel_number=None):
        """Create a new channel with the next available or specified channel #.

        :param int channel_number: Specify the channel number

        """
        self._channel_open = False
        if not channel_number:
            channel_number = self._next_channel_number()
        LOGGER.debug('Opening channel %i', channel_number)
        self._channels[channel_number] = BlockingChannel(self, channel_number)
        return self._channels[channel_number]

    def close(self, reply_code=200, reply_text='Normal shutdown'):
        """Disconnect from RabbitMQ. If there are any open channels, it will
        attempt to close them prior to fully disconnecting. Channels which
        have active consumers will attempt to send a Basic.Cancel to RabbitMQ
        to cleanly stop the delivery of messages prior to closing the channel.

        :param int reply_code: The code number for the close
        :param str reply_text: The text reason for the close

        """
        self._remove_connection_callbacks()
        super(BlockingConnection, self).close(reply_code, reply_text)
        while not self.is_closed:
            self.process_data_events()

    def disconnect(self):
        """Disconnect from the socket"""
        self.socket.close()

    def process_data_events(self):
        """Will make sure that data events are processed. Your app can
        block on this method.

        """
        try:
            if self._handle_read():
                self._socket_timeouts = 0
        except socket.timeout:
            self._handle_timeout()
        self._flush_outbound()
        self.process_timeouts()

    def process_timeouts(self):
        """Process the self._timeouts event stack"""
        for timeout_id in self._timeouts.keys():
            if self._deadline_passed(timeout_id):
                self._call_timeout_method(self._timeouts.pop(timeout_id))

    def remove_timeout(self, timeout_id):
        """Remove the timeout from the IOLoop by the ID returned from
        add_timeout.

        :param str timeout_id: The id of the timeout to remove

        """
        if timeout_id in self._timeouts:
            del self._timeouts[timeout_id]

    def send_method(self, channel_number, method_frame, content=None):
        """Constructs a RPC method frame and then sends it to the broker.

        :param int channel_number: The channel number for the frame
        :param pika.object.Method method_frame: The method frame to send
        :param tuple content: If set, is a content frame, is tuple of
                              properties and body.

        """
        self._send_method(channel_number, method_frame, content)

    def _adapter_connect(self):
        """Connect to the RabbitMQ broker"""
        super(BlockingConnection, self)._adapter_connect()
        LOGGER.debug('Setting socket connection timeout')
        self.socket.settimeout(self.SOCKET_CONNECT_TIMEOUT)
        self._frames_written_without_read = 0
        self._socket_timeouts = 0
        self._timeouts = dict()
        self._on_connected()
        while not self.is_open:
            self.process_data_events()

        LOGGER.debug('Setting socket timeout to %s', self.params.socket_timeout)
        self.socket.settimeout(self.params.socket_timeout)
        LOGGER.info('Adapter connected')

    def _adapter_disconnect(self):
        """Called if the connection is being requested to disconnect."""
        self.disconnect()
        self._check_state_on_disconnect()

    def _call_timeout_method(self, timeout_value):
        """Execute the method that was scheduled to be called.

        :param dict timeout_value: The configuration for the timeout

        """
        LOGGER.debug('Invoking scheduled call of %s', timeout_value['method'])
        timeout_value['method']()

    def _deadline_passed(self, timeout_id):
        """Returns True if the deadline has passed for the specified timeout_id.

        :param str timeout_id: The id of the timeout to check
        :rtype: bool

        """
        if timeout_id not in self._timeouts.keys():
            return False
        return self._timeouts[timeout_id]['deadline'] <= time.time()

    def _handle_disconnect(self):
        """Called internally when the socket is disconnected already"""
        LOGGER.debug('Handling disconnect')
        self.disconnect()
        self._on_connection_closed(None, True)

    def _handle_read(self):
        super(BlockingConnection, self)._handle_read()
        self._frames_written_without_read = 0

    def _handle_timeout(self):
        """Invoked whenever the socket times out"""
        self._socket_timeouts += 1
        threshold = (self.SOCKET_CONNECT_TIMEOUT if not self.is_closing else
                     self.SOCKET_TIMEOUT_CLOSE_THRESHOLD)

        LOGGER.debug('Handling timeout %i with a threshold of %i',
                     self._socket_timeouts, threshold)
        if (self.is_closing and self._socket_timeouts > threshold):
            LOGGER.critical('Closing connection due to timeout')
            self._on_connection_closed(None, True)

    def _flush_outbound(self):
        """Flush the outbound socket buffer."""
        LOGGER.debug('Outbound buffer size: %r', self.outbound_buffer.size)
        if self.outbound_buffer.size > 0:
            try:
                if self._handle_write():
                    self._socket_timeouts = 0
            except socket.timeout:
                return self._handle_timeout()

    def _on_connection_closed(self, method_frame, from_adapter=False):
        """Called when the connection is closed remotely. The from_adapter value
        will be true if the connection adapter has been disconnected from
        the broker and the method was invoked directly instead of by receiving
        a Connection.Close frame.

        :param pika.frame.Method: The Connection.Close frame
        :param bool from_adapter: Called by the connection adapter
        :raises: AMQPConnectionError

        """
        if self._is_connection_close_frame(method_frame):
            self.closing = (method_frame.method.reply_code,
                            method_frame.method.reply_text)
            LOGGER.warning("Disconnected from RabbitMQ at %s:%i (%s): %s",
                           self.params.host, self.params.port,
                           self.closing[0], self.closing[1])
        self._set_connection_state(self.CONNECTION_CLOSED)
        self._remove_connection_callbacks()
        if not from_adapter:
            self._adapter_disconnect()
        for channel in self._channels:
            self._channels[channel].on_remote_close(method_frame)
        self._remove_connection_callbacks()
        if self.closing[0] != 200:
            raise exceptions.AMQPConnectionError(*self.closing)

    def _send_frame(self, frame_value):
        """This appends the fully generated frame to send to the broker to the
        output buffer which will be then sent via the connection adapter.

        :param frame_value: The frame to write
        :type frame_value:  pika.frame.Frame|pika.frame.ProtocolHeader

        """
        super(BlockingConnection, self)._send_frame(frame_value)
        self._frames_written_without_read += 1
        if self._frames_written_without_read == self.WRITE_TO_READ_RATIO:
            self._frames_written_without_read = 0
            self.process_data_events()




class BlockingChannel(channel.Channel):
    """The BlockingChannel class implements a blocking layer on top of the
    Channel class.

    """
    NO_RESPONSE_FRAMES = ['Basic.Ack', 'Basic.Reject', 'Basic.RecoverAsync']

    def __init__(self, connection, channel_number):
        """Create a new instance of the Channel

        :param BlockingConnection connection: The connection
        :param int channel_number: The channel number for this instance

        """
        super(BlockingChannel, self).__init__(connection, channel_number)
        self._confirmation = False
        self._frames = dict()
        self._replies = list()
        self._wait = False
        self.connection = connection
        self.open()

    def basic_cancel(self, consumer_tag='', nowait=False):
        """This method cancels a consumer. This does not affect already
        delivered messages, but it does mean the server will not send any more
        messages for that consumer. The client may receive an arbitrary number
        of messages in between sending the cancel method and receiving the
        cancel-ok reply. It may also be sent from the server to the client in
        the event of the consumer being unexpectedly cancelled (i.e. cancelled
        for any reason other than the server receiving the corresponding
        basic.cancel from the client). This allows clients to be notified of
        the loss of consumers due to events such as queue deletion.

        :param str consumer_tag: Identifier for the consumer
        :param bool nowait: Do not expect a Basic.CancelOk response

        """
        if consumer_tag not in self._consumers:
            return
        self._cancelled.append(consumer_tag)
        replies = [spec.Basic.CancelOk] if not nowait else []
        self._rpc(spec.Basic.Cancel(consumer_tag=consumer_tag, nowait=nowait),
                  self._on_basic_cancel_ok, replies)

    def basic_get(self, queue=None, no_ack=False):
        """Get a single message from the AMQP broker. The callback method
        signature should have 3 parameters: The method frame, header frame and
        the body, like the consumer callback for Basic.Consume.

        :param str|unicode queue: The queue to get a message from
        :param bool no_ack: Tell the broker to not expect a reply

        """
        self._response = None
        super(BlockingChannel, self).basic_get(self._on_basic_get, queue,
                                               no_ack)
        while not self._response:
            self.connection.process_data_events()
        return self._response[0], self._response[1], self._response[2]

    def basic_publish(self, exchange, routing_key, body,
                      properties=None, mandatory=False, immediate=False):
        """Publish to the channel with the given exchange, routing key and body.
        For more information on basic_publish and what the parameters do, see:

        http://www.rabbitmq.com/amqp-0-9-1-reference.html#basic.publish

        :param str exchange: The exchange name
        :param str routing_key: The routing key
        :param str body: The message body
        :param pika.spec.Properties properties: Basic.properties
        :param bool mandatory: The mandatory flag
        :param bool immediate: The immediate flag

        """
        if not self.is_open:
            raise exceptions.ChannelClosed()
        if immediate:
            LOGGER.warning('The immediate flag is deprecated in RabbitMQ')
        properties = properties or spec.BasicProperties()

        if self._confirmation:
            response = self._rpc(spec.Basic.Publish(exchange=exchange,
                                                    routing_key=routing_key,
                                                    mandatory=mandatory,
                                                    immediate=immediate),
                                 None,
                                 [spec.Basic.Ack,
                                  spec.Basic.Nack,
                                  spec.Basic.Reject],
                                 (properties, body))
            if isinstance(response.method, spec.Basic.Ack):
                return True
            elif (isinstance(response.method, spec.Basic.Nack) or
                  isinstance(response.method, spec.Basic.Reject)):
                return False
            else:
                raise ValueError('Unexpected frame type: %r', response)
        else:
            self._send_method(spec.Basic.Publish(exchange=exchange,
                                                 routing_key=routing_key,
                                                 mandatory=mandatory,
                                                 immediate=immediate),
                              (properties, body), False)

    def basic_qos(self, prefetch_size=0, prefetch_count=0, all_channels=False):
        """Specify quality of service. This method requests a specific quality
        of service. The QoS can be specified for the current channel or for all
        channels on the connection. The client can request that messages be sent
        in advance so that when the client finishes processing a message, the
        following message is already held locally, rather than needing to be
        sent down the channel. Prefetching gives a performance improvement.

        :param int prefetch_size:  This field specifies the prefetch window
                                   size. The server will send a message in
                                   advance if it is equal to or smaller in size
                                   than the available prefetch size (and also
                                   falls into other prefetch limits). May be set
                                   to zero, meaning "no specific limit",
                                   although other prefetch limits may still
                                   apply. The prefetch-size is ignored if the
                                   no-ack option is set.
        :param int prefetch_count: Specifies a prefetch window in terms of whole
                                   messages. This field may be used in
                                   combination with the prefetch-size field; a
                                   message will only be sent in advance if both
                                   prefetch windows (and those at the channel
                                   and connection level) allow it. The
                                   prefetch-count is ignored if the no-ack
                                   option is set.
        :param bool all_channels: Should the QoS apply to all channels

        """
        return self._rpc(spec.Basic.Qos(prefetch_size, prefetch_count,
                                        all_channels), None, [spec.Basic.QosOk])

    def basic_recover(self, requeue=False):
        """This method asks the server to redeliver all unacknowledged messages
        on a specified channel. Zero or more messages may be redelivered. This
        method replaces the asynchronous Recover.

        :param bool requeue: If False, the message will be redelivered to the
                             original recipient. If True, the server will
                             attempt to requeue the message, potentially then
                             delivering it to an alternative subscriber.

        """
        return self._rpc(spec.Basic.Recover(requeue), None,
                         [spec.Basic.RecoverOk])

    def confirm_delivery(self, nowait=False):
        """Turn on Confirm mode in the channel.

        For more information see:
            http://www.rabbitmq.com/extensions.html#confirms

        :param bool nowait: Do not send a reply frame (Confirm.SelectOk)

        """
        if (not self.connection.publisher_confirms or
            not self.connection.basic_nack):
            raise exceptions.MethodNotImplemented('Not Supported on Server')
        self._confirmation = True
        replies = [spec.Confirm.SelectOk] if not nowait else []
        self._rpc(spec.Confirm.Select(nowait), None, replies)

    def exchange_bind(self, destination=None, source=None, routing_key='',
                      nowait=False, arguments=None):
        """Bind an exchange to another exchange.

        :param str|unicode destination: The destination exchange to bind
        :param str|unicode source: The source exchange to bind to
        :param str|unicode routing_key: The routing key to bind on
        :param bool nowait: Do not wait for an Exchange.BindOk
        :param dict arguments: Custom key/value pair arguments for the binding

        """
        replies = [spec.Exchange.BindOk] if not nowait else []
        return self._rpc(spec.Exchange.Bind(0, destination, source,
                                            routing_key, nowait,
                                            arguments or dict()), None, replies)

    def exchange_declare(self, exchange=None,
                         exchange_type='direct', passive=False, durable=False,
                         auto_delete=False, internal=False, nowait=False,
                         arguments=None):
        """This method creates an exchange if it does not already exist, and if
        the exchange exists, verifies that it is of the correct and expected
         class.

        If passive set, the server will reply with Declare-Ok if the exchange
        already exists with the same name, and raise an error if not and if the
        exchange does not already exist, the server MUST raise a channel
        exception with reply code 404 (not found).

        :param str|unicode exchange: The exchange name consists of a non-empty
                                     sequence of these characters: letters,
                                     digits, hyphen, underscore, period, or
                                     colon.
        :param str exchange_type: The exchange type to use
        :param bool passive: Perform a declare or just check to see if it exists
        :param bool durable: Survive a reboot of RabbitMQ
        :param bool auto_delete: Remove when no more queues are bound to it
        :param bool internal: Can only be published to by other exchanges
        :param bool nowait: Do not expect an Exchange.DeclareOk response
        :param dict arguments: Custom key/value pair arguments for the exchange

        """
        replies = [spec.Exchange.DeclareOk] if not nowait else []
        return self._rpc(spec.Exchange.Declare(0, exchange, exchange_type,
                                               passive, durable, auto_delete,
                                               internal, nowait,
                                               arguments or dict()),
                         None, replies)

    def exchange_delete(self, exchange=None, if_unused=False, nowait=False):
        """Delete the exchange.

        :param method callback: The method to call on Exchange.DeleteOk
        :param str|unicode exchange: The exchange name
        :param bool if_unused: only delete if the exchange is unused
        :param bool nowait: Do not wait for an Exchange.DeleteOk

        """
        replies = [spec.Exchange.DeleteOk] if not nowait else []
        return self._rpc(spec.Exchange.Delete(0, exchange, if_unused, nowait),
                         None, replies)

    def exchange_unbind(self, destination=None, source=None, routing_key='',
                        nowait=False, arguments=None):
        """Unbind an exchange from another exchange.

        :param str|unicode destination: The destination exchange to unbind
        :param str|unicode source: The source exchange to unbind from
        :param str|unicode routing_key: The routing key to unbind
        :param bool nowait: Do not wait for an Exchange.UnbindOk
        :param dict arguments: Custom key/value pair arguments for the binding

        """
        replies = [spec.Exchange.UnbindOk] if not nowait else []
        return self._rpc(spec.Exchange.Unbind(0, destination, source,
                                              routing_key, nowait, arguments),
                         None, replies)

    def open(self):
        """Open the channel"""
        self._set_state(self.OPENING)
        self._add_callbacks()
        self._rpc(spec.Channel.Open(), self._on_open_ok, [spec.Channel.OpenOk])

    def queue_bind(self, queue, exchange, routing_key, nowait=False,
                   arguments=None):
        """Bind the queue to the specified exchange

        :param str|unicode queue: The queue to bind to the exchange
        :param str|unicode exchange: The source exchange to bind to
        :param str|unicode routing_key: The routing key to bind on
        :param bool nowait: Do not wait for a Queue.BindOk
        :param dict arguments: Custom key/value pair arguments for the binding

        """
        replies = [spec.Queue.BindOk] if not nowait else []
        return self._rpc(spec.Queue.Bind(0, queue, exchange, routing_key,
                                         nowait, arguments or dict()),
                         None, replies)

    def queue_declare(self, queue, passive=False, durable=False,
                      exclusive=False, auto_delete=False, nowait=False,
                      arguments=None):
        """Declare queue, create if needed. This method creates or checks a
        queue. When creating a new queue the client can specify various
        properties that control the durability of the queue and its contents,
        and the level of sharing for the queue.

        :param str|unicode queue: The queue name
        :param bool passive: Only check to see if the queue exists
        :param bool durable: Survive reboots of the broker
        :param bool exclusive: Only allow access by the current connection
        :param bool auto_delete: Delete after consumer cancels or disconnects
        :param bool nowait: Do not wait for a Queue.DeclareOk
        :param dict arguments: Custom key/value arguments for the queue

        """
        replies = [spec.Queue.DeclareOk] if not nowait else []
        return self._rpc(spec.Queue.Declare(0, queue, passive, durable,
                                            exclusive, auto_delete, nowait,
                                            arguments or dict()),
                         None, replies)

    def queue_delete(self, queue='', if_unused=False, if_empty=False,
                     nowait=False):
        """Delete a queue from the broker.

        :param str|unicode queue: The queue to delete
        :param bool if_unused: only delete if it's unused
        :param bool if_empty: only delete if the queue is empty
        :param bool nowait: Do not wait for a Queue.DeleteOk

        """
        replies = [spec.Queue.DeleteOk] if not nowait else []
        return self._rpc(spec.Queue.Delete(0, queue, if_unused, if_empty,
                                           nowait), None, replies)

    def queue_purge(self, queue='', nowait=False):
        """Purge all of the messages from the specified queue

        :param str|unicode: The queue to purge
        :param bool nowait: Do not expect a Queue.PurgeOk response

        """
        replies = [spec.Queue.PurgeOk] if not nowait else []
        return self._rpc(spec.Queue.Purge(0, queue, nowait), None, replies)

    def queue_unbind(self, queue='', exchange=None, routing_key='',
                     arguments=None):
        """Unbind a queue from an exchange.

        :param str|unicode queue: The queue to unbind from the exchange
        :param str|unicode exchange: The source exchange to bind from
        :param str|unicode routing_key: The routing key to unbind
        :param dict arguments: Custom key/value pair arguments for the binding

        """
        return self._rpc(spec.Queue.Unbind(0, queue, exchange, routing_key,
                                           arguments or dict()), None,
                         [spec.Queue.UnbindOk])

    def start_consuming(self):
        """Starts consuming from registered callbacks."""
        while len(self._consumers):
            self.connection.process_data_events()

    def stop_consuming(self, consumer_tag=None):
        """Sends off the Basic.Cancel to let RabbitMQ know to stop consuming and
        sets our internal state to exit out of the basic_consume.

        """
        if consumer_tag:
            self.basic_cancel(consumer_tag)
        else:
            for consumer_tag in self._consumers.keys():
                self.basic_cancel(consumer_tag)
        self.wait = True

    def tx_commit(self):
        """Commit a transaction."""
        self._validate_channel_and_callback(callback)
        return self._rpc(spec.Tx.Commit(), None, [spec.Tx.CommitOk])

    def tx_rollback(self):
        """Rollback a transaction."""
        self._validate_channel_and_callback(callback)
        return self._rpc(spec.Tx.Rollback(), None, [spec.Tx.RollbackOk])

    def tx_select(self):
        """Select standard transaction mode. This method sets the channel to use
        standard transactions. The client must use this method at least once on
        a channel before using the Commit or Rollback methods.

        """
        return self._rpc(spec.Tx.Select(), None, [spec.Tx.SelectOk])

    # Internal methods

    def _add_reply(self, reply):
        reply = callback._name_or_value(reply)
        self._replies.append(reply)

    def _add_callbacks(self):
        """Add callbacks for when the channel opens and closes."""
        self.connection.callbacks.add(self.channel_number,
                                      spec.Channel.CloseOk,
                                      self._on_rpc_complete)

    def _on_basic_get(self, caller_unused, method_frame, header_frame, body):
        self._received_response = True
        self._response = method_frame, header_frame, body

    def _on_basic_get_empty(self, frame):
        self._received_response = True
        self._response = frame.method, None, None

    def _on_close(self, method_frame):
        LOGGER.warning('Received Channel.Close, closing: %r', method_frame)
        self._send_method(spec.Channel.CloseOk(), None, False)
        self._set_state(self.CLOSED)
        raise exceptions.ChannelClosed(self._reply_code, self._reply_text)

    def _on_open_ok(self, method_frame):
        """Open the channel by sending the RPC command and remove the reply
        from the transport.

        """
        super(BlockingChannel, self)._on_open_ok(method_frame)
        self._remove_reply(method_frame)

    def _on_rpc_complete(self, frame):
        key = callback._name_or_value(frame)
        self._replies.append(key)
        self._frames[key] = frame
        self._received_response = True

    def _process_replies(self, replies, callback):
        """Process replies from RabbitMQ, looking in the stack of callback
        replies for a match. Will optionally call callback prior to
        returning the frame_value.

        :param list replies: The reply handles to iterate
        :param method callback: The method to optionally call
        :rtype: pika.frame.Frame

        """
        for reply in self._replies:
            if reply in replies:
                frame_value = self._frames[reply]
                self._received_response = True
                if callback:
                    callback(frame_value)
                del(self._frames[reply])
                return frame_value

    def _remove_reply(self, frame):
        key = callback._name_or_value(frame)
        if key in self._replies:
            self._replies.remove(key)

    def _rpc(self, method_frame, callback=None, acceptable_replies=None,
             content=None):
        """Make an RPC call for the given callback, channel number and method.
        acceptable_replies lists out what responses we'll process from the
        server with the specified callback.

        :param pika.amqp_object.Method method_frame: The method frame to call
        :param method callback: The callback for the RPC response
        :param list acceptable_replies: The replies this RPC call expects
        :param tuple content: Properties and Body for content frames

        """
        if self.is_closed:
            raise exceptions.ChannelClosed
        self._validate_acceptable_replies(acceptable_replies)
        self._validate_callback(callback)
        replies = list()
        for reply in acceptable_replies or list():
            prefix, key = self.callbacks.add(self.channel_number,
                                             reply,
                                             self._on_rpc_complete)
            replies.append(key)
        self._received_response = False
        self._send_method(method_frame, content,
                          self._wait_on_response(method_frame))
        return self._process_replies(replies, callback)

    def _send_method(self, method_frame, content=None, wait=True):
        """Shortcut wrapper to send a method through our connection, passing in
        our channel number.

        :param pika.amqp_object.Method method_frame: The method frame to send
        :param str|tuple content: The content to send
        :param bool wait: Wait for a response

        """
        self.wait = wait
        self._received_response = False
        LOGGER.debug('Connection: %r', self.connection)
        self.connection.send_method(self.channel_number, method_frame, content)
        while self.connection.outbound_buffer.size > 0:
            try:
                self.connection.process_data_events()
            except exceptions.AMQPConnectionError:
                break
        while wait and not self._received_response:
            try:
                self.connection.process_data_events()
            except exceptions.AMQPConnectionError:
                break

    def _shutdown(self):
        """Handle Channel.Close as a blocking RPC call"""
        self._set_state(self.CLOSING)
        self._rpc(spec.Channel.Close(self._reply_code, self._reply_text, 0, 0),
                  None,
                  [spec.Channel.CloseOk])

    def _validate_acceptable_replies(self, acceptable_replies):
        """Validate the list of acceptable replies

        :param acceptable_replies:
        :raises: TypeError

        """
        if acceptable_replies and not isinstance(acceptable_replies, list):
            raise TypeError("acceptable_replies should be list or None, is %s",
                            type(acceptable_replies))

    def _validate_callback(self, callback):
        """Validate the value passed in is a method or function.

        :param method callback callback: The method to validate
        :raises: TypeError

        """
        if (callback is not None and
            not utils.is_callable(callback)):
            raise TypeError("Callback should be a function or method, is %s",
                            type(callback))

    def _wait_on_response(self, method_frame):
        """Returns True if the rpc call should wait on a response.

        :param pika.frame.Method method_frame: The frame to check

        """
        return method_frame.NAME not in self.NO_RESPONSE_FRAMES

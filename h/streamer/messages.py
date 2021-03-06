# -*- coding: utf-8 -*-

from collections import namedtuple
import logging

from gevent.queue import Full

from h import realtime
from h.realtime import Consumer
from memex import presenters
from memex import storage
from memex.links import LinksService
from h.auth.util import translate_annotation_principals
from h.nipsa.services import NipsaService
from h.streamer import websocket
import h.sentry
import h.stats

log = logging.getLogger(__name__)


# An incoming message from a subscribed realtime consumer
Message = namedtuple('Message', ['topic', 'payload'])


def process_messages(settings, routing_key, work_queue, raise_error=True):
    """
    Configure, start, and monitor a realtime consumer for the specified
    routing key.

    This sets up a :py:class:`h.realtime.Consumer` to route messages from
    `routing_key` to the passed `work_queue`, and starts it. The consumer
    should never return. If it does, this function will raise an exception.
    """

    def _handler(payload):
        try:
            message = Message(topic=routing_key, payload=payload)
            work_queue.put(message, timeout=0.1)
        except Full:
            log.warn('Streamer work queue full! Unable to queue message from '
                     'h.realtime having waited 0.1s: giving up.')

    conn = realtime.get_connection(settings)
    sentry_client = h.sentry.get_client(settings)
    statsd_client = h.stats.get_client(settings)
    consumer = Consumer(connection=conn,
                        routing_key=routing_key,
                        handler=_handler,
                        sentry_client=sentry_client,
                        statsd_client=statsd_client)
    consumer.run()

    if raise_error:
        raise RuntimeError('Realtime consumer quit unexpectedly!')


def handle_message(message, session, topic_handlers):
    """
    Deserialize and process a message from the reader.

    For each message, `handler` is called with the deserialized message and a
    single :py:class:`h.streamer.WebSocket` instance, and should return the
    message to be sent to the client on that socket. The handler can return
    `None`, to signify that no message should be sent, or a JSON-serializable
    object. It is assumed that there is a 1:1 request-reply mapping between
    incoming messages and messages to be sent out over the websockets.
    """
    try:
        handler = topic_handlers[message.topic]
    except KeyError:
        raise RuntimeError("Don't know how to handle message from topic: "
                           "{}".format(message.topic))

    # N.B. We iterate over a non-weak list of instances because there's nothing
    # to stop connections being added or dropped during iteration, and if that
    # happens Python will throw a "Set changed size during iteration" error.
    sockets = list(websocket.WebSocket.instances)
    handler(message.payload, sockets, session)


def handle_annotation_event(message, sockets, session):
    id_ = message['annotation_id']
    annotation = storage.fetch_annotation(session, id_)

    # FIXME: It isn't really nice to try and get the userid from the fetched
    # annotation or otherwise get it from the maybe-already serialized
    # annotation dict, to then only access the database for the nipsa flag once.
    # We do this because the event action is `delete` at which point we can't
    # load the annotation from the database. Handling annotation deletions is
    # a known problem and will be fixed in the future.
    userid = None
    if annotation:
        userid = annotation.userid
    else:
        userid = message.get('annotation_dict', {}).get('user')
    nipsa_service = NipsaService(session)
    user_nipsad = nipsa_service.is_flagged(userid)

    for socket in sockets:
        reply = _generate_annotation_event(message, socket, annotation, user_nipsad)
        if reply is None:
            continue
        socket.send_json(reply)


def handle_user_event(message, sockets, _):
    for socket in sockets:
        reply = _generate_user_event(message, socket)
        if reply is None:
            continue
        socket.send_json(reply)


def _generate_annotation_event(message, socket, annotation, user_nipsad):
    """
    Get message about annotation event `message` to be sent to `socket`.

    Inspects the embedded annotation event and decides whether or not the
    passed socket should receive notification of the event.

    Returns None if the socket should not receive any message about this
    annotation event, otherwise a dict containing information about the event.
    """
    action = message['action']

    if action == 'read':
        return None

    if message['src_client_id'] == socket.client_id:
        return None

    # We don't send anything until we have received a filter from the client
    if socket.filter is None:
        return None

    notification = {
        'type': 'annotation-notification',
        'options': {'action': action},
    }
    id_ = message['annotation_id']

    # Return early when action is delete
    serialized = None
    if action == 'delete':
        serialized = message['annotation_dict']
    else:
        if annotation is None:
            return None

        base_url = socket.registry.settings.get('h.app_url',
                                                'http://localhost:5000')
        links_service = LinksService(base_url, socket.registry)
        serialized = presenters.AnnotationJSONPresenter(annotation,
                                                        links_service).asdict()

    userid = serialized.get('user')
    if user_nipsad and socket.authenticated_userid != userid:
        return None

    permissions = serialized.get('permissions')
    if not _authorized_to_read(socket.effective_principals, permissions):
        return None

    if not socket.filter.match(serialized, action):
        return None

    notification['payload'] = [serialized]
    if action == 'delete':
        notification['payload'] = [{'id': id_}]
    return notification


def _generate_user_event(message, socket):
    """
    Get message about user event `message` to be sent to `socket`.

    Inspects the embedded user event and decides whether or not the passed
    socket should receive notification of the event.

    Returns None if the socket should not receive any message about this user
    event, otherwise a dict containing information about the event.
    """
    if socket.authenticated_userid != message['userid']:
        return None

    # for session state change events, the full session model
    # is included so that clients can update themselves without
    # further API requests
    return {
        'type': 'session-change',
        'action': message['type'],
        'model': message['session_model']
    }


def _authorized_to_read(effective_principals, permissions):
    """Return True if the passed request is authorized to read the annotation.

    If the annotation belongs to a private group, this will return False if the
    authenticated user isn't a member of that group.
    """
    read_permissions = permissions.get('read', [])
    read_principals = translate_annotation_principals(read_permissions)
    if set(read_principals).intersection(effective_principals):
        return True
    return False

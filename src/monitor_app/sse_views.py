import json
import logging
import threading
import queue
import time
from typing import Dict, Optional
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


class SSEMessageBroadcaster:
    """
    Singleton broadcaster that manages SSE connections and message distribution.
    Receives messages from ActiveMQ processor and forwards to connected SSE clients.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, 'initialized'):
            return
        self.client_queues: Dict[str, queue.Queue] = {}
        self.client_filters: Dict[str, Dict] = {}
        self.client_subscribers: Dict[str, int] = {}  # Maps client_id to subscriber_id
        self._lock = threading.Lock()
        self.initialized = True
        logger.info("SSE Message Broadcaster initialized")
        # Start background subscriber to channel layer group if available
        try:
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                group = getattr(settings, 'SSE_CHANNEL_GROUP', 'workflow_events')
                threading.Thread(
                    target=_channel_layer_subscriber_loop,
                    args=(group, ),
                    name="SSEChannelLayerSubscriber",
                    daemon=True,
                ).start()
                logger.info(f"SSE Channel layer subscriber started for group '{group}'")
        except Exception as e:
            logger.debug(f"SSE channel layer subscriber not started: {e}")
    
    def add_client(self, client_id: str, request, filters: Optional[Dict] = None) -> queue.Queue:
        """Add a new SSE client and track as subscriber."""
        with self._lock:
            # Create queue (hardcoded for now, will be configurable later)
            client_queue = queue.Queue(maxsize=100)
            self.client_queues[client_id] = client_queue
            self.client_filters[client_id] = filters or {}
            
            # Create/update subscriber record
            from .models import Subscriber
            
            subscriber_name = f"sse_{client_id[:8]}"  # Use first 8 chars of UUID
            
            subscriber, created = Subscriber.objects.update_or_create(
                subscriber_name=subscriber_name,
                defaults={
                    'delivery_type': 'sse',
                    'client_ip': self._get_client_ip(request),
                    'client_location': self._get_client_location(request),
                    'connected_at': timezone.now(),
                    'disconnected_at': None,
                    'last_activity': timezone.now(),
                    'is_active': True,
                    'message_filters': filters or {},
                    'description': f"SSE client from {self._get_client_ip(request)}"
                }
            )
            
            # Store subscriber ID for cleanup on disconnect
            self.client_subscribers[client_id] = subscriber.subscriber_id
            
            logger.info(f"Added SSE client {client_id} as subscriber {subscriber_name}")
            return client_queue
    
    def remove_client(self, client_id: str):
        """Remove disconnected SSE client and update subscriber record."""
        with self._lock:
            self.client_queues.pop(client_id, None)
            self.client_filters.pop(client_id, None)
            
            # Update subscriber record
            if client_id in self.client_subscribers:
                from .models import Subscriber
                
                try:
                    subscriber = Subscriber.objects.get(
                        subscriber_id=self.client_subscribers[client_id]
                    )
                    subscriber.disconnected_at = timezone.now()
                    subscriber.is_active = False
                    subscriber.save()
                except Subscriber.DoesNotExist:
                    pass
                
                self.client_subscribers.pop(client_id, None)
            
            logger.info(f"Removed SSE client {client_id}")
    
    def broadcast_message(self, message_data: Dict):
        """
        Broadcast a message to all connected SSE clients that match filters.
        Called by ActiveMQ processor when new messages arrive.
        """
        with self._lock:
            disconnected_clients = []
            
            for client_id, client_queue in self.client_queues.items():
                try:
                    # Check if message passes client's filters
                    if self._message_matches_filters(message_data, self.client_filters.get(client_id, {})):
                        # Update subscriber stats
                        if client_id in self.client_subscribers:
                            self._update_subscriber_stats(self.client_subscribers[client_id], 'sent')
                        
                        # Non-blocking put
                        try:
                            client_queue.put_nowait(message_data)
                        except queue.Full:
                            # Remove oldest message and add new one
                            try:
                                client_queue.get_nowait()
                                client_queue.put_nowait(message_data)
                                if client_id in self.client_subscribers:
                                    self._update_subscriber_stats(self.client_subscribers[client_id], 'dropped')
                            except queue.Empty:
                                pass
                except Exception as e:
                    logger.error(f"Error broadcasting to client {client_id}: {e}")
                    disconnected_clients.append(client_id)
            
            # Clean up disconnected clients
            for client_id in disconnected_clients:
                self.remove_client(client_id)
    
    def _message_matches_filters(self, message: Dict, filters: Dict) -> bool:
        """Check if a message matches the client's subscription filters."""
        if not filters:
            return True
        
        # Filter by message type
        if 'msg_types' in filters:
            msg_type = message.get('msg_type')
            if msg_type not in filters['msg_types']:
                return False
        
        # Filter by agent
        if 'agents' in filters:
            sender = message.get('processed_by', '')
            if sender not in filters['agents']:
                return False
        
        # Filter by run_id
        if 'run_ids' in filters:
            run_id = message.get('run_id')
            if run_id not in filters['run_ids']:
                return False
        
        return True
    
    def _get_client_ip(self, request):
        """Extract client IP from request."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    def _get_client_location(self, request):
        """Determine client location from IP or headers."""
        # Could be enhanced with IP geolocation
        # For now, check for custom header or default
        location = request.META.get('HTTP_X_CLIENT_LOCATION', '')
        if not location:
            # Simple heuristic based on IP ranges (customize for your network)
            ip = self._get_client_ip(request)
            if ip.startswith('192.168.'):
                location = 'Local'
            elif ip.startswith('10.'):
                location = 'Internal'
            else:
                location = 'Remote'
        return location
    
    def _update_subscriber_stats(self, subscriber_id: int, stat_type: str):
        """Update subscriber statistics in database."""
        from .models import Subscriber
        from django.db.models import F
        
        try:
            if stat_type == 'sent':
                Subscriber.objects.filter(subscriber_id=subscriber_id).update(
                    messages_sent=F('messages_sent') + 1,
                    last_activity=timezone.now()
                )
            elif stat_type == 'dropped':
                Subscriber.objects.filter(subscriber_id=subscriber_id).update(
                    messages_dropped=F('messages_dropped') + 1
                )
        except Exception as e:
            logger.error(f"Failed to update subscriber stats: {e}")


def sse_event_generator(client_id: str, client_queue: queue.Queue):
    """
    Generator function that yields SSE events from the client's message queue.
    """
    logger.info(f"Starting SSE event stream for client {client_id}")
    
    # Send initial connection message
    yield f"event: connected\ndata: {json.dumps({'client_id': client_id, 'status': 'connected'})}\n\n"
    
    # Heartbeat interval hardcoded for now (will be configurable later)
    last_heartbeat = time.time()
    heartbeat_interval = 30  # seconds
    
    try:
        while True:
            try:
                # Try to get a message with short timeout (hardcoded for now)
                message = client_queue.get(timeout=1.0)
                
                # Format as SSE event
                event_type = message.get('msg_type', 'message')
                event_data = json.dumps(message)
                yield f"event: {event_type}\ndata: {event_data}\n\n"
                
            except queue.Empty:
                # No message available, check if we need to send heartbeat
                current_time = time.time()
                if current_time - last_heartbeat > heartbeat_interval:
                    yield f"event: heartbeat\ndata: {json.dumps({'timestamp': current_time})}\n\n"
                    last_heartbeat = current_time
                    
    except GeneratorExit:
        logger.info(f"SSE client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Error in SSE event generator for client {client_id}: {e}")


def sse_message_stream(request):
    """
    SSE endpoint for streaming ActiveMQ messages to remote clients.
    
    This is a plain Django view (not DRF) to avoid content negotiation issues with SSE.
    Authentication is handled manually to support text/event-stream responses.
    
    Query parameters:
    - msg_types: Comma-separated list of message types to filter (e.g., "stf_gen,data_ready")
    - agents: Comma-separated list of agent names to filter
    - run_ids: Comma-separated list of run IDs to filter
    
    Example:
    GET /api/messages/stream/?msg_types=stf_gen,data_ready&agents=daq-simulator
    """
    import uuid
    from django.http import HttpResponse
    from rest_framework.authtoken.models import Token
    from django.contrib.auth.models import AnonymousUser
    
    # Manual authentication handling (supports both session and token auth)
    user = request.user if hasattr(request, 'user') else AnonymousUser()
    
    # Check for token authentication if user is not authenticated
    if not user.is_authenticated:
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Token '):
            token_key = auth_header[6:]  # Remove 'Token ' prefix
            try:
                token = Token.objects.get(key=token_key)
                user = token.user
            except Token.DoesNotExist:
                pass
    
    # Check if user is authenticated
    if not user.is_authenticated:
        return HttpResponse(
            json.dumps({'detail': 'Authentication credentials were not provided.'}),
            status=401,
            content_type='application/json'
        )
    
    # Generate unique client ID
    client_id = str(uuid.uuid4())
    
    # Parse filters from query parameters
    filters = {}
    
    msg_types = request.GET.get('msg_types')
    if msg_types:
        filters['msg_types'] = [t.strip() for t in msg_types.split(',')]
    
    agents = request.GET.get('agents')
    if agents:
        filters['agents'] = [a.strip() for a in agents.split(',')]
    
    run_ids = request.GET.get('run_ids')
    if run_ids:
        filters['run_ids'] = [r.strip() for r in run_ids.split(',')]
    
    # Get broadcaster instance and add client
    broadcaster = SSEMessageBroadcaster()
    client_queue = broadcaster.add_client(client_id, request, filters)
    
    def event_stream():
        try:
            yield from sse_event_generator(client_id, client_queue)
        finally:
            broadcaster.remove_client(client_id)
    
    # Create SSE response with appropriate headers
    response = StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream'
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # Disable Nginx buffering
    response['Access-Control-Allow-Origin'] = '*'  # Configure as needed for production
    
    return response


@api_view(['GET'])
@authentication_classes([SessionAuthentication, TokenAuthentication])
@permission_classes([IsAuthenticated])
def sse_status(request):
    """
    Get current SSE broadcaster status including connected clients.
    """
    from django.http import JsonResponse
    
    broadcaster = SSEMessageBroadcaster()
    
    status = {
        'connected_clients': len(broadcaster.client_queues),
        'client_ids': list(broadcaster.client_queues.keys()),
        'client_filters': broadcaster.client_filters
    }
    
    return JsonResponse(status)


def _channel_layer_subscriber_loop(group_name: str):
    """Background loop: receive messages from Channels group and forward to SSE broadcaster."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.debug("No channel layer available; subscriber loop exiting")
            return
        # Create a unique channel and join the group
        channel_name = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(group_name, channel_name)
        logger.info(f"Subscribed to channel layer group '{group_name}' as '{channel_name}'")
        broadcaster = SSEMessageBroadcaster()
        while True:
            message = async_to_sync(channel_layer.receive)(channel_name)
            if not message:
                continue
            if message.get('type') == 'broadcast':
                payload = message.get('payload', {})
                try:
                    broadcaster.broadcast_message(payload)
                except Exception as e:
                    logger.error(f"Failed to broadcast SSE payload from channel layer: {e}")
    except Exception as e:
        logger.error(f"Channel layer subscriber loop error: {e}")
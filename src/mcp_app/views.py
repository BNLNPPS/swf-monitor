import uuid
from datetime import datetime, timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiResponse
from monitor_app.models import SystemAgent
from .serializers import (
    MCPRequestSerializer,
    MCPResponseSerializer,
    MCPErrorResponseSerializer,
    HeartbeatRequestSerializer,
    DiscoverCapabilitiesRequestSerializer,
    GetAgentLivenessRequestSerializer,
    CapabilitiesResponseSerializer,
    AgentLivenessResponseSerializer,
)


class MCPService:
    """Service class containing MCP business logic shared between WebSocket and REST."""
    
    MCP_VERSION = "1.0"
    
    @classmethod
    def get_capabilities(cls):
        """Return available MCP capabilities."""
        return {
            'discover_capabilities': 'Lists all available commands and their descriptions.',
            'get_agent_liveness': 'Returns a report of all agents, categorized as alive or dead based on recent heartbeats.',
            'heartbeat': 'A notification sent by an agent to signal it is still active. Does not receive a direct response.'
        }
    
    @classmethod
    def handle_heartbeat(cls, payload):
        """Process heartbeat data and update agent status."""
        agent_name = payload.get('name')
        timestamp_str = payload.get('timestamp')
        agent_status = payload.get('status')

        if not all([agent_name, timestamp_str, agent_status]):
            raise ValueError("Incomplete heartbeat payload")

        try:
            agent = SystemAgent.objects.get(instance_name=agent_name)
            agent.status = agent_status
            agent.last_heartbeat = datetime.fromisoformat(timestamp_str)
            agent.save()
            return True
        except SystemAgent.DoesNotExist:
            raise ValueError(f"Unknown agent: {agent_name}")
        except ValueError as e:
            if "fromisoformat" in str(e):
                raise ValueError(f"Invalid timestamp format: {timestamp_str}")
            raise
    
    @classmethod
    def get_agent_liveness(cls):
        """Get liveness status for all agents."""
        alive_threshold = timezone.now() - timedelta(minutes=5)
        agents = SystemAgent.objects.all()
        liveness_status = {}
        
        for agent in agents:
            if agent.last_heartbeat and agent.last_heartbeat >= alive_threshold:
                liveness_status[agent.instance_name] = 'alive'
            else:
                liveness_status[agent.instance_name] = 'dead'
                
        return liveness_status
    
    @classmethod
    def create_response(cls, status_value, payload, in_reply_to):
        """Create standardized MCP response."""
        return {
            "mcp_version": cls.MCP_VERSION,
            "message_id": str(uuid.uuid4()),
            "in_reply_to": in_reply_to,
            "status": status_value,
            "payload": payload,
        }
    
    @classmethod
    def create_error_response(cls, code, message, in_reply_to=None):
        """Create standardized MCP error response."""
        response = {
            "mcp_version": cls.MCP_VERSION,
            "message_id": str(uuid.uuid4()),
            "status": "error",
            "error": {
                "code": code,
                "message": message,
            },
        }
        if in_reply_to:
            response["in_reply_to"] = in_reply_to
        return response


@extend_schema(
    request=HeartbeatRequestSerializer,
    responses={
        200: OpenApiResponse(description="Heartbeat processed successfully"),
        400: MCPErrorResponseSerializer,
        500: MCPErrorResponseSerializer,
    },
    description="Process agent heartbeat to update status and timestamp",
    tags=["MCP"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def heartbeat(request):
    """Process agent heartbeat notifications."""
    serializer = HeartbeatRequestSerializer(data=request.data)
    
    if not serializer.is_valid():
        error_response = MCPService.create_error_response(
            4000, 
            f"Invalid request format: {serializer.errors}",
            request.data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        MCPService.handle_heartbeat(serializer.validated_data['payload'])
        # Heartbeat is a notification - return simple success
        return Response({"status": "success"}, status=status.HTTP_200_OK)
        
    except ValueError as e:
        error_response = MCPService.create_error_response(
            4000,
            str(e),
            serializer.validated_data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
    
    except Exception as e:
        error_response = MCPService.create_error_response(
            5000,
            f"An unexpected server error occurred: {str(e)}",
            serializer.validated_data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(
    request=DiscoverCapabilitiesRequestSerializer,
    responses={
        200: MCPResponseSerializer,
        400: MCPErrorResponseSerializer,
    },
    description="Discover available MCP capabilities and commands",
    tags=["MCP"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def discover_capabilities(request):
    """Return available MCP capabilities."""
    serializer = DiscoverCapabilitiesRequestSerializer(data=request.data)
    
    if not serializer.is_valid():
        error_response = MCPService.create_error_response(
            4000,
            f"Invalid request format: {serializer.errors}",
            request.data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
    
    capabilities = MCPService.get_capabilities()
    response = MCPService.create_response(
        'success',
        capabilities,
        serializer.validated_data['message_id']
    )
    
    return Response(response, status=status.HTTP_200_OK)


@extend_schema(
    request=GetAgentLivenessRequestSerializer,
    responses={
        200: MCPResponseSerializer,
        400: MCPErrorResponseSerializer,
    },
    description="Get liveness status for all agents",
    tags=["MCP"]
)
@api_view(['POST'])
@permission_classes([AllowAny])
def get_agent_liveness(request):
    """Return agent liveness status."""
    serializer = GetAgentLivenessRequestSerializer(data=request.data)
    
    if not serializer.is_valid():
        error_response = MCPService.create_error_response(
            4000,
            f"Invalid request format: {serializer.errors}",
            request.data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        liveness_data = MCPService.get_agent_liveness()
        response = MCPService.create_response(
            'success',
            liveness_data,
            serializer.validated_data['message_id']
        )
        
        return Response(response, status=status.HTTP_200_OK)
        
    except Exception as e:
        error_response = MCPService.create_error_response(
            5000,
            f"An unexpected server error occurred: {str(e)}",
            serializer.validated_data.get('message_id')
        )
        return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

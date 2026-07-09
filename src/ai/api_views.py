"""AI app REST surface: propose / decide / delete for the proposal list.

Thin peers over ``ai.services`` — the same auth stack as the PCS API
(tunnel, session, token). The propose verb is the only mutation surface AI
clients get; decide demands an authenticated human.
"""
from rest_framework import status
from rest_framework.authentication import (
    SessionAuthentication, TokenAuthentication,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from monitor_app.middleware import TunnelAuthentication
from pcs.services import ServiceError

from . import services


class _AiApiView(APIView):
    authentication_classes = [TunnelAuthentication, SessionAuthentication,
                              TokenAuthentication]
    permission_classes = [IsAuthenticated]


class ProposalProposeView(_AiApiView):
    def post(self, request):
        """Create AI propagation proposals (AI_PROPOSALS.md).

        Body: ``names``, ``state``, ``comment`` (required), ``replaced_by``,
        ``proposer``, ``scan_version``, ``batch_id``.
        """
        try:
            result = services.dataset_proposal_create(
                request.data.get('names') or [],
                request.data.get('state'),
                request.data.get('comment'),
                replaced_by=request.data.get('replaced_by', ''),
                proposer=request.data.get('proposer', ''),
                scan_version=request.data.get('scan_version', 1),
                batch_id=request.data.get('batch_id', ''),
                created_by=request.user.username,
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(result, status=status.HTTP_200_OK)


class ProposalDecideView(_AiApiView):
    def post(self, request):
        """Approve or deny pending AI proposals.

        Body: ``names`` and/or ``ids``, ``decision`` ('approve' | 'deny'),
        ``quality`` (optional: wrong | poor | ok | good), ``filter``
        (optional audit record of the selecting filter).
        """
        try:
            result = services.dataset_proposal_decide(
                request.data.get('names') or [],
                request.data.get('decision'),
                decided_by=request.user.username,
                quality=request.data.get('quality', ''),
                filter_state=request.data.get('filter', ''),
                proposal_ids=request.data.get('ids') or [],
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        except (TypeError, ValueError):
            return Response({'detail': 'ids must be integers'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)


class ProposalDeleteView(_AiApiView):
    def post(self, request):
        """Delete AI proposal list rows (operator housekeeping). Body: ids."""
        try:
            result = services.dataset_proposal_delete(
                request.data.get('ids') or [],
                deleted_by=request.user.username,
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        except (TypeError, ValueError):
            return Response({'detail': 'ids must be integers'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema
import logging
from .services import plan_route
from .serializers import RoutePlanRequestSerializer, RoutePlanResponseSerializer


logger = logging.getLogger(__name__)


class RoutePlanView(APIView):
    """Plan the optimal fuel route between two locations."""

    @extend_schema(
        request=RoutePlanRequestSerializer,
        responses={
            200: RoutePlanResponseSerializer,
            400: {'type': 'object', 'properties': {'error': {'type': 'string'}}},
            500: {'type': 'object', 'properties': {'error': {'type': 'string'}}},
        },
        description="Plan the optimal fuel stops along a route. Provide start and finish locations."
    )
    def post(self, request):
        start = request.data.get("start", "").strip()
        finish = request.data.get("finish", "").strip()

        if not start or not finish:
            return Response(
                {"error": "Both 'start' and 'finish' fields are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if start.lower() == finish.lower():
            return Response(
                {"error": "'start' and 'finish' cannot be the same location."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            logger.info(f"Planning route from '{start}' to '{finish}'")
            result = plan_route(start, finish)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"error": f"Route planning failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

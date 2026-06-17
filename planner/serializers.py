from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    """Serializer for route planning request"""
    start = serializers.CharField(
        help_text="Starting location (city name or address)"
    )
    finish = serializers.CharField(
        help_text="Destination location (city name or address)"
    )


class FuelStopSerializer(serializers.Serializer):
    """Serializer for a fuel stop"""
    location = serializers.CharField(help_text="Location name")
    distance_from_start = serializers.FloatField(help_text="Distance from start in miles")
    fuel_price = serializers.FloatField(help_text="Fuel price at this location")


class RoutePlanResponseSerializer(serializers.Serializer):
    """Serializer for route planning response"""
    total_distance = serializers.FloatField(help_text="Total distance in miles")
    total_cost = serializers.FloatField(help_text="Total fuel cost in dollars")
    fuel_stops = FuelStopSerializer(many=True, help_text="List of recommended fuel stops")
    estimated_drive_time = serializers.CharField(
        help_text="Estimated driving time",
        required=False
    )

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path, include

urlpatterns = [
    path('api/', include('planner.urls')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]

urlpatterns += staticfiles_urlpatterns()
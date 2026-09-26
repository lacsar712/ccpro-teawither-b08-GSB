from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("gardens/", views.GardenListView.as_view(), name="garden_list"),
    path("gardens/new/", views.GardenCreateView.as_view(), name="garden_create"),
    path(
        "gardens/<int:pk>/edit/",
        views.GardenUpdateView.as_view(),
        name="garden_edit",
    ),
    path(
        "gardens/<int:pk>/delete/",
        views.GardenDeleteView.as_view(),
        name="garden_delete",
    ),
    path("troughs/", views.TroughListView.as_view(), name="trough_list"),
    path("troughs/new/", views.TroughCreateView.as_view(), name="trough_create"),
    path(
        "troughs/<int:pk>/edit/",
        views.TroughUpdateView.as_view(),
        name="trough_edit",
    ),
    path(
        "troughs/<int:pk>/delete/",
        views.TroughDeleteView.as_view(),
        name="trough_delete",
    ),
    path("batches/", views.BatchListView.as_view(), name="batch_list"),
    path("batches/new/", views.BatchCreateView.as_view(), name="batch_create"),
    path(
        "batches/<int:pk>/edit/",
        views.BatchUpdateView.as_view(),
        name="batch_edit",
    ),
    path(
        "batches/<int:pk>/delete/",
        views.BatchDeleteView.as_view(),
        name="batch_delete",
    ),
]

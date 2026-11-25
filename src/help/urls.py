from django.urls import path

from . import api_views, views
from .feeds import WhatsNewFeed

app_name = 'help'

urlpatterns = [
    path('whats-new/', views.whats_new, name='whats_new'),
    path('whats-new/new/', views.whats_new_new, name='whats_new_new'),
    path('whats-new/feed/', WhatsNewFeed(), name='whats_new_feed'),
    path('whats-new/<slug:slug>/', views.whats_new_item, name='whats_new_item'),
    path('whats-new/<slug:slug>/versions/<int:version_num>/',
         views.whats_new_version, name='whats_new_version'),
    path('api/whats-new/create/', api_views.ItemCreateView.as_view(),
         name='api_whats_new_create'),
    path('api/whats-new/comments/<str:comment_id>/edit/',
         api_views.CommentEditView.as_view(), name='api_whats_new_comment_edit'),
    path('api/whats-new/<slug:slug>/edit/', api_views.ItemEditView.as_view(),
         name='api_whats_new_edit'),
    path('api/whats-new/<slug:slug>/restore/', api_views.ItemRestoreView.as_view(),
         name='api_whats_new_restore'),
    path('api/whats-new/<slug:slug>/archive/', api_views.ItemArchiveView.as_view(),
         name='api_whats_new_archive'),
    path('api/whats-new/<slug:slug>/vote/', api_views.ItemVoteView.as_view(),
         name='api_whats_new_vote'),
    path('api/whats-new/<slug:slug>/comment/', api_views.CommentAddView.as_view(),
         name='api_whats_new_comment'),
]

from django.urls import path

from . import api_views, views

app_name = 'ai'

urlpatterns = [
    path('proposals/', views.ai_proposals, name='ai_proposals'),
    path('narratives/', views.ai_narratives, name='ai_narratives'),
    path('api/proposals/propose/', api_views.ProposalProposeView.as_view(),
         name='api_proposal_propose'),
    path('api/proposals/decide/', api_views.ProposalDecideView.as_view(),
         name='api_proposal_decide'),
    path('api/proposals/delete/', api_views.ProposalDeleteView.as_view(),
         name='api_proposal_delete'),
]

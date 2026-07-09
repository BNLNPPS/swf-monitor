from django.urls import path

from . import api_views, views

app_name = 'ai'

urlpatterns = [
    path('proposals/', views.ai_proposals, name='ai_proposals'),
    path('narratives/', views.ai_narratives, name='ai_narratives'),
    path('narratives/<str:name>/', views.ai_narrative_detail, name='ai_narrative_detail'),
    path('api/proposals/propose/', api_views.ProposalProposeView.as_view(),
         name='api_proposal_propose'),
    path('api/proposals/decide/', api_views.ProposalDecideView.as_view(),
         name='api_proposal_decide'),
    path('api/proposals/delete/', api_views.ProposalDeleteView.as_view(),
         name='api_proposal_delete'),
    path('api/narratives/save/', api_views.NarrativeSaveView.as_view(),
         name='api_narrative_save'),
    path('api/narratives/comment/', api_views.NarrativeCommentView.as_view(),
         name='api_narrative_comment'),
]

from django.urls import path

from .views import (
    ProjectCatalogView,
    ProjectChartsView,
    ProjectDetailView,
    ProjectFavoriteView,
    ProjectListView,
    ProjectTaskStatesView,
    ProjectWorkersView,
    TaskDependenciesView,
    TaskDetailView,
    TaskListView,
    TaskStateView,
    TaskStatusTemplatesView,
    TaskStatusTemplateView,
    TaskActivityView, TaskSubtasksView, TaskRelationCandidatesView, TaskRelationsView, TaskRelationView,
    ProjectTeamsView, TaskTeamsView, TaskMoveView,
)


urlpatterns = [
    path("", ProjectListView.as_view()),
    path("catalogs/", ProjectCatalogView.as_view()),
    path("workers/", ProjectWorkersView.as_view()),
    path("teams/", ProjectTeamsView.as_view()),
    path("<int:project_id>/charts/", ProjectChartsView.as_view()),
    path("<int:project_id>/", ProjectDetailView.as_view()),
    path("<int:project_id>/favorite/", ProjectFavoriteView.as_view()),
    path("<int:project_id>/task-states/", ProjectTaskStatesView.as_view()),
    path("<int:project_id>/state-templates/", TaskStatusTemplatesView.as_view()),
    path("<int:project_id>/state-templates/<int:template_id>/", TaskStatusTemplateView.as_view()),
    path("<int:project_id>/tasks/", TaskListView.as_view()),
    path("<int:project_id>/assignment-teams/", TaskTeamsView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/move/", TaskMoveView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/", TaskDetailView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/activity/", TaskActivityView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/subtasks/", TaskSubtasksView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/related-candidates/", TaskRelationCandidatesView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/relations/", TaskRelationsView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/relations/<int:relation_id>/", TaskRelationView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/state/", TaskStateView.as_view()),
    path("<int:project_id>/tasks/<int:task_id>/dependencies/", TaskDependenciesView.as_view()),
]

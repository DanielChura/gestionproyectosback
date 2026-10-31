from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
from unittest.mock import patch
from wsgiref.simple_server import WSGIServer, make_server

import django


class ExclusiveServer(WSGIServer):
    allow_reuse_address = False


def serve():
    if os.environ.get("GM_LIST_BROWSER") != "1":
        raise RuntimeError("Se requiere el wrapper de navegador aislado")
    django.setup()
    from django.core.wsgi import get_wsgi_application
    from django.db import connection
    from apps.workers.models import WorkerSpecialty
    from . import test_performance as fixtures
    from . import test_projects as project_fixtures
    from .pagination_seed import seed_lists

    if (connection.vendor != "mysql" or connection.settings_dict["HOST"] != "127.0.0.1"
            or connection.settings_dict["NAME"] != "gm_test_pagination" or connection.introspection.table_names()):
        raise RuntimeError("Solo se admite la base MySQL nueva y vacía de paginación")
    with connection.schema_editor() as editor:
        for model in [*fixtures.PerformanceRegressionTests.tables, WorkerSpecialty]:
            editor.create_model(model)
    fixture = project_fixtures.ProjectFlowTests()
    fixture.setUp()
    context = seed_lists(fixture)
    credentials = {"context": context}
    for role in ("admin", "worker"):
        user = getattr(fixture, role)
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")
    with ExitStack() as stack:
        stack.enter_context(patch("apps.authentication.services.accounts.send_link", return_value="qa-local-message"))
        stack.enter_context(patch("apps.authentication.views.send_link", return_value="qa-local-message"))
        print("Django y MySQL nuevos con 240 proyectos, 79 cuentas y 70 solicitudes QA; correo sustituido.", flush=True)
        try:
            with make_server("127.0.0.1", 8819, get_wsgi_application(), server_class=ExclusiveServer) as server:
                server.serve_forever()
        finally:
            connection.close()


if __name__ == "__main__":
    serve()

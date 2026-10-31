from copy import deepcopy

from . import settings_test_mysql as mysql_settings

globals().update({name: getattr(mysql_settings, name) for name in dir(mysql_settings) if name.isupper()})
DATABASES = deepcopy(mysql_settings.DATABASES)
DATABASES["default"]["NAME"] = "gm_test_pagination"

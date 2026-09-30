"""Importing this package registers every provider in the catalogue."""

from app.integrations.providers import biostar2  # noqa: F401
from app.integrations.providers import biotime  # noqa: F401
from app.integrations.providers import cams  # noqa: F401
from app.integrations.providers import cosec  # noqa: F401
from app.integrations.providers import cosec_centra  # noqa: F401
from app.integrations.providers import crosschex  # noqa: F401
from app.integrations.providers import dahua  # noqa: F401
from app.integrations.providers import hikcentral  # noqa: F401
from app.integrations.providers import hikconnect  # noqa: F401
from app.integrations.providers import hikvision  # noqa: F401
from app.integrations.providers import zkteco  # noqa: F401
from app.integrations.providers import zkteco_adms  # noqa: F401

__all__ = ["biostar2", "biotime", "cams", "cosec", "cosec_centra", "crosschex", "dahua", "hikcentral", "hikconnect", "hikvision", "zkteco", "zkteco_adms"]

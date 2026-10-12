"""Importing this package registers every provider in the catalogue."""

from app.integrations.providers import (
    biostar2,
    biotime,
    cams,
    cosec,
    cosec_centra,
    crosschex,
    dahua,
    hikcentral,
    hikconnect,
    hikvision,
    zkteco,
    zkteco_adms,
)

__all__ = ["biostar2", "biotime", "cams", "cosec", "cosec_centra", "crosschex", "dahua", "hikcentral", "hikconnect", "hikvision", "zkteco", "zkteco_adms"]

"""Importing this package registers every provider in the catalogue."""

from app.integrations.providers import biotime  # noqa: F401

__all__ = ["biotime"]

"""Gateway REST genérico para serviços SOAP corporativos (SAP)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sap-gateway")
except PackageNotFoundError:  # pragma: no cover - execução sem instalação
    __version__ = "0.0.0"

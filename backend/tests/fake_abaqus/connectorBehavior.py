"""Stand-in for Abaqus' connectorBehavior module (tests only)."""

from abaqus import LOG, Recorder, _show


def ConnectorElasticity(**kwargs):
    LOG.append({"call": "connectorBehavior.ConnectorElasticity", "args": [], "kwargs": _show(kwargs)})
    return Recorder("ConnectorElasticity")


def ConnectorDamping(**kwargs):
    LOG.append({"call": "connectorBehavior.ConnectorDamping", "args": [], "kwargs": _show(kwargs)})
    return Recorder("ConnectorDamping")

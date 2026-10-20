"""Tiny fake for transient regionToolset.Region objects used by section assignments."""

from abaqus import Recorder


def Region(**kwargs):
    return Recorder("regionToolset.Region")(**kwargs)

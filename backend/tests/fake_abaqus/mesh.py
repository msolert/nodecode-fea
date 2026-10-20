"""Tiny fake for generated CAE mesh.ElemType calls."""

from abaqus import Recorder


def ElemType(**kwargs):
    return Recorder("mesh.ElemType")(**kwargs)

"""Stand-in for Abaqus/CAE's displayGroupOdbToolset (tests only)."""

from abaqus import Recorder

LeafFromElementSets = Recorder("dgo.LeafFromElementSets")
LeafFromNodeSets = Recorder("dgo.LeafFromNodeSets")
LeafFromElementLabels = Recorder("dgo.LeafFromElementLabels")
LeafFromNodeLabels = Recorder("dgo.LeafFromNodeLabels")

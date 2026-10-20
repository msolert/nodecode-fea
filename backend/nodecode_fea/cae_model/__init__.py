"""Modelling in Abaqus/CAE, rebuilt incrementally.

Each Model / Sketch / Part / Instance value becomes one CAE object with a fingerprint: a hash of the value, which
holds everything upstream of it (a part holds its model and sketch, an instance its part). A named sketch is built in
its Model, and in the model of each part made from it (no Model and no part: NoCode-Model). The generated script keeps,
inside the CAE session, which fingerprint built each object, and on every update deletes and recreates only the
objects whose fingerprint changed (or that went missing in CAE), plus whatever depends on them. Objects whose
node was removed are deleted. CAE restarted means an empty record, so everything is built again.

The generated code avoids f-strings so it also runs on older (Python 2) Abaqus versions.

One module per area (geometry, material, regions, step, loads, interaction, assembly), with collect.py
turning a graph into objects and show.py showing a node. Every name is re-exported here."""

from __future__ import annotations


# Every name the old single module had, for `from nodecode_fea.cae_model import ...` elsewhere
from .core import (  # noqa: F401
    ASSEMBLY, built, _core, final_part, fingerprint, _flat, HELPERS, is_model_node, _item_of, _n, ORDER, PART, _pt,
    SKETCH,
)
from .material import (  # noqa: F401
    copy_code, material_code, native_sequence_lines, section_assignment_code, section_code, _selector_parts,
)
from .regions import (  # noqa: F401
    combined_region_code, CombinedRegion, coupling_point, object_code, region_code, region_kw_code, REPOS,
    rp_feature_name, selection_code,
)
from .geometry import (  # noqa: F401
    datum_code, datum_model, defeature_code, _edge_seed_lines, feature_code, FINISH, model_code, operation_code,
    part_code, PartFinish, sketch_code, split_finish,
)
from .step import (  # noqa: F401
    output_code, step_code,
)
from .loads import (  # noqa: F401
    _amplitude_lines, AMPLITUDE_MAKERS, _bolt_axis, _bolt_fix_steps, _bolt_output, CONDITION_ARGS, _condition_args,
    condition_code, _condition_create, condition_plan, _condition_repo, _condition_step_name, model_step_names,
    _over_time_and_space, ZERO_IS_OFF, _zero_is_off,
)
from .interaction import (  # noqa: F401
    cleanup_lines, connector_code, _contact_property, CONTACTS, coupling_code, explicit_model, _faces_code,
    general_contact_code, interaction_code, interaction_plan,
)
from .assembly import (  # noqa: F401
    ALIGN_CALLS, _align_lines, instance_code, job_code, merge_code, _superseded,
)
from .collect import (  # noqa: F401
    collect, node_keys, picked_value,
)
from .show import (  # noqa: F401
    _display, _independent, module_for, MODULES, passed_on, _regions_on_assembly, _show_code, show_code, SHOW_RANK,
    _show_regions, SHOWN_AS, shown_node, shown_sketch, _step_name,
)

"""The node library. Each node declares typed ports, parameters and a run function (see core.py).

Nodes live in one module per area (data, field, maths, lists, loops, text, output, model/*); importing this
package registers all of them in REGISTRY, in palette order. Every name is re-exported here, so
``from nodecode_fea.nodes import ...`` works as it did when this was one file."""

from __future__ import annotations


# Every name the old single module had, for `from nodecode_fea.nodes import ...` elsewhere
from .core import (  # noqa: F401
    CATEGORIES, _fmt, FRAME_RANGE, _frames_in, MAX_CURVES, MODEL_VALUES, NodeDef, opt, Param, param_from_value,
    param_port_type, Port, register, REGISTRY, _require_scalar, STEP_HELP, _step_hints, _step_or_last,
)
from .common import (  # noqa: F401
    _as_list, as_set, _as_text, ASSEMBLY, entity_name, _finite, _flat_items, group_members, _is_collection,
    _is_matrix, _kind_name, _merged_set, _numbers_table, _region_model, _size,
)
from .data import (  # noqa: F401
    COMBINE_FRAMES, _combine_frames, _field_cache, _field_from, _field_output_hints, _field_row_keys, MAX_FILES,
    odb_field, _odb_files, odb_from_path, odb_history, odb_open, _odb_path, odb_set, _open_version, _point_word,
    PROBLEM_SETS, _read_field, _set_names, source_files,
)
from .field import (  # noqa: F401
    _axes_matrix, cae_pick, _check_invariant, convert_set, _curves, _custom_operand, CUSTOM_REST, EACH,
    field_component, _field_coordinates, field_expression, field_filter, field_filter_mask, field_from_set,
    field_invariant, field_over_frames, field_path, field_probe, field_quantity, field_reduce, field_time,
    _field_title, _LABEL_RE, _label_rows, math_reduce, _over_frames_hints, parse_labels, parse_points, _point_lists,
    _points_from, _probe_hints, _quantity, _quantity_hints, _quantity_label, _REDUCE_ALIASES, _reduce_any, REDUCE_OPS,
    _RUNNING, _running, _safe_set, SAMPLE_MODES, sample_path, _scalar_options, _set_rows, _transform_field,
)
from .maths import (  # noqa: F401
    _all, AS_TABLES, CAE_OBJECTS, _DIMENSION_WORDS, _field_operand, _field_result, _flatten, _formula_name,
    FORMULA_NAMES, _formula_once, _formula_operand, _formula_out, FORMULA_OUTPUTS, _formula_tables, _formula_values,
    INPUT_TYPES, input_value, _LEGACY_USE, _lined_up, math_expression, math_formula, math_number, _matrix, _named,
    _one, _row_extras, _script_outputs, _script_value, series_expression, series_reduce, series_xy, _special,
    _table_hint, _table_names, _table_out, _TENSOR_ORDER, _tidy, _typed_list, value_table, VALUES, ValueTable,
)
from .lists import (  # noqa: F401
    _CMP, _compare_one, COMPARE_OPS, _compare_values, field_where, _flatten_all, list_filter, list_item, list_length,
    list_merge, list_pick, list_reverse, list_slice, list_sort, list_split, list_stats, logic_compare, logic_gate,
    logic_if, logic_test, MAX_SPLIT, _member, _parse_slice, _plain, _region_mask, _sort_key, _take, TEST_OPS,
    TEXT_OPS, _truth, _value_of,
)
from .loops import (  # noqa: F401
    _curve_numbers, _curves_apart, data_properties, FOR_BY, input_bool, input_integer, list_range, list_to_series,
    loop_accumulate, loop_for, loop_foreach, MAX_RANGE, odb_frames, odb_steps, odb_timeline, _properties_display,
    _property_of, _property_text, _sequence, series_make,
)
from .text import (  # noqa: F401
    _broadcast, _FIELD_RE, _one_or_many, _TemplateFormatter, text_format, text_from, text_join, text_replace,
    text_split, text_test, text_to_number, text_value,
)
from .output import (  # noqa: F401
    _chart_display, _contour_display, _contour_markers, CSV_ROW_LIMIT, EDGES, _group_display, LABELLED_MARKERS,
    _limit, MAX_MARKERS, _member_markers, _odb_for, _region_boolean, _set_from_index, set_op, SET_OPS, SPECTRA,
    _table_display, _value_display, view_chart, view_contour, view_group, view_table, view_value, VIEWS,
)
from .model.geometry import (  # noqa: F401
    _abaqus_name, AXES, BODIES, _body3d, CAD_EXTENSIONS, DEFAULT_MODEL, DEFEATURE, EXTEND, FEATURE_OPERATIONS,
    _flat_points, _geometry_points, _graph_model, IMPORT_EXTENSIONS, _item, METHODS, _model3d, model_datum,
    model_feature, model_model, model_part, model_sketch, _model_value_display, _numbered, _profile_points,
    _reference_points, _same_geometry, SHAPE_FEATURES, SHAPES, _sketch_display, SKETCHED, SPLIT, SPACES, PART_TYPES,
)
from .model.region import (  # noqa: F401
    BY_SHAPE, MESH_MEASURES, model_measure, model_region, PER_ITEM, RADIUS_MATCH, _region_display, REGION_GROUPING,
    REGION_MEMBERS,
)
from .model.material import (  # noqa: F401
    BEHAVIOURS, _cae_material, ELEMENT_SHAPES, ELEMENT_TYPES, MESH_BIAS, MESH_SEEDS, MESH_SHAPES, MESH_TECHNIQUES,
    model_section, ORTHOTROPIC, OWN_MATERIAL, _plastic_table, _plies, ELEMENT_FAMILIES, HARDENINGS, _hardening,
    HEAT_ELEMENTS, SECTION_FOR,
)
from .model.mesh import (  # noqa: F401
    model_mesh,
)
from .model.step import (  # noqa: F401
    EIGEN_STEPS, INCREMENTED, model_step, _output_names, STEP_KINDS, THERMAL_STEPS, TIMED_STEPS,
)
from .model.loads import (  # noqa: F401
    _amplitude_curve, _amplitude_display, _amplitude_numbers, _amplitude_steps, _amplitude_table, _amplitude_value,
    _cell_text, _cells, _check_stepwise, _condition_atom, _condition_displacement, CONDITION_KINDS, _condition_number,
    CSYS_KINDS, FIXES, NO_AMPLITUDE, _region_part, SURFACE_KINDS, THERMAL_KINDS,
    _condition_setting, CONDITION_SETTINGS, _distribution, EXPRESSION_NAMES, IN_INITIAL, MAX_FIELD_POINTS,
    model_amplitude, model_condition, POINT_AMPLITUDES, SETTING_LABELS, SMOOTHED_AMPLITUDES, STARTED_AMPLITUDES,
    _step_table, STEP_TABLE_HELP, _stepwise_values, _table_columns, _validate_condition_values,
)
from .model.interaction import (  # noqa: F401
    _connector_end, CONNECTOR_FREE, CONNECTORS, COUPLINGS, DOF_NAMES, _dofs, _friction_steps, INTERACTION_KINDS,
    model_interaction, _stiffness,
)
from .model.job import (  # noqa: F401
    _job_action, _job_display, _job_file, _job_increments, JOB_RUN, JOB_SHOW, _job_spec, _job_text, _LEGACY_JOB_RUN,
    model_job,
)
from .model.assembly import (  # noqa: F401
    ALIGN_KINDS, _alignment, ALIGNMENTS, model_instance, model_merge_instances,
)
from .docs import (  # noqa: F401
    PORT_HELP,
)
from .previews import (  # noqa: F401
    _bools_table, default_display, _matrix_table, _model_list_display, summarize,
)
from .replaced import (  # noqa: F401
    REPLACED_BY,
)

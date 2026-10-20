"""Matrices in Formula: transpose, products, linear algebra, tensor fields as 3x3 matrices per row."""

import numpy as np
import pytest

from nodecode_fea import ops
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph


def formula(expr, inputs=(), edges=()):
    nodes = list(inputs) + [("fx", "math.formula", {"expr": expr}), ("sink", "list.length", {})]
    res, got = Engine().evaluate(graph(nodes, list(edges) + [("fx", "value", "sink", "list")]))
    assert res["fx"]["status"] == "ok", res["fx"]
    return got["sink"]["list"]


def values(m):
    return [[x.value for x in row] for row in m]


def test_matrix_input_transpose_and_product():
    m = [("m", "input.value", {"type": "List / matrix", "value": "[[1, 2], [3, 4]]"})]
    e = [("m", "value", "fx", "a")]
    assert values(formula("a.T", m, e)) == [[1, 3], [2, 4]]
    assert values(formula("a @ a.T", m, e)) == [[5, 11], [11, 25]]
    assert formula("det(a)", m, e).value == pytest.approx(-2)
    assert np.allclose(values(formula("np.linalg.inv(a) @ a", m, e)), np.eye(2))
    assert [x.value for x in formula("a[:, 0]", m, e)] == [1, 3]


def test_list_input():
    v = formula("np.sum(a)", [("m", "input.value", {"type": "List / matrix", "value": "1, 2, 3"})],
                [("m", "value", "fx", "a")])
    assert v.value == 6


def test_tensor_field_as_matrices():
    base = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"})]
    e = [("odb", "odb", "s", "odb"), ("s", "field", "fx", "a")]
    t = formula("a.T", base, e)
    assert t.kind == "TENSOR" and t.components[:3] == ["S11", "S22", "S33"]
    pressure = formula("-trace(a) / 3", base, e)
    p_ref = formula("a", base + [("q", "field.quantity", {"quantity": "Pressure"})],
                    [("odb", "odb", "s", "odb"), ("s", "field", "q", "field"), ("q", "field", "fx", "a")])
    assert np.allclose(pressure.values[:, 0], p_ref.values[:, 0])
    top = formula("eigvalsh(a)[:, -1]", base, e)
    assert top.values.shape[1] == 1


def test_strain_shear_stays_engineering():
    """E comes in as a tensor (shear halved) and goes back out with Abaqus' engineering shear: a.T == E."""
    base = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "E"})]
    e = [("odb", "odb", "s", "odb"), ("s", "field", "fx", "a")]
    same = formula("a.T", base, e)
    _, got = Engine().evaluate(graph(base + [("sink", "list.length", {})], e[:1] + [("s", "field", "sink", "list")]))
    orig = got["sink"]["list"]
    for k, c in enumerate(same.components):
        assert np.allclose(same.values[:, k], orig.values[:, orig.components.index(c)]), c


def test_tuple_indexing_and_matmul_operator():
    assert ops.evaluate("a[:, 1]", {"a": np.arange(6).reshape(2, 3)}).tolist() == [1, 4]
    assert ops.evaluate("a @ b", {"a": np.eye(2), "b": np.array([1.0, 2.0])}).tolist() == [1.0, 2.0]

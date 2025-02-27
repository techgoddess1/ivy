# global
from hypothesis import strategies as st, given, assume
import numpy as np
import tensorflow as tf

# local
import ivy
import ivy_tests.test_ivy.helpers as helpers
from ivy_tests.test_ivy.helpers import handle_frontend_method, BackendHandler
from ivy_tests.test_ivy.test_frontends.test_tensorflow.test_raw_ops import (
    _pow_helper_shared_dtype,
)
from ivy.functional.frontends.tensorflow import EagerTensor

CLASS_TREE = "ivy.functional.frontends.tensorflow.EagerTensor"


# --- Helpers --- #
# --------------- #


@st.composite
def _array_and_shape(
    draw,
    *,
    min_num_dims=1,
    max_num_dims=3,
    min_dim_size=1,
    max_dim_size=10,
):
    if isinstance(min_dim_size, st._internal.SearchStrategy):
        min_dim_size = draw(min_dim_size)
    if isinstance(max_dim_size, st._internal.SearchStrategy):
        max_dim_size = draw(max_dim_size)

    available_dtypes = draw(helpers.get_dtypes("numeric"))
    dtype = draw(
        helpers.array_dtypes(
            num_arrays=1,
            available_dtypes=available_dtypes,
        )
    )
    dtype.append("int32")
    shape = draw(
        st.shared(
            helpers.get_shape(
                min_num_dims=min_num_dims,
                max_num_dims=max_num_dims,
                min_dim_size=min_dim_size,
                max_dim_size=max_dim_size,
            ),
            key="shape",
        )
    )
    array = draw(
        helpers.array_values(
            dtype=dtype[0],
            shape=shape,
        )
    )
    to_shape = [(None if draw(st.booleans()) else _) for _ in shape]

    return dtype, [array, to_shape]


# same implementation as in tensorflow backend but was causing backend conflict issues
def _check_query(query):
    return not isinstance(query, list) and (
        not (ivy.is_array(query) and ivy.is_bool_dtype(query) ^ bool(query.ndim > 0))
    )


def _helper_init_tensorarray(backend_fw, l_kwargs, fn=None):
    id_write, kwargs = l_kwargs
    with BackendHandler.update_backend(backend_fw) as ivy_backend:
        local_importer = ivy_backend.utils.dynamic_import
        tf_frontend = local_importer.import_module(
            "ivy.functional.frontends.tensorflow"
        )
        ta = tf_frontend.tensor.TensorArray(**kwargs)
        ta_gt = tf.TensorArray(**kwargs)
        if fn == "unstack":
            ta_gt = ta_gt.unstack(tf.constant(id_write))
            ta = ta.unstack(tf_frontend.constant(id_write))
        elif fn == "split":
            ta_gt = ta_gt.split(**id_write)
            ta = ta.split(**id_write)
        elif fn == "scatter":
            indices, value = [*zip(*id_write)]
            ta_gt = ta_gt.scatter(indices, tf.cast(tf.stack(value), dtype=ta_gt.dtype))
            value = tf_frontend.stack(list(map(tf_frontend.constant, value)))
            ta = ta.scatter(indices, tf_frontend.cast(value, ta.dtype))
        else:
            for id, write in id_write:
                ta_gt = ta_gt.write(id, tf.constant(write))
                ta = ta.write(id, tf_frontend.constant(write))
    return ta_gt, ta


@st.composite
def _helper_random_tensorarray(draw, fn=None):
    size = draw(st.integers(1, 10))
    dynamic_size = draw(st.booleans())
    clear_after_read = draw(st.booleans())
    infer_shape = draw(st.booleans())
    element_shape = draw(helpers.get_shape())
    element_shape = draw(st.one_of(st.just(None), st.just(element_shape)))
    shape = None
    if (
        infer_shape
        or element_shape is not None
        or fn in ["scatter", "stack", "gather", "concat"]
    ):
        if fn == "concat":
            element_shape = None
            infer_shape = False
            shape = list(draw(helpers.get_shape(min_num_dims=1)))
        elif element_shape is None:
            shape = draw(helpers.get_shape())
        else:
            shape = element_shape
    dtype = draw(helpers.get_dtypes(full=False, prune_function=False))[0]
    if fn in ["stack", "concat"]:
        ids_to_write = [True for i in range(size)]
    else:
        ids_to_write = [draw(st.booleans()) for i in range(size)]
        if sum(ids_to_write) == 0:
            ids_to_write[draw(st.integers(0, size - 1))] = True
    kwargs = {
        "dtype": dtype,
        "size": size,
        "dynamic_size": dynamic_size,
        "clear_after_read": clear_after_read,
        "infer_shape": infer_shape,
        "element_shape": element_shape,
    }
    id_write = []
    for id, flag in enumerate(ids_to_write):
        if fn == "concat":
            shape[0] = draw(st.integers(1, 10))
        if flag:
            write = np.array(
                draw(
                    helpers.array_values(
                        dtype=dtype,
                        shape=shape if shape is not None else helpers.get_shape(),
                    )
                )
            )
            id_write.append((id, write))
    if fn != "gather":
        return id_write, kwargs
    else:
        ids = []
        for id, _ in id_write:
            if draw(st.booleans()):
                ids.append(id)
        if not ids:
            ids.append(id)
        return id_write, kwargs, ids


@st.composite
def _helper_tensorarray_split(draw):
    shape = draw(helpers.get_shape(min_num_dims=1))
    dtype = draw(helpers.get_dtypes(full=False, prune_function=False))[0]
    value = draw(helpers.array_values(dtype=dtype, shape=shape))
    dynamic_size = draw(st.booleans())
    if dynamic_size:
        size = draw(st.integers(1, shape[0] + 5))
    else:
        size = shape[0]
    total = 0
    length = []
    for i in range(shape[0]):
        length.append(draw(st.integers(0, shape[0] - total)))
        total += length[-1]
    if total != shape[0]:
        length[-1] += shape[0] - total
    return {"value": value, "lengths": length}, {
        "dtype": dtype,
        "size": size,
        "dynamic_size": dynamic_size,
    }


@st.composite
def _helper_tensorarray_unstack(draw):
    shape = draw(helpers.get_shape(min_num_dims=1))
    size = draw(st.integers(1, 10))
    dynamic_size = draw(st.booleans()) if size >= shape[0] else True
    dtype = draw(helpers.get_dtypes(full=False, prune_function=False))[0]
    tensor = draw(helpers.array_values(dtype=dtype, shape=shape))
    kwargs = {"dtype": dtype, "size": size, "dynamic_size": dynamic_size}
    return tensor, kwargs


# --- Main --- #
# ------------ #


# __add__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__add__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__add__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __and__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__and__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__and__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __array__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__array__",
    dtype_and_x=helpers.dtype_and_values(available_dtypes=helpers.get_dtypes("valid")),
    dtype=helpers.get_dtypes("valid", full=False),
)
def test_tensorflow__array__(
    dtype_and_x,
    dtype,
    frontend,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    dtype[0] = np.dtype(dtype[0])
    ret_gt = tf.constant(x[0]).__array__(dtype[0])
    with BackendHandler.update_backend(backend_fw) as ivy_backend:
        local_importer = ivy_backend.utils.dynamic_import
        function_module = local_importer.import_module(
            "ivy.functional.frontends.tensorflow"
        )
        ret = function_module.constant(x[0]).__array__(dtype[0])
    helpers.value_test(
        ret_np_flat=ret.ravel(),
        ret_np_from_gt_flat=ret_gt.ravel(),
        ground_truth_backend="tensorflow",
        backend=backend_fw,
    )


# __bool__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__bool__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        max_dim_size=1,
    ),
)
def test_tensorflow__bool__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__div__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=10,
        small_abs_safety_factor=10,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__div__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    assume(not np.any(np.isclose(x[0], 0)))
    assume(not np.any(np.isclose(x[1], 0)))
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__eq__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("float"),
        num_arrays=2,
    ),
)
def test_tensorflow__eq__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "other": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__floordiv__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("float"),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=10,
        small_abs_safety_factor=10,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__floordiv__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __ge__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__ge__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__ge__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __getitem__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__getitem__",
    dtype_x_index=helpers.dtype_array_query(
        available_dtypes=helpers.get_dtypes("valid"),
    ).filter(
        lambda x: (
            all(_check_query(i) for i in x[-1])
            if isinstance(x[-1], tuple)
            else _check_query(x[-1])
        )
    ),
)
def test_tensorflow__getitem__(
    dtype_x_index,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x, index = dtype_x_index
    helpers.test_frontend_method(
        init_input_dtypes=[input_dtype[0]],
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={"value": x},
        method_input_dtypes=[*input_dtype[1:]],
        method_all_as_kwargs_np={"slice_spec": index},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __gt__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__gt__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__gt__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __invert__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__invert__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer")
    ),
)
def test_tensorflow__invert__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __le__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__le__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__le__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __len__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__len__",
    dtype_and_x=_array_and_shape(
        min_num_dims=1,
        max_num_dims=5,
    ),
)
def test_tensorflow__len__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __lt__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__lt__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__lt__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __matmul__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__matmul__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=[
            "float32",
            "float64",
            "int32",
            "int64",
        ],
        shape=(3, 3),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=10,
        small_abs_safety_factor=10,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__matmul__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __mod__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__mod__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__mod__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    assume(not np.any(np.isclose(x[0], 0)))
    assume(not np.any(np.isclose(x[1], 0)))
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__mul__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__mul__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __ne__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__ne__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__ne__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "other": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __neg__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__neg__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=[
            "float32",
            "float64",
            "int8",
            "int16",
            "int32",
            "int64",
        ],
    ),
)
def test_tensorflow__neg__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __nonzero__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__nonzero__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        max_dim_size=1,
    ),
)
def test_tensorflow__nonzero__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __or__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__or__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__or__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __pow__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__pow__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=[
            "float16",
            "float32",
            "float64",
            "int32",
            "int64",
        ],
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__pow__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    if x[1].dtype == "int32" or x[1].dtype == "int64":
        if x[1].ndim == 0:
            if x[1] < 0:
                x[1] *= -1
        else:
            x[1][(x[1] < 0).nonzero()] *= -1

    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __radd__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__radd__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__radd__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rand__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rand__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__rand__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rfloordiv__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rfloordiv__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("float"),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=10,
        small_abs_safety_factor=10,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__rfloordiv__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rmatmul__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rmatmul__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=[
            "float32",
            "float64",
            "int32",
            "int64",
        ],
        shape=(3, 3),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=10,
        small_abs_safety_factor=10,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__rmatmul__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rmul__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rmul__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
        min_value=-100,
        max_value=100,
    ),
)
def test_tensorflow__rmul__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __ror__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__ror__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__ror__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rpow__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rpow__",
    dtype_and_x=_pow_helper_shared_dtype(),
)
def test_tensorflow__rpow__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rsub__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rsub__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__rsub__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rtruediv__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rtruediv__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=15,
        small_abs_safety_factor=15,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__rtruediv__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __rxor__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__rxor__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__rxor__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "x": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __sub__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__sub__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__sub__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __truediv__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__truediv__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("numeric"),
        num_arrays=2,
        shared_dtype=True,
        large_abs_safety_factor=15,
        small_abs_safety_factor=15,
        safety_factor_scale="log",
    ),
)
def test_tensorflow__truediv__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    assume(not np.any(np.isclose(x[1], 0)))
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


# __xor__
@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="__xor__",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("integer"),
        num_arrays=2,
        shared_dtype=True,
    ),
)
def test_tensorflow__xor__(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={
            "y": x[1],
        },
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@given(
    dtype_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("valid", prune_function=False)
    ),
)
def test_tensorflow_tensor_device(
    dtype_x,
    backend_fw,
):
    ivy.set_backend(backend_fw)
    _, data = dtype_x
    data = ivy.native_array(data[0])
    x = EagerTensor(data)
    ivy.utils.assertions.check_equal(x.device, ivy.dev(data), as_array=False)
    ivy.previous_backend()


@given(
    dtype_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("valid", prune_function=False),
    ),
)
def test_tensorflow_tensor_dtype(
    dtype_x,
    backend_fw,
):
    ivy.set_backend(backend_fw)
    dtype, data = dtype_x
    x = EagerTensor(data[0])
    ivy.utils.assertions.check_equal(x.dtype, ivy.Dtype(dtype[0]), as_array=False)
    ivy.previous_backend()


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="get_shape",
    dtype_and_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("valid"),
        min_num_dims=1,
        min_dim_size=1,
    ),
)
def test_tensorflow_tensor_get_shape(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    on_device,
    backend_fw,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=input_dtype,
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={
            "value": x[0],
        },
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@given(
    dtype_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("valid", prune_function=False)
    ),
)
def test_tensorflow_tensor_ivy_array(
    dtype_x,
    backend_fw,
):
    ivy.set_backend(backend_fw)
    _, data = dtype_x
    x = EagerTensor(data[0])
    ret = helpers.flatten_and_to_np(ret=x.ivy_array.data, backend=backend_fw)
    ret_gt = helpers.flatten_and_to_np(ret=data[0], backend=backend_fw)
    helpers.value_test(
        ret_np_flat=ret,
        ret_np_from_gt_flat=ret_gt,
        ground_truth_backend="tensorflow",
    )
    ivy.previous_backend()


@handle_frontend_method(
    class_tree=CLASS_TREE,
    init_tree="tensorflow.constant",
    method_name="set_shape",
    dtype_and_x=_array_and_shape(
        min_num_dims=0,
        max_num_dims=5,
    ),
)
def test_tensorflow_tensor_set_shape(
    dtype_and_x,
    frontend,
    frontend_method_data,
    init_flags,
    method_flags,
    backend_fw,
    on_device,
):
    input_dtype, x = dtype_and_x
    helpers.test_frontend_method(
        init_input_dtypes=[input_dtype[0]],
        backend_to_test=backend_fw,
        init_all_as_kwargs_np={"value": x[0]},
        method_input_dtypes=input_dtype,
        method_all_as_kwargs_np={"shape": x[1]},
        frontend=frontend,
        frontend_method_data=frontend_method_data,
        init_flags=init_flags,
        method_flags=method_flags,
        on_device=on_device,
    )


@given(
    dtype_x=helpers.dtype_and_values(
        available_dtypes=helpers.get_dtypes("valid", prune_function=False),
        ret_shape=True,
    ),
)
def test_tensorflow_tensor_shape(
    dtype_x,
    backend_fw,
):
    ivy.set_backend(backend_fw)
    dtype, data, shape = dtype_x
    x = EagerTensor(data[0])
    ivy.utils.assertions.check_equal(
        x.ivy_array.shape, ivy.Shape(shape), as_array=False
    )
    ivy.previous_backend()


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_close(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    ta.close()
    ta_gt.close()
    assert np.array(ta.size()) == 0
    assert np.array(ta_gt.size()) == 0


@given(l_kwargs=_helper_random_tensorarray(fn="concat"))
def test_tensorflow_tensorarray_concat(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    helpers.value_test(
        ret_np_from_gt_flat=ta_gt.concat().numpy().flatten(),
        ret_np_flat=np.array(ta.concat()).flatten(),
        backend=backend_fw,
    )


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_dtype(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    assert ta_gt.dtype == ta.dtype.ivy_dtype


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_dynamic_size(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    assert ta_gt.dynamic_size == ta.dynamic_size


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_element_shape(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    assert ta_gt.element_shape == ta.element_shape


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_flow(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    assert ta_gt.flow == ta.flow


@given(l_kwargs=_helper_random_tensorarray(fn="gather"))
def test_tensorflow_tensorarray_gather(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs[:2])
    *_, indices = l_kwargs
    helpers.value_test(
        ret_np_from_gt_flat=ta_gt.gather(indices).numpy().flatten(),
        ret_np_flat=np.array(ta.gather(indices)).flatten(),
        backend=backend_fw,
    )


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_handle(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    assert ta_gt.handle == ta.handle


# test for read and write methods
@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_read(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    id_read, _ = l_kwargs
    for id, read in id_read:
        helpers.value_test(
            ret_np_from_gt_flat=ta_gt.read(id).numpy().flatten(),
            ret_np_flat=np.array(ta.read(id)).flatten(),
            backend=backend_fw,
        )


@given(l_kwargs=_helper_random_tensorarray(fn="scatter"))
def test_tensorflow_tensorarray_scatter(
    l_kwargs,
    backend_fw,
):
    id_read, _ = l_kwargs
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs, "scatter")
    for id, read in id_read:
        helpers.value_test(
            ret_np_from_gt_flat=ta_gt.read(id).numpy().flatten(),
            ret_np_flat=np.array(ta.read(id)).flatten(),
            backend=backend_fw,
        )


@given(l_kwargs=_helper_random_tensorarray())
def test_tensorflow_tensorarray_size(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    helpers.value_test(
        ret_np_from_gt_flat=ta_gt.size().numpy().flatten(),
        ret_np_flat=np.array(ta.size()).flatten(),
        backend=backend_fw,
    )


@given(
    kwargs_v_l=_helper_tensorarray_split(),
)
def test_tensorflow_tensorarray_split(kwargs_v_l, backend_fw):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, kwargs_v_l, "split")
    for id in range(ta_gt.size()):
        helpers.value_test(
            ret_np_from_gt_flat=ta_gt.read(id).numpy().flatten(),
            ret_np_flat=np.array(ta.read(id)).flatten(),
            backend=backend_fw,
        )


@given(l_kwargs=_helper_random_tensorarray(fn="stack"))
def test_tensorflow_tensorarray_stack(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs)
    helpers.value_test(
        ret_np_from_gt_flat=ta_gt.stack().numpy().flatten(),
        ret_np_flat=np.array(ta.stack()).flatten(),
        backend=backend_fw,
    )


@given(l_kwargs=_helper_tensorarray_unstack())
def test_tensorflow_tensorarray_unstack(
    l_kwargs,
    backend_fw,
):
    ta_gt, ta = _helper_init_tensorarray(backend_fw, l_kwargs, "unstack")
    helpers.value_test(
        ret_np_from_gt_flat=ta_gt.stack().numpy().flatten(),
        ret_np_flat=np.array(ta.stack()).flatten(),
        backend=backend_fw,
    )

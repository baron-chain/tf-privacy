# Copyright 2022, The TensorFlow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from absl.testing import parameterized
import tensorflow as tf
from tensorflow_privacy.privacy.fast_gradient_clipping import clip_grads
from tensorflow_privacy.privacy.fast_gradient_clipping import common_test_utils
from tensorflow_privacy.privacy.fast_gradient_clipping import gradient_clipping_utils
from tensorflow_privacy.privacy.fast_gradient_clipping import layer_registry
from tensorflow_privacy.privacy.fast_gradient_clipping import type_aliases


class DoubleDense(tf.keras.layers.Layer):
  """Generates two dense layers nested together."""

  def __init__(self, units: int):
    super().__init__()
    self.dense1 = tf.keras.layers.Dense(units)
    self.dense2 = tf.keras.layers.Dense(1)

  def call(self, inputs: Any):
    x = self.dense1(inputs)
    return self.dense2(x)


def double_dense_layer_computation(
    layer_instance: tf.keras.layers.Layer,
    input_args: Sequence[Any],
    input_kwargs: Mapping[str, Any],
    tape: tf.GradientTape,
    num_microbatches: Optional[int],
) -> type_aliases.RegistryFunctionOutput:
  """Layer registry function for the custom `DoubleDense` layer class."""
  vars1, outputs, sqr_norm_fn1 = layer_registry.dense_layer_computation(
      layer_instance.dense1, input_args, input_kwargs, tape, num_microbatches
  )
  vars2, outputs, sqr_norm_fn2 = layer_registry.dense_layer_computation(
      layer_instance.dense2, (outputs,), {}, tape, num_microbatches
  )

  def sqr_norm_fn(base_vars):
    norms1 = sqr_norm_fn1(base_vars[0])
    norms2 = sqr_norm_fn2(base_vars[1])
    return norms1 + norms2

  return [vars1, vars2], outputs, sqr_norm_fn


class DirectWeightsTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.product(
      input_dim=[1, 2], clip_value=[1e-6, 0.5, 1.0, 2.0, 10.0, 1e6]
  )
  def test_clip_weights(self, input_dim, clip_value):
    tol = 1e-6
    ts, _ = common_test_utils.get_nd_test_batches(input_dim)
    for t in ts:
      weights = clip_grads.compute_clip_weights(clip_value, t)
      self.assertAllLessEqual(t * weights, clip_value + tol)

  def test_clip_weights_none(self):
    self.assertIsNone(clip_grads.compute_clip_weights(None, tf.ones(3)))


class CustomLayerTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.product(
      input_dim=[3],
      output_dim=[2],
      per_example_loss_fn=[None, common_test_utils.test_loss_fn],
      num_microbatches=[None, 2],
      is_eager=[True, False],
      partial=[True, False],
      weighted=[True, False],
  )
  def test_gradient_norms_on_various_models(
      self,
      input_dim,
      output_dim,
      per_example_loss_fn,
      num_microbatches,
      is_eager,
      partial,
      weighted,
  ):
    registry = layer_registry.make_default_layer_registry()
    registry.insert(DoubleDense, double_dense_layer_computation)
    x_batches, weight_batches = common_test_utils.get_nd_test_batches(input_dim)
    for x_batch, weight_batch in zip(x_batches, weight_batches):
      batch_size = x_batch.shape[0]
      if num_microbatches is not None and batch_size % num_microbatches != 0:
        continue
      (computed_norms, true_norms) = (
          common_test_utils.get_computed_and_true_norms(
              model_generator=common_test_utils.make_two_layer_functional_model,
              layer_generator=lambda a, b: DoubleDense(*b),
              input_dims=[input_dim],
              output_dims=[output_dim],
              per_example_loss_fn=per_example_loss_fn,
              num_microbatches=num_microbatches,
              is_eager=is_eager,
              x_batch=x_batch,
              weight_batch=weight_batch if weighted else None,
              registry=registry,
              partial=partial,
          )
      )
      self.assertEqual(computed_norms.shape[0], num_microbatches or batch_size)
      self.assertAllClose(computed_norms, true_norms, rtol=1e-3, atol=1e-2)


def _run_model_forward_backward_pass(
    model: tf.keras.Model,
    x_batch: type_aliases.InputTensors,
    y_batch: type_aliases.OutputTensors,
):
  tape = tf.GradientTape(persistent=True, watch_accessed_variables=False)
  registry_generator_fn = gradient_clipping_utils.get_registry_generator_fn(
      tape=tape,
      layer_registry=layer_registry.make_default_layer_registry(),
      num_microbatches=None,
  )
  layer_grad_vars, registry_fn_outputs_list = (
      gradient_clipping_utils.model_forward_backward_pass(
          tape=tape,
          input_model=model,
          x_batch=x_batch,
          y_batch=y_batch,
          registry_generator_fn=registry_generator_fn,
      )
  )
  return layer_grad_vars, registry_fn_outputs_list


class ComputeClippedGradsAndOutputsTest(
    tf.test.TestCase, parameterized.TestCase
):

  def setUp(self):
    super().setUp()
    dense_generator = lambda a, b: tf.keras.layers.Dense(b)
    self._input_dim = 2
    self._output_dim = 3
    self._model = common_test_utils.make_two_layer_functional_model(
        dense_generator, self._input_dim, self._output_dim
    )

  @parameterized.product(
      batch_size=[1, 2, 10],
      l2_norm_clip=[0.1, 1.0, 10],
      is_eager=[True, False],
      reduction=['auto', 'sum', 'sum_over_batch_size', 'none'],
  )
  def test_clipped_gradients_on_different_losses(
      self, batch_size, l2_norm_clip, is_eager, reduction
  ):
    loss_fn = tf.keras.losses.MeanSquaredError(reduction=reduction)
    self._model.compile(loss=loss_fn, run_eagerly=is_eager)
    x_batch = tf.reshape(
        tf.range(batch_size * self._input_dim, dtype=tf.float32),
        [batch_size, -1],
    )
    y_batch = tf.reshape(
        1.0 + tf.range(batch_size, dtype=tf.float32), [batch_size, -1]
    )
    layer_grad_vars, registry_fn_outputs_list = (
        _run_model_forward_backward_pass(self._model, x_batch, y_batch)
    )
    # Stop early for efficiency.
    if reduction == 'none':
      with self.assertRaises(NotImplementedError):
        clip_grads.compute_clipped_gradients_and_outputs(
            self._model,
            registry_fn_outputs_list,
            layer_grad_vars,
            l2_norm_clip,
            x_batch,
            y_batch,
        )
      return
    # NOTE: losses from this point are scalar losses.
    with tf.GradientTape() as tape:
      y_pred = self._model(x_batch)
      loss_value = loss_fn(y_pred, y_batch)
    true_grads = tape.gradient(loss_value, self._model.trainable_variables)

    clipped_grads, _, _ = clip_grads.compute_clipped_gradients_and_outputs(
        self._model,
        registry_fn_outputs_list,
        layer_grad_vars,
        l2_norm_clip,
        x_batch,
        y_batch,
    )

    # Computes the L2 norm manually.
    def compute_l2_norm(t):
      sqr_sum_fn = lambda x: tf.reduce_sum(tf.square(x))
      return tf.sqrt(tf.add_n(tf.nest.map_structure(sqr_sum_fn, t)))

    true_norm = compute_l2_norm(true_grads)
    computed_norm = compute_l2_norm(clipped_grads)
    norm_bound = (
        l2_norm_clip * batch_size if reduction == 'sum' else l2_norm_clip
    )
    if true_norm >= norm_bound:
      # All of the per-example gradient norms should be less than the L2 norm
      # clip value. Hence, by the triangle inequality, the gradient norm of the
      # summed loss (averaged loss) should be less than the clip value times
      # the batch size (just the clip value).
      self.assertLessEqual(computed_norm, norm_bound)
    else:
      self.assertAlmostEqual(computed_norm, true_norm)


class SharedLayerTest(tf.test.TestCase, parameterized.TestCase):

  def _make_shared_model(self, num_inputs, input_dim):
    base_model = tf.keras.Sequential([tf.keras.layers.Dense(1, use_bias=False)])
    inputs = []
    outputs = []
    for _ in range(num_inputs):
      input_tensor = tf.keras.Input(shape=[input_dim])
      inputs.append(input_tensor)
      output_tensor = base_model(input_tensor)
      outputs.append(output_tensor)
    return tf.keras.Model(inputs=inputs, outputs=tf.add_n(outputs))

  def _get_computed_and_true_norms(self, model, x_batch, y_batch, is_eager):
    model.compile(
        loss=tf.keras.losses.MeanSquaredError(reduction='none'),
        run_eagerly=is_eager,
    )
    computed_norms = clip_grads.compute_gradient_norms(
        model, layer_registry.make_default_layer_registry(), x_batch, y_batch
    )
    with tf.GradientTape() as tape:
      y_pred = model(x_batch)
      loss_value = model.loss(y_pred, y_batch)
    true_grads = tape.jacobian(loss_value, model.trainable_variables)
    true_norms = tf.sqrt(
        tf.add_n([tf.reduce_sum(tf.square(g), axis=[1, 2]) for g in true_grads])
    )
    return computed_norms, true_norms

  @parameterized.product(
      num_inputs=[1, 2, 10],
      batch_size=[1, 2],
      input_dim=[1, 3],
      is_eager=[True, False],
  )
  def test_gradient_norms_on_multiple_inputs_are_upper_bounded(
      self, num_inputs, batch_size, input_dim, is_eager
  ):
    model = self._make_shared_model(num_inputs, input_dim)
    model.compile(
        loss=tf.keras.losses.MeanSquaredError(reduction='none'),
        run_eagerly=is_eager,
    )
    x_batch = [
        float(k + 1) * tf.ones([batch_size, input_dim], dtype=tf.float64)
        for k in range(num_inputs)
    ]
    y_batch = tf.reshape(
        1.0 + tf.range(batch_size, dtype=tf.float32), [batch_size, -1]
    )
    computed_norms, true_norms = self._get_computed_and_true_norms(
        model, x_batch, y_batch, is_eager
    )
    self.assertAllLessEqual(true_norms - computed_norms, 1e-3)

  @parameterized.product(
      num_repeats=[1, 2, 10],
      batch_size=[1, 2],
      input_dim=[1, 3],
      is_eager=[True, False],
  )
  def test_gradient_norms_on_single_repeated_input_are_upper_bounded(
      self, num_repeats, batch_size, input_dim, is_eager
  ):
    base_model = tf.keras.Sequential([tf.keras.layers.Dense(1, use_bias=False)])
    inputs = tf.keras.layers.Input([input_dim])
    outputs = tf.add_n([base_model(inputs) for _ in range(num_repeats)])
    model = tf.keras.Model(inputs=inputs, outputs=outputs)
    x_batch = tf.ones([batch_size, input_dim], dtype=tf.float64)
    y_batch = tf.reshape(
        1.0 + tf.range(batch_size, dtype=tf.float32), [batch_size, -1]
    )
    computed_norms, true_norms = self._get_computed_and_true_norms(
        model, x_batch, y_batch, is_eager
    )
    self.assertAllLessEqual(true_norms - computed_norms, 1e-3)

  @parameterized.product(
      batch_size=[1, 2],
      input_dim=[1, 3],
      is_eager=[True, False],
  )
  def test_gradient_norms_on_input_slices_are_upper_bounded(
      self, batch_size, input_dim, is_eager
  ):
    base_model = tf.keras.Sequential([tf.keras.layers.Dense(1, use_bias=False)])
    inputs = tf.keras.layers.Input([input_dim, 2])
    outputs = base_model(inputs[:, :, 0]) + base_model(inputs[:, :, 1])
    model = tf.keras.Model(inputs=inputs, outputs=outputs)
    x_batch = tf.reshape(
        tf.range(batch_size * input_dim * 2, dtype=tf.float64),
        [batch_size, input_dim, -1],
    )
    y_batch = tf.reshape(
        1.0 + tf.range(batch_size, dtype=tf.float32), [batch_size, -1]
    )
    computed_norms, true_norms = self._get_computed_and_true_norms(
        model, x_batch, y_batch, is_eager
    )
    self.assertAllLessEqual(true_norms - computed_norms, 1e-3)


if __name__ == '__main__':
  tf.test.main()

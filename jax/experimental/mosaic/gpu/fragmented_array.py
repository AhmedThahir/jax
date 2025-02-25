# Copyright 2024 The JAX Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Utilities for code generator."""

from __future__ import annotations

import dataclasses
import functools
import math
from typing import Iterable, Sequence, TypeVar

import jax
from jaxlib.mlir import ir
from jaxlib.mlir.dialects import arith
from jaxlib.mlir.dialects import gpu
from jaxlib.mlir.dialects import llvm
from jaxlib.mlir.dialects import math as mlir_math
from jaxlib.mlir.dialects import memref
from jaxlib.mlir.dialects import nvvm
from jaxlib.mlir.dialects import vector
import numpy as np

import jax.experimental.mosaic.gpu as mgpu
from . import utils

# mypy: ignore-errors

T = TypeVar("T")
WARPGROUP_SIZE = utils.WARPGROUP_SIZE
WARP_SIZE = 32
WARPS_IN_WARPGROUP = WARPGROUP_SIZE // WARP_SIZE
c = utils.c


@dataclasses.dataclass(frozen=True)
class Tiling:
  """A tiling expression describing a permutation of elements of an nd-array.

  To apply one level of tiling to an array, each of the trailing dimensions (up
  to the rank of the tile) is unfolded into two dimensions: first equal to the
  ratio of the dimension size and the tile size, and second equal to the tile
  size. Then, all newly unfolded minor dimensions are transposed to appear at
  the end.

  This expression describes multi-level tiling, by applying each element of
  `tiles` in sequence to the array.

  See https://openxla.org/xla/tiled_layout for a more detailed explanation.
  """
  tiles: tuple[tuple[int, ...], ...]

  def __post_init__(self):
    max_rank = math.inf
    for tile in self.tiles:
      if not tile:
        raise ValueError("Tiles must not be empty")
      if len(tile) > max_rank:
        raise ValueError("Tile ranks must be non-increasing")
      max_rank = len(tile)
      if any(d <= 0 for d in tile):
        raise ValueError(f"Tile shape must only have positive sizes, got: {self.tiles}")

  def __str__(self):
    return f"Tiling({''.join(map(str, self.tiles))})"

  def tile_shape(self, shape: tuple[int, ...]) -> tuple[int, ...]:
    """Computes the shape of an array after tiling."""
    def fail():
      raise ValueError(f"Tiling {self.tiles} does not apply to shape {shape}")
    for tile in self.tiles:
      if len(tile) > len(shape):
        fail()
      untiled_dims, tiled_dims = shape[:-len(tile)], shape[-len(tile):]
      if any(s % t != 0 for s, t in zip(tiled_dims, tile)):
        fail()
      shape = (*untiled_dims, *(d // t for d, t in zip(tiled_dims, tile)), *tile)
    return shape

  def untile_shape(self, shape: tuple[int, ...]) -> tuple[int, ...]:
    """Computes the shape of an array before tiling from its tiled shape."""
    def fail():
      raise ValueError("Shape does not look like it's been tiled?")
    for tile in reversed(self.tiles):
      if len(tile) > len(shape):
        fail()
      untiled_dims = shape[:-2 * len(tile)]
      tiled_dims = shape[-2 * len(tile):-len(tile)]
      tiling_dims = shape[-len(tile):]
      if tiling_dims != tile:
        fail()
      shape = (*untiled_dims, *(d * t for d, t in zip(tiled_dims, tile)))
    return shape

  def tile_strides(self, strides: tuple[int, ...]) -> tuple[int, ...]:
    """Computes the strides of an array after tiling."""
    for tile in self.tiles:
      untiled, tiled = strides[:-len(tile)], strides[-len(tile):]
      strides = (*untiled, *(s * t for s, t in zip(tiled, tile)), *tiled)
    return strides

  def tile_indices(self, indices: tuple[int, ...]) -> tuple[int, ...]:
    for tile in self.tiles:
      untiled, tiled = indices[:-len(tile)], indices[-len(tile):]
      indices = (
          *untiled,
          *(i // t for i, t in zip(tiled, tile)),
          *(i % t for i, t in zip(tiled, tile)),
      )
    return indices

  def untile_indices(self, indices: tuple[int, ...]) -> tuple[int, ...]:
    for tile in reversed(self.tiles):
      untiled = indices[:-2 * len(tile)]
      outer = indices[-2 * len(tile):-len(tile)]
      inner = indices[-len(tile):]
      indices = (*untiled, *(o * t + i for o, i, t in zip(outer, inner, tile)))
    return indices

def enumerate_negative(elems: Sequence[T]) -> Iterable[tuple[int, T]]:
  """Like built-in enumerate, but returns negative indices into the sequence."""
  offset = len(elems)
  for i, e in enumerate(elems):
    yield i - offset, e


@dataclasses.dataclass(frozen=True)
class TiledLayout:
  """A FragmentedArray layout derived from a tiling expression.

  A logical array is transformed according to the tiling expression, and then
  split across warps (within a warpgroup), lanes, and vectorized according to
  the dimension indices. All dimension indices must be negative and should refer
  to the dimensions after tiling is applied.

  Note that warp_dim and vector_dim could be sets as well, but we don't have a
  usecase for that yet.

  To better understand this layout, consider the example of WGMMA-related tiling
  from https://docs.nvidia.com/cuda/parallel-thread-execution/#wgmma-64n16-d as
  applied to a 128x128 array. The corresponding TiledLayout has a tiling of:

      (64, 8)(16, 8)(8, 8)(1, 2)

  and warp_dim=-8, lane_dims={-4, -3}, vector_dim=-1.

  We begin by applying the tiling (note that it always applies to a suffix):

          Tiled shape                       Remaining tiling actions
  ===========================================================================
  128 128                                  (64, 8)(16, 8)(8, 8)(1, 2)
    2  16  64  8                           (16, 8)(8, 8)(1, 2)
    2  16   4  1  16  8                    (8, 8)(1, 2)
    2  16   4  1   2  1  8  8              (1, 2)
    2  16   4  1   2  1  8  4  1  2

  The last expression is our final shape. At this stage, we're ready to
  interpret the dimensions: warp_dim=-8 means that the 8-th dimension from the
  end is partitioned over 4 warps in a warpgroup (and so it must be of size 4).
  lane_dims={-4, -3} indicate that those two dimensions are partitioned over
  the lanes within a warp (their product must be equal to 32, i.e. warp size).
  Finally, vector_dim=-1 indicates that each (logical) register is a vector
  containing 2 elements (there are no shape restrictions here).

  Given the above, the shape of the (logical) register array used to represent
  the array in each thread is: (2, 16, 1, 1, 2, 1, 1, 1, 1, 1). We have set all
  the dimensions above to 1, since each thread is a member of a single warp,
  a single lane, and the elements along the vectorized dimension are represented
  by a single (logical) register.
  """
  tiling: Tiling
  warp_dim: int
  lane_dims: frozenset[int]
  vector_dim: int

  def __post_init__(self):
    if not self.tiling.tiles:
      raise ValueError("Tiling must have at least one tile")
    min_shape = self.tiling.tiles[0]
    min_tiled_shape = self.tiling.tile_shape(min_shape)
    dims_set = {self.warp_dim, *self.lane_dims, self.vector_dim}
    if len(dims_set) != len(self.lane_dims) + 2:
      raise ValueError
    for d in dims_set:
      if d >= 0:
        raise ValueError("All dimensions must be negative")
      if d < -(len(min_tiled_shape) - len(min_shape)):
        raise ValueError("Dimension out of range")
    if min_tiled_shape[self.warp_dim] != WARPS_IN_WARPGROUP:
      raise ValueError
    if math.prod(min_tiled_shape[d] for d in self.lane_dims) != WARP_SIZE:
      raise ValueError

  @property
  def base_tile_shape(self) -> int:
    """The shape of the first tile in the tiling expression.

    This tile acts as the divisibility constraint for a suffix of arrays to
    which this layout applies.
    """
    return self.tiling.tiles[0]

  @functools.cached_property
  def tiled_tiling_shape(self) -> tuple[int, ...]:
    """The shape of the suffix of the array after tiling.

    We only allow our repeated tiling actions to further subdivide the
    dimensions created by previous tiling actions (except for the first one),
    so the tiled shape always ends with this suffix, no matter what array shape
    it's applied to.
    """
    return self.tiling.tile_shape(self.base_tile_shape)

  @property
  def vector_length(self) -> int:
    return self.tiled_tiling_shape[self.vector_dim]

  def registers_shape(self, shape: tuple[int, ...]) -> tuple[int, ...]:
    """Returns the shape of the register array needed to represent an array of the given logical shape."""
    tiled_shape = list(self.tiling.tile_shape(shape))
    tiled_shape[self.warp_dim] = 1
    for d in self.lane_dims:
      tiled_shape[d] = 1
    tiled_shape[self.vector_dim] = 1
    return tuple(tiled_shape)

  def shape_from_registers_shape(self, shape: tuple[int, ...]) -> tuple[int, ...]:
    """Returns the logical shape of an array given its register array shape.

    Inverse to `registers_shape`.
    """
    tiled_tiling = self.tiled_tiling_shape
    shape = list(shape)
    shape[self.warp_dim] = WARPS_IN_WARPGROUP
    for d in self.lane_dims:
      shape[d] = tiled_tiling[d]
    shape[self.vector_dim] = tiled_tiling[self.vector_dim]
    return self.tiling.untile_shape(tuple(shape))

  def lane_indices(self) -> tuple[ir.Value, ...]:
    i32 = ir.IntegerType.get_signless(32)
    tiled_shape = tuple(
        d if i in self.lane_dims else 1
        for i, d in enumerate_negative(self.tiled_tiling_shape)
    )
    assert math.prod(tiled_shape) == WARP_SIZE
    lane_strides = utils.get_contiguous_strides(tiled_shape)
    lane_idx = arith.remui(utils.thread_idx(), c(WARP_SIZE, i32))
    # TODO(apaszke): Rewrite so that we can be sure that this never actually
    # does arithmetic for any dimensions that are not in lane_dims.
    return tuple(
        arith.remui(arith.divui(lane_idx, c(stride, i32)), c(size, i32))
        for stride, size in zip(lane_strides, tiled_shape)
    )

  def warp_indices(self) -> tuple[ir.Value, ...]:
    i32 = ir.IntegerType.get_signless(32)
    tiled_shape = tuple(
        d if i == self.warp_dim else 1
        for i, d in enumerate_negative(self.tiled_tiling_shape)
    )
    assert math.prod(tiled_shape) == WARPS_IN_WARPGROUP
    warp_idx = arith.remui(
        arith.divui(utils.thread_idx(), c(WARP_SIZE, i32)),
        c(WARPS_IN_WARPGROUP, i32),
    )
    indices = [arith.constant(i32, 0)] * len(tiled_shape)
    indices[self.warp_dim] = warp_idx
    return tuple(indices)


def _tiled_wgmma_layout(shape: tuple[int, ...]):
  """Returns the tiled layout relevant for WGMMA operations.

  The tiled layout is equivalent to one described here in PTX documentation:
  https://docs.nvidia.com/cuda/parallel-thread-execution/#wgmma-64n16-d

  This tiled layout is equivalent to WGMMAFragLayout and will subsume it.
  """
  if len(shape) != 2:
    raise ValueError(f"Shape {shape} is not 2D")
  if shape[0] % 64 != 0 or shape[1] % 8 != 0:
    raise ValueError(f"Shape {shape} is not a multiple of 64x8")
  return TiledLayout(
      Tiling(((64, 8), (16, 8), (8, 8), (1, 2))),
      warp_dim=-8,
      lane_dims=frozenset((-4, -3)),
      vector_dim=-1,
  )


@dataclasses.dataclass(frozen=True)
class WGMMAFragLayout:
  """[m, n] matrix, where m % 64 == 0 == n % 8."""

  def thread_idxs(self, shape):
    index = ir.IndexType.get()
    assert shape[0] % 64 == 0 and shape[1] % 8 == 0
    tid = arith.index_cast(ir.IndexType.get(), mgpu.thread_idx())
    tid_wg = arith.remui(tid, c(WARPGROUP_SIZE, index))
    warp_idx = arith.divui(tid_wg, c(32, index))
    tid_warp = arith.remui(tid_wg, c(32, index))
    col_base = arith.muli(arith.remui(tid_warp, c(4, index)), c(2, index))
    row_base = arith.addi(
        arith.divui(tid_warp, c(4, index)), arith.muli(warp_idx, c(16, index))
    )
    for row_group in range(0, shape[0], 64):
      for col_group in range(0, shape[1], 8):
        for row_subgroup in range(0, 16, 8):
          row = arith.addi(row_base, c(row_group + row_subgroup, index))
          yield row, arith.addi(col_base, c(col_group, index))

  def registers_shape(self, shape: tuple[int, ...]) -> tuple[int, ...]:
    assert len(shape) == 2
    assert shape[0] % 64 == 0 and shape[1] % 8 == 0
    return (shape[0] // 64, shape[1] // 8, 2, 1)


@dataclasses.dataclass(frozen=True)
class WGMMARowFragLayout:
  """[m] matrix, where m % 64 == 0."""

  def thread_idxs(self, shape):
    raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class WGSplatFragLayout:
  """A fragmented array where all the values are equal represented as a register per thread.

  FragmentedArrays in this layout can be are always the result of a
  splat, each thread in the warpgroup has a single copy of the value,
  while the FragmentedArray pretends it has whatever shape the user
  wants. This means we can trivially broadcast, reshape and do
  elementwise operations with all other layouts.

  Examples:

  To load a value in
  ```
  FragmentedArray.splat(memref.load(ref_1d, [1]), (10,20,2))
  ```

  A shape is always provided for sanity check reasons.

  """

  shape: tuple[int, ...] = ()

  def can_broadcast_to(self, shape) -> bool:
    """Check that the shape can be broadcast.

    Only dimensions of size 1 can be broadcast. All other dimensions
    must be the same as the argument shape.
    """
    return all(dim1 == dim2 or dim1 == 1 for dim1, dim2 in zip(self.shape[::-1], shape[::-1]))

  def thread_idxs(self, shape):
    assert shape == self.shape
    raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class WGStridedFragLayout:
  """Convert the array to 1D and then shard across threads."""

  shape: tuple[int, ...]
  vec_size: int

  def __post_init__(self):
    if np.prod(self.shape) % (self.vec_size * WARPGROUP_SIZE) != 0:
      raise ValueError((self, WARPGROUP_SIZE))

  @classmethod
  def from_memref_type(cls, memref_ty: ir.Type):
    if not ir.MemRefType.isinstance(memref_ty):
      raise TypeError(memref_ty)

    memref_type = ir.MemRefType(memref_ty)
    bw = mgpu.bytewidth(memref_type.element_type)
    assert 8 % bw == 0 and 8 // bw != 0, bw
    if math.prod(memref_type.shape) % WARPGROUP_SIZE != 0:
      raise ValueError(
          "Ref must have a number of elements that is a multiple of"
          f" {WARPGROUP_SIZE} (got {math.prod(memref_type.shape)})"
      )
    max_vec_size = np.prod(memref_type.shape) // WARPGROUP_SIZE
    return cls(
        shape=tuple(memref_type.shape), vec_size=min(8 // bw, max_vec_size)
    )

  def thread_idxs(self, shape):
    assert shape == self.shape
    index = ir.IndexType.get()
    for v in self.linear_thread_idxs():
      res = []
      for dim in reversed(self.shape):
        dim = c(dim, index)
        res.append(arith.remui(v, dim))
        v = arith.divui(v, dim)
      res.reverse()
      yield res

  def linear_thread_idxs(self):
    """The indexes to be used for vector load/store WGStridedFragLayout.

    Yields:
      The indices of the vector that correspond to the current thread.
    """
    index = ir.IndexType.get()
    cardinality = np.prod(self.shape)
    assert cardinality % (WARPGROUP_SIZE * self.vec_size) == 0
    reg_num = cardinality // (WARPGROUP_SIZE * self.vec_size)
    tidx = arith.remui(gpu.thread_id(gpu.Dimension.x), c(WARPGROUP_SIZE, index))
    off = arith.muli(tidx, c(self.vec_size, tidx.type))
    for i in range(reg_num):
      yield arith.addi(off, c(i * WARPGROUP_SIZE * self.vec_size, tidx.type))


FragmentedLayout = WGSplatFragLayout | WGStridedFragLayout | WGMMAFragLayout | WGMMARowFragLayout | TiledLayout


WGMMA_LAYOUT = WGMMAFragLayout()
WGMMA_ROW_LAYOUT = WGMMARowFragLayout()


@jax.tree_util.register_pytree_node_class
@dataclasses.dataclass(init=False, eq=False, frozen=True, slots=True)
class FragmentedArray:
  # An array of ir.Value, see checks in init for shapes.
  registers: np.ndarray = dataclasses.field(repr=False)
  layout: FragmentedLayout
  is_signed: bool | None

  def __init__(
      self,
      *,
      _registers: np.ndarray,
      _layout: FragmentedLayout,
      _is_signed: bool | None,
  ):
    """Initializes a fragmented array.

    This is a low-level API. Prefer using classmethods to construct fragmented
    arrays instead.
    """
    # We need to use ``object.__setattr__`` here because of ``frozen=True``.
    object.__setattr__(self, "registers", _registers)
    object.__setattr__(self, "layout", _layout)
    object.__setattr__(self, "is_signed", _is_signed)

    if (_is_signed is not None) != ir.IntegerType.isinstance(self.mlir_dtype):
      raise TypeError(
          "is_signed must only be non-None if the MLIR type is an integer"
          f" type, got {_is_signed=} for {self.mlir_dtype}"
      )

    match self.layout:
      # Registers are [m_tiles, n_tiles, 2 rows, 1 cols] in WGMMA layout
      # Each element is a vector<2xdtype>
      case WGMMAFragLayout():
        if _registers.ndim != 4 or _registers.shape[2:] != (2, 1):
          raise ValueError(f"Invalid register array shape: {_registers.shape}")

      # Registers are [m_tiles, 2 rows] in WGMMA_ROW layout
      # Each element is a dtype scalar
      case WGMMARowFragLayout():
        if _registers.ndim != 2 or _registers.shape[-1] != 2:
          raise ValueError(f"Invalid register array shape: {_registers.shape}")

      # Registers are flat
      case WGStridedFragLayout(shape):
        [reg_size] = ir.VectorType(_registers.flat[0].type).shape
        if (
            math.prod(shape)
            != math.prod(_registers.shape) * WARPGROUP_SIZE * reg_size
        ):
          raise ValueError(
              "Invalid register array shape: math.prod({_registers.shape}) *"
              " {WARPGROUP_SIZE} * {reg_size}, want: math.prod({shape})"
          )

      # Just a single register
      case WGSplatFragLayout():
        if _registers.size != 1:
          raise ValueError(f"Invalid register array shape: {_registers.shape}")

      case TiledLayout():
        try:
          self.layout.shape_from_registers_shape(_registers.shape)
        except ValueError:
          raise ValueError(
              "Register array shape does not match the tiled layout"
          ) from None

      case _:
        raise NotImplementedError

  @classmethod
  def load_strided(cls, ref: ir.Value, *, is_signed: bool | None = None):
    if not ir.MemRefType.isinstance(ref.type):
      raise TypeError(ref.type)

    ref_ty = ir.MemRefType(ref.type)
    shape = tuple(ref_ty.shape)
    layout = WGStridedFragLayout.from_memref_type(ref_ty)
    vec_ty = ir.VectorType.get((layout.vec_size,), ref_ty.element_type)
    try:
      # Flattening the reference potentially produces simpler PTX but
      # if the ref is not already 1D and has strided dimensions
      # flattening won't work.
      ref_ = mgpu.memref_fold(ref, 0, len(ref_ty.shape))
      vecs = [vector.load(vec_ty, ref_, [vec_idx]) for vec_idx in layout.linear_thread_idxs()]
    except NotImplementedError:
      vecs = [vector.load(vec_ty, ref, vec_idx) for vec_idx in layout.thread_idxs(shape)]
    return cls(_registers=np.array(vecs), _layout=layout, _is_signed=is_signed)

  @classmethod
  def splat(cls, value, shape, layout=None, *, is_signed: bool | None = None):
    layout = layout or WGSplatFragLayout(shape)
    match layout:
      case WGMMARowFragLayout():
        if len(shape) != 1:
          raise ValueError("WGMMARowFragLayout requires a 1D shape")
        if shape[0] % 64:
          raise ValueError(
              "WGMMARowFragLayout requires shape[0] to be a multiple of 64"
          )
        reg_shape = (shape[0] // 64, 2)
      case WGMMAFragLayout():
        if len(shape) != 2:
          raise ValueError("WGMMAFragLayout requires a 2D shape")
        if shape[0] % 64 or shape[1] % 8:
          raise ValueError(
              "WGMMAFragLayout requires shape[0] to be a multiple of 64, and"
              " shape[1] to be a multiple of 8"
          )
        reg_shape = (shape[0] // 64, shape[1] // 8, 2, 1)
        value = vector.splat(ir.VectorType.get((2,), value.type), value)
      case WGStridedFragLayout(vec_size=vec_size):
        assert shape == layout.shape
        elems = np.prod(shape)
        reg_shape = (elems // (WARPGROUP_SIZE * vec_size),)
        value = vector.splat(ir.VectorType.get((vec_size,), value.type), value)
      case WGSplatFragLayout():
        assert shape == layout.shape
        reg_shape = ()
      case _:
        raise NotImplementedError(layout)

    return cls(
        _registers=np.full(reg_shape, value, dtype=object),
        _layout=layout,
        _is_signed=is_signed,
    )

  @property
  def shape(self):
    match self.layout:
      case WGMMAFragLayout():
        row_tiles, col_tiles = self.registers.shape[:2]
        return (row_tiles * 64, col_tiles * 8)
      case WGMMARowFragLayout():
        row_tiles = self.registers.shape[0]
        return (row_tiles * 64,)
      case WGStridedFragLayout(shape):
        return shape
      case WGSplatFragLayout(shape=shape):
        return shape
      case TiledLayout():
        return self.layout.shape_from_registers_shape(self.registers.shape)
      case _:
        raise NotImplementedError

  @property
  def mlir_dtype(self):
    reg_ty = self.registers.flat[0].type
    match self.layout:
      case WGMMAFragLayout() | WGStridedFragLayout() | TiledLayout():
        return ir.VectorType(reg_ty).element_type
      case WGMMARowFragLayout() | WGSplatFragLayout():
        return reg_ty
      case _:
        raise NotImplementedError

  def to_layout(self, new_layout: FragmentedLayout):
    """Converts the fragmented array to the given layout.

    At the moment, only conversions from ``WGSplatFragLayout`` are supported.
    """
    if self.layout == new_layout:
      return self
    shape = self.shape
    if len(shape) == 2 and shape[0] % 64 == 0 and shape[1] % 8 == 0:
      tiled_layout = _tiled_wgmma_layout(shape)
      if (self.layout == WGMMA_LAYOUT and new_layout == tiled_layout) or (
          self.layout == tiled_layout and new_layout == WGMMA_LAYOUT
      ):
        return FragmentedArray(
            _registers=self.registers.reshape(new_layout.registers_shape(shape)),
            _layout=new_layout,
            _is_signed=self.is_signed,
        )
    if not isinstance(self.layout, WGSplatFragLayout):
      raise NotImplementedError(
          f"Cannot convert from {self.layout} to {new_layout}"
      )
    [reg] = self.registers.flat
    return type(self).splat(
        reg, self.shape, new_layout, is_signed=self.is_signed
    )

  def _pointwise(self, op, *other, output_is_signed: bool | None = None):
    is_signed = (
        output_is_signed if output_is_signed is not None else self.is_signed
    )

    other_arrs = []
    for o in other:
      if not isinstance(o, FragmentedArray):
        if isinstance(o, (float, int)):
          o = utils.c(o, self.mlir_dtype)
        elif not isinstance(o, ir.Value):
          raise NotImplementedError(o)

        o = FragmentedArray.splat(
            o, shape=self.shape, layout=self.layout, is_signed=is_signed
        )

      if isinstance(o.layout, WGSplatFragLayout):
        if not o.layout.can_broadcast_to(self.shape):
          raise ValueError("Can't broadcast shape.")
        o = FragmentedArray.splat(
            o.registers.flat[0],
            shape=self.shape,
            layout=self.layout,
            is_signed=is_signed,
        )
      else:
        if self.layout != o.layout:
          raise ValueError("Incompatible FragmentedArray layouts")
        if self.registers.shape != o.registers.shape:
          raise ValueError("Incompatible FragmentedArray shapes")

      other_arrs.append(o)
    new_regs = np.empty_like(self.registers)

    for idx, reg in np.ndenumerate(self.registers):
      new_regs[idx] = op(reg, *(o.registers[idx] for o in other_arrs))
    return FragmentedArray(
        _registers=new_regs, _layout=self.layout, _is_signed=is_signed
    )

  def __pos__(self):
    return self

  def __neg__(self):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.negf)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return 0 - self
    else:
      return NotImplemented

  def __add__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.addf, other)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.addi, other)
    else:
      return NotImplemented

  def __radd__(self, other):
    return self + other

  def __mul__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.mulf, other)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.muli, other)
    else:
      return NotImplemented

  def __rmul__(self, other):
    return self * other

  def __sub__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.subf, other)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.subi, other)
    else:
      return NotImplemented

  def __rsub__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(lambda s, o: arith.subf(o, s), other)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return self._pointwise(lambda s, o: arith.subi(o, s), other)
    else:
      return NotImplemented

  def __truediv__(self, other):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self._pointwise(arith.divf, other)

  def __rtruediv__(self, other):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self._pointwise(lambda s, o: arith.divf(o, s), other)

  def __floordiv__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(
          lambda s, o: mlir_math.floor(arith.divf(s, o)), other
      )
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      if self.is_signed:
        return self._pointwise(arith.floordivsi, other)
      else:
        return self._pointwise(arith.divui, other)
    else:
      return NotImplemented

  def __rfloordiv__(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(
          lambda s, o: mlir_math.floor(arith.divf(o, s)), other
      )
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      if self.is_signed:
        return self._pointwise(lambda s, o: arith.floordivsi(o, s), other)
      else:
        return self._pointwise(lambda s, o: arith.divui(o, s), other)
    else:
      return NotImplemented

  def __mod__(self, other):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    if self.is_signed:
      return self._pointwise(arith.remsi, other)
    else:
      return self._pointwise(arith.remui, other)

  def __rmod__(self, other):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    if self.is_signed:
      return self._pointwise(lambda s, o: arith.remsi(o, s), other)
    else:
      return self._pointwise(lambda s, o: arith.remui(o, s), other)

  def __invert__(self):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self ^ ~0

  def __or__(self, other):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self._pointwise(arith.ori, other)

  def __ror__(self, other):
    return self | other

  def __and__(self, other):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self._pointwise(arith.andi, other)

  def __rand__(self, other):
    return self & other

  def __xor__(self, other):
    if not ir.IntegerType.isinstance(self.mlir_dtype):
      return NotImplemented
    return self._pointwise(arith.xori, other)

  def __rxor__(self, other):
    return self ^ other

  def __eq__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.OEQ,
        si_pred=arith.CmpIPredicate.eq,
        ui_pred=arith.CmpIPredicate.eq,
    )

  def __ne__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.UNE,
        si_pred=arith.CmpIPredicate.ne,
        ui_pred=arith.CmpIPredicate.ne,
    )

  def __lt__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.OLT,
        si_pred=arith.CmpIPredicate.slt,
        ui_pred=arith.CmpIPredicate.ult,
    )

  def __le__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.OLE,
        si_pred=arith.CmpIPredicate.sle,
        ui_pred=arith.CmpIPredicate.ule,
    )

  def __gt__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.OGT,
        si_pred=arith.CmpIPredicate.sgt,
        ui_pred=arith.CmpIPredicate.ugt,
    )

  def __ge__(self, other):
    return self._compare(
        other,
        f_pred=arith.CmpFPredicate.OGE,
        si_pred=arith.CmpIPredicate.sge,
        ui_pred=arith.CmpIPredicate.uge,
    )

  def _compare(self, other, *, f_pred, si_pred, ui_pred):
    if ir.FloatType.isinstance(self.mlir_dtype):
      pred = functools.partial(arith.cmpf, f_pred)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      if ir.IntegerType(self.mlir_dtype).is_signed:
        pred = functools.partial(arith.cmpi, si_pred)
      else:
        pred = functools.partial(arith.cmpi, ui_pred)
    else:
      raise NotImplementedError
    return self._pointwise(pred, other, output_is_signed=False)

  def max(self, other):
    if ir.FloatType.isinstance(self.mlir_dtype):
      return self._pointwise(arith.maximumf, other)
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      return self._pointwise(
          arith.maxsi if self.is_signed else arith.maxui, other
      )
    else:
      return NotImplemented

  def exp(self, *, approx: bool = False):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      raise NotImplementedError
    if approx:
      f32 = ir.F32Type.get()
      if self.mlir_dtype != f32:
        raise NotImplementedError
      log2e = arith.constant(f32, ir.FloatAttr.get(f32, 1.4426950408889634))
      def fast_exp(x):
        scaled = arith.mulf(x, log2e)
        return llvm.inline_asm(f32, [scaled], "ex2.approx.f32 $0, $1;", "=f,f")
      return self._pointwise(self._lift_fast_unary(fast_exp))
    return self._pointwise(mlir_math.exp)

  def sin(self, *, approx: bool = False):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      raise NotImplementedError
    if approx and self.mlir_dtype != ir.F32Type.get():
      raise NotImplementedError
    return self._pointwise(
        self._lift_fast_unary("sin.approx.f32") if approx else mlir_math.sin
    )

  def cos(self, *, approx: bool = False):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      raise NotImplementedError
    if approx and self.mlir_dtype != ir.F32Type.get():
      raise NotImplementedError
    return self._pointwise(
        self._lift_fast_unary("cos.approx.f32") if approx else mlir_math.cos
    )

  def rsqrt(self, *, approx: bool = False):
    if not ir.FloatType.isinstance(self.mlir_dtype):
      raise NotImplementedError
    if approx and self.mlir_dtype != ir.F32Type.get():
      raise NotImplementedError
    return self._pointwise(
        self._lift_fast_unary("rsqrt.approx.f32") if approx else mlir_math.rsqrt
    )

  @staticmethod
  def _lift_fast_unary(
      instr: str | Callable[[ir.Value], ir.Value],
  ) -> Callable[[ir.Value], ir.Value]:
    def fast_instr(x):
      f32 = ir.F32Type.get()
      if x.type == f32:
        if isinstance(instr, str):
          return llvm.inline_asm(f32, [x], instr + " $0, $1;", "=f,f")
        else:
          return instr(x)
      elif ir.VectorType.isinstance(x.type):
        index = ir.IndexType.get()
        result = llvm.mlir_undef(x.type)
        [vec_len] = ir.VectorType(x.type).shape
        for i in range(vec_len):
          v = vector.extractelement(x, position=c(i, index))
          vr = fast_instr(v)
          result = vector.insertelement(vr, result, position=c(i, index))
        return result
      else:
        raise NotImplementedError(x.type)
    return fast_instr

  def bitcast(self, elt: ir.Type):
    reg_type = self.registers.flat[0].type
    if ir.VectorType.isinstance(reg_type):
      reg_shape = ir.VectorType(reg_type).shape
      ty = ir.VectorType.get(reg_shape, elt)
    else:
      ty = elt

    return self._pointwise(lambda x: arith.bitcast(ty, x))

  def __getitem__(self, idx):
    if self.layout != WGMMA_LAYOUT:
      raise NotImplementedError("Only WGMMA layouts support slicing")
    base_idx, slice_shape, is_squeezed = utils.parse_indices(idx, self.shape)
    if any(is_squeezed):
      raise NotImplementedError("Only slicing implemented")
    if (
        base_idx[0] % 64
        or slice_shape[0] % 64
        or base_idx[1] % 8
        or slice_shape[1] % 8
    ):
      raise NotImplementedError("Only tile aligned slicing supported")
    base_idx[0] //= 64
    slice_shape[0] //= 64
    base_idx[1] //= 8
    slice_shape[1] //= 8
    new_regs = self.registers[
        base_idx[0] : base_idx[0] + slice_shape[0],
        base_idx[1] : base_idx[1] + slice_shape[1],
    ]
    return FragmentedArray(
        _registers=new_regs, _layout=self.layout, _is_signed=self.is_signed
    )

  # TODO(apaszke): Support JAX dtypes here as well?
  def astype(self, new_dtype: ir.Type, *, is_signed: bool | None = None):
    i8 = ir.IntegerType.get_signless(8)
    i16 = ir.IntegerType.get_signless(16)
    i32 = ir.IntegerType.get_signless(32)
    bf16 = ir.BF16Type.get()

    cur_dtype = self.mlir_dtype
    if cur_dtype == new_dtype:
      if self.is_signed == is_signed:
        return self
      return FragmentedArray(
          _registers=self.registers, _layout=self.layout, _is_signed=is_signed
      )
    reg_type = self.registers.flat[0].type
    is_vector_reg = ir.VectorType.isinstance(reg_type)
    reg_shape = tuple(ir.VectorType(reg_type).shape) if is_vector_reg else ()
    if cur_dtype == i8 and new_dtype == bf16 and reg_shape == (2,):
      new_registers = np.empty_like(self.registers)
      for idx, reg in np.ndenumerate(self.registers):
        reg_16 = vector.bitcast(ir.VectorType.get((1,), i16), reg)
        val_16 = llvm.extractelement(reg_16, c(0, i32))
        # We first embed the s8 into a bf16 with the exponent equal to
        # bias + mantissa bits. Then, we zero the msb that didn't fit into the
        # mantissa, zero out all bits other than msb, and subtract the last
        # two values from each other. This takes advantage of the fact that the
        # lsb of the exponent (msb of the second byte) is zero, which allows us
        # to losslesly pack the msb there. When 1, it doubles the value of s2,
        # making the result negative.
        new_val_32 = llvm.inline_asm(
            i32,
            [val_16],
            """
            {
            .reg .b32 s<3>;
            prmt.b32 s0, $1, 0x43, 0x4140;
            and.b32 s1, s0, 0xff7fff7f;
            and.b32 s2, s0, 0xff80ff80;
            sub.bf16x2 $0, s1, s2;
            }
            """,
            "=r,r",
        )
        new_vec = llvm.mlir_undef(ir.VectorType.get((1,), i32))
        new_vec = llvm.insertelement(new_vec, new_val_32, c(0, i32))
        new_registers[idx] = vector.bitcast(
            ir.VectorType.get((2,), new_dtype), new_vec
        )
      return FragmentedArray(
          _registers=new_registers, _layout=self.layout, _is_signed=is_signed
      )
    # Generic path.
    from_float = ir.FloatType.isinstance(cur_dtype)
    to_float = ir.FloatType.isinstance(new_dtype)
    from_integer = ir.IntegerType.isinstance(cur_dtype)
    to_integer = ir.IntegerType.isinstance(new_dtype)
    if from_float and to_float:
      if ir.FloatType(cur_dtype).width > ir.FloatType(new_dtype).width:
        convert = arith.truncf
      else:
        convert = arith.extf
    elif from_integer and to_integer:
      if ir.IntegerType(cur_dtype).width > ir.IntegerType(new_dtype).width:
        convert = arith.trunci
      else:
        convert = arith.extsi
    elif from_integer and to_float:
      convert = arith.sitofp
    elif from_float and to_integer:
      convert = arith.fptosi
    else:
      raise NotImplementedError(f"Unsupported conversion {cur_dtype} -> {new_dtype}")
    new_registers = np.empty_like(self.registers)
    match self.layout:
      case WGMMAFragLayout() | WGStridedFragLayout() | TiledLayout():
        shape = ir.VectorType(self.registers.flat[0].type).shape
        new_reg_ty = ir.VectorType.get(shape, new_dtype)
      case WGMMARowFragLayout() | WGSplatFragLayout():
        new_reg_ty = new_dtype
      case _:
        raise NotImplementedError(f"Unsupported layout {self.layout}")
    for idx, reg in np.ndenumerate(self.registers):
      new_registers[idx] = convert(new_reg_ty, reg)
    return FragmentedArray(
        _registers=new_registers, _layout=self.layout, _is_signed=is_signed
    )

  # NOTE: scratch can be reused immediately once this function returns.
  def reduce_sum(self, scratch) -> ir.Value:
    if ir.FloatType.isinstance(self.mlir_dtype):
      op = arith.addf
    elif ir.IntegerType.isinstance(self.mlir_dtype):
      op = arith.addi
    else:
      raise NotImplementedError(self.mlir_dtype)

    index = ir.IndexType.get()
    if not isinstance(self.layout, WGStridedFragLayout):
      raise NotImplementedError(f"Unsupported layout {self.layout}")
    result = c(0, self.mlir_dtype)
    for reg in self.registers:
      result = op(
          result,
          vector.reduction(self.mlir_dtype, vector.CombiningKind.ADD, reg),
      )
    scratch_ty = ir.MemRefType(scratch.type)
    if scratch_ty.element_type != self.mlir_dtype or scratch_ty.shape != [4]:
      raise ValueError(f"Expected shape={(4,)}, {self.mlir_dtype} (got {scratch_ty})")

    warp_result = utils.warp_tree_reduce(result, op, 32)
    warp_id = arith.divui(gpu.thread_id(gpu.Dimension.x), c(32, index))
    memref.store(warp_result, scratch, [warp_id])
    utils.warpgroup_barrier()
    zero_index = c(0, index)
    with mgpu.single_thread(per_block=False):
      scratch_vec = vector.load(
          ir.VectorType.get((4,), self.mlir_dtype),
          scratch,
          [zero_index],
      )
      scratch_sum = vector.reduction(
          self.mlir_dtype, vector.CombiningKind.ADD, scratch_vec
      )
      memref.store(scratch_sum, scratch, [zero_index])
    utils.warpgroup_barrier()
    result = memref.load(scratch, [zero_index])
    utils.warpgroup_barrier()  # Make sure everyone is done using scratch.
    return result

  def reduce(self, op, axis):
    if self.layout != WGMMA_LAYOUT:
      raise NotImplementedError(self.layout)
    if axis != 1:
      raise NotImplementedError
    index = ir.IndexType.get()
    i32 = ir.IntegerType.get_signless(32)
    new_regs = np.empty(self.registers.shape[::2], dtype=object)
    assert self.registers.shape[-1] == 1
    for row_tile, row_subtile in np.ndindex(new_regs.shape):
      # Reduce the registers owned by the current thread over n tiles
      thread_result_vec = self.registers[row_tile, 0, row_subtile, 0]
      for n_tile in range(1, self.registers.shape[1]):
        thread_result_vec = op(
            thread_result_vec, self.registers[row_tile, n_tile, row_subtile, 0]
        )
      thread_result = op(
          vector.extractelement(thread_result_vec, position=c(0, index)),
          vector.extractelement(thread_result_vec, position=c(1, index)),
      )
      # Do a shuffle to reduce in groups of 4 consecutive threads.
      result = thread_result
      for i in (1, 2):
        other_result = nvvm.shfl_sync(
            result.type,
            c(0xFFFFFFFF, i32),
            result,
            c(i, i32),
            c(0x1F, i32),
            nvvm.ShflKind.bfly,
        )
        result = op(result, other_result)
      new_regs[row_tile, row_subtile] = result
    return FragmentedArray(
        _registers=new_regs, _layout=WGMMA_ROW_LAYOUT, _is_signed=self.is_signed
    )

  def broadcast(self, shape):
    if not isinstance(self.layout, WGSplatFragLayout):
      raise NotImplementedError(self.layout)

    if self.shape == shape:
      return self

    if not self.layout.can_broadcast_to(shape):
      raise ValueError(f"Can't broadcast {self.shape} to {shape}")

    return FragmentedArray(
        _registers=self.registers,
        _layout=WGSplatFragLayout(shape),
        _is_signed=self.is_signed,
    )

  def reshape(self, shape):
    if self.shape == shape:
      return self

    if not isinstance(self.layout, WGSplatFragLayout):
      raise NotImplementedError(self.layout)

    if np.prod(shape) != np.prod(self.shape):
      raise ValueError(f"Can't reshape {self.shape} to {shape}")

    return FragmentedArray(
        _registers=self.registers,
        _layout=WGSplatFragLayout(shape),
        _is_signed=self.is_signed,
    )

  def broadcast_minor(self, n):
    if self.layout != WGMMA_ROW_LAYOUT:
      raise NotImplementedError
    num_row_tiles = self.registers.shape[0]
    num_col_tiles, rem = divmod(n, 8)
    if rem:
      raise ValueError("Number of columns must be divisible by 8")
    new_regs = np.empty((num_row_tiles, num_col_tiles, 2, 1), dtype=object)
    dtype = self.mlir_dtype
    for (row_tile, row_subtile), reg in np.ndenumerate(self.registers):
      new_regs[row_tile, :, row_subtile, :] = vector.splat(
          ir.VectorType.get((2,), dtype), reg
      )
    return FragmentedArray(
        _registers=new_regs, _layout=WGMMA_LAYOUT, _is_signed=self.is_signed
    )

  def select(self, on_true, on_false):
    if (
        not ir.IntegerType.isinstance(self.mlir_dtype)
        or ir.IntegerType(self.mlir_dtype).width != 1
    ):
      raise NotImplementedError
    return self._pointwise(arith.select, on_true, on_false)

  def foreach(self, fn: Callable[[ir.Value, tuple[ir.Value, ...]], None]):
    """Call a function for each value and index."""
    index = ir.IndexType.get()
    for idx, reg in zip(self.layout.thread_idxs(self.shape), self.registers.flat, strict=True):
      assert len(idx) == len(self.shape), (idx, self.shape)
      [elems] = ir.VectorType(reg.type).shape
      for i in range(elems):
        i = c(i, index)
        fn(vector.extractelement(reg, position=i), (*idx[:-1], arith.addi(idx[-1], i)))

  def store_untiled(self, ref: ir.Value):
    if not ir.MemRefType.isinstance(ref.type):
      raise ValueError(ref)

    match self.layout:
      case WGMMAFragLayout():
        self._store_untiled_wgmma(ref)
      case WGSplatFragLayout():
        self._store_untiled_splat(ref)
      case WGStridedFragLayout():
        self._store_untiled_wg_strided(ref)
      case TiledLayout():
        self._store_untiled_tiled(ref)
      case _:
        raise NotImplementedError(self.layout)

  def _store_untiled_splat(self, ref: ir.Value):
    vec_size = 8 // mgpu.bytewidth(self.mlir_dtype)
    if np.prod(self.shape) < vec_size * WARPGROUP_SIZE:
      vec_size = 1

    if np.prod(self.shape) % WARPGROUP_SIZE * vec_size:
      raise ValueError(self.shape, WARPGROUP_SIZE, vec_size)

    fa = FragmentedArray.splat(
        self.registers.flat[0],
        self.shape,
        layout=WGStridedFragLayout(shape=self.shape, vec_size=vec_size),
        is_signed=self.is_signed,
    )
    fa.store_untiled(ref)

  def _store_untiled_wg_strided(self, ref: ir.Value):
    ref_ty = ir.MemRefType(ref.type)
    try:
      # Flattening the reference potentially produces simpler PTX but
      # if the ref is not already 1D and has strided dimensions
      # flattening won't work. We use a different variable for ref in
      # case `NotImplementedError` is thrown by
      # .linear_thread_idxs().
      ref_ = mgpu.memref_fold(ref, 0, len(ref_ty.shape))
      idxs = ([i] for i in self.layout.linear_thread_idxs())
    except NotImplementedError:
      ref_ = ref
      idxs = self.layout.thread_idxs()
    ref_shape = tuple(ref_ty.shape)
    if ref_shape != self.shape:
      raise ValueError((ref_shape, self.shape))
    for idx, reg in zip(idxs, self.registers.flat):
      vector.store(reg, ref_, idx)

  def _store_untiled_wgmma(self, ref: ir.Value):
    """Stores accumulator to a 2D memref. Not optimized at the moment."""
    assert self.layout == WGMMA_LAYOUT
    index = ir.IndexType.get()
    m, n = self.shape
    ref_ty = ir.MemRefType(ref.type)
    if ref_ty.shape != [m, n]:
      raise ValueError(ref.type, (m, n))

    def c(x):
      return arith.ConstantOp(index, ir.IntegerAttr.get(index, x))

    tidx = arith.remui(gpu.thread_id(gpu.Dimension.x), c(WARPGROUP_SIZE))
    lane_id = arith.remui(tidx, c(32))  # {0, 1, ..., 31}
    warp_id = arith.divui(tidx, c(32))  # {0, 1, 2, 3}
    row_base = arith.addi(
        arith.divui(lane_id, c(4)), arith.muli(warp_id, c(16))
    )
    col_base = arith.muli(arith.remui(lane_id, c(4)), c(2))  # {0, 2, 4, 6}
    it = np.ndenumerate(self.registers)
    for (row_tile, col_tile, row_idx, col_zero), elem in it:
      del col_zero
      row = arith.addi(row_base, c(row_tile * 64 + row_idx * 8))
      for col_idx in range(2):
        value = vector.extractelement(elem, position=c(col_idx))
        col = arith.addi(col_base, c(col_tile * 8 + col_idx))
        memref.store(value, ref, [row, col])

  def _store_untiled_tiled(self, ref: ir.Value):
    """Stores an array with a tiled layout. Not optimized at the moment."""
    i32 = ir.IntegerType.get_signless(32)
    layout = self.layout
    assert isinstance(layout, TiledLayout)
    ref_strides, _ = ir.MemRefType(ref.type).get_strides_and_offset()
    if ref_strides[layout.vector_dim] != 1:
      raise NotImplementedError(
          "Can't use vector stores with non-unit minormost stride"
      )
    strides = layout.tiling.tile_strides(ref_strides)
    ptr = utils.memref_ptr(ref)
    # Fold warp and lane offsets into the pointer once, since they are dynamic.
    dyn_strides = [arith.constant(i32, s) for s in strides]
    warp_offset = utils.dyn_dot(layout.warp_indices(), dyn_strides)
    lane_offset = utils.dyn_dot(layout.lane_indices(), dyn_strides)
    dyn_offset = arith.addi(warp_offset, lane_offset)
    ptr = utils.getelementptr(ptr, [dyn_offset], self.mlir_dtype)
    # All warp tile offsets are static and can be fused into the store.
    for tile_idx, reg in np.ndenumerate(self.registers):
      lin_idx = sum(i * s for i, s in zip(tile_idx, strides, strict=True))
      reg_ptr = utils.getelementptr(ptr, [lin_idx], self.mlir_dtype)
      llvm.store(reg, reg_ptr)

  def store_tiled(self, ref, swizzle: int | None):
    match self.layout:
      case WGMMAFragLayout():
        dtype = self.mlir_dtype
        bw = mgpu.bytewidth(dtype)
        m, n = self.shape
        assert m % 64 == 0  # This is implied by the layout.
        cols_per_tile = swizzle // bw
        expected_shape = [m // 64, n // cols_per_tile, 64, cols_per_tile]
        if n < cols_per_tile:  # We allow singular tiles shorter than swizzle.
          expected_shape = [m // 64, 1, 64, cols_per_tile]
        if ir.MemRefType(ref.type).shape != expected_shape:
          raise ValueError(ref.type, (m, n))
        for get, _, idxs in self.transfer_tiled(self.shape, dtype, swizzle):
          vector.store(get(self.registers), ref, idxs)
      case TiledLayout():
        layout, shape = self.layout, self.shape
        for get, _, ptr in self.transfer_tiled2(ref, swizzle, layout, shape):
          llvm.store(get(self.registers), ptr)
      case _:
        raise NotImplementedError(self.layout)

  @classmethod
  def load_tiled(
      cls,
      ref,
      swizzle: int | None,
      *,
      is_signed: bool | None = None,
      layout: FragmentedLayout = WGMMA_LAYOUT,
  ):
    ref_ty = ir.MemRefType(ref.type)
    dtype = ref_ty.element_type
    match layout:
      case TiledLayout():
        ref_ty = ir.MemRefType(ref.type)
        tiled_shape = ref_ty.shape
        if len(tiled_shape) % 2:
          raise ValueError("Tiled reference must have even rank")
        tiling = Tiling((tiled_shape[len(tiled_shape) // 2:],))
        shape = tiling.untile_shape(tiled_shape)
        registers = np.full(layout.registers_shape(shape), None, dtype=object)
        reg_ty = ir.VectorType.get((layout.vector_length,), ref_ty.element_type)
        for _, update, ptr in cls.transfer_tiled2(ref, swizzle, layout, shape):
          update(registers, llvm.load(reg_ty, ptr))
        assert all(r is not None for r in registers.flat)
      case WGMMAFragLayout():
        bw = mgpu.bytewidth(dtype)
        m_tiles, n_tiles, m_tile_size, n_tile_size = ref_ty.shape
        if m_tile_size != 64 or n_tile_size != (swizzle // bw):
          raise ValueError
        m, n = m_tiles * m_tile_size, n_tiles * n_tile_size
        assert m % 64 == 0  # This is implied by the layout.
        registers = np.full(
            (m_tiles, n // 8, 2, 1),
            vector.splat(ir.VectorType.get((2,), dtype), c(0, dtype)),
            dtype=object,
        )
        for _, update, idxs in cls.transfer_tiled((m, n), dtype, swizzle):
          update(registers, vector.load(ir.VectorType.get((2,), dtype), ref, idxs))
      case _:
        raise NotImplementedError(layout)
    return cls(_registers=registers, _layout=layout, _is_signed=is_signed)

  @staticmethod
  def transfer_tiled(shape, dtype, swizzle: int | None):
    # TODO(apaszke): We could use ldmatrix/stmatrix for 16-bit types.
    bw = mgpu.bytewidth(dtype)
    m, n = shape
    assert m % 64 == 0 and n % 8 == 0  # Implied by the layout.
    cols_per_tile = swizzle_elems = swizzle // bw
    if n < swizzle_elems:
      cols_per_tile = n
    else:
      assert n % swizzle_elems == 0, (n, swizzle_elems)
    if swizzle not in {32, 64, 128}:
      raise NotImplementedError("Only swizzled stores supported")

    c = arith.ConstantOp.create_index
    tidx = arith.remui(gpu.thread_id(gpu.Dimension.x), c(WARPGROUP_SIZE))
    lane_id = arith.remui(tidx, c(32))  # {0, 1, ..., 31}
    warp_id = arith.divui(tidx, c(32))  # {0, 1, 2, 3}
    sub_row_base = arith.divui(lane_id, c(4))  # {0, 1, ..., 7}
    if bw > 2:  # Stagger is only necessary for values larger than 16bit.
      # We split the rows into two groups (left/right) and change the order in
      # which they perform accesses to avoid bank conflicts.
      # It seems that the STS.64 is 2x faster (and the hardware reports no
      # conflicts) when the conflicts are split between half-warps, as
      # opposed to having them within the half-warp. This requires a
      # little more work for the selects, but is ultimately worth it.
      match swizzle:
        case 128:
          is_stagger_left = arith.cmpi(
              arith.CmpIPredicate.eq, arith.remui(sub_row_base, c(2)), c(0)
          )
        case 64:
          is_stagger_left = arith.cmpi(
              arith.CmpIPredicate.eq,
              arith.remui(arith.divui(sub_row_base, c(2)), c(2)),
              c(0),
          )
        case 32:
          # 32-byte tiles of 4-byte types have only 8 columns so there is no way
          # to stagger the memory accesses within a single tile. We could do it
          # across tiles, but that would be a completely different scheme.
          raise NotImplementedError
        case _:
          raise AssertionError(swizzle)
      stagger_amount = swizzle // 64
      if (cols_per_tile // 8) % (stagger_amount * 2):
        raise NotImplementedError
    else:
      # We rely on canonicalization to clean up the selects.
      i1 = ir.IntegerType.get_signless(1)
      is_stagger_left = arith.constant(i1, ir.BoolAttr.get(True))
      stagger_amount = 0
    row_base = arith.addi(sub_row_base, arith.muli(warp_id, c(16)))
    col_base = arith.muli(arith.remui(lane_id, c(4)), c(2))  # {0, 2, 4, 6}
    # The swizzle pattern is constant for a given thread.
    col_swizzle_bits = arith.muli(
        arith.divui(sub_row_base, c(128 // swizzle)), c(16 // bw),
    )
    for row_group in range(m // 64):
      for col_group in range(n // cols_per_tile):
        for row_subidx in range(2):
          row = arith.addi(row_base, c(row_subidx * 8))
          for col_subidx in range(cols_per_tile // 8):
            col_subidx_left = col_subidx
            col_subidx_right = col_subidx ^ stagger_amount
            col_off = arith.select(
                is_stagger_left, c(col_subidx_left * 8), c(col_subidx_right * 8)
            )
            col = arith.addi(col_base, col_off)
            col = arith.xori(col, col_swizzle_bits)
            reg_idx_left = col_subidx_left + col_group * (cols_per_tile // 8)
            reg_idx_right = col_subidx_right + col_group * (cols_per_tile // 8)
            left_idx = row_group, reg_idx_left, row_subidx, 0
            right_idx = row_group, reg_idx_right, row_subidx, 0
            idx = c(row_group), c(col_group), row, col
            def get_register(regs, left_idx=left_idx, right_idx=right_idx):
              value_left = regs[left_idx]
              value_right = regs[right_idx]
              return arith.select(is_stagger_left, value_left, value_right)
            def update_registers(regs, new, left_idx=left_idx, right_idx=right_idx):
              regs[left_idx] = arith.select(is_stagger_left, new, regs[left_idx])
              regs[right_idx] = arith.select(is_stagger_left, regs[right_idx], new)
            yield get_register, update_registers, idx

  @staticmethod
  def transfer_tiled2(
      ref: ir.Value,
      swizzle: int | None,
      layout: TiledLayout,
      shape: tuple[int, ...],
  ):
    """Generate a transfer schedule for a tiled layout.

    Given a ref with one level tiling applied to it (we assume all dimensions
    have been tiled), this function generates an iterable describing a good
    schedule for swizzled SMEM loads/stores.

    At each step, the iterable yields a tuple of three values:
    * a function that takes a register array and returns the register to be
      stored at the current address
    * a function that takes a register array and a register loaded from the
      current address, and updates the register array with that register
    * the current address for load/store instructions
    """
    # TODO(apaszke): Use ldmatrix/stmatrix when possible.
    c = lambda x: arith.constant(ir.IntegerType.get_signless(32), x)
    tiling = layout.tiling

    ref_ty = ir.MemRefType(ref.type)
    dtype = ref_ty.element_type
    if ref_ty.rank % 2:
      raise ValueError("Tiled refence must have even rank")
    ref_tiling_shape = tuple(ref_ty.shape[ref_ty.rank // 2:])
    ref_tiling = Tiling((ref_tiling_shape,))
    ref_strides, _ = ref_ty.get_strides_and_offset()
    if ref_tiling.untile_shape(tuple(ref_ty.shape)) != shape:
      raise ValueError()
    if len(layout.base_tile_shape) > len(ref_tiling_shape):
      raise ValueError("Memory tiling must be a multiple of the register tiling")
    ref_tiling_suffix = ref_tiling_shape[-len(layout.base_tile_shape):]
    if any(t % wt for t, wt in zip(ref_tiling_suffix, layout.base_tile_shape)):
      raise ValueError("Memory tiling must be a multiple of the register tiling")

    if swizzle not in {32, 64, 128}:
      raise ValueError("Only swizzled transfers supported")
    bw = mgpu.bytewidth(dtype)
    swizzle_tile_elems = 16 // bw
    swizzle_group_elems = 128 // bw
    swizzle_groups_per_block = swizzle // 16
    swizzle_block_elems = swizzle_groups_per_block * swizzle_group_elems

    tiled_strides = list(tiling.tile_strides(tuple(ref_strides)))
    tiled_shape = list(tiling.tile_shape(tuple(ref_ty.shape)))
    if tiled_strides[layout.vector_dim] != 1:
      raise ValueError("Stride of the vectorized dimension should be 1")
    for d in (layout.warp_dim, *layout.lane_dims, layout.vector_dim):
      tiled_shape[d] = 1
    full_tiling = Tiling((ref_tiling_shape, *tiling.tiles))
    full_layout = dataclasses.replace(layout, tiling=full_tiling)

    # XXX: This method is still slightly incompete. For example, it does not
    # verify that the vector transfers don't cross swizzle tile boundaries. It
    # also does not guarantee that the transfer pattern does not cause bank
    # conflicts. For that reason, we only allow a select subset of layouts.
    if layout != _tiled_wgmma_layout(shape) or bw > 2:
      raise NotImplementedError("transfer_tiled2 not general enough yet")

    dyn_tiled_strides = [c(s) for s in tiled_strides]
    lane_offset = utils.dyn_dot(full_layout.lane_indices(), dyn_tiled_strides)
    warp_offset = utils.dyn_dot(full_layout.warp_indices(), dyn_tiled_strides)
    dyn_offset = arith.addi(lane_offset, warp_offset)
    if ref_ty.memory_space != ir.Attribute.parse("#gpu.address_space<workgroup>"):
      raise ValueError("Tiled stores can be performed into SMEM")
    ptr = utils.memref_ptr(ref, memory_space=3)
    for tile_idx in np.ndindex(*tiled_shape):
      const_offset = sum(i * s for i, s in zip(tile_idx, tiled_strides))
      # We split the offset into a part that interacts with swizzling and a
      # part that doesn't. This lets us generate better code because constant
      # offsets can be fused into load and store instructions.
      const_offset_swizzle = const_offset % swizzle_block_elems
      const_offset_no_swizzle = const_offset - const_offset_swizzle
      offset_pre_swizzle = arith.addi(dyn_offset, c(const_offset_swizzle))
      swizzle_group = arith.remui(
          arith.divui(offset_pre_swizzle, c(swizzle_group_elems)),
          c(swizzle_groups_per_block),
      )
      swizzle_bits = arith.muli(swizzle_group, c(swizzle_tile_elems))
      offset = arith.xori(offset_pre_swizzle, swizzle_bits)
      reg_ptr = utils.getelementptr(ptr, [offset], dtype)
      reg_ptr = utils.getelementptr(reg_ptr, [const_offset_no_swizzle], dtype)
      reg_idx = tiling.tile_indices(full_tiling.untile_indices(tile_idx))
      def get_register(regs, reg_idx=reg_idx):
        return regs[reg_idx]
      def update_registers(regs, new, reg_idx=reg_idx):
        regs[reg_idx] = new
      yield get_register, update_registers, reg_ptr

  def tree_flatten(self):
    aux = self.layout, self.registers.shape, self.is_signed
    return list(self.registers.flat), aux

  @classmethod
  def tree_unflatten(cls, aux, flat_registers):
    layout, reg_shape, is_signed = aux
    registers = np.asarray(flat_registers, dtype=object).reshape(reg_shape)
    return cls(_registers=registers, _layout=layout, _is_signed=is_signed)

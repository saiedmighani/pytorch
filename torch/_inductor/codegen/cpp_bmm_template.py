# mypy: allow-untyped-defs
import contextlib
from typing import Callable, List, Optional
from unittest.mock import patch

import torch
from .. import ir, lowering as L
from ..virtualized import V
from .cpp_gemm_template import (
    CppGemmTemplate,
    GEMM_TEMPLATE,
    get_padded_n,
    MICROKERNEL_DEF,
)

from .cpp_micro_gemm import LayoutType
from .cpp_template_kernel import CppTemplateKernel
from .cpp_utils import DTYPE_TO_CPP, GemmBlocking

GEMM_SINGLE_THREAD_MM_STUB = r"""
void single_thread_mm(
    const {{X_dtype}}* X,
    const {{W_dtype}}* W,
    {{Y_dtype}}* Y
    {%- if is_dynamic_M %},
    const int64_t {{kernel.size(GemmOut, -2, unwrapped=True)}}
    {%- endif %}
)
"""

GEMM_THREADED_MM_STUB = r"""
void threaded_mm(
    const {{X_dtype}}* X,
    const {{W_dtype}}* W,
    {{Y_dtype}}* Y
    {%- if is_dynamic_M %},
    const int64_t {{kernel.size(GemmOut, -2, unwrapped=True)}}
    {%- endif %}
)
"""

BMM_WRAPPER = r"""
extern "C"
{{kernel.def_kernel(inputs={"BX": BX, "BW": BW}, outputs={"BY": BY}, aliases=aliases)}}
{
    const int64_t B = {{kernel.size(BY, -3, unwrapped=True)}};
    {%- if num_threads > 1 %}
    constexpr int64_t num_threads = {{num_threads}};
    int64_t B_single_thread_block = (B / num_threads) * num_threads;

    #pragma omp parallel for num_threads({{num_threads}})
    {%- else %}
    int64_t B_single_thread_block = B;
    {%- endif %}
    for (int64_t b_start = 0; b_start < B_single_thread_block; ++b_start) {
        single_thread_mm(
            &{{kernel.index(BX, ["b_start", 0, 0])}},
            {%- if template.should_block_weights %}
            &{{kernel.index(BW, ["b_start", 0, 0, 0])}},
            {%- else %}
            &{{kernel.index(BW, ["b_start", 0, 0])}},
            {%- endif %}
            &{{kernel.index(BY, ["b_start", 0, 0])}}
            {%- if is_dynamic_M %},
            {{kernel.size(GemmOut, -2)}}
            {%- endif %}
        );
    }
    for (int64_t b_start = B_single_thread_block; b_start < B; ++b_start) {
        threaded_mm(
            &{{kernel.index(BX, ["b_start", 0, 0])}},
            {%- if template.should_block_weights %}
            &{{kernel.index(BW, ["b_start", 0, 0, 0])}},
            {%- else %}
            &{{kernel.index(BW, ["b_start", 0, 0])}},
            {%- endif %}
            &{{kernel.index(BY, ["b_start", 0, 0])}}
            {%- if is_dynamic_M %},
            {{kernel.size(GemmOut, -2)}}
            {%- endif %}
        );
    }
}
"""


class CppBmmTemplate(CppGemmTemplate):
    def __init__(
        self,
        input_nodes,
        layout: ir.Layout,
        num_threads: int,
        register_blocking: GemmBlocking,
        beta=1,
        alpha=1,
        has_bias=False,
        epilogue_creator: Optional[Callable[[ir.Buffer], ir.Pointwise]] = None,
        name="bmm",
    ):
        super().__init__(
            input_nodes,
            layout,
            num_threads,
            register_blocking,
            beta=beta,
            alpha=alpha,
            has_bias=has_bias,
            epilogue_creator=epilogue_creator,
            name=name,
        )
        # Value may be changed after micro_gemm is instantiated if using VNNI layout
        self.should_block_weights = False

    @staticmethod
    def get_padded_size(n, block_n, k, block_weight):
        padded_n = get_padded_n(n, block_n)
        if block_weight:
            new_size = [-1, padded_n // block_n, k, block_n]
        else:
            new_size = [-1, k, padded_n]
        return new_size, padded_n

    #@classmethod
    #def prep_weight(cls, inputs, layout_or_out, micro_gemm, block_weight=False):
    #    if isinstance(inputs[1], ir.IRNode):
    #        n = inputs[1].get_size()[-1]
    #    else:
    #        n = inputs[1].shape[-1]
    #    _, block_n, _ = micro_gemm.register_blocking
    #    padded_n = get_padded_n(n, block_n)
    #    if n != padded_n and micro_gemm.get_b_layout() == LayoutType.NORMAL:
    #        inputs[1] = cls.pad_weight(inputs[1], padding=padded_n - n)
    #    elif micro_gemm.get_b_layout() != LayoutType.NORMAL:
    #        inputs, layout_or_out = CppGemmTemplate.prep_weight(
    #            inputs, layout_or_out, micro_gemm, block_weight=block_weight
    #        )
    #    return inputs, layout_or_out

    @staticmethod
    def block_weight_irnode(W, new_size, padding):
        assert isinstance(W, ir.IRNode)
        if not isinstance(W, ir.TensorBox):
            W = ir.TensorBox(W)
        permuted_size = list(new_size)
        permuted_size[-2], permuted_size[-3] = permuted_size[-3], permuted_size[-2] 
        blocked_w = L.constant_pad_nd(W, (0, padding))
        blocked_w = L.permute(
            L.view(blocked_w, permuted_size),
            [0, 2, 1, 3],
        )
        return blocked_w

    @staticmethod
    def pack_vnni_weight_irnode(W, micro_gemm, new_size):
        #new_size = [padded_n // block_n, k, block_n]
        # TODO: (frost-intel): For non-constant packed weights, do this VNNI conversion at microkernel level
        k = new_size[-2]
        if not isinstance(W, ir.TensorBox):
            W = ir.TensorBox(W)
        if micro_gemm.get_b_layout() != LayoutType.NORMAL:
            vnni_size = 4 if micro_gemm.get_b_layout() == LayoutType.VNNI4 else 2
            vnni_view_size = list(new_size)
            vnni_view_size[-2] = k // vnni_size
            vnni_view_size.insert(-1, vnni_size)
            W = L.view(
                L.permute(L.view(W,vnni_view_size), [0, 1, 2, 4, 3]),
                new_size,
            )
            W = CppBmmTemplate.realize_permuted_irnode(W)
        return W

    def get_default_reindexers(self, epilogue_nodes):
        def reindexer(args):
            # if epilogue nodes exist, they have 3D ranges but args are 2D, so add 0 index
            if len(epilogue_nodes) == 0:
                return args
            return [0] + args

        return [reindexer]

    def get_options(self, kernel, template_buffer_node, epilogue_nodes, **kwargs):
        options, fake_buffers = super().get_options(
            kernel, template_buffer_node, epilogue_nodes, **kwargs
        )
        if options["micro_gemm"].get_b_layout() != LayoutType.NORMAL:
            self.should_block_weights = True

        BX, BW, BY = options["X"], options["W"], options["Y"]
        options["BX"], options["BW"], options["BY"] = BX, BW, BY
        for kword in ["X", "W", "Y", "GemmOut", "Y_2d"]:
            options[kword] = kernel.select(options[kword], 0, 0)
        for kword in ["X", "W", "Y"]:
            options[kword + "_dtype"] = DTYPE_TO_CPP[options[kword].dtype]
        return options, fake_buffers

    def render(  # type: ignore[override]
        self,
        kernel: CppTemplateKernel,
        template_buffer_node: Optional[ir.CppTemplateBuffer] = None,
        epilogue_nodes: Optional[List[ir.IRNode]] = None,
        **kwargs,
    ) -> str:
        options, fake_buffers = self.get_options(
            kernel, template_buffer_node, epilogue_nodes, **kwargs
        )
        BX, BW, BY = options["BX"], options["BW"], options["BY"]
        X, W, Y = options["X"], options["W"], options["Y"]
        aliases = options["aliases"]

        with contextlib.ExitStack() as stack:
            for buf in fake_buffers:
                stack.enter_context(
                    patch.object(V.graph, "get_dtype", self._fake_get_dtype(buf))
                )
            kernel.set_args(inputs={"X": X, "W": W}, outputs={"Y": Y}, aliases=aliases)
            result = self._template_from_string(MICROKERNEL_DEF).render(**options)
            result += self._template_from_string(
                GEMM_THREADED_MM_STUB + GEMM_TEMPLATE
            ).render(**options)
            result += self._template_from_string(
                GEMM_SINGLE_THREAD_MM_STUB + GEMM_TEMPLATE
            ).render(**{**options, "num_threads": 1})
            kernel.set_args(
                inputs={"BX": BX, "BW": BW}, outputs={"BY": BY}, aliases=aliases
            )
            result += self._template_from_string(BMM_WRAPPER).render(**options)
            return result
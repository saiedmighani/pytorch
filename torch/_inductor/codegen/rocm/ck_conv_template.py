# mypy: allow-untyped-defs
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import logging
import os
import random
import subprocess
from typing import Tuple, Optional, List
from torch._inductor import config
from torch._inductor.codegen.rocm.ck_template import CKTemplate
from torch._inductor.utils import IndentedBuffer

from ck4inductor.util import library_path

log = logging.getLogger(__name__)


@dataclass
class CKConvOp:
    n_dim_spatial: int
    a_layout: str
    b_layout: str
    ds_layout: Tuple[str]
    e_layout: str
    a_element_dtype: str
    b_element_dtype: str
    acc_dtype: str
    c_shuffle_dtype: str
    ds_element_dtype: Tuple[str]
    e_element_dtype: str
    a_elementwise_op: str
    b_elementwise_op: str
    cde_elementwise_op: str
    conv_forward_specialization: str
    gemm_specialization: str

    block_size: int
    m_per_block: int
    n_per_block: int
    k_per_block: int
    ak1: int
    bk1: int
    m_per_xdl: int
    n_per_xdl: int
    m_xdl_per_wave: int
    n_xdl_per_wave: int
    a_block_transfer_thread_cluster_lengths_ak0_m_ak1: Tuple[int, int, int]
    a_block_transfer_thread_cluster_arrange_order: Tuple[int, int, int]
    a_block_transfer_src_access_order: Tuple[int, int, int]
    a_block_transfer_src_vector_dim: int
    a_block_transfer_src_scalar_per_vector: int
    a_block_transfer_dst_scalar_per_vector_ak1: int
    a_block_lds_extra_m: bool

    b_block_transfer_thread_cluster_lengths_bk0_n_bk1: Tuple[int, int, int]
    b_block_transfer_thread_cluster_arrange_order: Tuple[int, int, int]
    b_block_transfer_src_access_order: Tuple[int, int, int]

    b_block_transfer_src_vector_dim: int
    b_block_transfer_src_scalar_per_vector: int
    b_block_transfer_dst_scalar_per_vector_bk1: int
    b_block_lds_extra_n: bool

    c_shuffle_m_xdl_per_wave_per_shuffle: int
    c_shuffle_n_xdl_per_wave_per_shuffle: int
    cde_block_transfer_cluster_lengths_m_block_m_per_block_n_block_n_per_block: Tuple[int, int, int, int]
    cde_block_transfer_scalar_per_vector_n_per_block: int
    block_gemm_pipeline_scheduler: str
    block_gemm_pipeline_version: str

    a_compute_dtype: Optional[str] = None
    b_compute_dtype: Optional[str] = None

    def name(self):
        # cpp alias for template instance
        return f"ck_device_grouped_convolution_fwd_multiple_abd_xdl_c_shuffle_v3_{self.key_name()}"

    def key_name(self):
        # TBD; must be unique per instance. Intended to use as dict key
        return "_".join(
            [
                "K"
                + field_name.replace("_", "").lower()
                + "V"
                + (
                    "x".join(map(str, iter(field_value)))
                    if isinstance(field_value, tuple)
                    else str(field_value).replace(":", "")
                )
                for field_name, field_value in self.dict_items()
            ]
        )

    def dict_items(self):
        return asdict(self).items()


def _ck_conv_instances_path():
    conv_instances_path = os.path.join(  # noqa: F821
        library_path(), "include", "ck", "library", "tensor_operation_instance", "gpu", "grouped_conv_fwd"
    )
    if not os.path.exists(conv_instances_path):
        log.error("CK library conv instances path %s does not exist", conv_instances_path)
        return None
    return conv_instances_path


def parse_instances(str_instances: List[str]) -> List[CKConvOp]:
    """
    Parse the lines containing Universal Gemm template instances into `CKGemmOperation` instances
    """

    def maybe_int(s):
        try:
            return int(s)
        except ValueError:
            return s

    op_instances = []
    for line in str_instances:
        s_template_args = line.split("DeviceGroupedConvFwdMultipleABD_Xdl_CShuffle_V3")[-1].strip("<>, ")
        template_args = []
        i_current = 0
        while i_current < len(s_template_args):
            if s_template_args[i_current] == " ":
                # skip whitespace
                i_current += 1
                continue
            elif s_template_args[i_current : i_current + 2] == "S<":
                # parse template S<Index...>
                i_next = s_template_args.find(">", i_current)
                template_args.append(
                    tuple(map(int, s_template_args[i_current + 2 : i_next].split(",")))
                )
                i_current = i_next + 2
            else:
                # all string attributes must be either type aliases or global constants in C++
                i_next = s_template_args.find(",", i_current)
                template_args.append(
                    maybe_int(
                        s_template_args[i_current : i_next if i_next != -1 else None]
                    )
                )
                if i_next != -1:
                    i_current = i_next + 1
            if i_next == -1:
                break

        template_args[0] = -1
        template_args[3] = tuple()
        template_args[9] = tuple()

        new_instance = CKConvOp(
            *template_args,  # type: ignore[arg-type]
        )

        op_instances.append(new_instance)
    return op_instances


@lru_cache(None)
def gen_conv_ops_library() -> List[CKConvOp]:
    """
    Parse the Universal Gemm instances defined in the composable kernel library folder.
    """
    ck_library_dir = _ck_conv_instances_path()
    if not ck_library_dir:
        return []

    grep_result = subprocess.run(
        [
            "grep",
            "-inR",
            "DeviceGroupedConvFwdMultipleABD_Xdl_CShuffle_V3",
            ck_library_dir,
        ],
        capture_output=True,
        text=True,
    )

    op_instances = parse_instances(grep_result.stdout.strip().split("\n"))

    log.debug("ck instances from library: %d", len(op_instances))

    schedulers = [
        "BlockGemmPipelineScheduler::Intrawave",
        "BlockGemmPipelineScheduler::Interwave",
    ]
    conv_specs = [
        "ConvolutionForwardSpecialization::ConvFwdDefault",
        "ConvolutionForwardSpecialization::ConvFwd1x1P0",
        "ConvolutionForwardSpecialization::ConvFwd1x1S1P0",
        "ConvolutionForwardSpecialization::ConvFwdOddC",
    ]

    # substitute templated args by looping through their domains
    substitute_instances = []
    for instance in op_instances:
        sub_scheduler = instance.block_gemm_pipeline_scheduler == "BlkGemmPipeSched"
        sub_spec = instance.conv_forward_specialization == "ConvSpec"
        schedulers_range = (
            schedulers if sub_scheduler else [instance.block_gemm_pipeline_scheduler]
        )
        spec_range = conv_specs if sub_spec else [instance.conv_forward_specialization]
        for scheduler in schedulers_range:
            for spec in spec_range:
                substitute_instances.append(
                    replace(
                        instance,
                        block_gemm_pipeline_scheduler=scheduler,
                        conv_forward_specialization=spec,
                        gemm_specialization="GemmSpecialization::MNKPadding",
                        n_dim_spatial=2,
                        a_layout="NHWGC",
                        b_layout="GKYXC",
                        e_layout="NHWGK",
                    )
                )

    return substitute_instances

class CKConvTemplate(CKTemplate):
    conv_template = r"""
    {{headers}}
    {{globals}}
    {{instance_definition}}
    extern "C" {
    PT_EXPORT {{kernel_definition}} {
        auto conv = {{instance_type}} {};
        auto invoker = conv.MakeInvoker();

        using ck::index_t;

        constexpr index_t NumDTensor = {{NumDTensor}};
        constexpr index_t NDimSpatial = {{NDimSpatial}};

        const void* p_a;
        const void* p_b;
        const std::array<const void*, NumDTensor> p_ds;
        void* p_e;
        const std::array<index_t, NDimSpatial + 3> a_g_n_c_wis_lengths;
        const std::array<index_t, NDimSpatial + 3> a_g_n_c_wis_strides;
        const std::array<index_t, NDimSpatial + 3> b_g_k_c_xs_lengths;
        const std::array<index_t, NDimSpatial + 3> b_g_k_c_xs_strides;
        const std::array<std::array<index_t, NDimSpatial + 3>, NumDTensor> ds_g_n_k_wos_lengths;
        const std::array<std::array<index_t, NDimSpatial + 3>, NumDTensor> ds_g_n_k_wos_strides;
        const std::array<index_t, NDimSpatial + 3> e_g_n_k_wos_lengths;
        const std::array<index_t, NDimSpatial + 3> e_g_n_k_wos_strides;
        const std::array<index_t, NDimSpatial> conv_filter_strides;
        const std::array<index_t, NDimSpatial> conv_filter_dilations;
        const std::array<index_t, NDimSpatial> input_left_pads;
        const std::array<index_t, NDimSpatial> input_right_pads;
        const AElementwiseOperation a_element_op = PassThrough;
        const BElementwiseOperation b_element_op = PassThrough;
        const CDEElementwiseOperation cde_element_op = PassThrough;

        auto argument = conv.MakeArgument(
            p_a,
            p_b,
            p_ds,
            p_e,
            a_g_n_c_wis_lengths,
            a_g_n_c_wis_strides,
            b_g_k_c_xs_lengths,
            b_g_k_c_xs_strides,
            ds_g_n_k_wos_lengths,
            ds_g_n_k_wos_strides,
            e_g_n_k_wos_lengths,
            e_g_n_k_wos_strides,
            conv_filter_strides,
            conv_filter_dilations,
            input_left_pads,
            input_right_pads,
            a_element_op,
            b_element_op,
            cde_element_op
        );
        if (!conv.IsSupportedArgument(argument)) {
            // we do our best to statically avoid this case in `filter_op`
            std::cerr << "invalid argument for conv instance " << conv.GetTypeString() << std::endl;
            argument.Print();
            return -23;
        }
        if (workspace_size) {
            *workspace_size = conv.GetWorkSpaceSize(&argument);
            return 0;
        }
        // run the kernel
        float elapsed_time = invoker.Run(argument, StreamConfig{stream, /* time kernel */ false, /* log level */ kDEBUG_LOG});
        return 0;
    } // kernel definition
    } // extern C
"""

    def globals(self) -> IndentedBuffer:
        res = super().globals()
        res.splice(
            """
                // CK conv globals

                using NWC   = ck::tensor_layout::convolution::NWC;
                using NHWC  = ck::tensor_layout::convolution::NHWC;
                using NDHWC = ck::tensor_layout::convolution::NDHWC;

                using KXC   = ck::tensor_layout::convolution::KXC;
                using KYXC  = ck::tensor_layout::convolution::KYXC;
                using KZYXC = ck::tensor_layout::convolution::KZYXC;

                using NWK   = ck::tensor_layout::convolution::NWK;
                using NHWK  = ck::tensor_layout::convolution::NHWK;
                using NDHWK = ck::tensor_layout::convolution::NDHWK;

                using GNWC   = ck::tensor_layout::convolution::GNWC;
                using GNHWC  = ck::tensor_layout::convolution::GNHWC;
                using GNDHWC = ck::tensor_layout::convolution::GNDHWC;

                using GKXC   = ck::tensor_layout::convolution::GKXC;
                using GKYXC  = ck::tensor_layout::convolution::GKYXC;
                using GKZYXC = ck::tensor_layout::convolution::GKZYXC;

                using GNWK   = ck::tensor_layout::convolution::GNWK;
                using GNHWK  = ck::tensor_layout::convolution::GNHWK;
                using GNDHWK = ck::tensor_layout::convolution::GNDHWK;

                using NGKW   = ck::tensor_layout::convolution::NGKW;
                using NGKHW  = ck::tensor_layout::convolution::NGKHW;
                using NGKDHW = ck::tensor_layout::convolution::NGKDHW;

                using NWGC   = ck::tensor_layout::convolution::NWGC;
                using NHWGC  = ck::tensor_layout::convolution::NHWGC;
                using NDHWGC = ck::tensor_layout::convolution::NDHWGC;

                using KXGC   = ck::tensor_layout::convolution::KXGC;
                using KYXGC  = ck::tensor_layout::convolution::KYXGC;
                using KZYXGC = ck::tensor_layout::convolution::KZYXGC;

                using NWGK   = ck::tensor_layout::convolution::NWGK;
                using NHWGK  = ck::tensor_layout::convolution::NHWGK;
                using NDHWGK = ck::tensor_layout::convolution::NDHWGK;

                using NGCW   = ck::tensor_layout::convolution::NGCW;
                using NGCHW  = ck::tensor_layout::convolution::NGCHW;
                using NGCDHW = ck::tensor_layout::convolution::NGCDHW;

                using G_K    = ck::tensor_layout::convolution::G_K;

                using BlockGemmPipelineScheduler = ck::BlockGemmPipelineScheduler;
                using GemmSpecialization = ck::tensor_operation::device::GemmSpecialization;
                using BlockGemmPipelineVersion = ck::BlockGemmPipelineVersion;

                using ConvolutionForwardSpecialization = ck::tensor_operation::device::ConvolutionForwardSpecialization;
            """
        )
        return res

    def header(self) -> IndentedBuffer:
        res = super().header()
        res.splice(
            """
                // CK conv headers

                #include "ck/tensor_operation/gpu/device/impl/device_grouped_conv_fwd_multiple_abd_xdl_cshuffle.hpp"
                #include "ck/tensor_operation/gpu/device/convolution_forward_specialization.hpp"
                #include "ck/tensor_operation/gpu/device/gemm_specialization.hpp"
            """
        )
        return res

    @staticmethod
    def add_ck_conv_choices(
        choices,
        layout,
        input_nodes,
    ):
        template = CKConvTemplate(
            input_nodes,
            layout,
        )
        ops = template.gen_ops()
        for op in ops:
            template.maybe_append_choice(
                choices,
                op=op,
            )

    def __init__(
        self,
        input_nodes,
        layout,
    ):
        super().__init__(
            "ck_conv_template",
            input_nodes,
            layout,
        )

    def filter_op(self, op: CKConvOp) -> bool:
        metas = [T.get_layout() for T in [*self.input_nodes, self.output_node]]
        X_meta = metas[0]
        W_meta = metas[1]
        Y_meta = metas[-1]
        # disable the instance if dtypes don't match
        if op.a_element_dtype != self._TORCH_DTYPE_TO_CK[X_meta.dtype]:
            return None
        if op.b_element_dtype != self._TORCH_DTYPE_TO_CK[W_meta.dtype]:
            return None
        if op.e_element_dtype != self._TORCH_DTYPE_TO_CK[Y_meta.dtype]:
            return None
        return op

    def gen_ops(self):
        unfiltered_instances = gen_conv_ops_library()
        filtered_instances = list(
            filter(lambda op: self.filter_op(op), unfiltered_instances)
        )
        # NB: when using a fixed list order, most likely we will pick the subset of instances
        # which are very similar to each other. Randomizing the choice seems to solve this.
        random.seed(-11)
        chosen_instances = (
            random.sample(
                filtered_instances,
                min(len(filtered_instances), config.rocm.n_max_profiling_configs),
            )
            if config.rocm.n_max_profiling_configs
            else filtered_instances
        )
        log.debug(
            "generated %d ck instances after filter: %s",
            len(chosen_instances),
            chosen_instances,
        )
        return chosen_instances

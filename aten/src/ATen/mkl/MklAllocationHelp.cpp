#include <ATen/Version.h>
#include <ATen/Config.h>

#if AT_MKLDNN_ENABLED()
#ifdef USE_MIMALLOC_ON_MKL
#include <c10/core/impl/alloc_cpu.h>

/*
MKL have a method to register memory allocation APIs via i_malloc.h, High
performance memory allocation APIs will help improve MKL performance.
Please check MKL online document：
https://www.intel.com/content/www/us/en/docs/onemkl/developer-guide-windows/2024-2/redefining-memory-functions.html
*/
#include <i_malloc.h>

bool register_mimalloc_api_to_mkl()
{
    i_malloc  = c10::mi_malloc_wrapper::c10_mi_malloc;
    i_calloc  = c10::mi_malloc_wrapper::c10_mi_calloc;
    i_realloc = c10::mi_malloc_wrapper::c10_mi_realloc;
    i_free    = c10::mi_malloc_wrapper::c10_mi_free;

    return true;
}

static bool g_b_registered_mkl_alloction = register_mimalloc_api_to_mkl();
#endif
#endif

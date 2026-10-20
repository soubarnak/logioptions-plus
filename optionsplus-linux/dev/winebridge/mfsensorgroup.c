/* Dev-only stub: Wine ships no MFSENSORGROUP.dll; the agent only needs the import to resolve. */
#include <windows.h>
__declspec(dllexport) HRESULT WINAPI MFCreateSensorActivityMonitor(void *cb, void **out)
{
    if (out) *out = NULL;
    return E_NOTIMPL;
}

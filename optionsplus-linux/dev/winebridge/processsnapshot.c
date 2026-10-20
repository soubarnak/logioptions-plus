/* Dev-only stub: Wine lacks the Pss* process snapshot API. Every call reports ERROR_NOT_SUPPORTED. */
#include <windows.h>
#define E ERROR_NOT_SUPPORTED
__declspec(dllexport) DWORD WINAPI PssCaptureSnapshot(HANDLE p, DWORD f, DWORD c, void **s) { if (s) *s = NULL; return E; }
__declspec(dllexport) DWORD WINAPI PssFreeSnapshot(HANDLE p, void *s) { return ERROR_SUCCESS; }
__declspec(dllexport) DWORD WINAPI PssQuerySnapshot(void *s, DWORD c, void *b, DWORD l) { return E; }
__declspec(dllexport) DWORD WINAPI PssWalkMarkerCreate(void *a, void **m) { if (m) *m = NULL; return E; }
__declspec(dllexport) DWORD WINAPI PssWalkMarkerFree(void *m) { return ERROR_SUCCESS; }
__declspec(dllexport) DWORD WINAPI PssWalkSnapshot(void *s, DWORD c, void *m, void *b, DWORD l) { return E; }
